from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from openwopan.storage.settings import AppSettings
from openwopan.tasks.download import (
    DownloadCallbacks,
    DownloadError,
    DownloadResult,
    DownloadTaskControl,
    DownloadTaskState,
    DownloadTaskStore,
)
from openwopan.tasks.scheduler import DownloadScheduler, DownloadTaskInput


def _task(tmp_path: Path, task_id: str) -> DownloadTaskInput:
    return DownloadTaskInput(
        task_id=task_id,
        file_name=f"{task_id}.bin",
        local_path=tmp_path / f"{task_id}.bin",
        url=f"https://example.test/{task_id}",
        download_id=task_id,
        settings=AppSettings(max_concurrent_downloads=2),
        store=DownloadTaskStore(tmp_path / "store"),
        http_client=httpx.Client(),
    )


def _wait_for(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    assert condition()


def test_scheduler_is_fifo_and_refills_slots(tmp_path: Path) -> None:
    started: list[str] = []
    active = 0
    maximum = 0
    lock = threading.Lock()
    release = threading.Event()

    def execute(
        task: DownloadTaskInput, control: DownloadTaskControl, callbacks: DownloadCallbacks
    ) -> DownloadResult:
        nonlocal active, maximum
        started.append(task.task_id)
        with lock:
            active += 1
            maximum = max(maximum, active)
        while not release.is_set():
            time.sleep(0.005)
        with lock:
            active -= 1
        return DownloadResult("已完成", task.task_id, task.local_path)

    scheduler = DownloadScheduler(max_concurrent_downloads=2, executor=execute)
    try:
        for task_id in ("one", "two", "three"):
            scheduler.submit(_task(tmp_path, task_id))
        _wait_for(lambda: started == ["one", "two"])
        assert scheduler.state("three").status == "等待中"
        release.set()
        _wait_for(lambda: scheduler.state("three").status == "已完成")
        assert started == ["one", "two", "three"]
        assert maximum == 2
    finally:
        scheduler.close()


def test_task_failure_does_not_block_next_task(tmp_path: Path) -> None:
    started: list[str] = []
    release = threading.Event()

    def execute(
        task: DownloadTaskInput, control: DownloadTaskControl, callbacks: DownloadCallbacks
    ) -> DownloadResult:
        started.append(task.task_id)
        if task.task_id == "bad":
            raise DownloadError("network failed")
        release.wait(1)
        return DownloadResult("已完成", task.task_id, task.local_path)

    scheduler = DownloadScheduler(max_concurrent_downloads=1, executor=execute)
    try:
        scheduler.submit(_task(tmp_path, "bad"))
        scheduler.submit(_task(tmp_path, "good"))
        _wait_for(lambda: scheduler.state("bad").status == "失败")
        _wait_for(lambda: started == ["bad", "good"])
        release.set()
        _wait_for(lambda: scheduler.state("good").status == "已完成")
        assert "network failed" in scheduler.state("bad").error
    finally:
        scheduler.close()


def test_pause_resume_and_cancel_are_task_scoped(tmp_path: Path) -> None:
    started: list[str] = []
    release = threading.Event()

    def execute(
        task: DownloadTaskInput, control: DownloadTaskControl, callbacks: DownloadCallbacks
    ) -> DownloadResult:
        started.append(task.task_id)
        while not release.is_set():
            stop = control.stop_result()
            if stop == "paused":
                return DownloadResult("已暂停", task.task_id, task.local_path)
            if stop == "cancelled":
                return DownloadResult("已取消", task.task_id, task.local_path)
            time.sleep(0.005)
        return DownloadResult("已完成", task.task_id, task.local_path)

    scheduler = DownloadScheduler(max_concurrent_downloads=1, executor=execute)
    try:
        scheduler.submit(_task(tmp_path, "pause"))
        scheduler.submit(_task(tmp_path, "cancel"))
        _wait_for(lambda: started == ["pause"])
        assert scheduler.pause("pause")
        _wait_for(lambda: scheduler.state("pause").status == "已暂停")
        _wait_for(lambda: started == ["pause", "cancel"])
        assert scheduler.cancel("cancel")
        _wait_for(lambda: scheduler.state("cancel").status == "已取消")
        assert scheduler.resume("pause")
        _wait_for(lambda: started == ["pause", "cancel", "pause"])
        release.set()
        _wait_for(lambda: scheduler.state("pause").status == "已完成")
        assert scheduler.state("cancel").status == "已取消"
    finally:
        scheduler.close()


def test_cancel_cleanup_does_not_recreate_persisted_state(tmp_path: Path) -> None:
    started = threading.Event()

    def execute(
        task: DownloadTaskInput,
        control: DownloadTaskControl,
        _callbacks: DownloadCallbacks,
    ) -> DownloadResult:
        started.set()
        while control.stop_result() != "cancelled":
            time.sleep(0.005)
        return DownloadResult("已取消", task.task_id, task.local_path)

    task = _task(tmp_path, "cleanup")
    scheduler = DownloadScheduler(max_concurrent_downloads=1, executor=execute)
    try:
        scheduler.submit(task)
        assert started.wait(1)
        assert scheduler.cancel(task.task_id, cleanup=True) is True
        _wait_for(lambda: scheduler.state(task.task_id).status == "已取消")
        assert task.store.load(task.task_id) is None
    finally:
        scheduler.close()


def test_recovery_requeues_active_state_and_keeps_paused_and_failed(
    tmp_path: Path,
) -> None:
    store = DownloadTaskStore(tmp_path / "store")
    states = [
        DownloadTaskState("running", "running.bin", tmp_path / "running.bin", status="下载中"),
        DownloadTaskState("paused", "paused.bin", tmp_path / "paused.bin", status="已暂停"),
        DownloadTaskState(
            "failed",
            "failed.bin",
            tmp_path / "failed.bin",
            status="失败",
            error="旧错误",
        ),
    ]
    for state in states:
        store.save(state)

    started = threading.Event()
    release = threading.Event()
    events: list[tuple[str, str]] = []

    def execute(
        task: DownloadTaskInput,
        control: DownloadTaskControl,
        callbacks: DownloadCallbacks,
    ) -> DownloadResult:
        started.set()
        release.wait(1)
        return DownloadResult("已完成", task.task_id, task.local_path)

    tasks = [_task(tmp_path, task_id) for task_id in ("running", "paused", "failed")]
    scheduler = DownloadScheduler(
        max_concurrent_downloads=1,
        executor=execute,
        on_event=lambda event: events.append((event.task_id, event.status)),
    )
    try:
        scheduler.recover(tasks)
        assert started.wait(1)
        assert scheduler.state("running").status == "下载中"
        assert scheduler.state("paused").status == "已暂停"
        assert scheduler.state("failed").status == "失败"
        assert scheduler.state("failed").error == "旧错误"
        assert {task_id for task_id, _ in events} == {"running", "paused", "failed"}
        release.set()
        _wait_for(lambda: scheduler.state("running").status == "已完成")
    finally:
        scheduler.close()


def test_store_load_all_uses_creation_order(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path / "store")
    later = DownloadTaskState("a-task", "later.bin", tmp_path / "later.bin", created_at=20.0)
    earlier = DownloadTaskState("z-task", "earlier.bin", tmp_path / "earlier.bin", created_at=10.0)
    store.save(later)
    store.save(earlier)

    assert [state.task_id for state in store.load_all()] == ["z-task", "a-task"]
