from __future__ import annotations

import errno
import hashlib
import json
import logging
import math
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from platformdirs import user_cache_path

from openwopan.storage.settings import APP_AUTHOR, APP_NAME, AppSettings

LOGGER = logging.getLogger(__name__)

DOWNLOAD_CHUNK_SIZE = 1024 * 256
BYTES_PER_MB = 1024 * 1024
RATE_LIMIT_STATUS_CODES = frozenset({429, 503})
RATE_LIMIT_BACKOFF_SECONDS = 2.0
MAX_RATE_LIMITS = 50
MAX_URL_REFRESHES = 3
TASK_METADATA_VERSION = 1

DownloadStatus = Literal[
    "等待中",
    "校验中",
    "下载中",
    "合并中",
    "已暂停",
    "已完成",
    "失败",
    "已取消",
]
PartResult = Literal["ok", "paused", "cancelled", "rate_limited", "url_expired", "fatal"]
ProgressCallback = Callable[[int, int | None], None]
StatusCallback = Callable[[DownloadStatus], None]
ConnectionCallback = Callable[[int, int], None]
RefreshUrlCallback = Callable[[], str]


class DownloadError(RuntimeError):
    """UI-facing download error without sensitive URL details."""


class RangeDownloadUnsupported(DownloadError):
    """Raised when the server does not honor byte range requests."""


@dataclass(frozen=True, slots=True)
class DownloadCallbacks:
    """Callbacks used by UI or tests to observe one download task."""

    progress: ProgressCallback | None = None
    status: StatusCallback | None = None
    connections: ConnectionCallback | None = None


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Result returned by a download execution."""

    status: DownloadStatus
    task_id: str
    local_path: Path


@dataclass(frozen=True, slots=True)
class DownloadTaskRecord:
    """Persisted download task summary for the transfer center."""

    task_id: str
    name: str
    target_path: Path
    status: DownloadStatus
    bytes_done: int = 0
    total_bytes: int | None = None
    supports_resume: bool = False
    error: str = ""
    active_connections: int = 0
    max_connections: int = 1


@dataclass(frozen=True, slots=True)
class DownloadPart:
    """Stable byte range in one download task."""

    index: int
    start: int
    end: int

    @property
    def expected_size(self) -> int:
        return self.end - self.start + 1


@dataclass(frozen=True, slots=True)
class DownloadPartRecord:
    """Persisted completed part metadata."""

    index: int
    start: int
    end: int
    expected_size: int
    actual_size: int
    md5: str


@dataclass(slots=True)
class DownloadTaskState:
    """Mutable metadata persisted for resumable downloads."""

    task_id: str
    file_name: str
    save_path: Path
    status: DownloadStatus = "等待中"
    download_id: str | None = None
    total_bytes: int | None = None
    bytes_done: int = 0
    part_size: int | None = None
    max_connections: int = 1
    supports_resume: bool = False
    error: str = ""
    version: int = TASK_METADATA_VERSION
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    parts: list[DownloadPartRecord] = field(default_factory=list)


class DownloadTaskControl:
    """Thread-safe control surface for one active download task."""

    def __init__(self) -> None:
        self._pause_requested = threading.Event()
        self._cancel_requested = threading.Event()
        self.cleanup_on_cancel = False
        self._response_lock = threading.Lock()
        self._active_response: httpx.Response | None = None

    def request_pause(self) -> None:
        """Ask the download task to pause at the next safe point."""
        self._pause_requested.set()
        self._close_active_response()

    def request_cancel(self, *, cleanup: bool = False) -> None:
        """Ask the download task to cancel at the next safe point."""
        self.cleanup_on_cancel = cleanup
        self._cancel_requested.set()
        self._close_active_response()

    def stop_result(self) -> PartResult | None:
        """Return the requested stop result, if any."""
        if self._cancel_requested.is_set():
            return "cancelled"
        if self._pause_requested.is_set():
            return "paused"
        return None

    def set_active_response(self, response: httpx.Response | None) -> None:
        """Track the active response so pause/cancel can unblock network reads."""
        with self._response_lock:
            self._active_response = response

    def _close_active_response(self) -> None:
        with self._response_lock:
            response = self._active_response
        if response is None:
            return
        try:
            response.close()
        except RuntimeError:
            LOGGER.debug("download.active_response_close_failed")


class DownloadTaskStore:
    """JSON-backed storage for resumable download metadata and part files."""

    def __init__(self, root_path: Path | None = None) -> None:
        self._root_path = root_path or user_cache_path(APP_NAME, APP_AUTHOR) / "downloads"
        self._lock = threading.RLock()

    @property
    def root_path(self) -> Path:
        """Return the storage root for tests and diagnostics."""
        return self._root_path

    def task_path(self, task_id: str) -> Path:
        """Return one task metadata path."""
        return self._root_path / "tasks" / f"{task_id}.json"

    def task_temp_dir(self, task_id: str) -> Path:
        """Return one task temporary part directory."""
        return self._root_path / "parts" / task_id

    def part_path(self, task_id: str, index: int) -> Path:
        """Return one completed part file path."""
        return self.task_temp_dir(task_id) / f"part{index}"

    def part_downloading_path(self, task_id: str, index: int) -> Path:
        """Return one partial part file path."""
        return self.task_temp_dir(task_id) / f"part{index}.downloading"

    def merged_path(self, task_id: str) -> Path:
        """Return the temporary merged file path."""
        return self.task_temp_dir(task_id) / "merged"

    def load(self, task_id: str) -> DownloadTaskState | None:
        """Load a persisted task state."""
        path = self.task_path(task_id)
        if not path.exists():
            return None
        with self._lock:
            try:
                with path.open("r", encoding="utf-8") as file:
                    raw = json.load(file)
            except (OSError, json.JSONDecodeError):
                LOGGER.warning("download.task_state.invalid task_id=%s", task_id)
                return None
        if not isinstance(raw, dict):
            return None
        return _read_task_state(raw)

    def load_all(self) -> list[DownloadTaskState]:
        """Load all valid persisted task states in creation order."""
        tasks_path = self._root_path / "tasks"
        if not tasks_path.exists():
            return []
        with self._lock:
            paths = sorted(tasks_path.glob("*.json"))
        states: list[DownloadTaskState] = []
        for path in paths:
            task_id = path.stem
            state = self.load(task_id)
            if state is not None:
                states.append(state)
        states.sort(key=lambda state: (state.created_at, state.task_id))
        return states

    def save(self, state: DownloadTaskState) -> None:
        """Persist task metadata atomically."""
        state.updated_at = time.time()
        path = self.task_path(state.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _dump_task_state(state)
        tmp_path = path.with_suffix(".json.tmp")
        with self._lock:
            with tmp_path.open("w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.write("\n")
            tmp_path.replace(path)

    def update(
        self, task_id: str, change: Callable[[DownloadTaskState], None]
    ) -> DownloadTaskState:
        """Apply a scheduler state change without overwriting concurrent part writes."""
        with self._lock:
            state = self.load(task_id)
            if state is None:
                raise KeyError(f"unknown download task: {task_id}")
            change(state)
            self.save(state)
            return state

    def delete(self, task_id: str) -> None:
        """Delete task metadata and temporary files."""
        with self._lock:
            self.task_path(task_id).unlink(missing_ok=True)
            shutil.rmtree(self.task_temp_dir(task_id), ignore_errors=True)

    def cleanup_temp(self, task_id: str) -> None:
        """Delete temporary part files while keeping task metadata."""
        shutil.rmtree(self.task_temp_dir(task_id), ignore_errors=True)

    def list_records(self) -> tuple[DownloadTaskRecord, ...]:
        """Return persisted transfer-center records."""
        tasks_dir = self._root_path / "tasks"
        if not tasks_dir.exists():
            return ()
        records: list[DownloadTaskRecord] = []
        for path in sorted(tasks_dir.glob("*.json")):
            state = self.load(path.stem)
            if state is None:
                continue
            records.append(_state_to_record(state))
        return tuple(records)

    def record_part(self, task_id: str, record: DownloadPartRecord) -> None:
        """Persist one part record."""
        with self._lock:
            state = self.load(task_id)
            if state is None:
                return
            existing = [part for part in state.parts if part.index != record.index]
            state.parts = [*existing, record]
            state.bytes_done = sum(part.actual_size for part in state.parts)
            self.save(state)

    def remove_part_record(self, task_id: str, index: int) -> None:
        """Delete one part record and file."""
        with self._lock:
            state = self.load(task_id)
            if state is not None:
                state.parts = [part for part in state.parts if part.index != index]
                state.bytes_done = sum(part.actual_size for part in state.parts)
                self.save(state)
            self.part_path(task_id, index).unlink(missing_ok=True)
            self.part_downloading_path(task_id, index).unlink(missing_ok=True)


def make_download_task_id(download_id: str, local_path: Path) -> str:
    """Build a stable non-secret task id from file id and local path."""
    raw = f"{download_id}|{local_path.expanduser().resolve(strict=False)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def download_url(
    http_client: httpx.Client,
    url: str,
    local_path: Path,
    *,
    settings: AppSettings,
    store: DownloadTaskStore,
    task_id: str,
    file_name: str,
    download_id: str | None = None,
    refresh_url: RefreshUrlCallback | None = None,
    callbacks: DownloadCallbacks | None = None,
    control: DownloadTaskControl | None = None,
) -> DownloadResult:
    """Download one URL with resumable range support when the server allows it."""
    if not url:
        raise DownloadError("下载地址为空")
    callbacks = callbacks or DownloadCallbacks()
    control = control or DownloadTaskControl()

    state = store.load(task_id) or DownloadTaskState(
        task_id=task_id,
        file_name=file_name,
        save_path=local_path,
        download_id=download_id,
    )
    state.file_name = file_name
    state.save_path = local_path
    state.download_id = download_id or state.download_id
    store.save(state)

    total_size = (
        _probe_download_size(http_client, url) if settings.max_download_threads > 1 else None
    )
    part_size = state.part_size or _download_part_size(total_size, settings)
    should_try_ranges = (
        total_size is not None
        and total_size > part_size
        and settings.max_download_threads > 1
    )

    if should_try_ranges and total_size is not None:
        try:
            return _download_with_ranges(
                http_client,
                url,
                local_path,
                total_size=total_size,
                part_size=part_size,
                settings=settings,
                store=store,
                state=state,
                refresh_url=refresh_url,
                callbacks=callbacks,
                control=control,
            )
        except RangeDownloadUnsupported:
            LOGGER.info("download.range_unsupported task_id=%s", task_id)
            store.cleanup_temp(task_id)
            state.parts = []
            state.bytes_done = 0
            state.supports_resume = False
            state.part_size = None
            store.save(state)

    return _download_single_stream(
        http_client,
        url,
        local_path,
        total_size=total_size,
        store=store,
        state=state,
        refresh_url=refresh_url,
        callbacks=callbacks,
        control=control,
    )


def _download_with_ranges(
    http_client: httpx.Client,
    url: str,
    local_path: Path,
    *,
    total_size: int,
    part_size: int,
    settings: AppSettings,
    store: DownloadTaskStore,
    state: DownloadTaskState,
    refresh_url: RefreshUrlCallback | None,
    callbacks: DownloadCallbacks,
    control: DownloadTaskControl,
) -> DownloadResult:
    max_workers = min(max(settings.max_download_threads, 1), 16)
    if state.total_bytes is not None and state.total_bytes != total_size:
        state.parts = []
        state.bytes_done = 0
        store.cleanup_temp(state.task_id)
    state.total_bytes = total_size
    state.part_size = part_size
    state.max_connections = max_workers
    state.supports_resume = True
    state.status = "校验中"
    state.error = ""
    _emit_status(callbacks, "校验中")
    store.save(state)

    parts = _build_parts(total_size, part_size)
    _clear_parts_if_plan_changed(store, state, parts)
    reused_bytes, reusable_indexes = _validate_existing_parts(store, state, parts)
    state = store.load(state.task_id) or state
    state.bytes_done = reused_bytes
    state.status = "下载中"
    store.save(state)
    _emit_progress(callbacks, reused_bytes, total_size)
    _emit_status(callbacks, "下载中")

    reusable = set(reusable_indexes)
    pending = [part for part in parts if part.index not in reusable]
    current_url = url
    allowed_workers = 1
    rate_limit_count = 0
    url_refresh_count = 0
    records_by_index = {record.index: record for record in state.parts}
    part_progress = {
        part.index: (
            part.expected_size
            if part.index in reusable
            else records_by_index[part.index].actual_size
        )
        for part in parts
        if part.index in reusable or part.index in records_by_index
    }
    progress_lock = threading.Lock()

    def report_part_progress(index: int, value: int) -> None:
        with progress_lock:
            part_progress[index] = value
            _emit_progress(callbacks, sum(part_progress.values()), total_size)

    while pending:
        stop_result = control.stop_result()
        if stop_result in ("paused", "cancelled"):
            return _stop_range_download(stop_result, store, state, callbacks, control)

        batch = pending[:allowed_workers]
        pending = pending[allowed_workers:]
        _emit_connections(callbacks, len(batch), max_workers)
        results: list[tuple[DownloadPart, PartResult]] = []
        with ThreadPoolExecutor(max_workers=len(batch)) as executor:
            futures = [
                executor.submit(
                    _download_range_part,
                    http_client,
                    current_url,
                    store,
                    state.task_id,
                    part,
                    settings.retry_max_attempts,
                    callbacks,
                    control,
                    report_part_progress,
                )
                for part in batch
            ]
            for part, future in zip(batch, futures, strict=True):
                try:
                    results.append((part, future.result()))
                except RangeDownloadUnsupported:
                    raise
        _emit_connections(callbacks, 0, max_workers)
        stop_result = control.stop_result()
        if stop_result in ("paused", "cancelled"):
            return _stop_range_download(stop_result, store, state, callbacks, control)

        for part, result in results:
            if result == "ok":
                allowed_workers = min(max_workers, allowed_workers + 1)
                url_refresh_count = 0
                continue
            if result in ("paused", "cancelled"):
                return _stop_range_download(result, store, state, callbacks, control)
            report_part_progress(part.index, 0)
            if result == "rate_limited":
                rate_limit_count += 1
                if rate_limit_count > MAX_RATE_LIMITS:
                    _mark_failed(store, state, callbacks, "分片下载被限流")
                    raise DownloadError("分片下载被限流，请稍后重试")
                allowed_workers = max(1, allowed_workers - 1)
                pending.append(part)
                time.sleep(RATE_LIMIT_BACKOFF_SECONDS)
                continue
            if result == "url_expired":
                url_refresh_count += 1
                if refresh_url is None or url_refresh_count > MAX_URL_REFRESHES:
                    _mark_failed(store, state, callbacks, "下载链接已过期或刷新失败")
                    raise DownloadError("下载链接已过期或刷新失败")
                current_url = refresh_url()
                allowed_workers = max(1, allowed_workers - 1)
                pending.append(part)
                continue
            _mark_failed(store, state, callbacks, "分片下载失败")
            raise DownloadError("分片下载失败")

    latest = store.load(state.task_id) or state
    latest.status = "合并中"
    latest.bytes_done = total_size
    store.save(latest)
    _emit_status(callbacks, "合并中")
    _emit_progress(callbacks, total_size, total_size)
    _merge_parts(store, latest, parts, control)

    stop_result = control.stop_result()
    if stop_result in ("paused", "cancelled"):
        return _stop_range_download(stop_result, store, latest, callbacks, control)

    merged_path = store.merged_path(latest.task_id)
    if merged_path.stat().st_size != total_size:
        _mark_failed(store, latest, callbacks, "下载分片合并后大小不一致")
        raise DownloadError("下载分片合并后大小不一致")
    local_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _replace_output_file(merged_path, local_path)
    except DownloadError as exc:
        _mark_failed(store, latest, callbacks, str(exc))
        raise
    store.delete(latest.task_id)
    _emit_status(callbacks, "已完成")
    _emit_connections(callbacks, 0, max_workers)
    return DownloadResult(status="已完成", task_id=latest.task_id, local_path=local_path)


def _download_single_stream(
    http_client: httpx.Client,
    url: str,
    local_path: Path,
    *,
    total_size: int | None,
    store: DownloadTaskStore,
    state: DownloadTaskState,
    refresh_url: RefreshUrlCallback | None,
    callbacks: DownloadCallbacks,
    control: DownloadTaskControl,
) -> DownloadResult:
    state.total_bytes = total_size
    state.supports_resume = False
    state.max_connections = 1
    state.status = "下载中"
    state.error = ""
    state.parts = []
    state.bytes_done = 0
    store.save(state)
    _emit_status(callbacks, "下载中")
    _emit_connections(callbacks, 1, 1)

    local_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = store.task_temp_dir(state.task_id) / "single.part"
    part_path.parent.mkdir(parents=True, exist_ok=True)
    bytes_done = 0
    current_url = url
    refresh_count = 0
    try:
        while True:
            try:
                with http_client.stream("GET", current_url) as response:
                    control.set_active_response(response)
                    if response.status_code == 403:
                        if refresh_url is None or refresh_count >= MAX_URL_REFRESHES:
                            raise DownloadError("下载链接已过期或刷新失败")
                        refresh_count += 1
                        current_url = refresh_url()
                        continue
                    if response.status_code in RATE_LIMIT_STATUS_CODES:
                        raise DownloadError("下载被限流，请稍后重试")
                    response.raise_for_status()
                    total = total_size or _read_content_length(
                        response.headers.get("Content-Length")
                    )
                    with part_path.open("wb") as output:
                        for chunk in response.iter_bytes(chunk_size=DOWNLOAD_CHUNK_SIZE):
                            stop_result = control.stop_result()
                            if stop_result in ("paused", "cancelled"):
                                return _stop_single_download(
                                    stop_result,
                                    store,
                                    state,
                                    callbacks,
                                    control,
                                    part_path,
                                )
                            if not chunk:  # pragma: no cover - docs/testing-exemptions.md
                                continue
                            output.write(chunk)
                            bytes_done += len(chunk)
                            state.bytes_done = bytes_done
                            store.save(state)
                            _emit_progress(callbacks, bytes_done, total)
                break
            finally:
                control.set_active_response(None)
        expected = total_size
        if expected is not None and part_path.stat().st_size != expected:
            raise DownloadError("下载大小不一致")
        _replace_output_file(part_path, local_path)
        store.delete(state.task_id)
        _emit_status(callbacks, "已完成")
        _emit_connections(callbacks, 0, 1)
        return DownloadResult(status="已完成", task_id=state.task_id, local_path=local_path)
    except DownloadError as exc:
        _remove_partial_file(part_path)
        _mark_failed(store, state, callbacks, str(exc))
        raise
    except httpx.HTTPStatusError as exc:
        _remove_partial_file(part_path)
        message = f"HTTP {exc.response.status_code}"
        _mark_failed(store, state, callbacks, message)
        raise DownloadError(message) from exc
    except httpx.HTTPError as exc:
        stop_result = control.stop_result()
        if stop_result in ("paused", "cancelled"):
            return _stop_single_download(
                stop_result, store, state, callbacks, control, part_path
            )
        _remove_partial_file(part_path)
        _mark_failed(store, state, callbacks, "网络错误")
        raise DownloadError("网络错误") from exc
    except OSError as exc:
        _remove_partial_file(part_path)
        message = f"无法写入本地文件：{exc}"
        _mark_failed(store, state, callbacks, message)
        raise DownloadError(message) from exc


def _stop_range_part(
    stop_result: PartResult,
    store: DownloadTaskStore,
    task_id: str,
    part: DownloadPart,
    temp_path: Path,
    progress_callback: Callable[[int, int], None],
) -> PartResult:
    if stop_result == "cancelled":
        store.remove_part_record(task_id, part.index)
        progress_callback(part.index, 0)
        return stop_result

    actual_size = temp_path.stat().st_size if temp_path.exists() else 0
    if 0 < actual_size <= part.expected_size:
        digest = _compute_md5(temp_path)
        if actual_size == part.expected_size:
            temp_path.replace(store.part_path(task_id, part.index))
        store.record_part(
            task_id,
            DownloadPartRecord(
                index=part.index,
                start=part.start,
                end=part.end,
                expected_size=part.expected_size,
                actual_size=actual_size,
                md5=digest,
            ),
        )
        progress_callback(part.index, actual_size)
    else:
        store.remove_part_record(task_id, part.index)
        progress_callback(part.index, 0)
    return stop_result


def _download_range_part(
    http_client: httpx.Client,
    url: str,
    store: DownloadTaskStore,
    task_id: str,
    part: DownloadPart,
    retry_max_attempts: int,
    callbacks: DownloadCallbacks,
    control: DownloadTaskControl,
    progress_callback: Callable[[int, int], None],
) -> PartResult:
    part_dir = store.task_temp_dir(task_id)
    part_dir.mkdir(parents=True, exist_ok=True)
    final_path = store.part_path(task_id, part.index)
    temp_path = store.part_downloading_path(task_id, part.index)
    state = store.load(task_id)
    partial_record = next(
        (
            record
            for record in (state.parts if state is not None else [])
            if record.index == part.index and 0 < record.actual_size < record.expected_size
        ),
        None,
    )
    resume_size = partial_record.actual_size if partial_record is not None else 0
    total_size = state.total_bytes if state is not None else None
    attempts = retry_max_attempts + 1
    for attempt in range(attempts):
        stop_result = control.stop_result()
        if stop_result is not None:
            return _stop_range_part(
                stop_result, store, task_id, part, temp_path, progress_callback
            )
        bytes_done = resume_size if attempt == 0 else 0
        md5 = hashlib.md5()
        if bytes_done:
            with temp_path.open("rb") as existing:
                while chunk := existing.read(DOWNLOAD_CHUNK_SIZE):
                    md5.update(chunk)
        try:
            with http_client.stream(
                "GET",
                url,
                headers={"Range": f"bytes={part.start + bytes_done}-{part.end}"},
            ) as response:
                control.set_active_response(response)
                if response.status_code == 200:
                    store.remove_part_record(task_id, part.index)
                    raise RangeDownloadUnsupported("Range download unsupported")
                if response.status_code == 403:
                    store.remove_part_record(task_id, part.index)
                    progress_callback(part.index, 0)
                    return "url_expired"
                if response.status_code in RATE_LIMIT_STATUS_CODES:
                    store.remove_part_record(task_id, part.index)
                    progress_callback(part.index, 0)
                    return "rate_limited"
                response.raise_for_status()
                if response.status_code != 206:
                    store.remove_part_record(task_id, part.index)
                    progress_callback(part.index, 0)
                    return "fatal"
                content_range = response.headers.get("Content-Range", "")
                range_text, _, total_text = content_range.partition("/")
                if range_text != f"bytes {part.start + bytes_done}-{part.end}" or (
                    total_size is not None and total_text != str(total_size)
                ):
                    store.remove_part_record(task_id, part.index)
                    progress_callback(part.index, 0)
                    return "fatal"
                stop_requested: PartResult | None = None
                with temp_path.open("ab" if bytes_done else "wb") as output:
                    for chunk in response.iter_bytes(chunk_size=DOWNLOAD_CHUNK_SIZE):
                        stop_requested = control.stop_result()
                        if stop_requested is not None:
                            break
                        if not chunk:  # pragma: no cover - docs/testing-exemptions.md
                            continue
                        output.write(chunk)
                        md5.update(chunk)
                        bytes_done += len(chunk)
                        progress_callback(part.index, bytes_done)
            if stop_requested is not None:
                return _stop_range_part(
                    stop_requested, store, task_id, part, temp_path, progress_callback
                )
        except RangeDownloadUnsupported:
            raise
        except httpx.HTTPStatusError:
            store.remove_part_record(task_id, part.index)
        except httpx.HTTPError:
            stop_result = control.stop_result()
            if stop_result is not None:
                return _stop_range_part(
                    stop_result, store, task_id, part, temp_path, progress_callback
                )
            store.remove_part_record(task_id, part.index)
        except OSError:
            store.remove_part_record(task_id, part.index)
        finally:
            control.set_active_response(None)

        if temp_path.exists() and temp_path.stat().st_size == part.expected_size:
            temp_path.replace(final_path)
            store.record_part(
                task_id,
                DownloadPartRecord(
                    index=part.index,
                    start=part.start,
                    end=part.end,
                    expected_size=part.expected_size,
                    actual_size=part.expected_size,
                    md5=md5.hexdigest(),
                ),
            )
            return "ok"
        store.remove_part_record(task_id, part.index)
        progress_callback(part.index, 0)
        if attempt < attempts - 1:
            time.sleep(attempt + 1)
    return "fatal"


def _stop_range_download(
    stop_result: PartResult,
    store: DownloadTaskStore,
    state: DownloadTaskState,
    callbacks: DownloadCallbacks,
    control: DownloadTaskControl,
) -> DownloadResult:
    latest = store.load(state.task_id) or state
    if stop_result == "cancelled":
        latest.status = "已取消"
        latest.error = "用户取消下载"
        latest.bytes_done = 0
        latest.parts = []
        store.cleanup_temp(latest.task_id)
        _emit_status(callbacks, "已取消")
        _emit_connections(callbacks, 0, latest.max_connections)
        if control.cleanup_on_cancel:
            store.delete(latest.task_id)
        else:
            store.save(latest)
        return DownloadResult(status="已取消", task_id=latest.task_id, local_path=latest.save_path)
    latest.status = "已暂停"
    latest.error = ""
    latest.bytes_done = sum(part.actual_size for part in latest.parts)
    store.merged_path(latest.task_id).unlink(missing_ok=True)
    store.save(latest)
    _emit_status(callbacks, "已暂停")
    _emit_progress(callbacks, latest.bytes_done, latest.total_bytes)
    _emit_connections(callbacks, 0, latest.max_connections)
    return DownloadResult(status="已暂停", task_id=latest.task_id, local_path=latest.save_path)


def _stop_single_download(
    stop_result: PartResult,
    store: DownloadTaskStore,
    state: DownloadTaskState,
    callbacks: DownloadCallbacks,
    control: DownloadTaskControl,
    part_path: Path,
) -> DownloadResult:
    if stop_result == "cancelled":
        _remove_partial_file(part_path)
        state.status = "已取消"
        state.error = "用户取消下载"
        state.bytes_done = 0
        store.save(state)
        if control.cleanup_on_cancel:
            store.delete(state.task_id)
        _emit_status(callbacks, "已取消")
        _emit_connections(callbacks, 0, 1)
        return DownloadResult(status="已取消", task_id=state.task_id, local_path=state.save_path)
    _remove_partial_file(part_path)
    state.status = "已暂停"
    state.error = "远端不支持可续传下载"
    state.bytes_done = 0
    store.save(state)
    _emit_status(callbacks, "已暂停")
    _emit_connections(callbacks, 0, 1)
    return DownloadResult(status="已暂停", task_id=state.task_id, local_path=state.save_path)


def _mark_failed(
    store: DownloadTaskStore,
    state: DownloadTaskState,
    callbacks: DownloadCallbacks,
    message: str,
) -> None:
    latest = store.load(state.task_id) or state
    latest.status = "失败"
    latest.error = message
    latest.parts = [part for part in latest.parts if part.actual_size == part.expected_size]
    latest.bytes_done = sum(part.actual_size for part in latest.parts)
    temp_dir = store.task_temp_dir(latest.task_id)
    if temp_dir.exists():
        for entry in temp_dir.glob("part*.downloading"):
            _remove_partial_file(entry)
    store.save(latest)
    _emit_status(callbacks, "失败")
    _emit_connections(callbacks, 0, latest.max_connections)


def _validate_existing_parts(
    store: DownloadTaskStore,
    state: DownloadTaskState,
    parts: list[DownloadPart],
) -> tuple[int, set[int]]:
    part_by_index = {part.index: part for part in parts}
    reusable: set[int] = set()
    partial: set[int] = set()
    downloaded = 0
    for record in list(state.parts):
        planned = part_by_index.get(record.index)
        if (
            planned is None
            or record.start != planned.start
            or record.end != planned.end
            or record.expected_size != planned.expected_size
        ):
            store.remove_part_record(state.task_id, record.index)
            continue
        if record.actual_size == planned.expected_size:
            path = store.part_path(state.task_id, record.index)
            if not path.exists():
                store.remove_part_record(state.task_id, record.index)
                continue
            if path.stat().st_size != planned.expected_size:
                store.remove_part_record(state.task_id, record.index)
                continue
            if _compute_md5(path) != record.md5:
                store.remove_part_record(state.task_id, record.index)
                continue
            reusable.add(record.index)
            downloaded += planned.expected_size
            continue
        if not 0 < record.actual_size < planned.expected_size:
            store.remove_part_record(state.task_id, record.index)
            continue
        path = store.part_downloading_path(state.task_id, record.index)
        if not path.exists():
            store.remove_part_record(state.task_id, record.index)
            continue
        if path.stat().st_size != record.actual_size:
            store.remove_part_record(state.task_id, record.index)
            continue
        if _compute_md5(path) != record.md5:
            store.remove_part_record(state.task_id, record.index)
            continue
        partial.add(record.index)
        downloaded += record.actual_size

    temp_dir = store.task_temp_dir(state.task_id)
    if temp_dir.exists():
        for entry in temp_dir.iterdir():
            if entry.name == "merged":
                _remove_partial_file(entry)
                continue
            if not entry.name.startswith("part"):
                continue
            index_text = entry.name.removeprefix("part").removesuffix(".downloading")
            try:
                index = int(index_text)
            except ValueError:
                continue
            if entry.name.endswith(".downloading"):
                if index not in partial:
                    _remove_partial_file(entry)
            elif index not in reusable:
                _remove_partial_file(entry)
    return downloaded, reusable


def _clear_parts_if_plan_changed(
    store: DownloadTaskStore,
    state: DownloadTaskState,
    parts: list[DownloadPart],
) -> None:
    if state.total_bytes is not None and parts and parts[-1].end + 1 != state.total_bytes:
        state.parts = []
        state.bytes_done = 0
        store.cleanup_temp(state.task_id)
        store.save(state)


def _merge_parts(
    store: DownloadTaskStore,
    state: DownloadTaskState,
    parts: list[DownloadPart],
    control: DownloadTaskControl,
) -> None:
    merged_path = store.merged_path(state.task_id)
    merged_path.parent.mkdir(parents=True, exist_ok=True)
    _remove_partial_file(merged_path)
    try:
        with merged_path.open("wb") as output:
            for part in parts:
                stop_result = control.stop_result()
                if stop_result in ("paused", "cancelled"):
                    return
                part_path = store.part_path(state.task_id, part.index)
                with part_path.open("rb") as input_file:
                    while chunk := input_file.read(DOWNLOAD_CHUNK_SIZE):
                        output.write(chunk)
    except OSError as exc:
        raise DownloadError(f"合并分片文件失败：{exc}") from exc


def _build_parts(total_size: int, part_size: int) -> list[DownloadPart]:
    return [
        DownloadPart(index=index, start=start, end=min(start + part_size - 1, total_size - 1))
        for index, start in enumerate(range(0, total_size, part_size))
    ]


def _download_part_size(total_size: int | None, settings: AppSettings) -> int:
    configured_size = settings.download_part_size_mb * BYTES_PER_MB
    if total_size is None or settings.download_part_mode == "fixed":
        return configured_size
    target_workers = max(1, min(settings.max_download_threads, 16))
    return max(configured_size, math.ceil(total_size / target_workers))


def _probe_download_size(http_client: httpx.Client, url: str) -> int | None:
    try:
        response = http_client.head(url)
        response.raise_for_status()
    except httpx.HTTPError:
        LOGGER.info("download.head_unavailable")
        return None
    content_length = _read_content_length(response.headers.get("Content-Length"))
    if content_length is None or content_length <= 0:
        return None
    return content_length


def _read_content_length(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    if parsed < 0:
        return None
    return parsed


def _compute_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 64):
            digest.update(chunk)
    return digest.hexdigest()


def _replace_output_file(source_path: Path, target_path: Path) -> None:
    """Publish a completed download only when the destination is still unoccupied."""
    try:
        os.link(source_path, target_path)
    except FileExistsError as exc:
        raise DownloadError("下载目标已存在，请重新选择保存路径") from exc
    except OSError as exc:
        if exc.errno in {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP}:
            _copy_output_exclusive(source_path, target_path)
        elif exc.errno == errno.EXDEV:
            descriptor, name = tempfile.mkstemp(
                prefix=f".{target_path.name}.", dir=target_path.parent
            )
            os.close(descriptor)
            tmp_path = Path(name)
            try:
                shutil.copy2(source_path, tmp_path)
                if tmp_path.stat().st_size != source_path.stat().st_size:
                    raise OSError("跨盘拷贝大小不匹配")
                try:
                    os.link(tmp_path, target_path)
                except FileExistsError as conflict:
                    raise DownloadError("下载目标已存在，请重新选择保存路径") from conflict
                except OSError as link_error:
                    if link_error.errno not in {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP}:
                        raise
                    _copy_output_exclusive(tmp_path, target_path)
            finally:
                tmp_path.unlink(missing_ok=True)
        else:
            raise
    source_path.unlink()


def _copy_output_exclusive(source_path: Path, target_path: Path) -> None:
    try:
        output = target_path.open("xb")
    except FileExistsError as exc:
        raise DownloadError("下载目标已存在，请重新选择保存路径") from exc
    try:
        with output, source_path.open("rb") as input_file:
            shutil.copyfileobj(input_file, output, DOWNLOAD_CHUNK_SIZE)
    except BaseException:
        target_path.unlink(missing_ok=True)
        raise


def _remove_partial_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("download.partial_cleanup_failed")


def _emit_progress(callbacks: DownloadCallbacks, bytes_done: int, total_bytes: int | None) -> None:
    if callbacks.progress is not None:
        callbacks.progress(bytes_done, total_bytes)


def _emit_status(callbacks: DownloadCallbacks, status: DownloadStatus) -> None:
    if callbacks.status is not None:
        callbacks.status(status)


def _emit_connections(callbacks: DownloadCallbacks, active: int, maximum: int) -> None:
    if callbacks.connections is not None:
        callbacks.connections(active, maximum)


def _state_to_record(state: DownloadTaskState) -> DownloadTaskRecord:
    return DownloadTaskRecord(
        task_id=state.task_id,
        name=state.file_name,
        target_path=state.save_path,
        status=state.status,
        bytes_done=state.bytes_done,
        total_bytes=state.total_bytes,
        supports_resume=state.supports_resume and state.status in {"已暂停", "失败"},
        error=state.error,
        max_connections=state.max_connections,
    )


def _dump_task_state(state: DownloadTaskState) -> dict[str, Any]:
    return {
        "version": state.version,
        "task_id": state.task_id,
        "file_name": state.file_name,
        "save_path": str(state.save_path),
        "status": state.status,
        "download_id": state.download_id,
        "total_bytes": state.total_bytes,
        "bytes_done": state.bytes_done,
        "part_size": state.part_size,
        "max_connections": state.max_connections,
        "supports_resume": state.supports_resume,
        "error": state.error,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "parts": [
            {
                "index": part.index,
                "start": part.start,
                "end": part.end,
                "expected_size": part.expected_size,
                "actual_size": part.actual_size,
                "md5": part.md5,
            }
            for part in state.parts
        ],
    }


def _read_task_state(raw: dict[str, Any]) -> DownloadTaskState | None:
    task_id = _read_text(raw.get("task_id"))
    file_name = _read_text(raw.get("file_name"))
    save_path_text = _read_text(raw.get("save_path"))
    if not task_id or not file_name or not save_path_text:
        return None
    parts: list[DownloadPartRecord] = []
    raw_parts = raw.get("parts")
    if isinstance(raw_parts, list):
        for raw_part in raw_parts:
            part = _read_part_record(raw_part)
            if part is not None:
                parts.append(part)
    return DownloadTaskState(
        task_id=task_id,
        file_name=file_name,
        save_path=Path(save_path_text),
        status=_read_status(raw.get("status")),
        download_id=_read_text(raw.get("download_id")) or None,
        total_bytes=_read_optional_positive_int(raw.get("total_bytes")),
        bytes_done=_read_non_negative_int(raw.get("bytes_done")),
        part_size=_read_optional_positive_int(raw.get("part_size")),
        max_connections=max(1, _read_non_negative_int(raw.get("max_connections"))),
        supports_resume=bool(raw.get("supports_resume")),
        error=_read_text(raw.get("error")),
        version=_read_non_negative_int(raw.get("version")) or TASK_METADATA_VERSION,
        created_at=float(raw.get("created_at") or time.time()),
        updated_at=float(raw.get("updated_at") or time.time()),
        parts=parts,
    )


def _read_part_record(raw: object) -> DownloadPartRecord | None:
    if not isinstance(raw, dict):
        return None
    index = _read_non_negative_int(raw.get("index"))
    start = _read_non_negative_int(raw.get("start"))
    end = _read_non_negative_int(raw.get("end"))
    expected_size = _read_non_negative_int(raw.get("expected_size"))
    actual_size = _read_non_negative_int(raw.get("actual_size"))
    md5 = _read_text(raw.get("md5"))
    if (
        end < start
        or expected_size <= 0
        or actual_size <= 0
        or actual_size > expected_size
        or not md5
    ):
        return None
    return DownloadPartRecord(
        index=index,
        start=start,
        end=end,
        expected_size=expected_size,
        actual_size=actual_size,
        md5=md5,
    )


def _read_status(value: object) -> DownloadStatus:
    if value in {"等待中", "校验中", "下载中", "合并中", "已暂停", "已完成", "失败", "已取消"}:
        return value  # type: ignore[return-value]
    return "等待中"


def _read_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""


def _read_non_negative_int(value: object) -> int:
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str):
        try:
            return max(0, int(value))
        except ValueError:
            return 0
    return 0


def _read_optional_positive_int(value: object) -> int | None:
    parsed = _read_non_negative_int(value)
    if parsed <= 0:
        return None
    return parsed
