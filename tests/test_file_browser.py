from __future__ import annotations

import os
import threading
import time
import types
from pathlib import Path

import httpx
import pytest

from openwopan.app.file_browser import (
    FileBrowserError,
    FileBrowserLoginRequiredError,
    FileBrowserService,
    FileBrowserUploadCancelledError,
)
from openwopan.storage.settings import AppSettings
from openwopan.tasks.download import (
    DownloadResult,
    DownloadTaskControl,
    DownloadTaskState,
    DownloadTaskStore,
    make_download_task_id,
)
from openwopan.tasks.scheduler import DownloadCallbacks, DownloadScheduler, DownloadTaskInput
from openwopan.tasks.upload import (
    UploadTaskState,
    UploadTaskStore,
    make_upload_task_id,
)
from openwopan.wopan.client import UploadResumeContext
from openwopan.wopan.errors import (
    WopanAuthenticationError,
    WopanBusinessError,
    WopanUploadCancelledError,
)
from openwopan.wopan.models import DownloadInfo, WopanCloudUsage, WopanItem, WopanItemKind


class FakeClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.requested_parent_ids: list[str] = []
        self.created_folders: list[tuple[str, str]] = []
        self.renamed_items: list[tuple[str, str, WopanItemKind, str | None]] = []
        self.deleted_items: list[tuple[str, WopanItemKind]] = []
        self.moved_items: list[tuple[str, WopanItemKind, str]] = []
        self.downloaded_item_ids: list[str] = []
        self.uploaded_files: list[tuple[str, Path]] = []
        self.upload_kwargs: list[dict[str, object]] = []
        self.usage_account_ids: list[str] = []

    def list_files(self, parent_id: str) -> list[WopanItem]:
        self.requested_parent_ids.append(parent_id)
        if self.error is not None:
            raise self.error
        return [WopanItem(item_id="folder-1", name="Folder", kind=WopanItemKind.FOLDER)]

    def create_folder(self, parent_id: str, name: str) -> WopanItem:
        self.created_folders.append((parent_id, name))
        if self.error is not None:
            raise self.error
        return WopanItem(
            item_id="created-folder",
            name=name,
            kind=WopanItemKind.FOLDER,
            parent_id=parent_id,
        )

    def rename(
        self,
        item_id: str,
        new_name: str,
        kind: WopanItemKind,
        file_type: str | None = None,
    ) -> None:
        self.renamed_items.append((item_id, new_name, kind, file_type))
        if self.error is not None:
            raise self.error

    def delete(self, item_id: str, kind: WopanItemKind) -> None:
        self.deleted_items.append((item_id, kind))
        if self.error is not None:
            raise self.error

    def move(self, item_id: str, kind: WopanItemKind, target_parent_id: str) -> None:
        self.moved_items.append((item_id, kind, target_parent_id))
        if self.error is not None:
            raise self.error

    def get_download_info(self, item_id: str) -> DownloadInfo:
        self.downloaded_item_ids.append(item_id)
        if self.error is not None:
            raise self.error
        return DownloadInfo(url="https://download.example.test/file")

    def upload_file(
        self,
        parent_id: str,
        local_path: Path,
        **_kwargs: object,
    ) -> WopanItem:
        self.uploaded_files.append((parent_id, local_path))
        self.upload_kwargs.append(_kwargs)
        if self.error is not None:
            raise self.error
        return WopanItem(
            item_id="uploaded-file",
            name=local_path.name,
            kind=WopanItemKind.FILE,
            parent_id=parent_id,
            download_id="uploaded-fid",
            size=local_path.stat().st_size,
        )

    def query_cloud_usage(self, account_id: str) -> WopanCloudUsage:
        self.usage_account_ids.append(account_id)
        if self.error is not None:
            raise self.error
        return WopanCloudUsage(used_bytes=1024, total_bytes=2048)


def test_file_browser_service_returns_openwopan_items() -> None:
    client = FakeClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]

    items = service.list_directory("0")

    assert client.requested_parent_ids == ["0"]
    assert items[0].name == "Folder"


def test_file_browser_service_delegates_basic_operations() -> None:
    client = FakeClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]
    item = WopanItem(
        item_id="file-1",
        name="report.txt",
        kind=WopanItemKind.FILE,
        file_type="4",
    )

    created = service.create_folder("0", "Reports")
    service.rename_item(item, "renamed.txt")
    service.delete_item(item)
    service.move_item(item, "folder-2")

    assert created.name == "Reports"
    assert client.created_folders == [("0", "Reports")]
    assert client.renamed_items == [("file-1", "renamed.txt", WopanItemKind.FILE, "4")]
    assert client.deleted_items == [("file-1", WopanItemKind.FILE)]
    assert client.moved_items == [("file-1", WopanItemKind.FILE, "folder-2")]


def test_file_browser_service_downloads_file_to_local_path(tmp_path: Path) -> None:
    requests: list[str] = []
    content = b"download-content"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(len(content))})
        _, range_value = request.headers["Range"].split("=", 1)
        start_text, end_text = range_value.split("-", 1)
        start, end = int(start_text), int(end_text)
        return httpx.Response(
            206,
            content=content[start : end + 1],
            headers={"Content-Range": f"bytes {start}-{end}/{len(content)}"},
        )

    client = FakeClient()
    service = FileBrowserService(  # type: ignore[arg-type]
        client,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    item = WopanItem(
        item_id="file-1",
        name="report.txt",
        kind=WopanItemKind.FILE,
        download_id="fid-1",
    )
    progress: list[tuple[int, int | None]] = []
    local_path = tmp_path / "report.txt"

    service.download_file(
        item,
        local_path,
        lambda bytes_read, total_bytes: progress.append((bytes_read, total_bytes)),
    )

    assert client.downloaded_item_ids == ["fid-1"]
    # 统一 Range 路径：HEAD 探测一次 + 单 Range GET 一次
    assert requests.count("https://download.example.test/file") == 2
    assert local_path.read_bytes() == content
    # 统一 Range 路径进度：校验后初始 0 进度 + 分片 chunk 完成 + 合并完成
    assert progress == [(0, 16), (16, 16), (16, 16)]
    assert not local_path.with_name("report.txt.part").exists()


def test_file_browser_service_downloads_file_with_ranges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("openwopan.tasks.download.BYTES_PER_MB", 4)
    content = b"abcdefghijklmnopq"
    requested_ranges: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(len(content))})
        range_header = request.headers.get("Range")
        requested_ranges.append(range_header)
        assert range_header is not None
        _, range_value = range_header.split("=", 1)
        start_text, end_text = range_value.split("-", 1)
        start = int(start_text)
        end = int(end_text)
        body = content[start : end + 1]
        return httpx.Response(
            206,
            content=body,
            headers={
                "Content-Length": str(len(body)),
                "Content-Range": f"bytes {start}-{end}/{len(content)}",
            },
        )

    service = FileBrowserService(  # type: ignore[arg-type]
        FakeClient(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        settings=AppSettings(
            max_download_threads=2,
            download_part_mode="fixed",
            download_part_size_mb=4,
        ),
    )
    item = WopanItem(
        item_id="file-1",
        name="report.bin",
        kind=WopanItemKind.FILE,
        download_id="fid-1",
    )
    local_path = tmp_path / "report.bin"

    service.download_file(item, local_path)

    assert local_path.read_bytes() == content
    assert requested_ranges == ["bytes=0-15", "bytes=16-16"]


def test_file_browser_service_fails_when_range_is_unsupported(
    tmp_path: Path,
) -> None:
    """R7：服务端忽略 Range 时明确失败，不再退回单流下载。"""
    content = b"abcdefghijklmnopq"
    requests: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.headers.get("Range")))
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(len(content))})
        return httpx.Response(200, content=content)

    service = FileBrowserService(  # type: ignore[arg-type]
        FakeClient(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        settings=AppSettings(
            max_download_threads=2,
            download_part_mode="fixed",
            download_part_size_mb=4,
        ),
        download_store=DownloadTaskStore(tmp_path / "store"),
    )
    item = WopanItem(
        item_id="file-1",
        name="report.bin",
        kind=WopanItemKind.FILE,
        download_id="fid-1",
    )
    local_path = tmp_path / "report.bin"

    with pytest.raises(FileBrowserError, match="服务器不支持断点续传下载"):
        service.download_file(item, local_path)

    assert not local_path.exists()
    assert requests[0] == ("HEAD", None)
    assert any(method == "GET" and range_header is not None for method, range_header in requests)
    assert not any(method == "GET" and range_header is None for method, range_header in requests)


def test_file_browser_service_uploads_file_to_parent(tmp_path: Path) -> None:
    client = FakeClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]
    local_path = tmp_path / "upload.txt"
    local_path.write_bytes(b"upload-content")

    item = service.upload_file("folder-1", local_path)

    assert client.uploaded_files == [("folder-1", local_path)]
    assert item.name == "upload.txt"
    assert item.kind is WopanItemKind.FILE
    assert item.parent_id == "folder-1"


def test_file_browser_service_forwards_upload_progress(tmp_path: Path) -> None:
    class _ProgressClient(FakeClient):
        def upload_file(self, parent_id: str, local_path: Path, **kwargs: object) -> WopanItem:
            callback = kwargs.get("progress_callback")
            assert callable(callback)
            callback(2, 8)
            return super().upload_file(parent_id, local_path, **kwargs)

    client = _ProgressClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]
    local_path = tmp_path / "upload.txt"
    local_path.write_bytes(b"content")
    progress: list[tuple[int, int]] = []

    service.upload_file(
        "folder-1",
        local_path,
        progress_callback=lambda done, total: progress.append((done, total)),
    )

    assert progress == [(2, 8)]


    class _ExistingNameClient(FakeClient):
        def list_files(self, parent_id: str) -> list[WopanItem]:
            return [
                WopanItem(
                    item_id="existing-file",
                    name="upload.txt",
                    kind=WopanItemKind.FILE,
                    parent_id=parent_id,
                )
            ]

    client = _ExistingNameClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]
    local_path = tmp_path / "upload.txt"
    local_path.write_bytes(b"upload-content")

    with pytest.raises(FileBrowserError, match="上传目标已存在"):
        service.upload_file("folder-1", local_path, upload_name="upload.txt")

    assert client.uploaded_files == []


def test_file_browser_service_cancels_before_cloud_preflight(tmp_path: Path) -> None:
    client = FakeClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]
    local_path = tmp_path / "upload.txt"
    local_path.write_bytes(b"data")

    with pytest.raises(FileBrowserUploadCancelledError, match="上传已取消"):
        service.upload_file(
            "folder-1", local_path, upload_name="upload.txt", cancel_requested=lambda: True
        )

    assert client.requested_parent_ids == []
    assert client.uploaded_files == []


def test_file_browser_service_cancels_after_cloud_preflight(tmp_path: Path) -> None:
    cancelled = False

    class _PreflightClient(FakeClient):
        def list_files(self, parent_id: str) -> list[WopanItem]:
            nonlocal cancelled
            items = super().list_files(parent_id)
            cancelled = True
            return items

    client = _PreflightClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]
    local_path = tmp_path / "upload.txt"
    local_path.write_bytes(b"data")

    with pytest.raises(FileBrowserUploadCancelledError, match="上传已取消"):
        service.upload_file(
            "folder-1", local_path, upload_name="upload.txt",
            cancel_requested=lambda: cancelled,
        )

    assert client.requested_parent_ids == ["folder-1"]
    assert client.uploaded_files == []


def test_file_browser_service_maps_upload_cancellation(tmp_path: Path) -> None:
    client = FakeClient(WopanUploadCancelledError("protocol detail"))
    service = FileBrowserService(client)  # type: ignore[arg-type]
    local_path = tmp_path / "upload.txt"
    local_path.write_bytes(b"data")

    def requested() -> bool:
        return False

    with pytest.raises(FileBrowserUploadCancelledError, match="上传已取消") as error:
        service.upload_file("0", local_path, cancel_requested=requested)

    assert isinstance(error.value.__cause__, WopanUploadCancelledError)
    assert client.upload_kwargs[0]["cancel_requested"] is requested


def test_file_browser_service_updates_transfer_settings_for_future_uploads(
    tmp_path: Path,
) -> None:
    client = FakeClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]
    local_path = tmp_path / "upload.txt"
    local_path.write_bytes(b"upload-content")

    service.update_settings(
        AppSettings(
            upload_part_size_mb=8,
            max_upload_threads=4,
            retry_max_attempts=2,
        )
    )
    service.upload_file("folder-1", local_path)

    assert client.upload_kwargs == [
        {
            "upload_part_size_mb": 8,
            "max_upload_threads": 4,
            "retry_max_attempts": 2,
            "upload_name": None,
        }
    ]


def test_file_browser_service_syncs_runtime_download_settings(tmp_path: Path) -> None:
    first_started = threading.Event()
    second_started = threading.Event()
    third_started = threading.Event()
    release = threading.Event()
    started_tasks: dict[str, DownloadTaskInput] = {}

    def execute(
        task: DownloadTaskInput,
        _control: DownloadTaskControl,
        _callbacks: DownloadCallbacks,
    ) -> DownloadResult:
        started_tasks[task.local_path.name] = task
        if task.local_path.name == "first.txt":
            first_started.set()
        elif task.local_path.name == "second.txt":
            second_started.set()
        else:
            third_started.set()
        release.wait(5)
        return DownloadResult("已完成", task.task_id, task.local_path)

    old_settings = AppSettings(max_concurrent_downloads=1, max_download_threads=1)
    new_settings = AppSettings(max_concurrent_downloads=2, max_download_threads=3)
    scheduler = DownloadScheduler(max_concurrent_downloads=1, executor=execute)
    service = FileBrowserService(
        FakeClient(),
        settings=old_settings,
        download_store=DownloadTaskStore(tmp_path / "store"),
        download_scheduler=scheduler,
    )
    item = WopanItem(
        item_id="file-1",
        name="report.txt",
        kind=WopanItemKind.FILE,
        download_id="fid-1",
    )

    try:
        service.submit_download(item, tmp_path / "first.txt")
        assert first_started.wait(1)
        service.submit_download(item, tmp_path / "second.txt")

        service.update_settings(new_settings)
        service.submit_download(item, tmp_path / "third.txt")

        assert second_started.wait(1)
        assert started_tasks["first.txt"].settings is old_settings
        assert started_tasks["second.txt"].settings is old_settings
        release.set()
        assert third_started.wait(1)
        assert started_tasks["third.txt"].settings is new_settings
    finally:
        release.set()
        service.close_downloads()


def test_file_browser_service_keeps_settings_when_scheduler_update_fails(
    tmp_path: Path,
) -> None:
    old_settings = AppSettings(max_concurrent_downloads=1)
    new_settings = AppSettings(max_concurrent_downloads=2)
    scheduler = DownloadScheduler(max_concurrent_downloads=1)
    service = FileBrowserService(
        FakeClient(),
        settings=old_settings,
        download_store=DownloadTaskStore(tmp_path / "store"),
        download_scheduler=scheduler,
    )

    scheduler.close()
    with pytest.raises(RuntimeError, match="closed"):
        service.update_settings(new_settings)

    assert service._settings is old_settings


def test_file_browser_service_returns_cloud_usage() -> None:
    client = FakeClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]

    usage = service.get_cloud_usage("13800138000")

    assert client.usage_account_ids == ["13800138000"]
    assert usage.used_bytes == 1024
    assert usage.total_bytes == 2048


@pytest.mark.parametrize(
    ("kind", "download_id", "match"),
    [
        (WopanItemKind.FOLDER, None, "只能下载文件"),
        (WopanItemKind.FILE, None, "下载标识"),
    ],
)
def test_file_browser_service_rejects_invalid_downloads(
    tmp_path: Path, kind: WopanItemKind, download_id: str | None, match: str
) -> None:
    service = FileBrowserService(FakeClient())  # type: ignore[arg-type]
    item = WopanItem(item_id="item-1", name="item", kind=kind, download_id=download_id)

    with pytest.raises(FileBrowserError, match=match):
        service.download_file(item, tmp_path / "item")


@pytest.mark.parametrize(
    ("local_path_name", "match"),
    [
        ("missing.txt", "本地文件不存在"),
        (".", "只能上传文件"),
    ],
)
def test_file_browser_service_rejects_invalid_uploads(
    tmp_path: Path, local_path_name: str, match: str
) -> None:
    service = FileBrowserService(FakeClient())  # type: ignore[arg-type]
    local_path = tmp_path / local_path_name

    with pytest.raises(FileBrowserError, match=match):
        service.upload_file("0", local_path)


def test_file_browser_service_removes_partial_file_on_download_failure(tmp_path: Path) -> None:
    content = b"0123456789abcdef"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(len(content))})
        return httpx.Response(404, content=b"not found")

    store = DownloadTaskStore(tmp_path / "store")
    service = FileBrowserService(  # type: ignore[arg-type]
        FakeClient(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        settings=AppSettings(retry_max_attempts=0),
        download_store=store,
    )
    item = WopanItem(
        item_id="file-1",
        name="report.txt",
        kind=WopanItemKind.FILE,
        download_id="fid-1",
    )
    local_path = tmp_path / "report.txt"

    with pytest.raises(FileBrowserError, match="分片下载失败"):
        service.download_file(item, local_path)

    assert not local_path.exists()
    assert not local_path.with_name("report.txt.part").exists()
    state = store.load(make_download_task_id("fid-1", local_path))
    assert state is not None and state.status == "失败"


def test_file_browser_service_maps_login_expiry() -> None:
    service = FileBrowserService(FakeClient(WopanAuthenticationError("expired")))  # type: ignore[arg-type]

    with pytest.raises(FileBrowserLoginRequiredError, match="重新登录"):
        service.list_directory("0")


def test_file_browser_service_maps_protocol_errors() -> None:
    service = FileBrowserService(FakeClient(WopanBusinessError("9999", "failed")))  # type: ignore[arg-type]

    with pytest.raises(FileBrowserError, match="failed"):
        service.list_directory("0")


def test_file_browser_service_refreshes_expired_download_url(
    tmp_path: Path,
) -> None:
    """Regression: 下载 URL 403 过期后通过 refresh 回调重新取链接并完成下载。"""
    content = b"refreshed-content"
    info_calls: list[str] = []
    get_calls: list[str] = []

    class RefreshingClient(FakeClient):
        def get_download_info(self, item_id: str) -> DownloadInfo:
            info_calls.append(item_id)
            if len(info_calls) == 1:
                return DownloadInfo(url="https://download.example.test/expired")
            return DownloadInfo(url="https://download.example.test/fresh")

    def handler(request: httpx.Request) -> httpx.Response:
        get_calls.append(str(request.url))
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(len(content))})
        if str(request.url).endswith("/expired"):
            return httpx.Response(403)
        _, range_value = request.headers["Range"].split("=", 1)
        start_text, end_text = range_value.split("-", 1)
        start, end = int(start_text), int(end_text)
        return httpx.Response(
            206,
            content=content[start : end + 1],
            headers={"Content-Range": f"bytes {start}-{end}/{len(content)}"},
        )

    service = FileBrowserService(  # type: ignore[arg-type]
        RefreshingClient(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        download_store=DownloadTaskStore(tmp_path / "store"),
    )
    item = WopanItem(
        item_id="file-1",
        name="report.txt",
        kind=WopanItemKind.FILE,
        download_id="fid-1",
    )

    result = service.download_file(item, tmp_path / "report.txt")

    assert result.status == "已完成"
    assert info_calls == ["fid-1", "fid-1"]
    assert (tmp_path / "report.txt").read_bytes() == content
    assert any(url.endswith("/fresh") for url in get_calls)


def test_file_browser_service_download_records_and_removal(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path / "store")
    service = FileBrowserService(FakeClient(), download_store=store)  # type: ignore[arg-type]

    assert service.download_records() == ()

    state = DownloadTaskState(task_id="t1", file_name="a.bin", save_path=tmp_path / "a.bin")
    state.status = "已暂停"
    state.supports_resume = True
    store.save(state)
    state2 = DownloadTaskState(task_id="t2", file_name="b.bin", save_path=tmp_path / "b.bin")
    state2.status = "失败"
    state2.supports_resume = True
    store.save(state2)

    records = service.download_records()
    assert [record.task_id for record in records] == ["t1", "t2"]
    assert records[0].supports_resume is True  # 已暂停 + supports_resume 标记
    assert records[1].supports_resume is True

    service.remove_download_record("t1")
    assert [record.task_id for record in service.download_records()] == ["t2"]


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://upload.example.test")
    response = httpx.Response(status, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("unreachable")


def test_file_browser_service_rejects_empty_download_path() -> None:
    service = FileBrowserService(FakeClient())  # type: ignore[arg-type]
    item = WopanItem(
        item_id="file-1",
        name="report.txt",
        kind=WopanItemKind.FILE,
        download_id="fid-1",
    )

    with pytest.raises(FileBrowserError, match="保存路径不能为空"):
        service.download_file(item, Path(""))


def test_file_browser_service_rejects_empty_upload_parent(tmp_path: Path) -> None:
    service = FileBrowserService(FakeClient())  # type: ignore[arg-type]
    local_path = tmp_path / "upload.txt"
    local_path.write_bytes(b"data")

    with pytest.raises(FileBrowserError, match="目标文件夹不能为空"):
        service.upload_file("", local_path)


@pytest.mark.parametrize(
    ("error", "match"),
    [
        (_http_status_error(502), "HTTP 502"),
        (httpx.ConnectError("offline"), "网络错误"),
        (OSError("permission denied"), "无法读取本地文件"),
    ],
    ids=["http-status", "network", "read-error"],
)
def test_file_browser_service_maps_upload_failures(
    tmp_path: Path, error: Exception, match: str
) -> None:
    class _FailingUploadClient(FakeClient):
        def upload_file(self, parent_id: str, local_path: Path, **_kwargs: object) -> WopanItem:
            raise error

    service = FileBrowserService(_FailingUploadClient())  # type: ignore[arg-type]
    local_path = tmp_path / "upload.txt"
    local_path.write_bytes(b"data")

    with pytest.raises(FileBrowserError, match=match):
        service.upload_file("0", local_path)


def test_build_file_browser_service_constructs_service() -> None:
    from openwopan.app.file_browser import build_file_browser_service

    service = build_file_browser_service(
        "WoCloud-Web-Token=1234567890abcdef-token", settings=AppSettings()
    )

    assert isinstance(service, FileBrowserService)


class FolderUploadFakeClient(FakeClient):
    """Fake client with deterministic dir ids and per-directory existing names."""

    def __init__(self) -> None:
        super().__init__()
        self.existing_names: dict[str, set[str]] = {}

    def list_files(self, parent_id: str) -> list[WopanItem]:
        self.requested_parent_ids.append(parent_id)
        if self.error is not None:
            raise self.error
        return [
            WopanItem(item_id=f"{parent_id}:{name}", name=name, kind=WopanItemKind.FOLDER)
            for name in sorted(self.existing_names.get(parent_id, set()))
        ]

    def create_folder(self, parent_id: str, name: str) -> WopanItem:
        self.created_folders.append((parent_id, name))
        if self.error is not None:
            raise self.error
        item_id = f"dir-{len(self.created_folders)}"
        return WopanItem(
            item_id=item_id,
            name=name,
            kind=WopanItemKind.FOLDER,
            parent_id=parent_id,
        )


def _make_local_tree(tmp_path: Path) -> Path:
    root = tmp_path / "photos"
    root.mkdir()
    (root / "相册").mkdir()
    (root / "空目录").mkdir()
    (root / "top.txt").write_bytes(b"12345")
    (root / "相册" / "春节.md").write_bytes(b"hello")
    return root


def test_prepare_folder_upload_stops_between_cloud_creates(tmp_path: Path) -> None:
    client = FolderUploadFakeClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]
    local_root = _make_local_tree(tmp_path)
    stopped = False
    create_folder = client.create_folder

    def stop_after_root(parent_id: str, name: str) -> WopanItem:
        nonlocal stopped
        item = create_folder(parent_id, name)
        stopped = True
        return item

    client.create_folder = stop_after_root  # type: ignore[method-assign]
    with pytest.raises(FileBrowserUploadCancelledError, match="上传已取消"):
        service.prepare_folder_upload(
            "0", local_root, root_name="photos", cancel_requested=lambda: stopped
        )
    assert client.created_folders == [("0", "photos")]


def test_prepare_folder_upload_stops_before_cloud_create(tmp_path: Path) -> None:
    client = FolderUploadFakeClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]
    with pytest.raises(FileBrowserUploadCancelledError, match="上传已取消"):
        service.prepare_folder_upload(
            "0", _make_local_tree(tmp_path), cancel_requested=lambda: True
        )
    assert client.created_folders == []


def test_prepare_folder_upload_creates_tree_and_plans_files(tmp_path: Path) -> None:
    client = FolderUploadFakeClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]
    local_root = _make_local_tree(tmp_path)

    job = service.prepare_folder_upload("0", local_root)

    assert job.root_name == "photos"
    assert job.root_item_id == "dir-1"
    assert job.total_bytes == 10
    assert client.created_folders == [
        ("0", "photos"),
        ("dir-1", "相册"),
        ("dir-1", "空目录"),
    ]
    assert [(file.name, file.target_dir_id, file.size) for file in job.files] == [
        ("top.txt", "dir-1", 5),
        ("春节.md", "dir-2", 5),
    ]
    assert all(file.local_path.is_file() for file in job.files)
    # 只列出目标目录与接收内容的云端目录，每目录至多一次
    assert client.requested_parent_ids == ["0", "dir-1", "dir-2"]


def test_prepare_folder_upload_renames_conflicting_root_folder(tmp_path: Path) -> None:
    client = FolderUploadFakeClient()
    client.existing_names["0"] = {"photos", "photos (copy)"}
    service = FileBrowserService(client)  # type: ignore[arg-type]
    local_root = _make_local_tree(tmp_path)

    job = service.prepare_folder_upload("0", local_root)

    assert job.root_name == "photos (copy) (copy)"
    assert client.created_folders[0] == ("0", "photos (copy) (copy)")


def test_prepare_folder_upload_rejects_explicit_conflict_before_create(
    tmp_path: Path,
) -> None:
    client = FolderUploadFakeClient()
    client.existing_names["0"] = {"photos (copy)"}
    service = FileBrowserService(client)  # type: ignore[arg-type]
    local_root = _make_local_tree(tmp_path)

    with pytest.raises(FileBrowserError, match="上传目标已存在"):
        service.prepare_folder_upload("0", local_root, root_name="photos (copy)")

    assert client.created_folders == []


def test_prepare_folder_upload_renames_conflicting_subfolder_and_files(
    tmp_path: Path,
) -> None:
    client = FolderUploadFakeClient()
    # 模拟云端目录内已有同名内容（如并发上传或服务端同名合并）
    client.existing_names["dir-1"] = {"相册", "top.txt"}
    client.existing_names["dir-2"] = {"春节.md"}
    service = FileBrowserService(client)  # type: ignore[arg-type]
    local_root = _make_local_tree(tmp_path)

    job = service.prepare_folder_upload("0", local_root)

    assert client.created_folders == [
        ("0", "photos"),
        ("dir-1", "相册 (copy)"),
        ("dir-1", "空目录"),
    ]
    assert [(file.name, file.target_dir_id) for file in job.files] == [
        ("top (copy).txt", "dir-1"),
        ("春节 (copy).md", "dir-2"),
    ]


def test_prepare_folder_upload_keeps_distinct_local_names_without_extra_lists(
    tmp_path: Path,
) -> None:
    client = FolderUploadFakeClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]
    root = tmp_path / "solo"
    root.mkdir()
    (root / "a.txt").write_bytes(b"a")

    job = service.prepare_folder_upload("0", root)

    # 每个接收文件的云端目录恰好列出一次
    assert client.requested_parent_ids == ["0", "dir-1"]
    assert [file.name for file in job.files] == ["a.txt"]


def test_prepare_folder_upload_dedupes_same_local_name_in_one_dir(tmp_path: Path) -> None:
    """同名冲突防御：同一目录内已占用名会推进计数，即使来自本地重名计划。"""
    client = FolderUploadFakeClient()
    client.existing_names["dir-1"] = {"a.txt", "a (copy).txt"}
    service = FileBrowserService(client)  # type: ignore[arg-type]
    root = tmp_path / "dup"
    root.mkdir()
    (root / "a.txt").write_bytes(b"a")

    job = service.prepare_folder_upload("0", root)

    assert [file.name for file in job.files] == ["a (copy) (copy).txt"]


def test_prepare_folder_upload_fails_without_partial_job_on_create_error(
    tmp_path: Path,
) -> None:
    class _FailSecondCreate(FolderUploadFakeClient):
        def create_folder(self, parent_id: str, name: str) -> WopanItem:
            if self.created_folders:
                raise WopanBusinessError("0001", "denied")
            return super().create_folder(parent_id, name)

    client = _FailSecondCreate()
    service = FileBrowserService(client)  # type: ignore[arg-type]
    local_root = _make_local_tree(tmp_path)

    with pytest.raises(FileBrowserError, match="创建目录失败"):
        service.prepare_folder_upload("0", local_root)

    # 阶段一失败不产出任何文件上传计划（部分目录可能已创建，不做回滚）
    assert client.created_folders == [("0", "photos")]


def test_prepare_folder_upload_maps_login_expiry(tmp_path: Path) -> None:
    client = FolderUploadFakeClient()
    client.error = WopanAuthenticationError("expired")
    service = FileBrowserService(client)  # type: ignore[arg-type]
    local_root = _make_local_tree(tmp_path)

    with pytest.raises(FileBrowserLoginRequiredError, match="重新登录"):
        service.prepare_folder_upload("0", local_root)


def test_prepare_folder_upload_maps_scan_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service = FileBrowserService(FolderUploadFakeClient())  # type: ignore[arg-type]

    with caplog.at_level("WARNING", logger="openwopan.app.file_browser"):
        with pytest.raises(FileBrowserError, match="扫描本地文件夹失败"):
            service.prepare_folder_upload("0", tmp_path / "missing")

    # 日志只记错误类型，不携带本地路径（用户目录/文件名不进日志）
    scan_logs = [
        record.getMessage()
        for record in caplog.records
        if "prepare_folder_upload.scan_failed" in record.getMessage()
    ]
    assert scan_logs
    assert all(str(tmp_path) not in message for message in scan_logs)


def test_prepare_folder_upload_rejects_symlink_root(tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    linked_root = tmp_path / "linked-root"
    try:
        os.symlink(real_dir, linked_root)
    except (OSError, NotImplementedError):
        pytest.skip("platform cannot create symlinks")
    service = FileBrowserService(FolderUploadFakeClient())  # type: ignore[arg-type]

    with pytest.raises(FileBrowserError, match="扫描本地文件夹失败：不能上传符号链接"):
        service.prepare_folder_upload("0", linked_root)


@pytest.mark.parametrize(
    ("parent_id", "local_path_name", "match"),
    [
        ("", "root", "目标文件夹不能为空"),
        ("0", "", "上传文件夹不能为空"),
    ],
)
def test_prepare_folder_upload_rejects_invalid_arguments(
    tmp_path: Path, parent_id: str, local_path_name: str, match: str
) -> None:
    service = FileBrowserService(FolderUploadFakeClient())  # type: ignore[arg-type]
    if local_path_name:
        local_root = tmp_path / local_path_name
        local_root.mkdir(exist_ok=True)
    else:
        local_root = Path("/")

    with pytest.raises(FileBrowserError, match=match):
        service.prepare_folder_upload(parent_id, local_root)


def test_file_browser_service_submits_and_controls_task(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def execute(
        task: DownloadTaskInput,
        _control: DownloadTaskControl,
        _callbacks: DownloadCallbacks,
    ) -> DownloadResult:
        started.set()
        release.wait(1)
        return DownloadResult("已完成", task.task_id, task.local_path)

    scheduler = DownloadScheduler(max_concurrent_downloads=1, executor=execute)
    service = FileBrowserService(
        FakeClient(),
        download_store=DownloadTaskStore(tmp_path / "store"),
        download_scheduler=scheduler,
    )
    item = WopanItem(
        item_id="file-1",
        name="report.txt",
        kind=WopanItemKind.FILE,
        download_id="fid-1",
    )

    try:
        first = service.submit_download(item, tmp_path / "first.txt")
        assert started.wait(1)
        second = service.submit_download(item, tmp_path / "second.txt")

        assert first != second
        assert service.pause_download(second) is True
        records = {record.task_id: record for record in service.download_records()}
        assert records[second].status == "已暂停"
        service.remove_download_record(second)
        assert second not in {record.task_id for record in service.download_records()}
        with pytest.raises(KeyError):
            service.resume_download(second)
        replacement = service.submit_download(item, tmp_path / "second.txt")
        assert replacement != second
        assert service.cancel_download(first) is True
    finally:
        release.set()
        service.close_downloads()


def test_file_browser_service_allows_duplicate_submission_for_same_path(
    tmp_path: Path,
) -> None:
    def execute(
        task: DownloadTaskInput,
        _control: DownloadTaskControl,
        _callbacks: DownloadCallbacks,
    ) -> DownloadResult:
        return DownloadResult("已完成", task.task_id, task.local_path)

    scheduler = DownloadScheduler(max_concurrent_downloads=1, executor=execute)
    service = FileBrowserService(
        FakeClient(),
        download_store=DownloadTaskStore(tmp_path / "store"),
        download_scheduler=scheduler,
    )
    item = WopanItem(
        item_id="file-1",
        name="report.txt",
        kind=WopanItemKind.FILE,
        download_id="fid-1",
    )

    try:
        first = service.submit_download(item, tmp_path / "same.txt")
        second = service.submit_download(item, tmp_path / "same.txt")
    finally:
        service.close_downloads()

    assert first != second


def test_file_browser_service_recovers_tasks_by_persisted_download_id(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path / "store")
    state = DownloadTaskState(
        task_id="persisted-task",
        file_name="report.txt",
        save_path=tmp_path / "report.txt",
        status="已暂停",
        download_id="fid-persisted",
    )
    store.save(state)
    client = FakeClient()
    scheduler = DownloadScheduler(max_concurrent_downloads=1)
    service = FileBrowserService(
        client,
        download_store=store,
        download_scheduler=scheduler,  # type: ignore[arg-type]
    )

    try:
        records = service.recover_downloads()

        assert client.downloaded_item_ids == ["fid-persisted"]
        assert records[0].task_id == "persisted-task"
        assert records[0].status == "已暂停"
    finally:
        service.close_downloads()


# ---------------------------------------------------------------------------
# Upload resume: persisted sessions, terminal-state semantics, recovery
# ---------------------------------------------------------------------------


class ResumeAwareUploadClient(FakeClient):
    """Fake client that replays scripted part results before failing/succeeding."""

    def __init__(self) -> None:
        super().__init__()
        self.part_results: list[tuple[int, str]] = []
        self.upload_failure: Exception | None = None

    def upload_file(
        self,
        parent_id: str,
        local_path: Path,
        **kwargs: object,
    ) -> WopanItem:
        self.uploaded_files.append((parent_id, local_path))
        self.upload_kwargs.append(kwargs)
        resume = kwargs.get("resume")
        if isinstance(resume, UploadResumeContext) and resume.on_part_result is not None:
            for part_index, fid in self.part_results:
                resume.on_part_result(part_index, fid)
        if self.upload_failure is not None:
            raise self.upload_failure
        return WopanItem(
            item_id="uploaded-file",
            name=local_path.name,
            kind=WopanItemKind.FILE,
            parent_id=parent_id,
            download_id="uploaded-fid",
            size=local_path.stat().st_size,
        )


def _resume_service(
    tmp_path: Path,
    client: ResumeAwareUploadClient | None = None,
    store: UploadTaskStore | None = None,
) -> tuple[FileBrowserService, ResumeAwareUploadClient, UploadTaskStore]:
    resolved_client = client or ResumeAwareUploadClient()
    resolved_store = store or UploadTaskStore(tmp_path / "uploads")
    service = FileBrowserService(
        resolved_client,  # type: ignore[arg-type]
        settings=AppSettings(upload_part_size_mb=5),
        upload_store=resolved_store,
    )
    return service, resolved_client, resolved_store


def _three_part_file(tmp_path: Path) -> Path:
    local_path = tmp_path / "report.bin"
    local_path.write_bytes(b"012345678901234")
    return local_path


def _state(store: UploadTaskStore, task_id: str) -> UploadTaskState | None:
    return store.load(task_id)


def test_service_upload_records_parts_and_keeps_state_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1 前半：分片确认即落盘；失败保留已完成分片。"""
    monkeypatch.setattr("openwopan.wopan.client.BYTES_PER_MB", 1)
    service, client, store = _resume_service(tmp_path)
    client.part_results = [(1, "fid-1"), (2, "fid-2")]
    client.upload_failure = WopanBusinessError("9999", "busy")
    local_path = _three_part_file(tmp_path)

    with pytest.raises(FileBrowserError, match="busy"):
        service.upload_file("folder-1", local_path)

    task_id = make_upload_task_id("folder-1", local_path, None)
    state = _state(store, task_id)
    assert state is not None
    assert state.completed_indexes == [1, 2]
    assert state.fid == "fid-1"
    assert state.status == "失败"
    assert "busy" in state.error
    first_resume = client.upload_kwargs[0]["resume"]
    assert isinstance(first_resume, UploadResumeContext)
    assert first_resume.completed_indexes == frozenset()
    assert first_resume.known_fid == ""
    assert client.upload_kwargs[0]["upload_part_size_mb"] == 5


def test_service_upload_retry_reuses_session_and_skips_completed_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1 后半 + AC2：重试复用 uniqueId/batchNo，仅发剩余分片。"""
    monkeypatch.setattr("openwopan.wopan.client.BYTES_PER_MB", 1)
    service, client, store = _resume_service(tmp_path)
    client.part_results = [(1, "fid-1"), (2, "fid-2")]
    client.upload_failure = WopanBusinessError("9999", "busy")
    local_path = _three_part_file(tmp_path)

    with pytest.raises(FileBrowserError):
        service.upload_file("folder-1", local_path)

    # 第二个 (3, fid-3) 模拟重复确认，覆盖去重分支
    client.part_results = [(3, "fid-3"), (3, "fid-3")]
    client.upload_failure = None
    item = service.upload_file("folder-1", local_path)

    first_resume = client.upload_kwargs[0]["resume"]
    second_resume = client.upload_kwargs[1]["resume"]
    assert isinstance(first_resume, UploadResumeContext)
    assert isinstance(second_resume, UploadResumeContext)
    assert second_resume.completed_indexes == frozenset({1, 2})
    assert second_resume.known_fid == "fid-1"
    assert second_resume.unique_id == first_resume.unique_id
    assert second_resume.batch_no == first_resume.batch_no
    assert item.item_id == "uploaded-file"
    task_id = make_upload_task_id("folder-1", local_path, None)
    assert _state(store, task_id) is None


def test_service_upload_discards_state_on_file_size_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3 size 变化：旧进度清除，全新 uniqueId。"""
    monkeypatch.setattr("openwopan.wopan.client.BYTES_PER_MB", 1)
    service, client, _store = _resume_service(tmp_path)
    client.part_results = [(1, "fid-1")]
    client.upload_failure = WopanBusinessError("9999", "busy")
    local_path = _three_part_file(tmp_path)

    with pytest.raises(FileBrowserError):
        service.upload_file("folder-1", local_path)
    first_resume = client.upload_kwargs[0]["resume"]

    local_path.write_bytes(b"012345678901234-changed")
    client.upload_failure = None
    service.upload_file("folder-1", local_path)

    second_resume = client.upload_kwargs[1]["resume"]
    assert second_resume.completed_indexes == frozenset()
    assert second_resume.unique_id != first_resume.unique_id


def test_service_upload_discards_state_on_mtime_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3 mtime 变化：旧进度清除，全新 uniqueId。"""
    monkeypatch.setattr("openwopan.wopan.client.BYTES_PER_MB", 1)
    # unique_id 取 int(time.time() * 1000)，两次 upload_file 可能落入同一毫秒；
    # 注入可控时钟保证两次调用之间至少前进 1ms。
    clock_value = 1000.0

    def fake_time() -> float:
        nonlocal clock_value
        clock_value += 0.002
        return clock_value

    fake_time_module = types.SimpleNamespace(
        time=fake_time,
        strftime=time.strftime,
    )
    monkeypatch.setattr("openwopan.app.file_browser.time", fake_time_module)
    service, client, _store = _resume_service(tmp_path)
    client.part_results = [(1, "fid-1")]
    client.upload_failure = WopanBusinessError("9999", "busy")
    local_path = _three_part_file(tmp_path)

    with pytest.raises(FileBrowserError):
        service.upload_file("folder-1", local_path)
    first_resume = client.upload_kwargs[0]["resume"]

    stat = local_path.stat()
    os.utime(local_path, (stat.st_atime, stat.st_mtime + 30))
    client.upload_failure = None
    service.upload_file("folder-1", local_path)

    second_resume = client.upload_kwargs[1]["resume"]
    assert second_resume.completed_indexes == frozenset()
    assert second_resume.unique_id != first_resume.unique_id


def test_service_upload_discards_state_when_part_plan_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6 分片计划变化：丢弃旧进度，全新会话。"""
    monkeypatch.setattr("openwopan.wopan.client.BYTES_PER_MB", 1)
    service, client, _store = _resume_service(tmp_path)
    client.part_results = [(1, "fid-1")]
    client.upload_failure = WopanBusinessError("9999", "busy")
    local_path = _three_part_file(tmp_path)

    with pytest.raises(FileBrowserError):
        service.upload_file("folder-1", local_path)
    first_resume = client.upload_kwargs[0]["resume"]

    service.update_settings(AppSettings(upload_part_size_mb=10))
    client.upload_failure = None
    service.upload_file("folder-1", local_path)

    second_resume = client.upload_kwargs[1]["resume"]
    assert second_resume.completed_indexes == frozenset()
    assert second_resume.unique_id != first_resume.unique_id


def test_service_upload_discards_state_older_than_max_age(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6 超 24h：丢弃旧进度，全新会话。"""
    import json as _json

    monkeypatch.setattr("openwopan.wopan.client.BYTES_PER_MB", 1)
    service, client, store = _resume_service(tmp_path)
    client.part_results = [(1, "fid-1")]
    client.upload_failure = WopanBusinessError("9999", "busy")
    local_path = _three_part_file(tmp_path)

    with pytest.raises(FileBrowserError):
        service.upload_file("folder-1", local_path)
    first_resume = client.upload_kwargs[0]["resume"]

    task_id = make_upload_task_id("folder-1", local_path, None)
    path = store.task_path(task_id)
    data = _json.loads(path.read_text(encoding="utf-8"))
    data["updated_at"] = time.time() - 25 * 3600
    path.write_text(_json.dumps(data), encoding="utf-8")

    client.upload_failure = None
    service.upload_file("folder-1", local_path)

    second_resume = client.upload_kwargs[1]["resume"]
    assert second_resume.completed_indexes == frozenset()
    assert second_resume.unique_id != first_resume.unique_id


def test_service_upload_short_circuits_when_all_parts_and_fid_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4 前半：全部分片已完成且有 fid，零网络直接返回。"""
    monkeypatch.setattr("openwopan.wopan.client.BYTES_PER_MB", 1)
    service, client, store = _resume_service(tmp_path)
    local_path = tmp_path / "report.txt"
    local_path.write_bytes(b"012345678901234")
    task_id = make_upload_task_id("folder-1", local_path, None)
    state = UploadTaskState(
        task_id=task_id,
        file_name="report.txt",
        local_path=local_path,
        parent_id="folder-1",
        upload_name=None,
        file_size=15,
        file_mtime=local_path.stat().st_mtime,
        part_size=5,
        total_parts=3,
        unique_id="1690000000000",
        batch_no="20260101010101",
        fid="fid-full",
        completed_indexes=[1, 2, 3],
    )
    store.save(state)

    item = service.upload_file("folder-1", local_path)

    assert client.uploaded_files == []
    assert item.item_id == "fid-full"
    assert item.name == "report.txt"
    assert item.file_type == "4"
    assert _state(store, task_id) is None


def test_service_upload_deletes_state_on_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5 取消：持久化状态删除后原样抛出。"""
    monkeypatch.setattr("openwopan.wopan.client.BYTES_PER_MB", 1)
    service, client, store = _resume_service(tmp_path)
    client.part_results = [(1, "fid-1")]
    client.upload_failure = WopanUploadCancelledError("cancelled")
    local_path = _three_part_file(tmp_path)

    with pytest.raises(FileBrowserUploadCancelledError, match="上传已取消"):
        service.upload_file("folder-1", local_path)

    task_id = make_upload_task_id("folder-1", local_path, None)
    assert _state(store, task_id) is None


def test_service_recovers_interrupted_uploads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC7：中断任务归一为可重试行；已完成/缺文件残留清理。"""
    monkeypatch.setattr("openwopan.wopan.client.BYTES_PER_MB", 1)
    service, _client, store = _resume_service(tmp_path)
    existing = tmp_path / "kept.bin"
    existing.write_bytes(b"data")
    resumable = UploadTaskState(
        task_id="kept-task",
        file_name="kept.bin",
        local_path=existing,
        parent_id="folder-1",
        upload_name=None,
        file_size=4,
        file_mtime=1.0,
        part_size=5,
        total_parts=3,
        unique_id="u1",
        batch_no="b1",
        completed_indexes=[1],
    )
    store.save(resumable)
    finished = UploadTaskState(
        task_id="finished-task",
        file_name="done.bin",
        local_path=tmp_path / "done.bin",
        parent_id="folder-1",
        upload_name=None,
        file_size=1,
        file_mtime=1.0,
        part_size=5,
        total_parts=1,
        unique_id="u2",
        batch_no="b2",
        status="已完成",
    )
    store.save(finished)
    missing = UploadTaskState(
        task_id="missing-task",
        file_name="gone.bin",
        local_path=tmp_path / "gone.bin",
        parent_id="folder-1",
        upload_name=None,
        file_size=1,
        file_mtime=1.0,
        part_size=5,
        total_parts=1,
        unique_id="u3",
        batch_no="b3",
    )
    store.save(missing)

    records = service.recover_uploads()

    assert [record.task_id for record in records] == ["kept-task"]
    record = records[0]
    assert record.status == "失败"
    assert record.resumable is True
    assert record.completed_parts == 1
    assert record.total_parts == 3
    assert "应用中断" in record.error
    assert "1/3" in record.error
    assert _state(store, "kept-task") is not None
    assert _state(store, "kept-task").status == "失败"  # type: ignore[union-attr]
    assert _state(store, "finished-task") is None
    assert _state(store, "missing-task") is None


def test_service_recovers_nothing_without_store() -> None:
    service = FileBrowserService(FakeClient())  # type: ignore[arg-type]

    assert service.recover_uploads() == ()


def test_service_upload_without_store_keeps_legacy_behavior(tmp_path: Path) -> None:
    """不注入 store 时：kwargs 无 resume，行为与旧版完全一致。"""
    client = FakeClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]
    local_path = tmp_path / "upload.txt"
    local_path.write_bytes(b"data")

    service.upload_file("folder-1", local_path)

    assert "resume" not in client.upload_kwargs[0]
    assert service.recover_uploads() == ()


class LegacyKeywordUploadClient(FakeClient):
    """Backend without the resume keyword."""

    def __init__(self) -> None:
        super().__init__()
        self.received: list[dict[str, object]] = []

    def upload_file(
        self,
        parent_id: str,
        local_path: Path,
        *,
        upload_name: str | None = None,
        progress_callback=None,  # type: ignore[no-untyped-def]
        cancel_requested=None,  # type: ignore[no-untyped-def]
    ) -> WopanItem:
        self.received.append(
            {
                "upload_name": upload_name,
                "progress_callback": progress_callback,
                "cancel_requested": cancel_requested,
            }
        )
        return WopanItem(
            item_id="legacy-file",
            name=local_path.name,
            kind=WopanItemKind.FILE,
            parent_id=parent_id,
            download_id="legacy-fid",
            size=local_path.stat().st_size,
        )


class OlderUploadClient(LegacyKeywordUploadClient):
    """Backend without the cancel_requested keyword either."""

    def upload_file(  # type: ignore[override]
        self,
        parent_id: str,
        local_path: Path,
        *,
        upload_name: str | None = None,
        progress_callback=None,  # type: ignore[no-untyped-def]
    ) -> WopanItem:
        self.received.append({"upload_name": upload_name, "progress_callback": progress_callback})
        return WopanItem(
            item_id="older-file",
            name=local_path.name,
            kind=WopanItemKind.FILE,
            parent_id=parent_id,
            download_id="older-fid",
            size=local_path.stat().st_size,
        )


class OldestUploadClient(FakeClient):
    """Backend accepting only the historical keywords."""

    def __init__(self) -> None:
        super().__init__()
        self.received: list[dict[str, object]] = []

    def upload_file(
        self,
        parent_id: str,
        local_path: Path,
        *,
        upload_part_size_mb: int = 5,
        max_upload_threads: int = 16,
        retry_max_attempts: int = 3,
        upload_name: str | None = None,
    ) -> WopanItem:
        self.received.append({"upload_name": upload_name})
        return WopanItem(
            item_id="oldest-file",
            name=local_path.name,
            kind=WopanItemKind.FILE,
            parent_id=parent_id,
            download_id="oldest-fid",
            size=local_path.stat().st_size,
        )


def test_service_upload_degrades_kwargs_for_older_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resume/cancel_requested/progress_callback 逐级降级重试。"""
    monkeypatch.setattr("openwopan.wopan.client.BYTES_PER_MB", 1)
    client = OldestUploadClient()
    service, _resolved, _store = _resume_service(tmp_path, client=client)
    service._upload_store = UploadTaskStore(tmp_path / "uploads2")
    local_path = _three_part_file(tmp_path)

    item = service.upload_file(
        "folder-1",
        local_path,
        progress_callback=lambda done, total: None,
        cancel_requested=lambda: False,
    )

    assert item.item_id == "oldest-file"
    assert client.received[-1] == {"upload_name": None}


class VolatileStore(UploadTaskStore):
    """Store whose update always loses the race against a concurrent delete."""

    def update(self, task_id: str, change):  # type: ignore[no-untyped-def]
        raise KeyError(f"unknown upload task: {task_id}")


@pytest.mark.parametrize(
    ("failure", "match"),
    [
        (None, None),
        (WopanBusinessError("9999", "busy"), "busy"),
    ],
    ids=["success", "failure"],
)
def test_service_upload_tolerates_state_deleted_during_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception | None,
    match: str | None,
) -> None:
    """分片回调与终态写入撞上并发删除：吞掉 KeyError，不改变对外结果。"""
    monkeypatch.setattr("openwopan.wopan.client.BYTES_PER_MB", 1)
    client = ResumeAwareUploadClient()
    client.part_results = [(1, "fid-1")]
    client.upload_failure = failure
    service = FileBrowserService(
        client,  # type: ignore[arg-type]
        settings=AppSettings(upload_part_size_mb=5),
        upload_store=VolatileStore(tmp_path / "uploads"),
    )
    local_path = _three_part_file(tmp_path)

    if match is None:
        item = service.upload_file("folder-1", local_path)
        assert item.item_id == "uploaded-file"
    else:
        with pytest.raises(FileBrowserError, match=match):
            service.upload_file("folder-1", local_path)


def test_service_upload_rejects_empty_upload_name(tmp_path: Path) -> None:
    service, client, _store = _resume_service(tmp_path)
    local_path = tmp_path / "upload.txt"
    local_path.write_bytes(b"data")

    with pytest.raises(FileBrowserError, match="上传文件名称不能为空"):
        service.upload_file("folder-1", local_path, upload_name="")

    assert client.uploaded_files == []


def test_service_upload_maps_stat_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _client, _store = _resume_service(tmp_path)
    local_path = tmp_path / "upload.txt"
    local_path.write_bytes(b"data")
    real_stat = Path.stat
    calls = {"count": 0}

    def counted_stat(self: Path, *args: object, **kwargs: object) -> object:
        calls["count"] += 1
        if calls["count"] >= 3:
            raise OSError("stat denied")
        return real_stat(self)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", counted_stat)

    with pytest.raises(FileBrowserError, match="无法读取本地文件"):
        service.upload_file("folder-1", local_path)


class BareUploadClient(FakeClient):
    """Backend accepting nothing beyond the positional arguments."""

    def upload_file(self, parent_id: str, local_path: Path) -> WopanItem:
        return WopanItem(
            item_id="bare-file",
            name=local_path.name,
            kind=WopanItemKind.FILE,
            parent_id=parent_id,
            download_id="bare-fid",
            size=local_path.stat().st_size,
        )


def test_service_upload_reraises_type_error_without_droppable_kwargs(
    tmp_path: Path,
) -> None:
    client = BareUploadClient()
    service, _resolved, store = _resume_service(tmp_path, client=client)
    local_path = _three_part_file(tmp_path)

    with pytest.raises(TypeError):
        service.upload_file(
            "folder-1",
            local_path,
            progress_callback=lambda done, total: None,
        )

    task_id = make_upload_task_id("folder-1", local_path, None)
    state = _state(store, task_id)
    assert state is not None
    assert state.status == "失败"


def test_service_recovered_record_carries_upload_name_for_retry_key(
    tmp_path: Path,
) -> None:
    """恢复记录显式携带 upload_name，retry 派生的持久化键与原会话一致。"""
    service, _client, store = _resume_service(tmp_path)
    named_path = tmp_path / "kept.bin"
    named_path.write_bytes(b"data")
    named_task_id = make_upload_task_id("folder-1", named_path, "renamed.bin")
    store.save(
        UploadTaskState(
            task_id=named_task_id,
            file_name="renamed.bin",
            local_path=named_path,
            parent_id="folder-1",
            upload_name="renamed.bin",
            file_size=4,
            file_mtime=1.0,
            part_size=5,
            total_parts=3,
            unique_id="u9",
            batch_no="b9",
            completed_indexes=[1],
        )
    )
    anonymous_path = tmp_path / "plain.bin"
    anonymous_path.write_bytes(b"data")
    anonymous_task_id = make_upload_task_id("folder-1", anonymous_path, None)
    store.save(
        UploadTaskState(
            task_id=anonymous_task_id,
            file_name="plain.bin",
            local_path=anonymous_path,
            parent_id="folder-1",
            upload_name=None,
            file_size=4,
            file_mtime=1.0,
            part_size=5,
            total_parts=3,
            unique_id="u8",
            batch_no="b8",
            completed_indexes=[1],
        )
    )

    records = service.recover_uploads()

    by_id = {record.task_id: record for record in records}
    assert by_id[named_task_id].upload_name == "renamed.bin"
    assert by_id[anonymous_task_id].upload_name is None
    # retry 以记录中的 upload_name 显式重传时，派生键命中原会话状态
    assert (
        make_upload_task_id(
            by_id[named_task_id].target_parent_id,
            by_id[named_task_id].local_path,
            by_id[named_task_id].upload_name,
        )
        == named_task_id
    )
    assert (
        make_upload_task_id(
            by_id[anonymous_task_id].target_parent_id,
            by_id[anonymous_task_id].local_path,
            by_id[anonymous_task_id].upload_name,
        )
        == anonymous_task_id
    )
