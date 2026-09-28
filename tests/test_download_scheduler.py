from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

import openwopan.tasks.scheduler as scheduler_module
from openwopan.storage.settings import AppSettings
from openwopan.tasks.download import (
    DownloadCallbacks,
    DownloadError,
    DownloadPartRecord,
    DownloadResult,
    DownloadTaskControl,
    DownloadTaskState,
    DownloadTaskStore,
)
from openwopan.tasks.scheduler import DownloadScheduler, DownloadTaskEvent, DownloadTaskInput


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


def test_execute_download_forwards_task_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _task(tmp_path, "forward")
    control = DownloadTaskControl()
    callbacks = DownloadCallbacks()
    result = DownloadResult("已完成", task.task_id, task.local_path)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_download_url(*args: object, **kwargs: object) -> DownloadResult:
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(scheduler_module, "download_url", fake_download_url)

    assert scheduler_module._execute_download(task, control, callbacks) == result
    assert calls and calls[0][0][:3] == (task.http_client, task.url, task.local_path)
    assert calls[0][1]["control"] is control
    assert calls[0][1]["callbacks"] is callbacks


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


def test_scheduler_increases_limit_and_refills_fifo_queue(tmp_path: Path) -> None:
    task_ids = ("one", "two", "three", "four", "five")
    started: set[str] = set()
    scheduled: list[str] = []
    active = 0
    maximum = 0
    lock = threading.Lock()
    releases = {task_id: threading.Event() for task_id in task_ids}

    def execute(
        task: DownloadTaskInput, _control: DownloadTaskControl, _callbacks: DownloadCallbacks
    ) -> DownloadResult:
        nonlocal active, maximum
        with lock:
            started.add(task.task_id)
            active += 1
            maximum = max(maximum, active)
        releases[task.task_id].wait(5)
        with lock:
            active -= 1
        return DownloadResult("已完成", task.task_id, task.local_path)

    scheduler = DownloadScheduler(
        max_concurrent_downloads=1,
        executor=execute,
        on_event=lambda event: scheduled.append(event.task_id)
        if event.status == "下载中" else None,
    )
    try:
        for task_id in task_ids:
            scheduler.submit(_task(tmp_path, task_id))
        _wait_for(lambda: started == {"one"})

        scheduler.set_max_concurrent_downloads(5)

        _wait_for(lambda: started == set(task_ids))
        assert scheduled == list(task_ids)
        assert maximum == 5
        for release in releases.values():
            release.set()
        _wait_for(lambda: all(scheduler.state(task_id).status == "已完成" for task_id in task_ids))
    finally:
        for release in releases.values():
            release.set()
        scheduler.close()


def test_scheduler_decreasing_limit_waits_for_active_tasks(tmp_path: Path) -> None:
    task_ids = ("one", "two", "three")
    started: set[str] = set()
    active = 0
    maximum = 0
    lock = threading.Lock()
    releases = {task_id: threading.Event() for task_id in task_ids}

    def execute(
        task: DownloadTaskInput, _control: DownloadTaskControl, _callbacks: DownloadCallbacks
    ) -> DownloadResult:
        nonlocal active, maximum
        with lock:
            started.add(task.task_id)
            active += 1
            maximum = max(maximum, active)
        releases[task.task_id].wait(5)
        with lock:
            active -= 1
        return DownloadResult("已完成", task.task_id, task.local_path)

    scheduler = DownloadScheduler(max_concurrent_downloads=2, executor=execute)
    try:
        for task_id in task_ids:
            scheduler.submit(_task(tmp_path, task_id))
        _wait_for(lambda: started == {"one", "two"})

        scheduler.set_max_concurrent_downloads(1)
        releases["one"].set()
        _wait_for(lambda: scheduler.state("one").status == "已完成")
        assert started == {"one", "two"}
        assert scheduler.state("two").status == "下载中"
        assert scheduler.state("three").status == "等待中"

        releases["two"].set()
        _wait_for(lambda: started == set(task_ids))
        releases["three"].set()
        _wait_for(lambda: scheduler.state("three").status == "已完成")
        assert maximum == 2
    finally:
        for release in releases.values():
            release.set()
        scheduler.close()


@pytest.mark.parametrize("invalid_limit", [0, -1, 6])
def test_scheduler_rejects_invalid_update_without_refilling_queue(
    tmp_path: Path, invalid_limit: int
) -> None:
    started: list[str] = []
    release = threading.Event()

    def execute(
        task: DownloadTaskInput, _control: DownloadTaskControl, _callbacks: DownloadCallbacks
    ) -> DownloadResult:
        started.append(task.task_id)
        release.wait(5)
        return DownloadResult("已完成", task.task_id, task.local_path)

    scheduler = DownloadScheduler(max_concurrent_downloads=1, executor=execute)
    try:
        scheduler.submit(_task(tmp_path, "one"))
        scheduler.submit(_task(tmp_path, "two"))
        _wait_for(lambda: started == ["one"])

        with pytest.raises(ValueError, match="positive|capacity"):
            scheduler.set_max_concurrent_downloads(invalid_limit)
        assert scheduler.state("two").status == "等待中"

        scheduler.submit(_task(tmp_path, "three"))
        assert scheduler.state("three").status == "等待中"
        scheduler.set_max_concurrent_downloads(2)
        _wait_for(lambda: "two" in started)
        assert scheduler.state("three").status == "等待中"
    finally:
        release.set()
        scheduler.close()


def test_scheduler_rejects_invalid_and_closed_submission(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        DownloadScheduler(max_concurrent_downloads=0)

    scheduler = DownloadScheduler(max_concurrent_downloads=1, executor=lambda *_: DownloadResult(
        "已完成", "unused", Path("unused")
    ))
    with pytest.raises(ValueError, match="positive"):
        scheduler.set_max_concurrent_downloads(0)
    scheduler.set_max_concurrent_downloads(2)
    scheduler.close()
    scheduler.close()
    with pytest.raises(RuntimeError, match="closed"):
        scheduler.set_max_concurrent_downloads(1)
    with pytest.raises(RuntimeError, match="closed"):
        scheduler.submit(_task(tmp_path, "closed"))


def test_scheduler_controls_waiting_tasks_and_refuses_terminal_cancel(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def execute(
        task: DownloadTaskInput, _control: DownloadTaskControl, _callbacks: DownloadCallbacks
    ) -> DownloadResult:
        started.set()
        release.wait(1)
        return DownloadResult("已完成", task.task_id, task.local_path)

    scheduler = DownloadScheduler(max_concurrent_downloads=1, executor=execute)
    try:
        scheduler.submit(_task(tmp_path, "active"))
        scheduler.submit(_task(tmp_path, "cancelled"))
        scheduler.submit(_task(tmp_path, "paused"))
        assert started.wait(1)
        assert scheduler.cancel("cancelled")
        assert scheduler.state("cancelled").status == "已取消"
        assert scheduler.pause("paused")
        assert scheduler.state("paused").status == "已暂停"
        release.set()
        _wait_for(lambda: scheduler.state("active").status == "已完成")
        assert scheduler.cancel("active") is False
        assert scheduler.resume("paused")
        _wait_for(lambda: scheduler.state("paused").status == "已完成")
    finally:
        release.set()
        scheduler.close()


def test_scheduler_maps_unexpected_error_and_failed_result(tmp_path: Path) -> None:
    def unexpected(
        _task: DownloadTaskInput, _control: DownloadTaskControl, _callbacks: DownloadCallbacks
    ) -> DownloadResult:
        raise RuntimeError("unexpected")

    failed_scheduler = DownloadScheduler(max_concurrent_downloads=1, executor=unexpected)
    try:
        failed_scheduler.submit(_task(tmp_path, "unexpected"))
        _wait_for(lambda: failed_scheduler.state("unexpected").status == "失败")
        assert failed_scheduler.state("unexpected").error == "下载任务执行失败"
    finally:
        failed_scheduler.close()

    def failed(
        task: DownloadTaskInput, _control: DownloadTaskControl, _callbacks: DownloadCallbacks
    ) -> DownloadResult:
        return DownloadResult("失败", task.task_id, task.local_path)

    result_scheduler = DownloadScheduler(max_concurrent_downloads=1, executor=failed)
    try:
        result_scheduler.submit(_task(tmp_path, "failed"))
        _wait_for(lambda: result_scheduler.state("failed").status == "失败")
        assert result_scheduler.state("failed").error == "下载失败"
    finally:
        result_scheduler.close()


def test_scheduler_forwards_status_and_connection_events(tmp_path: Path) -> None:
    events: list[DownloadTaskEvent] = []

    def execute(
        task: DownloadTaskInput, _control: DownloadTaskControl, callbacks: DownloadCallbacks
    ) -> DownloadResult:
        assert callbacks.status is not None
        assert callbacks.connections is not None
        callbacks.status("校验中")
        callbacks.connections(2, 4)
        callbacks.status("已完成")
        return DownloadResult("已完成", task.task_id, task.local_path)

    scheduler = DownloadScheduler(
        max_concurrent_downloads=1,
        executor=execute,
    )
    scheduler.set_event_callback(lambda event: events.append(event))
    try:
        scheduler.submit(_task(tmp_path, "events"))
        _wait_for(lambda: scheduler.state("events").status == "已完成")
        assert any(event.status == "校验中" for event in events)
        connection_event = next(event for event in events if event.active_connections == 2)
        assert connection_event.task_id == "events"
        assert connection_event.max_connections == 4
    finally:
        scheduler.close()


def test_scheduler_progress_preserves_persisted_partial_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    callbacks: list[DownloadCallbacks] = []

    def execute(
        task: DownloadTaskInput,
        _control: DownloadTaskControl,
        task_callbacks: DownloadCallbacks,
    ) -> DownloadResult:
        callbacks.append(task_callbacks)
        started.set()
        release.wait(1)
        return DownloadResult("已暂停", task.task_id, task.local_path)

    task = _task(tmp_path, "partial")
    scheduler = DownloadScheduler(max_concurrent_downloads=1, executor=execute)
    record = DownloadPartRecord(
        index=0,
        start=0,
        end=3,
        expected_size=4,
        actual_size=4,
        md5="0" * 32,
    )
    try:
        scheduler.submit(task)
        assert started.wait(1)
        task.store.record_part(task.task_id, record)

        assert callbacks[0].progress is not None
        clock = iter((100.0, 100.6))
        monkeypatch.setattr(scheduler_module.time, "monotonic", lambda: next(clock))
        callbacks[0].progress(4, 8)
        callbacks[0].progress(4, 8)

        persisted = task.store.load(task.task_id)
        assert persisted is not None
        assert persisted.parts == [record]
        assert persisted.bytes_done == 4
        assert persisted.total_bytes == 8
    finally:
        release.set()
        scheduler.close()


def test_scheduler_throttles_progress_persistence(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    callbacks: list[DownloadCallbacks] = []
    task = _task(tmp_path, "throttle")

    def execute(
        current: DownloadTaskInput,
        _control: DownloadTaskControl,
        task_callbacks: DownloadCallbacks,
    ) -> DownloadResult:
        callbacks.append(task_callbacks)
        started.set()
        release.wait(1)
        return DownloadResult("已暂停", current.task_id, current.local_path)

    scheduler = DownloadScheduler(max_concurrent_downloads=1, executor=execute)
    try:
        scheduler.submit(task)
        assert started.wait(1)
        assert callbacks[0].progress is not None
        callbacks[0].progress(1, 8)
        persisted = task.store.load(task.task_id)
        assert persisted is not None and persisted.bytes_done == 0
        callbacks[0].progress(1024 * 1024, 8)
        persisted = task.store.load(task.task_id)
        assert persisted is not None and persisted.bytes_done == 1024 * 1024
    finally:
        release.set()
        scheduler.close()


def test_scheduler_progress_and_part_record_write_do_not_interleave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    loaded = threading.Event()
    proceed = threading.Event()
    task = _task(tmp_path, "race")
    callbacks: list[DownloadCallbacks] = []

    def execute(
        current: DownloadTaskInput, _control: DownloadTaskControl, cb: DownloadCallbacks
    ) -> DownloadResult:
        callbacks.append(cb)
        started.set()
        release.wait(2)
        return DownloadResult("已暂停", current.task_id, current.local_path)

    scheduler = DownloadScheduler(max_concurrent_downloads=1, executor=execute)
    original_load = task.store.load

    def gated_load(task_id: str) -> DownloadTaskState | None:
        state = original_load(task_id)
        if threading.current_thread().name == "progress":
            loaded.set()
            assert proceed.wait(2)
        return state

    record = DownloadPartRecord(0, 0, 3, 4, 2, "0" * 32)
    try:
        scheduler.submit(task)
        assert started.wait(1)
        monkeypatch.setattr(task.store, "load", gated_load)
        assert callbacks[0].progress is not None
        progress = threading.Thread(
            target=callbacks[0].progress, args=(1024 * 1024, 8), name="progress"
        )
        progress.start()
        assert loaded.wait(1)
        writer = threading.Thread(target=task.store.record_part, args=(task.task_id, record))
        writer.start()
        proceed.set()
        progress.join(2)
        writer.join(2)
        assert not progress.is_alive() and not writer.is_alive()
        persisted = original_load(task.task_id)
        assert persisted is not None and persisted.parts == [record]
    finally:
        proceed.set()
        release.set()
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


def test_close_preserves_active_and_queued_tasks_for_recovery(tmp_path: Path) -> None:
    started = threading.Event()

    def execute(
        task: DownloadTaskInput, control: DownloadTaskControl, _callbacks: DownloadCallbacks
    ) -> DownloadResult:
        started.set()
        while control.stop_result() != "paused":
            time.sleep(0.005)
        return DownloadResult("已暂停", task.task_id, task.local_path)

    active = _task(tmp_path, "active")
    queued = _task(tmp_path, "queued")
    scheduler = DownloadScheduler(max_concurrent_downloads=1, executor=execute)
    scheduler.submit(active)
    scheduler.submit(queued)
    assert started.wait(1)

    scheduler.close(wait=False)
    _wait_for(lambda: scheduler.state("active").status == "已暂停")
    assert scheduler.state("queued").status == "等待中"
    assert [state.task_id for state in active.store.load_all()] == ["active", "queued"]
    persisted_active = active.store.load("active")
    persisted_queued = queued.store.load("queued")
    assert persisted_active is not None and persisted_active.status == "已暂停"
    assert persisted_queued is not None and persisted_queued.status == "等待中"


def test_remove_paused_task_waits_for_worker_and_cleans_resume_data(tmp_path: Path) -> None:
    paused = threading.Event()
    release = threading.Event()
    events: list[str] = []

    def execute(
        task: DownloadTaskInput, control: DownloadTaskControl, callbacks: DownloadCallbacks
    ) -> DownloadResult:
        while control.stop_result() != "paused":
            time.sleep(0.005)
        if callbacks.status is not None:
            callbacks.status("已暂停")
        paused.set()
        release.wait(1)
        return DownloadResult("已暂停", task.task_id, task.local_path)

    task = _task(tmp_path, "paused")
    scheduler = DownloadScheduler(
        max_concurrent_downloads=1,
        executor=execute,
        on_event=lambda event: events.append(event.status),
    )
    try:
        scheduler.submit(task)
        assert scheduler.pause(task.task_id)
        assert paused.wait(1)
        temp = task.store.task_temp_dir(task.task_id)
        temp.mkdir(parents=True)
        (temp / "part0").write_bytes(b"partial")
        removed = threading.Event()
        outcome: list[bool] = []

        def remove() -> None:
            outcome.append(scheduler.remove_record(task.task_id))
            removed.set()

        remover = threading.Thread(target=remove)
        remover.start()
        _wait_for(lambda: task.task_id in scheduler._removing)
        assert not removed.wait(0.05)
        assert task.store.load(task.task_id) is not None
        assert scheduler.resume(task.task_id) is False
        release.set()
        assert removed.wait(1)
        remover.join()
        assert outcome == [True]
        assert task.store.load(task.task_id) is None
        assert not temp.exists()
        assert events[-1] == "已暂停"
        with pytest.raises(KeyError):
            scheduler.resume(task.task_id)
    finally:
        release.set()
        scheduler.close()


def test_remove_settled_paused_task_and_reject_active_task(tmp_path: Path) -> None:
    waiting = threading.Event()
    release = threading.Event()

    def execute(
        task: DownloadTaskInput, control: DownloadTaskControl, _callbacks: DownloadCallbacks
    ) -> DownloadResult:
        waiting.set()
        release.wait(1)
        return DownloadResult("已暂停", task.task_id, task.local_path)

    task = _task(tmp_path, "settled")
    scheduler = DownloadScheduler(max_concurrent_downloads=1, executor=execute)
    try:
        scheduler.submit(task)
        assert waiting.wait(1)
        with pytest.raises(ValueError):
            scheduler.remove_record(task.task_id)
        release.set()
        _wait_for(lambda: scheduler.state(task.task_id).status == "已暂停")
        assert scheduler.remove_record(task.task_id)
        assert task.store.load(task.task_id) is None
        assert not scheduler.remove_record(task.task_id)
    finally:
        release.set()
        scheduler.close()


def test_remove_paused_task_timeout_keeps_record_and_can_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()

    def execute(
        task: DownloadTaskInput, control: DownloadTaskControl, callbacks: DownloadCallbacks
    ) -> DownloadResult:
        started.set()
        while control.stop_result() != "paused":
            time.sleep(0.005)
        if callbacks.status is not None:
            callbacks.status("已暂停")
        release.wait(1)
        return DownloadResult("已暂停", task.task_id, task.local_path)

    task = _task(tmp_path, "timeout")
    scheduler = DownloadScheduler(max_concurrent_downloads=1, executor=execute)
    try:
        scheduler.submit(task)
        assert started.wait(1)
        assert scheduler.pause(task.task_id)
        _wait_for(lambda: scheduler.state(task.task_id).status == "已暂停")
        monkeypatch.setattr(scheduler_module, "REMOVE_WAIT_TIMEOUT_SECONDS", 0)
        with pytest.raises(TimeoutError, match="尚未退出"):
            scheduler.remove_record(task.task_id)
        assert task.store.load(task.task_id) is not None
        release.set()
        _wait_for(lambda: task.task_id not in scheduler.active_task_ids())
        assert task.store.load(task.task_id) is not None
        assert scheduler.state(task.task_id).status == "已暂停"
    finally:
        release.set()
        scheduler.close()


def test_remove_paused_task_reports_storage_error_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()

    def execute(
        task: DownloadTaskInput, control: DownloadTaskControl, callbacks: DownloadCallbacks
    ) -> DownloadResult:
        started.set()
        while control.stop_result() != "paused":
            time.sleep(0.005)
        if callbacks.status is not None:
            callbacks.status("已暂停")
        release.wait(1)
        return DownloadResult("已暂停", task.task_id, task.local_path)

    task = _task(tmp_path, "delete-error")
    scheduler = DownloadScheduler(max_concurrent_downloads=1, executor=execute)
    original_delete = task.store.delete
    try:
        scheduler.submit(task)
        assert started.wait(1)
        assert scheduler.pause(task.task_id)
        _wait_for(lambda: scheduler.state(task.task_id).status == "已暂停")

        def delete_error(_task_id: str) -> None:
            raise OSError("cannot delete")

        monkeypatch.setattr(task.store, "delete", delete_error)
        errors: list[Exception] = []
        finished = threading.Event()

        def remove() -> None:
            try:
                scheduler.remove_record(task.task_id)
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()

        remover = threading.Thread(target=remove)
        remover.start()
        _wait_for(lambda: task.task_id in scheduler._removing)
        release.set()
        assert finished.wait(1)
        remover.join()
        assert len(errors) == 1 and isinstance(errors[0], OSError)
        assert str(errors[0]) == "cannot delete"
        assert task.store.load(task.task_id) is not None
        monkeypatch.setattr(task.store, "delete", original_delete)
        assert scheduler.remove_record(task.task_id)
        assert task.store.load(task.task_id) is None
    finally:
        release.set()
        scheduler.close()


def test_store_load_all_uses_creation_order(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path / "store")
    later = DownloadTaskState("a-task", "later.bin", tmp_path / "later.bin", created_at=20.0)
    earlier = DownloadTaskState("z-task", "earlier.bin", tmp_path / "earlier.bin", created_at=10.0)
    store.save(later)
    store.save(earlier)

    assert [state.task_id for state in store.load_all()] == ["z-task", "a-task"]
