from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from openwopan.storage.settings import AppSettings
from openwopan.tasks import download
from openwopan.tasks.download import (
    DownloadCallbacks,
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
