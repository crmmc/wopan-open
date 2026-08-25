from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from openwopan.storage.settings import AppSettings
from openwopan.tasks import download
from openwopan.tasks.download import (
    DownloadCallbacks,
    DownloadError,
    DownloadTaskControl,
    DownloadTaskStore,
    download_url,
    make_download_task_id,
)


def test_download_url_reuses_valid_completed_parts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(download, "BYTES_PER_MB", 4)
    content = b"abcdefghijklmnopq"
    local_path = tmp_path / "report.bin"
    store = DownloadTaskStore(tmp_path / "store")
    task_id = make_download_task_id("fid-1", local_path)
    requested_ranges: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(len(content))})
        range_header = request.headers.get("Range")
        requested_ranges.append(range_header or "")
        start, end = _parse_range(range_header)
        return httpx.Response(206, content=content[start : end + 1])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = AppSettings(
        max_download_threads=2,
        download_part_mode="fixed",
        download_part_size_mb=4,
    )
    control = _PauseAfterFirstPart()

    paused = download_url(
        client,
        "https://download.example.test/file",
        local_path,
        settings=settings,
        store=store,
        task_id=task_id,
        file_name="report.bin",
        download_id="fid-1",
        callbacks=DownloadCallbacks(),
        control=control,
    )

    assert paused.status == "已暂停"
    assert store.load(task_id) is not None
    assert requested_ranges == ["bytes=0-15", "bytes=16-16"]

    resumed = download_url(
        client,
        "https://download.example.test/file",
        local_path,
        settings=settings,
        store=store,
        task_id=task_id,
        file_name="report.bin",
        download_id="fid-1",
        callbacks=DownloadCallbacks(),
    )

    assert resumed.status == "已完成"
    assert local_path.read_bytes() == content
    assert requested_ranges == ["bytes=0-15", "bytes=16-16", "bytes=16-16"]


def test_download_url_refreshes_expired_range_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(download, "BYTES_PER_MB", 4)
    content = b"abcdefghijklmnopq"
    local_path = tmp_path / "refresh.bin"
    store = DownloadTaskStore(tmp_path / "store")
    urls: list[str] = []
    refresh_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_calls
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(len(content))})
        urls.append(str(request.url))
        if str(request.url) == "https://download.example.test/expired":
            return httpx.Response(403)
        start, end = _parse_range(request.headers.get("Range"))
        return httpx.Response(206, content=content[start : end + 1])

    def refresh_url() -> str:
        nonlocal refresh_calls
        refresh_calls += 1
        return "https://download.example.test/fresh"

    result = download_url(
        httpx.Client(transport=httpx.MockTransport(handler)),
        "https://download.example.test/expired",
        local_path,
        settings=AppSettings(
            max_download_threads=2,
            download_part_mode="fixed",
            download_part_size_mb=4,
        ),
        store=store,
        task_id=make_download_task_id("fid-1", local_path),
        file_name="refresh.bin",
        download_id="fid-1",
        refresh_url=refresh_url,
    )

    assert result.status == "已完成"
    assert refresh_calls == 1
    assert "https://download.example.test/fresh" in urls
    assert local_path.read_bytes() == content


def test_download_url_retries_rate_limited_part(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(download, "BYTES_PER_MB", 4)
    monkeypatch.setattr(download.time, "sleep", lambda _seconds: None)
    content = b"abcdefghijklmnopq"
    local_path = tmp_path / "rate-limit.bin"
    first_part_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal first_part_attempts
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(len(content))})
        range_header = request.headers.get("Range")
        if range_header == "bytes=0-15" and first_part_attempts == 0:
            first_part_attempts += 1
            return httpx.Response(429)
        start, end = _parse_range(range_header)
        return httpx.Response(206, content=content[start : end + 1])

    result = download_url(
        httpx.Client(transport=httpx.MockTransport(handler)),
        "https://download.example.test/file",
        local_path,
        settings=AppSettings(
            max_download_threads=2,
            download_part_mode="fixed",
            download_part_size_mb=4,
        ),
        store=DownloadTaskStore(tmp_path / "store"),
        task_id=make_download_task_id("fid-1", local_path),
        file_name="rate-limit.bin",
        download_id="fid-1",
    )

    assert result.status == "已完成"
    assert first_part_attempts == 1
    assert local_path.read_bytes() == content


class _PauseAfterFirstPart(DownloadTaskControl):
    def __init__(self) -> None:
        super().__init__()
        self._range_count = 0

    def stop_result(self) -> download.PartResult | None:
        if self._range_count >= 2:
            return "paused"
        return super().stop_result()

    def set_active_response(self, response: httpx.Response | None) -> None:
        if response is not None:
            self._range_count += 1
        super().set_active_response(response)


def _parse_range(value: str | None) -> tuple[int, int]:
    assert value is not None
    range_value = value.split("=", 1)[1]
    start_text, end_text = range_value.split("-", 1)
    return int(start_text), int(end_text)


# ---------------------------------------------------------------------------
# Coverage additions: store, control, single-stream, range and helper paths
# ---------------------------------------------------------------------------

import errno
import json

from openwopan.tasks.download import (
    DownloadPart,
    DownloadPartRecord,
    DownloadTaskState,
    _build_parts,
    _clear_parts_if_plan_changed,
    _compute_md5,
    _download_part_size,
    _merge_parts,
    _read_content_length,
    _read_non_negative_int,
    _read_optional_positive_int,
    _read_status,
    _read_task_state,
    _read_text,
    _replace_output_file,
    _validate_existing_parts,
)


def _state(
    task_id: str = "task-1",
    save_path: Path | None = None,
    parts: list[DownloadPartRecord] | None = None,
) -> DownloadTaskState:
    return DownloadTaskState(
        task_id=task_id,
        file_name="file.bin",
        save_path=save_path or Path("/tmp/file.bin"),
        parts=parts or [],
    )


def _part_record(index: int, start: int, end: int) -> DownloadPartRecord:
    return DownloadPartRecord(
        index=index,
        start=start,
        end=end,
        expected_size=end - start + 1,
        actual_size=end - start + 1,
        md5="0" * 32,
    )


# -- DownloadTaskControl -----------------------------------------------------


class _CloseErrorResponse:
    def close(self) -> None:
        raise RuntimeError("response already closed")


def test_control_stop_result_reports_pause_and_cancel() -> None:
    control = DownloadTaskControl()

    assert control.stop_result() is None
    control.request_pause()
    assert control.stop_result() == "paused"
    control.request_cancel(cleanup=True)
    assert control.stop_result() == "cancelled"
    assert control.cleanup_on_cancel is True


def test_control_pause_and_cancel_close_active_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = DownloadTaskControl()
    closed: list[bool] = []

    class _Response:
        def close(self) -> None:
            closed.append(True)

    control.set_active_response(_Response())  # type: ignore[arg-type]
    control.request_pause()
    assert closed == [True]

    control.set_active_response(None)
    control.request_cancel()  # no active response: returns early
    assert closed == [True]


def test_control_close_active_response_swallows_runtime_error() -> None:
    control = DownloadTaskControl()
    control.set_active_response(_CloseErrorResponse())  # type: ignore[arg-type]

    control.request_pause()  # must not raise

    assert control.stop_result() == "paused"


# -- DownloadTaskStore -------------------------------------------------------


def test_store_paths_and_load_missing_task(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path)

    assert store.root_path == tmp_path
    assert store.task_path("t") == tmp_path / "tasks" / "t.json"
    assert store.task_temp_dir("t") == tmp_path / "parts" / "t"
    assert store.part_path("t", 2) == tmp_path / "parts" / "t" / "part2"
    assert store.merged_path("t") == tmp_path / "parts" / "t" / "merged"
    assert store.load("missing") is None


@pytest.mark.parametrize(
    "content",
    [b"not json", b"[1, 2]", b'"text"'],
)
def test_store_load_rejects_invalid_metadata(tmp_path: Path, content: bytes) -> None:
    store = DownloadTaskStore(tmp_path)
    store.task_path("task-1").parent.mkdir(parents=True)
    store.task_path("task-1").write_bytes(content)

    assert store.load("task-1") is None


def test_store_save_and_delete_roundtrip(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path)
    state = _state(save_path=tmp_path / "file.bin")
    store.save(state)
    part_file = store.part_path("task-1", 0)
    part_file.parent.mkdir(parents=True, exist_ok=True)
    part_file.write_bytes(b"part")

    assert store.load("task-1") is not None
    assert store.load("task-1").file_name == "file.bin"  # type: ignore[union-attr]

    store.cleanup_temp("task-1")
    assert store.load("task-1") is not None
    assert not part_file.exists()

    store.delete("task-1")
    assert store.load("task-1") is None
    assert not store.task_temp_dir("task-1").exists()


def test_store_list_records_returns_persisted_records(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path)
    assert store.list_records() == ()

    store.save(_state(task_id="task-1", save_path=tmp_path / "one.bin"))
    store.save(_state(task_id="task-2", save_path=tmp_path / "two.bin"))
    store.task_path("task-3").parent.mkdir(parents=True, exist_ok=True)
    store.task_path("task-3").write_text("broken")

    records = store.list_records()
    assert [record.task_id for record in records] == ["task-1", "task-2"]
    assert records[0].name == "file.bin"


def test_store_record_part_without_state_is_noop(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path)

    store.record_part("missing", _part_record(0, 0, 3))

    assert store.load("missing") is None


def test_store_remove_part_record_updates_state_and_file(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path)
    state = _state(
        task_id="task-1",
        save_path=tmp_path / "file.bin",
        parts=[_part_record(0, 0, 3), _part_record(1, 4, 7)],
    )
    store.save(state)
    part_file = store.part_path("task-1", 0)
    part_file.parent.mkdir(parents=True, exist_ok=True)
    part_file.write_bytes(b"part")

    store.remove_part_record("task-1", 0)

    updated = store.load("task-1")
    assert updated is not None
    assert [part.index for part in updated.parts] == [1]
    assert updated.bytes_done == 4
    assert not part_file.exists()

    store.remove_part_record("missing", 0)  # no state: only unlink runs


# -- download_url argument validation ----------------------------------------


def test_download_url_rejects_empty_url(tmp_path: Path) -> None:
    with pytest.raises(DownloadError, match="下载地址为空"):
        download_url(
            httpx.Client(),
            "",
            tmp_path / "file.bin",
            settings=AppSettings(),
            store=DownloadTaskStore(tmp_path),
            task_id="t",
            file_name="file.bin",
        )


# -- single-stream paths -----------------------------------------------------


def test_single_stream_refreshes_expired_url_until_success(tmp_path: Path) -> None:
    content = b"single-stream-content"
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url).endswith("/expired"):
            return httpx.Response(403)
        return httpx.Response(200, content=content, headers={"Content-Length": str(len(content))})

    refresh_count = 0

    def refresh_url() -> str:
        nonlocal refresh_count
        refresh_count += 1
        return "https://download.example.test/fresh"

    result = download_url(
        httpx.Client(transport=httpx.MockTransport(handler)),
        "https://download.example.test/expired",
        tmp_path / "out.bin",
        settings=AppSettings(),
        store=DownloadTaskStore(tmp_path / "store"),
        task_id="t1",
        file_name="out.bin",
        refresh_url=refresh_url,
        callbacks=DownloadCallbacks(
            progress=lambda _b, _t: None,
            status=lambda _s: None,
            connections=lambda _a, _m: None,
        ),
    )

    assert result.status == "已完成"
    assert refresh_count == 1
    assert (tmp_path / "out.bin").read_bytes() == content


@pytest.mark.parametrize(
    ("refresh_url", "match"),
    [
        (None, "下载链接已过期或刷新失败"),
        (lambda: "https://download.example.test/expired", "下载链接已过期或刷新失败"),
    ],
    ids=["no-refresh-callback", "refresh-exhausted"],
)
def test_single_stream_fails_when_url_refresh_exhausted(
    tmp_path: Path,
    refresh_url: object,
    match: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    store = DownloadTaskStore(tmp_path / "store")

    with pytest.raises(DownloadError, match=match):
        download_url(
            httpx.Client(transport=httpx.MockTransport(handler)),
            "https://download.example.test/expired",
            tmp_path / "out.bin",
            settings=AppSettings(),
            store=store,
            task_id="t1",
            file_name="out.bin",
            refresh_url=refresh_url,  # type: ignore[arg-type]
        )

    state = store.load("t1")
    assert state is not None and state.status == "失败"


def test_single_stream_fails_when_rate_limited(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    with pytest.raises(DownloadError, match="下载被限流"):
        download_url(
            httpx.Client(transport=httpx.MockTransport(handler)),
            "https://download.example.test/file",
            tmp_path / "out.bin",
            settings=AppSettings(),
            store=DownloadTaskStore(tmp_path / "store"),
            task_id="t1",
            file_name="out.bin",
        )


def test_single_stream_fails_on_network_error(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(DownloadError, match="网络错误"):
        download_url(
            httpx.Client(transport=httpx.MockTransport(handler)),
            "https://download.example.test/file",
            tmp_path / "out.bin",
            settings=AppSettings(),
            store=DownloadTaskStore(tmp_path / "store"),
            task_id="t1",
            file_name="out.bin",
        )


def test_single_stream_fails_on_size_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(download, "BYTES_PER_MB", 4)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": "10"})
        if request.headers.get("Range") is not None:
            # range unsupported: force fallback to single stream with total_size=10
            return httpx.Response(200, content=b"short")
        response = httpx.Response(200, content=b"short")
        response.headers["Content-Length"] = "10"
        return response

    with pytest.raises(DownloadError, match="下载大小不一致"):
        download_url(
            httpx.Client(transport=httpx.MockTransport(handler)),
            "https://download.example.test/file",
            tmp_path / "out.bin",
            settings=_range_settings(),
            store=DownloadTaskStore(tmp_path / "store"),
            task_id="t1",
            file_name="out.bin",
        )

    assert not (tmp_path / "out.bin").exists()
    assert not (tmp_path / "out.bin.part").exists()


def test_single_stream_fails_when_local_path_is_not_writable(tmp_path: Path) -> None:
    read_only_dir = tmp_path / "out-dir"
    read_only_dir.mkdir()
    read_only_dir.chmod(0o500)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"data")

    try:
        with pytest.raises(DownloadError, match="无法写入本地文件"):
            download_url(
                httpx.Client(transport=httpx.MockTransport(handler)),
                "https://download.example.test/file",
                read_only_dir / "out.bin",
                settings=AppSettings(),
                store=DownloadTaskStore(tmp_path / "store"),
                task_id="t1",
                file_name="out.bin",
            )
    finally:
        read_only_dir.chmod(0o700)


def test_single_stream_maps_http_status_error(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")

    store = DownloadTaskStore(tmp_path / "store")

    with pytest.raises(DownloadError, match="HTTP 404"):
        download_url(
            httpx.Client(transport=httpx.MockTransport(handler)),
            "https://download.example.test/file",
            tmp_path / "out.bin",
            settings=AppSettings(),
            store=store,
            task_id="t1",
            file_name="out.bin",
        )

    state = store.load("t1")
    assert state is not None and state.error == "HTTP 404"


class _StopAfterChunks(DownloadTaskControl):
    """Control that requests a stop once stop_result has been polled N times."""

    def __init__(self, result: str, after: int, cleanup: bool = False) -> None:
        super().__init__()
        self._wanted = result
        self._after = after
        self._cleanup = cleanup
        self._polls = 0

    def stop_result(self) -> download.PartResult | None:
        self._polls += 1
        if self._polls >= self._after:
            if self._wanted == "cancelled":
                self.request_cancel(cleanup=self._cleanup)
            else:
                self.request_pause()
            return super().stop_result()  # type: ignore[return-value]
        return super().stop_result()


@pytest.mark.parametrize(
    ("result", "cleanup", "expected_status"),
    [
        ("paused", False, "已暂停"),
        ("cancelled", False, "已取消"),
        ("cancelled", True, "已取消"),
    ],
)
def test_single_stream_stop_during_transfer(
    tmp_path: Path,
    result: str,
    cleanup: bool,
    expected_status: str,
) -> None:
    content = b"x" * (download.DOWNLOAD_CHUNK_SIZE + 16)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    store = DownloadTaskStore(tmp_path / "store")
    statuses: list[str] = []
    control = _StopAfterChunks(result, 2, cleanup=cleanup)

    download_result = download_url(
        httpx.Client(transport=httpx.MockTransport(handler)),
        "https://download.example.test/file",
        tmp_path / "out.bin",
        settings=AppSettings(),
        store=store,
        task_id="t1",
        file_name="out.bin",
        callbacks=DownloadCallbacks(status=statuses.append),
        control=control,
    )

    assert download_result.status == expected_status
    assert statuses[-1] == expected_status
    assert not (tmp_path / "out.bin").exists()
    assert not (tmp_path / "out.bin.part").exists()
    state = store.load("t1")
    if cleanup:
        assert state is None
    else:
        assert state is not None and state.status == expected_status


def test_single_stream_handles_empty_chunks(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=iter([b"", b"payload"]))

    result = download_url(
        httpx.Client(transport=httpx.MockTransport(handler)),
        "https://download.example.test/file",
        tmp_path / "out.bin",
        settings=AppSettings(),
        store=DownloadTaskStore(tmp_path / "store"),
        task_id="t1",
        file_name="out.bin",
    )

    assert result.status == "已完成"
    assert (tmp_path / "out.bin").read_bytes() == b"payload"


# -- range download failure semantics ----------------------------------------


def _range_settings(retries: int = 0) -> AppSettings:
    return AppSettings(
        max_download_threads=2,
        download_part_mode="fixed",
        download_part_size_mb=4,
        retry_max_attempts=retries,
    )


def _run_ranges(
    tmp_path: Path,
    handler,
    *,
    settings: AppSettings | None = None,
    store: DownloadTaskStore | None = None,
    refresh_url: object = None,
    control: DownloadTaskControl | None = None,
):
    return download_url(
        httpx.Client(transport=httpx.MockTransport(handler)),
        "https://download.example.test/file",
        tmp_path / "out.bin",
        settings=settings or _range_settings(),
        store=store or DownloadTaskStore(tmp_path / "store"),
        task_id="t1",
        file_name="out.bin",
        download_id="fid-1",
        refresh_url=refresh_url,  # type: ignore[arg-type]
        control=control,
    )


CONTENT = b"abcdefghijklmnopq"  # 17 bytes: parts 0-15 and 16-16


def _range_handler(
    status_for_range: dict[str, int] | None = None,
    head_status: int = 200,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            if head_status != 200:
                return httpx.Response(head_status)
            return httpx.Response(200, headers={"Content-Length": str(len(CONTENT))})
        range_header = request.headers.get("Range", "")
        status = (status_for_range or {}).get(range_header, 206)
        if status == 206:
            start, end = _parse_range(range_header)
            return httpx.Response(206, content=CONTENT[start : end + 1])
        return httpx.Response(status)

    return handler


def test_range_download_falls_back_to_single_stream_when_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(download, "BYTES_PER_MB", 4)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(len(CONTENT))})
        if request.headers.get("Range") is not None:
            return httpx.Response(200, content=CONTENT)
        return httpx.Response(200, content=CONTENT, headers={"Content-Length": str(len(CONTENT))})

    store = DownloadTaskStore(tmp_path / "store")
    seeded = _state(task_id="t1", save_path=tmp_path / "out.bin")
    seeded.parts = [_part_record(0, 0, 15)]
    seeded.part_size = 16
    seeded.total_bytes = 99  # remote size changed since last run
    seeded.supports_resume = True
    store.save(seeded)
    stale_part = store.part_path("t1", 0)
    stale_part.parent.mkdir(parents=True, exist_ok=True)
    stale_part.write_bytes(b"x" * 16)

    result = _run_ranges(tmp_path, handler, store=store)

    assert result.status == "已完成"
    assert (tmp_path / "out.bin").read_bytes() == CONTENT
    assert store.load("t1") is None  # deleted after successful completion


@pytest.mark.parametrize("cleanup", [False, True])
def test_range_download_cancelled_before_first_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup: bool,
) -> None:
    monkeypatch.setattr(download, "BYTES_PER_MB", 4)
    store = DownloadTaskStore(tmp_path / "store")
    store.save(_state(task_id="t1", save_path=tmp_path / "out.bin"))
    control = DownloadTaskControl()
    control.request_cancel(cleanup=cleanup)

    result = _run_ranges(
        tmp_path,
        _range_handler(),
        store=store,
        control=control,
    )

    assert result.status == "已取消"
    state = store.load("t1")
    if cleanup:
        assert state is None
    else:
        assert state is not None and state.status == "已取消"


@pytest.mark.parametrize("polls_before_stop", [2, 3])
def test_range_download_paused_during_part_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    polls_before_stop: int,
) -> None:
    monkeypatch.setattr(download, "BYTES_PER_MB", 4)
    control = _StopAfterChunks("paused", polls_before_stop)

    result = _run_ranges(tmp_path, _range_handler(), control=control)

    assert result.status == "已暂停"


def test_range_download_fails_when_rate_limits_exceed_maximum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(download, "BYTES_PER_MB", 4)
    monkeypatch.setattr(download, "MAX_RATE_LIMITS", 0)
    monkeypatch.setattr(download.time, "sleep", lambda _s: None)
    store = DownloadTaskStore(tmp_path / "store")

    with pytest.raises(DownloadError, match="分片下载被限流"):
        _run_ranges(
            tmp_path,
            _range_handler({"bytes=0-15": 429}),
            store=store,
        )

    state = store.load("t1")
    assert state is not None and state.status == "失败"


@pytest.mark.parametrize(
    ("refresh_url", "match"),
    [
        (None, "下载链接已过期或刷新失败"),
        (lambda: "https://download.example.test/other", "下载链接已过期或刷新失败"),
    ],
    ids=["no-refresh-callback", "refresh-exhausted"],
)
def test_range_download_url_expired_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refresh_url: object,
    match: str,
) -> None:
    monkeypatch.setattr(download, "BYTES_PER_MB", 4)
    monkeypatch.setattr(download, "MAX_URL_REFRESHES", 1)
    store = DownloadTaskStore(tmp_path / "store")

    with pytest.raises(DownloadError, match=match):
        _run_ranges(
            tmp_path,
            _range_handler({"bytes=0-15": 403, "bytes=16-16": 403}),
            store=store,
            refresh_url=refresh_url,
        )

    state = store.load("t1")
    assert state is not None and state.status == "失败"


@pytest.mark.parametrize("status", [204, 500])
def test_range_download_part_fatal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    monkeypatch.setattr(download, "BYTES_PER_MB", 4)
    monkeypatch.setattr(download.time, "sleep", lambda _s: None)
    store = DownloadTaskStore(tmp_path / "store")

    with pytest.raises(DownloadError, match="分片下载失败"):
        _run_ranges(
            tmp_path,
            _range_handler({"bytes=0-15": status}),
            store=store,
            settings=_range_settings(retries=1),
        )

    state = store.load("t1")
    assert state is not None and state.status == "失败"
    assert not store.part_path("t1", 0).exists()


def test_range_download_part_network_error_is_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(download, "BYTES_PER_MB", 4)
    monkeypatch.setattr(download.time, "sleep", lambda _s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(len(CONTENT))})
        raise httpx.ConnectError("boom")

    with pytest.raises(DownloadError, match="分片下载失败"):
        _run_ranges(tmp_path, handler)


def test_range_download_part_write_failure_is_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(download, "BYTES_PER_MB", 4)
    store = DownloadTaskStore(tmp_path / "store")
    temp_dir = store.task_temp_dir("t1")
    temp_dir.mkdir(parents=True)
    temp_dir.chmod(0o500)

    try:
        with pytest.raises(DownloadError, match="分片下载失败"):
            _run_ranges(tmp_path, _range_handler(), store=store)
    finally:
        temp_dir.chmod(0o700)


class _CancelAfterPartsStore(DownloadTaskStore):
    """Simulates a user cancelling after the last part completes."""

    def __init__(self, root: Path, cancel_after: int) -> None:
        super().__init__(root)
        self._cancel_after = cancel_after
        self._control: DownloadTaskControl | None = None
        self._recorded = 0

    def attach(self, control: DownloadTaskControl) -> None:
        self._control = control

    def record_part(self, task_id: str, record: DownloadPartRecord) -> None:
        super().record_part(task_id, record)
        self._recorded += 1
        if self._recorded >= self._cancel_after and self._control is not None:
            self._control.request_cancel()


def test_range_download_cancelled_after_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(download, "BYTES_PER_MB", 4)
    store = _CancelAfterPartsStore(tmp_path / "store", cancel_after=2)
    control = DownloadTaskControl()
    store.attach(control)

    result = _run_ranges(tmp_path, _range_handler(), store=store, control=control)

    assert result.status == "已取消"
    assert not (tmp_path / "out.bin").exists()


class _CorruptingStore(DownloadTaskStore):
    """Simulates external corruption of a part file after it is recorded."""

    def record_part(self, task_id: str, record: DownloadPartRecord) -> None:
        super().record_part(task_id, record)
        store_part = self.part_path(task_id, record.index)
        store_part.write_bytes(b"")  # truncated on disk after validation


def test_range_download_fails_when_merged_size_mismatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(download, "BYTES_PER_MB", 4)
    store = _CorruptingStore(tmp_path / "store")

    with pytest.raises(DownloadError, match="合并后大小不一致"):
        _run_ranges(tmp_path, _range_handler(), store=store)

    state = store.load("t1")
    assert state is not None and state.status == "失败"
    assert not (tmp_path / "out.bin").exists()


# -- part/state helpers -------------------------------------------------------


def test_merge_parts_returns_early_when_stopped(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path)
    parts = _build_parts(8, 4)
    for part in parts:
        path = store.part_path("t1", part.index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * part.expected_size)
    state = _state(task_id="t1", save_path=tmp_path / "out.bin")
    control = DownloadTaskControl()
    control.request_cancel()

    _merge_parts(store, state, parts, control)

    assert not store.merged_path("t1").exists() or store.merged_path("t1").stat().st_size == 0


def test_merge_parts_wraps_os_errors(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path)
    state = _state(task_id="t1", save_path=tmp_path / "out.bin")
    parts = _build_parts(8, 4)

    with pytest.raises(DownloadError, match="合并分片文件失败"):
        _merge_parts(store, state, parts, DownloadTaskControl())


def test_validate_existing_parts_rejects_and_cleans_stale_entries(
    tmp_path: Path,
) -> None:
    store = DownloadTaskStore(tmp_path)
    parts = _build_parts(12, 4)  # part0: 0-3, part1: 4-7, part2: 8-11

    good = store.part_path("t1", 1)
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_bytes(b"abcd")
    mismatched = store.part_path("t1", 0)
    mismatched.write_bytes(b"abcd")
    wrong_size = store.part_path("t1", 2)
    wrong_size.write_bytes(b"ab")

    state = _state(task_id="t1", save_path=tmp_path / "out.bin")
    state.parts = [
        # index 1 is valid and reusable
        DownloadPartRecord(
            index=1, start=4, end=7, expected_size=4, actual_size=4, md5=_compute_md5(good)
        ),
        # index 0: file exists but recorded start/end mismatch versus plan
        DownloadPartRecord(index=0, start=8, end=11, expected_size=4, actual_size=4, md5="0" * 32),
        # index 2: matching plan but wrong size on disk
        DownloadPartRecord(index=2, start=8, end=11, expected_size=4, actual_size=4, md5="0" * 32),
        # index 9: not planned at all
        DownloadPartRecord(index=9, start=0, end=3, expected_size=4, actual_size=4, md5="0" * 32),
    ]
    store.save(state)

    # stray temp entries: merged leftover, non-part file, bad index, stale part
    temp_dir = store.task_temp_dir("t1")
    (temp_dir / "merged").write_bytes(b"leftover")
    (temp_dir / "readme.txt").write_text("ignored")
    (temp_dir / "partX.downloading").write_bytes(b"junk")
    (temp_dir / "part0.downloading").write_bytes(b"partial")

    downloaded, reusable = _validate_existing_parts(store, state, parts)

    assert downloaded == 4
    assert reusable == {1}
    assert not (temp_dir / "merged").exists()
    assert (temp_dir / "readme.txt").exists()
    assert (temp_dir / "partX.downloading").exists()
    assert not (temp_dir / "part0.downloading").exists()
    updated = store.load("t1")
    assert updated is not None
    assert [part.index for part in updated.parts] == [1]


def test_validate_existing_parts_rejects_md5_mismatch(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path)
    parts = _build_parts(4, 4)
    part_file = store.part_path("t1", 0)
    part_file.parent.mkdir(parents=True, exist_ok=True)
    part_file.write_bytes(b"abcd")
    state = _state(
        task_id="t1",
        save_path=tmp_path / "out.bin",
        parts=[
            DownloadPartRecord(
                index=0, start=0, end=3, expected_size=4, actual_size=4, md5="f" * 32
            )
        ],
    )

    downloaded, reusable = _validate_existing_parts(store, state, parts)

    assert downloaded == 0
    assert reusable == set()


def test_clear_parts_if_plan_changed_resets_state(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path)
    state = _state(
        task_id="t1",
        save_path=tmp_path / "out.bin",
        parts=[_part_record(0, 0, 3)],
    )
    state.total_bytes = 9
    store.save(state)
    parts = _build_parts(8, 4)

    _clear_parts_if_plan_changed(store, state, parts)

    assert state.parts == []
    assert state.bytes_done == 0


def test_clear_parts_if_plan_changed_keeps_matching_plan(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path)
    state = _state(
        task_id="t1",
        save_path=tmp_path / "out.bin",
        parts=[_part_record(0, 0, 3)],
    )
    state.total_bytes = 4
    parts = _build_parts(4, 4)

    _clear_parts_if_plan_changed(store, state, parts)

    assert state.parts == [_part_record(0, 0, 3)]


def test_download_part_size_modes() -> None:
    fixed = AppSettings(download_part_mode="fixed", download_part_size_mb=4)
    assert _download_part_size(1024, fixed) == 4 * download.BYTES_PER_MB
    assert _download_part_size(None, AppSettings()) == 5 * download.BYTES_PER_MB

    auto = AppSettings(
        download_part_mode="auto",
        download_part_size_mb=4,
        max_download_threads=4,
    )
    assert _download_part_size(100 * download.BYTES_PER_MB, auto) == 25 * download.BYTES_PER_MB


def test_probe_download_size_handles_head_failures(tmp_path: Path) -> None:
    from openwopan.tasks.download import _probe_download_size

    def make(handler) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    assert _probe_download_size(make(lambda _r: httpx.Response(500)), "https://x.test/f") is None
    assert _probe_download_size(
        make(lambda _r: httpx.Response(200, headers={})), "https://x.test/f"
    ) is None
    assert _probe_download_size(
        make(lambda _r: httpx.Response(200, headers={"Content-Length": "abc"})), "https://x.test/f"
    ) is None
    assert _probe_download_size(
        make(lambda _r: httpx.Response(200, headers={"Content-Length": "-3"})), "https://x.test/f"
    ) is None
    assert (
        _probe_download_size(
            make(lambda _r: httpx.Response(200, headers={"Content-Length": "7"})), "https://x.test/f"
        )
        == 7
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), ("abc", None), ("-1", None), ("0", 0), ("42", 42)],
)
def test_read_content_length(value: str | None, expected: int | None) -> None:
    assert _read_content_length(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("已暂停", "已暂停"),
        ("unknown", "等待中"),
        (None, "等待中"),
    ],
)
def test_read_status_normalizes_unknown_values(value: object, expected: str) -> None:
    assert _read_status(value) == expected


def test_read_text_normalizes_non_strings() -> None:
    assert _read_text("x") == "x"
    assert _read_text(7) == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 0), (1.5, 0), (3, 3), ("5", 5), ("-2", 0), ("bad", 0)],
)
def test_read_non_negative_int(value: object, expected: int) -> None:
    assert _read_non_negative_int(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), (0, None), ("3", 3)],
)
def test_read_optional_positive_int(value: object, expected: int | None) -> None:
    assert _read_optional_positive_int(value) == expected


def test_read_task_state_rejects_incomplete_payload() -> None:
    assert _read_task_state({}) is None
    assert _read_task_state({"task_id": "t", "file_name": "f"}) is None

    state = _read_task_state(
        {"task_id": "t", "file_name": "f", "save_path": "/tmp/f", "parts": "garbage"}
    )
    assert state is not None
    assert state.parts == []


def test_read_task_state_parses_part_records() -> None:
    raw = {
        "task_id": "t",
        "file_name": "f",
        "save_path": "/tmp/f",
        "status": "已暂停",
        "bytes_done": "4",
        "parts": [
            {"index": 0, "start": 0, "end": 3, "expected_size": 4, "actual_size": 4, "md5": "m"},
            "not-a-dict",
            {"index": 1, "start": 4, "end": 2, "expected_size": 4, "actual_size": 4, "md5": "m"},
        ],
    }

    state = _read_task_state(raw)

    assert state is not None
    assert state.status == "已暂停"
    assert state.bytes_done == 4
    assert len(state.parts) == 1


# -- _replace_output_file -----------------------------------------------------


class _FakeEXDEV:
    """Path.replace stand-in raising EXDEV on the first call only."""


def test_replace_output_file_reraises_non_exdev_oserror(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.write_bytes(b"data")
    target = tmp_path / "dst"
    real_replace = Path.replace

    def replace(self: Path, other: str | Path) -> Path:
        raise OSError(errno.EPERM, "permission denied")

    original = Path.replace
    Path.replace = replace  # type: ignore[method-assign]
    try:
        with pytest.raises(OSError):
            _replace_output_file(source, target)
    finally:
        Path.replace = original  # type: ignore[method-assign]
        assert real_replace


def _patch_replace_exdev(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def replace(self: Path, other: str | Path) -> Path:
        calls.append(1)
        if len(calls) == 1:
            raise OSError(errno.EXDEV, "cross-device link")
        return Path(str(self)).rename(other)

    monkeypatch.setattr(Path, "replace", replace)


def test_replace_output_file_copies_across_devices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "src"
    source.write_bytes(b"cross-device")
    target = tmp_path / "dst"
    _patch_replace_exdev(monkeypatch)

    _replace_output_file(source, target)

    assert target.read_bytes() == b"cross-device"
    assert not source.exists()
    assert not (tmp_path / "dst.tmp").exists()


def test_replace_output_file_fails_on_copy_size_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil as shutil_module

    source = tmp_path / "src"
    source.write_bytes(b"12345678")
    target = tmp_path / "dst"
    _patch_replace_exdev(monkeypatch)

    def short_copy(_src, dst, **_kwargs):
        Path(dst).write_bytes(b"short")
        return str(dst)

    monkeypatch.setattr(shutil_module, "copy2", short_copy)

    with pytest.raises(OSError, match="跨盘拷贝大小不匹配"):
        _replace_output_file(source, target)

    assert not (tmp_path / "dst.tmp").exists()


def test_replace_output_file_cleans_up_when_copy_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil as shutil_module

    source = tmp_path / "src"
    source.write_bytes(b"data")
    target = tmp_path / "dst"
    _patch_replace_exdev(monkeypatch)
    tmp_file = target.with_name("dst.tmp")
    tmp_file.write_bytes(b"partial")

    def failing_copy(_src, _dst, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(shutil_module, "copy2", failing_copy)

    with pytest.raises(OSError, match="disk full"):
        _replace_output_file(source, target)

    assert not tmp_file.exists()


def test_remove_partial_file_swallows_os_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openwopan.tasks.download import _remove_partial_file

    def unlink(_self, missing_ok: bool = False) -> None:
        raise OSError("locked")

    monkeypatch.setattr(Path, "unlink", unlink)
    _remove_partial_file(tmp_path / "whatever")  # must not raise


def test_state_to_record_supports_resume_only_when_stopped(tmp_path: Path) -> None:
    from openwopan.tasks.download import _state_to_record

    paused = _state(save_path=tmp_path / "f")
    paused.supports_resume = True
    paused.status = "已暂停"
    assert _state_to_record(paused).supports_resume is True

    done = _state(save_path=tmp_path / "f")
    done.supports_resume = True
    done.status = "已完成"
    assert _state_to_record(done).supports_resume is False
