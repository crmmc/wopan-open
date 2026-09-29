from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Protocol

import httpx

from openwopan.storage.settings import AppSettings
from openwopan.tasks.download import (
    DownloadCallbacks,
    DownloadError,
    DownloadResult,
    DownloadStatus,
    DownloadTaskControl,
    DownloadTaskState,
    DownloadTaskStore,
    RefreshUrlCallback,
    download_url,
)

LOGGER = logging.getLogger(__name__)
_TERMINAL_STATUSES = frozenset({"已完成", "失败", "已取消"})
REMOVE_WAIT_TIMEOUT_SECONDS = 30
PROGRESS_PERSIST_INTERVAL_SECONDS = 0.5
PROGRESS_PERSIST_BYTES = 1024 * 1024
_MAX_PHYSICAL_CONCURRENT_DOWNLOADS = 5


@dataclass(frozen=True, slots=True)
class DownloadTaskInput:
    """All non-secret inputs needed to execute one download task."""

    task_id: str
    file_name: str
    local_path: Path
    url: str
    download_id: str | None
    settings: AppSettings
    store: DownloadTaskStore
    http_client: httpx.Client
    refresh_url: RefreshUrlCallback | None = None


@dataclass(frozen=True, slots=True)
class DownloadTaskEvent:
    """A task-scoped scheduler update suitable for UI or tests."""

    task_id: str
    status: DownloadStatus
    bytes_done: int = 0
    total_bytes: int | None = None
    active_connections: int = 0
    max_connections: int = 1
    error: str = ""
    result: DownloadResult | None = None


class DownloadEventCallback(Protocol):
    def __call__(self, event: DownloadTaskEvent) -> None: ...


DownloadExecutor = Callable[
    [DownloadTaskInput, DownloadTaskControl, DownloadCallbacks], DownloadResult
]


def _set_failed_state(state: DownloadTaskState, error: str) -> None:
    state.status = "失败"
    state.error = error


def _set_result_state(state: DownloadTaskState, status: DownloadStatus) -> None:
    state.status = status
    if status == "失败" and not state.error:
        state.error = "下载失败"


def _set_progress_state(
    state: DownloadTaskState, done: int, total: int | None
) -> None:
    state.bytes_done = done
    state.total_bytes = total


def _execute_download(
    task: DownloadTaskInput,
    control: DownloadTaskControl,
    callbacks: DownloadCallbacks,
) -> DownloadResult:
    return download_url(
        task.http_client,
        task.url,
        task.local_path,
        settings=task.settings,
        store=task.store,
        task_id=task.task_id,
        file_name=task.file_name,
        download_id=task.download_id,
        refresh_url=task.refresh_url,
        callbacks=callbacks,
        control=control,
    )


class DownloadScheduler:
    """FIFO scheduler that limits concurrently executing file downloads."""

    def __init__(
        self,
        *,
        max_concurrent_downloads: int,
        on_event: DownloadEventCallback | None = None,
        executor: DownloadExecutor = _execute_download,
    ) -> None:
        if max_concurrent_downloads < 1:
            raise ValueError("max_concurrent_downloads must be positive")
        self._max_concurrent_downloads = max_concurrent_downloads
        self._pool_max_workers = max(
            max_concurrent_downloads, _MAX_PHYSICAL_CONCURRENT_DOWNLOADS
        )
        self._on_event = on_event
        self._executor = executor
        self._pool = ThreadPoolExecutor(max_workers=self._pool_max_workers)
        self._lock = threading.RLock()
        self._tasks: dict[str, DownloadTaskInput] = {}
        self._states: dict[str, DownloadTaskState] = {}
        self._controls: dict[str, DownloadTaskControl] = {}
        self._active_connections: dict[str, int] = {}
        self._last_progress_persisted: dict[str, tuple[float, int]] = {}
        self._queue: deque[str] = deque()
        self._futures: dict[str, Future[DownloadResult]] = {}
        self._removing: dict[str, threading.Event] = {}
        self._removal_errors: dict[str, OSError] = {}
        self._retired_ids: set[str] = set()
        self._closed = False

    def set_event_callback(self, callback: DownloadEventCallback | None) -> None:
        """Replace the observer for future and in-flight task events."""
        with self._lock:
            self._on_event = callback

    def set_max_concurrent_downloads(self, max_concurrent_downloads: int) -> None:
        """Update the logical file-task limit and refill the FIFO queue."""
        with self._lock:
            self._ensure_open()
            if max_concurrent_downloads < 1:
                raise ValueError("max_concurrent_downloads must be positive")
            if max_concurrent_downloads > self._pool_max_workers:
                raise ValueError("max_concurrent_downloads exceeds scheduler capacity")
            self._max_concurrent_downloads = max_concurrent_downloads
            self._start_queued_locked()

    def submit(self, task: DownloadTaskInput) -> str:
        """Persist and enqueue a task, returning its stable task id."""
        with self._lock:
            self._ensure_open()
            if task.task_id in self._tasks or task.task_id in self._retired_ids:
                raise ValueError(f"download task already exists: {task.task_id}")
            state = task.store.load(task.task_id) or DownloadTaskState(
                task_id=task.task_id,
                file_name=task.file_name,
                save_path=task.local_path,
                download_id=task.download_id,
            )
            state.status = "等待中"
            state.error = ""
            task.store.save(state)
            self._tasks[task.task_id] = task
            self._states[task.task_id] = state
            self._queue.append(task.task_id)
            self._emit_state(task.task_id)
            self._start_queued_locked()
        return task.task_id

    def recover(self, tasks: Iterable[DownloadTaskInput]) -> None:
        """Restore persisted tasks after resolving their current download inputs."""
        with self._lock:
            self._ensure_open()
            for task in tasks:
                state = task.store.load(task.task_id)
                if state is None or state.status in {"已完成", "已取消"}:
                    continue
                if task.task_id in self._tasks:
                    raise ValueError(f"download task already exists: {task.task_id}")
                self._tasks[task.task_id] = task
                self._states[task.task_id] = state
                if state.status not in {"已暂停", "失败"}:
                    state.status = "等待中"
                    state.error = ""
                    task.store.save(state)
                    self._queue.append(task.task_id)
                self._emit_state(task.task_id)
            self._start_queued_locked()

    def pause(self, task_id: str) -> bool:
        """Pause one queued or active task without affecting other tasks."""
        with self._lock:
            state = self._state(task_id)
            if state.status == "等待中":
                self._queue_remove(task_id)
                state.status = "已暂停"
                self._tasks[task_id].store.save(state)
                self._emit_state(task_id)
                self._start_queued_locked()
                return True
            control = self._controls.get(task_id)
            if state.status in {"下载中", "校验中", "合并中"} and control is not None:
                control.request_pause()
                return True
            return False

    def resume(self, task_id: str) -> bool:
        """Move a paused or failed task back to the FIFO queue."""
        with self._lock:
            state = self._state(task_id)
            if state.status not in {"已暂停", "失败"} or task_id in self._removing:
                return False
            state.status = "等待中"
            state.error = ""
            self._tasks[task_id].store.save(state)
            self._queue.append(task_id)
            self._emit_state(task_id)
            self._start_queued_locked()
            return True

    def cancel(self, task_id: str, *, cleanup: bool = False) -> bool:
        """Cancel one task; queued tasks are cancelled immediately."""
        with self._lock:
            state = self._state(task_id)
            if state.status in _TERMINAL_STATUSES:
                return False
            control = self._controls.get(task_id)
            if control is not None:
                control.request_cancel(cleanup=cleanup)
                return True
            self._queue_remove(task_id)
            state.status = "已取消"
            state.error = "用户取消下载"
            self._tasks[task_id].store.save(state)
            self._emit_state(task_id)
            self._start_queued_locked()
            return True

    def remove_record(self, task_id: str) -> bool:
        """Forget a paused or terminal task and delete its recoverable data."""
        with self._lock:
            state = self._states.get(task_id)
            if state is None:
                return False
            if state.status != "已暂停" and state.status not in _TERMINAL_STATUSES:
                raise ValueError("cannot remove an active download task")
            if task_id in self._futures:
                finished = self._removing.setdefault(task_id, threading.Event())
            else:
                self._delete_record_locked(task_id)
                return True
        if not finished.wait(REMOVE_WAIT_TIMEOUT_SECONDS):
            with self._lock:
                if not finished.is_set():
                    self._removing.pop(task_id, None)
                    raise TimeoutError("下载任务尚未退出，请稍后重试删除")
        with self._lock:
            error = self._removal_errors.pop(task_id, None)
        if error is not None:
            raise error
        return True

    def _delete_record_locked(self, task_id: str) -> None:
        self._tasks[task_id].store.delete(task_id)
        self._tasks.pop(task_id)
        self._states.pop(task_id)
        self._retired_ids.add(task_id)
        finished = self._removing.pop(task_id, None)
        if finished is not None:
            finished.set()

    def state(self, task_id: str) -> DownloadTaskState:
        """Return a snapshot of one scheduler-owned task state."""
        with self._lock:
            state = self._state(task_id)
            return replace(state, parts=list(state.parts))

    def active_task_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._futures)

    def close(self, *, wait: bool = True) -> None:
        """Stop accepting work and cooperatively cancel active tasks."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for control in self._controls.values():
                control.request_pause()
        self._pool.shutdown(wait=wait, cancel_futures=True)

    def _start_queued_locked(self) -> None:
        if self._closed:
            return
        while self._queue and len(self._futures) < self._max_concurrent_downloads:
            task_id = self._queue.popleft()
            state = self._states[task_id]
            if state.status != "等待中":
                continue
            task = self._tasks[task_id]
            control = DownloadTaskControl()
            self._controls[task_id] = control
            self._active_connections[task_id] = 0
            state.status = "下载中"
            task.store.save(state)
            self._emit_state(task_id)
            callbacks = self._callbacks(task_id)
            future = self._pool.submit(self._executor, task, control, callbacks)
            self._futures[task_id] = future
            future.add_done_callback(partial(self._finished, task_id))

    def _callbacks(self, task_id: str) -> DownloadCallbacks:
        def progress(done: int, total: int | None) -> None:
            self._progress(task_id, done, total)

        def status(value: DownloadStatus) -> None:
            self._status(task_id, value)

        def connections(active: int, maximum: int) -> None:
            self._connections(task_id, active, maximum)

        return DownloadCallbacks(progress=progress, status=status, connections=connections)

    def _finished(self, task_id: str, future: Future[DownloadResult]) -> None:
        try:
            result = future.result()
        except Exception as exc:
            LOGGER.error("download.scheduler.task_failed task_id=%s", task_id)
            with self._lock:
                if task_id in self._removing:
                    self._finish_removing_locked(task_id)
                    return
                error = str(exc) if isinstance(exc, DownloadError) else "下载任务执行失败"
                state = self._tasks[task_id].store.update(
                    task_id, lambda current: _set_failed_state(current, error)
                )
                self._states[task_id] = state
                self._finish_locked(task_id)
                self._emit_state(task_id)
                self._start_queued_locked()
            return
        with self._lock:
            if task_id in self._removing:
                self._finish_removing_locked(task_id)
                return
            state = self._latest_state(task_id)
            state.status = result.status
            if result.status == "失败" and not state.error:
                state.error = "下载失败"
            control = self._controls.get(task_id)
            cleanup_cancel = (
                result.status == "已取消" and control is not None and control.cleanup_on_cancel
            )
            if cleanup_cancel:
                self._tasks[task_id].store.delete(task_id)
            elif result.status != "已完成":
                state = self._tasks[task_id].store.update(
                    task_id, lambda current: _set_result_state(current, result.status)
                )
                self._states[task_id] = state
            self._finish_locked(task_id)
            self._emit_state(task_id, result=result)
            self._start_queued_locked()

    def _finish_removing_locked(self, task_id: str) -> None:
        self._finish_locked(task_id)
        try:
            self._delete_record_locked(task_id)
        except OSError as exc:
            self._removal_errors[task_id] = exc
            self._removing.pop(task_id).set()
        self._start_queued_locked()

    def _finish_locked(self, task_id: str, result: DownloadResult | None = None) -> None:
        self._futures.pop(task_id, None)
        self._controls.pop(task_id, None)
        self._active_connections.pop(task_id, None)
        self._last_progress_persisted.pop(task_id, None)

    def _progress(self, task_id: str, done: int, total: int | None) -> None:
        with self._lock:
            state = self._states[task_id]
            state.bytes_done = done
            state.total_bytes = total
            now = time.monotonic()
            marker = self._last_progress_persisted.get(task_id)
            if marker is None:
                self._last_progress_persisted[task_id] = (now, 0)
                should_persist = done == total or done >= PROGRESS_PERSIST_BYTES
            else:
                last_time, last_bytes = marker
                should_persist = (
                    done == total
                    or now - last_time >= PROGRESS_PERSIST_INTERVAL_SECONDS
                    or done - last_bytes >= PROGRESS_PERSIST_BYTES
                )
            if should_persist:
                self._states[task_id] = self._tasks[task_id].store.update(
                    task_id,
                    lambda current: _set_progress_state(current, done, total),
                )
                self._last_progress_persisted[task_id] = (now, done)
            self._emit_state(task_id)

    def _status(self, task_id: str, status: DownloadStatus) -> None:
        with self._lock:
            if status == "已完成":
                state = self._latest_state(task_id)
                state.status = status
            else:
                self._states[task_id] = self._tasks[task_id].store.update(
                    task_id, lambda state: setattr(state, "status", status)
                )
            self._emit_state(task_id)

    def _connections(self, task_id: str, active: int, maximum: int) -> None:
        with self._lock:
            state = self._states[task_id]
            state.max_connections = maximum
            self._active_connections[task_id] = active
            self._emit_state(task_id)

    def _emit_state(
        self,
        task_id: str,
        *,
        active_connections: int | None = None,
        result: DownloadResult | None = None,
    ) -> None:
        if self._on_event is None or task_id in self._removing:
            return
        state = self._states[task_id]
        current_connections = (
            self._active_connections.get(task_id, 0)
            if active_connections is None
            else active_connections
        )
        self._on_event(
            DownloadTaskEvent(
                task_id=task_id,
                status=state.status,
                bytes_done=state.bytes_done,
                total_bytes=state.total_bytes,
                active_connections=current_connections,
                max_connections=state.max_connections,
                error=state.error,
                result=result,
            )
        )

    def _latest_state(self, task_id: str) -> DownloadTaskState:
        state = self._tasks[task_id].store.load(task_id)
        if state is None:
            state = self._states[task_id]
        else:
            self._states[task_id] = state
        return state

    def _state(self, task_id: str) -> DownloadTaskState:
        try:
            return self._states[task_id]
        except KeyError as exc:
            raise KeyError(f"unknown download task: {task_id}") from exc

    def _queue_remove(self, task_id: str) -> None:
        self._queue = deque(item for item in self._queue if item != task_id)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("download scheduler is closed")
