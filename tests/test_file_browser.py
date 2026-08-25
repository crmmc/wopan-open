from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from openwopan.app.file_browser import (
    FileBrowserError,
    FileBrowserLoginRequiredError,
    FileBrowserService,
)
from openwopan.storage.settings import AppSettings
from openwopan.tasks.download import DownloadTaskState, DownloadTaskStore
from openwopan.wopan.errors import WopanAuthenticationError, WopanBusinessError
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

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, content=b"download-content", headers={"Content-Length": "16"})

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
    assert requests == ["https://download.example.test/file"]
    assert local_path.read_bytes() == b"download-content"
    assert progress == [(16, 16)]
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


def test_file_browser_service_falls_back_when_range_is_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("openwopan.tasks.download.BYTES_PER_MB", 4)
    content = b"abcdefghijklmnopq"
    requests: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.headers.get("Range")))
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(len(content))})
        if request.headers.get("Range") is not None:
            return httpx.Response(200, content=content)
        return httpx.Response(200, content=content, headers={"Content-Length": str(len(content))})

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
    assert requests[0] == ("HEAD", None)
    assert any(method == "GET" and range_header is not None for method, range_header in requests)
    assert requests[-1] == ("GET", None)


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


def test_file_browser_service_updates_transfer_settings_for_future_uploads(tmp_path: Path) -> None:
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
        }
    ]


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
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")

    service = FileBrowserService(  # type: ignore[arg-type]
        FakeClient(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    item = WopanItem(
        item_id="file-1",
        name="report.txt",
        kind=WopanItemKind.FILE,
        download_id="fid-1",
    )
    local_path = tmp_path / "report.txt"

    with pytest.raises(FileBrowserError, match="HTTP 404"):
        service.download_file(item, local_path)

    assert not local_path.exists()
    assert not local_path.with_name("report.txt.part").exists()


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
        if str(request.url).endswith("/expired"):
            return httpx.Response(403)
        return httpx.Response(200, content=content, headers={"Content-Length": str(len(content))})

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
        def upload_file(
            self, parent_id: str, local_path: Path, **_kwargs: object
        ) -> WopanItem:
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
