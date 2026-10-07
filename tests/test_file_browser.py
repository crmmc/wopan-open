from __future__ import annotations

import os
import threading
import time
import types
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest

from openwopan.app.file_browser import (
    FileBrowserError,
    FileBrowserLoginRequiredError,
    FileBrowserService,
    FileBrowserUploadCancelledError,
    plan_transfer_batch,
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
    MergeUploadEstimate,
    UploadTaskState,
    UploadTaskStore,
    make_upload_task_id,
    server_file_name,
)
from openwopan.wopan.client import ROOT_DIRECTORY_ID, UploadResumeContext
from openwopan.wopan.errors import (
    WopanAuthenticationError,
    WopanBusinessError,
    WopanUploadCancelledError,
)
from openwopan.wopan.models import (
    DownloadInfo,
    WopanCloudUsage,
    WopanItem,
    WopanItemKind,
    WopanRecycleItem,
)


class FakeClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.requested_parent_ids: list[str] = []
        self.searched_keywords: list[tuple[str, int, int]] = []
        self.listings: dict[str, list[WopanItem]] = {}
        self.resolved_directory_ids: list[str] = []
        self.directory_paths: dict[str, list[tuple[str, str]]] = {}
        self.created_folders: list[tuple[str, str]] = []
        self.renamed_items: list[tuple[str, str, WopanItemKind, str | None]] = []
        self.deleted_items: list[tuple[str, WopanItemKind]] = []
        self.moved_items: list[tuple[str, WopanItemKind, str]] = []
        self.copied_items: list[tuple[str, WopanItemKind, str]] = []
        self.downloaded_item_ids: list[str] = []
        self.uploaded_files: list[tuple[str, Path]] = []
        self.upload_kwargs: list[dict[str, object]] = []
        self.usage_account_ids: list[str] = []
        self.listed_recycle_bin = False
        self.restored_delete_nos: list[tuple[str, ...]] = []
        self.purged_delete_nos: list[tuple[str, ...]] = []
        self.emptied_recycle_bin = False

    def list_files(self, parent_id: str) -> list[WopanItem]:
        self.requested_parent_ids.append(parent_id)
        if self.error is not None:
            raise self.error
        default = [WopanItem(item_id="folder-1", name="Folder", kind=WopanItemKind.FOLDER)]
        return list(self.listings.get(parent_id, default))

    def search_files(
        self, keyword: str, page_no: int = 1, page_size: int = 50
    ) -> list[WopanItem]:
        self.searched_keywords.append((keyword, page_no, page_size))
        if self.error is not None:
            raise self.error
        return [
            WopanItem(item_id="file-9", name=f"{keyword}.txt", kind=WopanItemKind.FILE)
        ]

    def get_directory_path(self, directory_id: str) -> list[tuple[str, str]]:
        self.resolved_directory_ids.append(directory_id)
        if self.error is not None:
            raise self.error
        return list(self.directory_paths.get(directory_id, []))

    def create_folder(self, parent_id: str, name: str, *, reuse_existing: bool = False) -> WopanItem:
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

    def delete_many(self, items: Sequence[tuple[str, WopanItemKind]]) -> None:
        for item_id, kind in items:
            self.delete(item_id, kind)

    def move(self, item_id: str, kind: WopanItemKind, target_parent_id: str) -> None:
        self.moved_items.append((item_id, kind, target_parent_id))
        if self.error is not None:
            raise self.error

    def move_many(
        self, items: Sequence[tuple[str, WopanItemKind]], target_parent_id: str
    ) -> None:
        for item_id, kind in items:
            self.move(item_id, kind, target_parent_id)

    def copy(self, item_id: str, kind: WopanItemKind, target_parent_id: str) -> None:
        self.copied_items.append((item_id, kind, target_parent_id))
        if self.error is not None:
            raise self.error

    def copy_many(self, items: Sequence[tuple[str, WopanItemKind]], target_parent_id: str) -> None:
        for item_id, kind in items:
            self.copy(item_id, kind, target_parent_id)

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

    def list_recycle_items(self) -> list[WopanRecycleItem]:
        self.listed_recycle_bin = True
        if self.error is not None:
            raise self.error
        return [
            WopanRecycleItem(
                delete_no="d-1",
                item_id="item-1",
                name="report.txt",
                kind=WopanItemKind.FILE,
                keep_days=30,
            )
        ]

    def restore_recycle_items(self, delete_nos: Sequence[str]) -> None:
        self.restored_delete_nos.append(tuple(delete_nos))
        if self.error is not None:
            raise self.error

    def purge_recycle_items(self, delete_nos: Sequence[str]) -> None:
        self.purged_delete_nos.append(tuple(delete_nos))
        if self.error is not None:
            raise self.error

    def empty_recycle_bin(self) -> None:
        self.emptied_recycle_bin = True
        if self.error is not None:
            raise self.error


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


def test_file_browser_service_deletes_many_items_in_one_request() -> None:
    client = FakeClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]
    folder = WopanItem(item_id="folder-1", name="Folder", kind=WopanItemKind.FOLDER)
    file_item = WopanItem(
        item_id="file-1", name="report.txt", kind=WopanItemKind.FILE, file_type="4"
    )

    service.delete_items([folder, file_item])

    assert client.deleted_items == [
        ("folder-1", WopanItemKind.FOLDER),
        ("file-1", WopanItemKind.FILE),
    ]


def test_file_browser_service_moves_many_items_in_one_request() -> None:
    client = FakeClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]
    file_item = WopanItem(
        item_id="file-1", name="report.txt", kind=WopanItemKind.FILE, file_type="4"
    )
    folder = WopanItem(item_id="folder-1", name="Folder", kind=WopanItemKind.FOLDER)

    service.move_items([file_item, folder], "folder-2")

    assert client.moved_items == [
        ("file-1", WopanItemKind.FILE, "folder-2"),
        ("folder-1", WopanItemKind.FOLDER, "folder-2"),
    ]


def test_file_browser_service_copies_many_items_in_one_request() -> None:
    client = FakeClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]
    file_item = WopanItem(
        item_id="file-1", name="report.txt", kind=WopanItemKind.FILE, file_type="4"
    )
    folder = WopanItem(item_id="folder-1", name="Folder", kind=WopanItemKind.FOLDER)

    service.copy_items([file_item, folder], "folder-2")

    assert client.copied_items == [
        ("file-1", WopanItemKind.FILE, "folder-2"),
        ("folder-1", WopanItemKind.FOLDER, "folder-2"),
    ]


def test_plan_transfer_batch_splits_conflicts_noops_and_transfers() -> None:
    items = (
        WopanItem(item_id="file-1", name="a.txt", kind=WopanItemKind.FILE),
        WopanItem(item_id="folder-1", name="docs", kind=WopanItemKind.FOLDER),
        WopanItem(item_id="file-2", name="b.txt", kind=WopanItemKind.FILE),
    )
    target_items = (
        WopanItem(item_id="target-file", name="a.txt", kind=WopanItemKind.FILE),
        WopanItem(item_id="file-2", name="b.txt", kind=WopanItemKind.FILE),
    )

    plan = plan_transfer_batch(items, target_items)

    assert plan.transfer_items == (items[1],)
    assert plan.conflict_names == ("a.txt",)
    assert plan.noop_ids == frozenset({"file-2"})


def test_plan_transfer_batch_without_conflicts_transfers_everything() -> None:
    items = (
        WopanItem(item_id="file-1", name="a.txt", kind=WopanItemKind.FILE),
        WopanItem(item_id="folder-1", name="docs", kind=WopanItemKind.FOLDER),
    )

    plan = plan_transfer_batch(items, ())

    assert plan.transfer_items == items
    assert plan.conflict_names == ()
    assert plan.noop_ids == frozenset()


def test_file_browser_service_searches_files() -> None:
    client = FakeClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]

    items = service.search_files("report", page_no=2, page_size=25)

    assert client.searched_keywords == [("report", 2, 25)]
    assert [item.name for item in items] == ["report.txt"]


def test_resolve_directory_path_uses_endpoint_and_caches() -> None:
    client = FakeClient()
    # client.get_directory_path 已归一化为根 -> 目标序
    client.directory_paths["folder-2"] = [
        (ROOT_DIRECTORY_ID, "个人云"),
        ("folder-1", "Folder"),
        ("folder-2", "2"),
    ]
    service = FileBrowserService(client)  # type: ignore[arg-type]

    path = service.resolve_directory_path("folder-2")

    assert path == [
        (ROOT_DIRECTORY_ID, "个人云"),
        ("folder-1", "Folder"),
        ("folder-2", "2"),
    ]
    assert client.resolved_directory_ids == ["folder-2"]

    # 二次解析命中缓存，零网络调用
    assert service.resolve_directory_path("folder-2") == path
    assert client.resolved_directory_ids == ["folder-2"]


def test_resolve_directory_path_shortcuts_root() -> None:
    client = FakeClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]

    assert service.resolve_directory_path(ROOT_DIRECTORY_ID) == [
        (ROOT_DIRECTORY_ID, "个人云")
    ]
    assert client.resolved_directory_ids == []


def test_resolve_directory_path_rejects_empty_id() -> None:
    service = FileBrowserService(FakeClient())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="directory_id"):
        service.resolve_directory_path("")


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


@pytest.mark.parametrize(
    ("sha256", "expected"),
    [("abc123def", "abc123def"), (None, None)],
    ids=["with-sha256", "without-sha256"],
)
def test_file_browser_service_passes_expected_sha256_to_downloader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sha256: str | None,
    expected: str | None,
) -> None:
    captured: dict[str, object] = {}

    def fake_download_url(http_client, url, local_path, **kwargs):
        captured.update(kwargs)
        return DownloadResult(
            status="已完成", task_id=kwargs["task_id"], local_path=local_path
        )

    monkeypatch.setattr("openwopan.app.file_browser.download_url", fake_download_url)
    service = FileBrowserService(FakeClient())  # type: ignore[arg-type]
    item = WopanItem(
        item_id="file-1",
        name="report.bin",
        kind=WopanItemKind.FILE,
        download_id="fid-1",
        sha256=sha256,
    )
    local_path = tmp_path / "report.bin"

    result = service.download_file(item, local_path)

    assert result.status == "已完成"
    assert captured["expected_sha256"] == expected
    assert captured["download_id"] == "fid-1"


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
            "quick_transfer": True,
        }
    ]


def test_file_browser_service_quick_transfer_setting_passes_through(tmp_path: Path) -> None:
    """秒传开关随设置传递：关闭后客户端收到 quick_transfer=False。"""
    client = FakeClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]
    service.update_settings(AppSettings(enable_quick_transfer=False))
    local_path = tmp_path / "upload.txt"
    local_path.write_bytes(b"upload-content")

    service.upload_file("folder-1", local_path)

    assert client.upload_kwargs[0]["quick_transfer"] is False


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


def test_file_browser_service_lists_recycle_items() -> None:
    client = FakeClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]

    items = service.list_recycle_items()

    assert client.listed_recycle_bin is True
    assert [item.delete_no for item in items] == ["d-1"]
    assert items[0].keep_days == 30


def test_file_browser_service_forwards_recycle_mutations() -> None:
    client = FakeClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]

    service.restore_recycle_items(["d-1", "d-2"])
    service.purge_recycle_items(["d-3"])
    service.empty_recycle_bin()

    assert client.restored_delete_nos == [("d-1", "d-2")]
    assert client.purged_delete_nos == [("d-3",)]
    assert client.emptied_recycle_bin is True


def test_file_browser_service_maps_recycle_login_expiry() -> None:
    client = FakeClient(WopanAuthenticationError("expired"))
    service = FileBrowserService(client)  # type: ignore[arg-type]

    with pytest.raises(FileBrowserLoginRequiredError, match="重新登录"):
        service.list_recycle_items()


def test_file_browser_service_maps_recycle_protocol_errors() -> None:
    client = FakeClient(WopanBusinessError("9999", "failed"))
    service = FileBrowserService(client)  # type: ignore[arg-type]

    with pytest.raises(FileBrowserError, match="failed"):
        service.purge_recycle_items(["d-1"])


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

    def create_folder(self, parent_id: str, name: str, *, reuse_existing: bool = False) -> WopanItem:
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


class _MergeCloudClient(FakeClient):
    """Cloud double whose listings carry folders and sized files (merge tests).

    ``create_folder(reuse_existing=True)`` emulates the server merge
    contract ``prepare_folder_upload`` now relies on: a same-name folder
    (matched by stored name, long names truncated) is reused and its id
    returned without creating anything; everything else creates a folder.
    Unverified edge kept out of the double: a same-name FILE occupying the
    name (real-server behavior unknown until UAT) falls through to create.
    """

    def __init__(self) -> None:
        super().__init__()
        self.entries: dict[str, list[WopanItem]] = {}
        self.created: list[tuple[str, str]] = []
        self.merged: list[tuple[str, str]] = []
        self.listed_dirs: list[str] = []

    def list_files(self, parent_id: str) -> list[WopanItem]:
        self.listed_dirs.append(parent_id)
        return list(self.entries.get(parent_id, []))

    def create_folder(
        self, parent_id: str, name: str, *, reuse_existing: bool = False
    ) -> WopanItem:
        if reuse_existing:
            for item in self.entries.get(parent_id, []):
                if item.kind is not WopanItemKind.FOLDER:
                    continue
                if name == item.name or server_file_name(name) == item.name:
                    self.merged.append((parent_id, name))
                    return item
        self.created.append((parent_id, name))
        item = WopanItem(
            item_id=f"new-dir-{len(self.created)}",
            name=name,
            kind=WopanItemKind.FOLDER,
            parent_id=parent_id,
        )
        self.entries.setdefault(parent_id, []).append(item)
        return item


def test_prepare_folder_upload_merge_reuses_dirs_and_skips_completed_files(
    tmp_path: Path,
) -> None:
    """合并模式：同名根/子目录复用不重建，同名同大小文件跳过，只补缺失。"""
    client, local_root = _merge_tree_fixture(tmp_path)

    service = FileBrowserService(client)  # type: ignore[arg-type]
    job = service.prepare_folder_upload("0", local_root, root_name="photos", merge=True)

    assert job.root_item_id == "cloud-photos"  # 根目录复用
    # 只新建了缺失的 season3；photos/season1/season2 都未重建
    assert client.created == [("cloud-photos", "season3")]

    planned = {(f.target_dir_id, f.name): f.size for f in job.files}
    assert ("cloud-s1", "ep2.mkv") in planned  # 缺失文件补传
    assert ("cloud-s2", "new.mkv") in planned  # 上传进复用目录
    assert any(dir_id.startswith("new-dir-") and name == "x.mkv" for (dir_id, name) in planned)
    assert all(name != "ep1.mkv" for (_dir_id, name) in planned)  # 同名同大小跳过
    # 同名不同大小 → 副本名补传
    assert any(name.startswith("ep3") and "copy" in name for (_dir_id, name) in planned)


def test_prepare_folder_upload_merge_delegates_dir_reuse_to_server(tmp_path: Path) -> None:
    """合并模式目录复用走服务端合并契约：根/子目录 id 一律来自
    create_folder(reuse_existing=True)，不再预列父目录按名字猜测。"""
    client, local_root = _merge_tree_fixture(tmp_path)
    service = FileBrowserService(client)  # type: ignore[arg-type]

    job = service.prepare_folder_upload("0", local_root, root_name="photos", merge=True)

    assert client.merged == [
        ("0", "photos"),
        ("cloud-photos", "season1"),
        ("cloud-photos", "season2"),
    ]
    assert client.created == [("cloud-photos", "season3")]  # 缺的目录仍新建
    assert job.root_item_id == "cloud-photos"
    # 父目录不列（"0"/"cloud-photos" 都不含文件）；列目录只发生在含文件
    # 的目录上，为文件级跳过/副本名服务。
    assert client.listed_dirs == ["cloud-s1", "cloud-s2", "new-dir-1"]


def test_prepare_merge_long_dir_name_merges_by_server_stored_form(tmp_path: Path) -> None:
    """合并模式下超长目录名交给服务端按存储形态合并：截断存储的云端
    目录照样复用，不需要客户端列目录比对名字。"""
    client = _MergeCloudClient()
    long_dir = "a" * 99 + ".dir"  # 103 字符，服务端存 100 字符截断名
    stored_dir = server_file_name(long_dir)
    cloud_root = WopanItem(item_id="r", name="photos", kind=WopanItemKind.FOLDER)
    cloud_sub = WopanItem(
        item_id="sub", name=stored_dir, kind=WopanItemKind.FOLDER, parent_id="r"
    )
    done = WopanItem(
        item_id="f1",
        name="ep1.mkv",
        kind=WopanItemKind.FILE,
        parent_id="sub",
        size=100,
        download_id="fid-1",
    )
    client.entries["0"] = [cloud_root]
    client.entries["r"] = [cloud_sub]
    client.entries["sub"] = [done]
    local_root = tmp_path / "photos"
    (local_root / long_dir).mkdir(parents=True)
    (local_root / long_dir / "ep1.mkv").write_bytes(b"a" * 100)
    service = FileBrowserService(client)  # type: ignore[arg-type]

    job = service.prepare_folder_upload("0", local_root, root_name="photos", merge=True)

    assert client.merged == [("0", "photos"), ("r", long_dir)]  # 长名子目录复用
    assert client.created == []
    assert job.files == ()  # 同名同大小文件跳过


def test_prepare_folder_upload_without_merge_rejects_existing_root(tmp_path: Path) -> None:
    """非合并模式保持原语义：根目录同名直接报错。"""
    client = _MergeCloudClient()
    client.entries["0"] = [
        WopanItem(item_id="cloud-photos", name="photos", kind=WopanItemKind.FOLDER)
    ]
    local_root = tmp_path / "photos"
    local_root.mkdir()
    (local_root / "a.mkv").write_bytes(b"x")
    service = FileBrowserService(client)  # type: ignore[arg-type]

    with pytest.raises(FileBrowserError, match="上传目标已存在"):
        service.prepare_folder_upload("0", local_root, root_name="photos")


def _merge_tree_fixture(tmp_path: Path) -> tuple[_MergeCloudClient, Path]:
    client = _MergeCloudClient()
    cloud_root = WopanItem(item_id="cloud-photos", name="photos", kind=WopanItemKind.FOLDER)
    cloud_s1 = WopanItem(
        item_id="cloud-s1", name="season1", kind=WopanItemKind.FOLDER, parent_id="cloud-photos"
    )
    cloud_s2 = WopanItem(
        item_id="cloud-s2", name="season2", kind=WopanItemKind.FOLDER, parent_id="cloud-photos"
    )
    done_ep1 = WopanItem(
        item_id="f1",
        name="ep1.mkv",
        kind=WopanItemKind.FILE,
        parent_id="cloud-s1",
        size=100,
        download_id="fid-1",
    )
    different_ep3 = WopanItem(
        item_id="f2",
        name="ep3.mkv",
        kind=WopanItemKind.FILE,
        parent_id="cloud-s1",
        size=999,
        download_id="fid-2",
    )
    client.entries["0"] = [cloud_root]
    client.entries["cloud-photos"] = [cloud_s1, cloud_s2]
    client.entries["cloud-s1"] = [done_ep1, different_ep3]

    local_root = tmp_path / "photos"
    (local_root / "season1").mkdir(parents=True)
    (local_root / "season2").mkdir()
    (local_root / "season3").mkdir()
    (local_root / "season1" / "ep1.mkv").write_bytes(b"a" * 100)
    (local_root / "season1" / "ep2.mkv").write_bytes(b"b" * 200)
    (local_root / "season1" / "ep3.mkv").write_bytes(b"c" * 300)
    (local_root / "season2" / "new.mkv").write_bytes(b"d" * 50)
    (local_root / "season3" / "x.mkv").write_bytes(b"e" * 10)
    return client, local_root


def test_estimate_merge_uploads_matches_prepare_skip_rules(tmp_path: Path) -> None:
    """估算与 prepare(merge) 的跳过判定同源：同名同大小跳过、其余新增，
    且完全只读（不创建任何云端内容）。"""
    client, local_root = _merge_tree_fixture(tmp_path)
    service = FileBrowserService(client)  # type: ignore[arg-type]

    estimate = service.estimate_merge_uploads("0", local_root, root_name="photos")

    # ep1 同名同大小跳过；ep2 缺失、ep3 大小不同、season2/new、season3/x 新增
    assert estimate.files_to_upload == 4
    assert estimate.files_skipped == 1
    assert client.created == []  # 只读：不建目录

    # 估算数与真实 prepare 的计划一致
    job = service.prepare_folder_upload("0", local_root, root_name="photos", merge=True)
    assert len(job.files) == estimate.files_to_upload


def test_estimate_merge_uploads_root_missing_counts_everything(tmp_path: Path) -> None:
    """同名根目录不存在：整棵树全量新增。"""
    client, local_root = _merge_tree_fixture(tmp_path)
    client.entries["0"] = []  # 云端没有 photos
    service = FileBrowserService(client)  # type: ignore[arg-type]

    estimate = service.estimate_merge_uploads("0", local_root, root_name="photos")

    assert estimate.files_to_upload == 5
    assert estimate.files_skipped == 0


def _truncated_name_fixture(tmp_path: Path, cloud_size: int) -> tuple[_MergeCloudClient, Path]:
    """本地 103 字符长名文件 vs 云端 100 字符截断名存储。"""
    client = _MergeCloudClient()
    long_name = "a" * 99 + ".mkv"  # 主名 99 + ".mkv" = 103
    stored_name = server_file_name(long_name)
    cloud_root = WopanItem(item_id="cloud-photos", name="photos", kind=WopanItemKind.FOLDER)
    stored_file = WopanItem(
        item_id="f1",
        name=stored_name,
        kind=WopanItemKind.FILE,
        parent_id="cloud-photos",
        size=cloud_size,
        download_id="fid-1",
    )
    client.entries["0"] = [cloud_root]
    client.entries["cloud-photos"] = [stored_file]

    local_root = tmp_path / "photos"
    local_root.mkdir()
    (local_root / long_name).write_bytes(b"a" * 100)
    return client, local_root


def test_prepare_merge_skips_files_stored_with_truncated_names(tmp_path: Path) -> None:
    """长名文件上传后云端以截断名存储：合并模式按存储形态识别为已存在。

    UAT 2026-10-06：服务端把超过 100 字符的文件名截断（主名保留至总长
    100），此前按精确名比对会误判"缺失"而反复重传。
    """
    client, local_root = _truncated_name_fixture(tmp_path, cloud_size=100)
    service = FileBrowserService(client)  # type: ignore[arg-type]

    job = service.prepare_folder_upload("0", local_root, root_name="photos", merge=True)
    estimate = service.estimate_merge_uploads("0", local_root, root_name="photos")

    assert job.files == ()  # 截断名 + 同大小 → 已上传，跳过
    assert client.created == []  # 根目录复用，不重建
    assert estimate == MergeUploadEstimate(files_to_upload=0, files_skipped=1)


def test_prepare_merge_truncated_name_size_mismatch_plans_fitting_copy(
    tmp_path: Path,
) -> None:
    """截断名同名但大小不同 → 补传副本名，且副本名必须在服务端限制内。"""
    client, local_root = _truncated_name_fixture(tmp_path, cloud_size=999)
    service = FileBrowserService(client)  # type: ignore[arg-type]

    job = service.prepare_folder_upload("0", local_root, root_name="photos", merge=True)
    estimate = service.estimate_merge_uploads("0", local_root, root_name="photos")

    (planned,) = job.files
    assert len(planned.name) <= 100  # (copy) 标记不能落在截断点之后
    assert "(copy)" in planned.name
    assert estimate == MergeUploadEstimate(files_to_upload=1, files_skipped=0)


def test_prepare_folder_upload_long_dotted_dir_names_do_not_collide(
    tmp_path: Path,
) -> None:
    """同 96 前缀的两个长名子目录：按服务端存储形态去重，第二个用副本名。

    目录名只在含扩展名点时适用截断规则；此处用带点目录名构造碰撞。
    """
    client = _MergeCloudClient()
    local_root = tmp_path / "fresh"
    long_a = "a" * 96 + "XXX" + ".dir"  # 103 字符，存储形态与 long_a/b 相同
    long_b = "a" * 96 + "YYY" + ".dir"
    (local_root / long_a).mkdir(parents=True)
    (local_root / long_b).mkdir()
    (local_root / long_a / "f.mkv").write_bytes(b"a")
    (local_root / long_b / "g.mkv").write_bytes(b"b")
    service = FileBrowserService(client)  # type: ignore[arg-type]

    job = service.prepare_folder_upload("0", local_root)  # 非合并、不重命名

    created_names = [name for (_parent, name) in client.created]
    assert len(created_names) == 3  # 根目录 + 两个子目录
    second_dir = created_names[2]
    assert len(second_dir) <= 100  # 副本名落在服务端限制内
    assert "(copy)" in second_dir
    assert len(job.files) == 2


def test_upload_file_precheck_matches_truncated_cloud_names(tmp_path: Path) -> None:
    """单文件上传前置检查：云端已有截断名 → 启动即拒绝，不再传完 N-1 片后 500。"""
    client = _MergeCloudClient()
    long_name = "a" * 99 + ".mkv"
    stored = WopanItem(
        item_id="f1",
        name=server_file_name(long_name),
        kind=WopanItemKind.FILE,
        parent_id="0",
        size=100,
        download_id="fid-1",
    )
    client.entries["0"] = [stored]
    local = tmp_path / long_name
    local.write_bytes(b"a" * 100)
    service = FileBrowserService(client)  # type: ignore[arg-type]

    with pytest.raises(FileBrowserError, match="上传目标已存在"):
        service.upload_file("0", local, upload_name=long_name)


def test_prepare_folder_upload_stops_between_cloud_creates(tmp_path: Path) -> None:
    client = FolderUploadFakeClient()
    service = FileBrowserService(client)  # type: ignore[arg-type]
    local_root = _make_local_tree(tmp_path)
    stopped = False
    create_folder = client.create_folder

    def stop_after_root(parent_id: str, name: str, *, reuse_existing: bool = False) -> WopanItem:
        nonlocal stopped
        item = create_folder(parent_id, name, reuse_existing=reuse_existing)
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
        def create_folder(
            self, parent_id: str, name: str, *, reuse_existing: bool = False
        ) -> WopanItem:
            if self.created_folders:
                raise WopanBusinessError("0001", "denied")
            return super().create_folder(parent_id, name, reuse_existing=reuse_existing)

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


def test_service_upload_cancel_preserves_session_when_abandon_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关闭退出的取消（cancel 回调带 preserve_session）保留会话分片；
    普通用户取消仍删除会话。"""
    monkeypatch.setattr("openwopan.wopan.client.BYTES_PER_MB", 1)
    service, client, store = _resume_service(tmp_path)
    client.part_results = [(1, "fid-1"), (2, "fid-2")]
    client.upload_failure = WopanUploadCancelledError("client cancelled")
    local_path = _three_part_file(tmp_path)

    def abandon_cancel() -> bool:
        return False

    abandon_cancel.preserve_session = True  # type: ignore[attr-defined]

    with pytest.raises(FileBrowserUploadCancelledError):
        service.upload_file("folder-1", local_path, cancel_requested=abandon_cancel)

    task_id = make_upload_task_id("folder-1", local_path, None)
    state = _state(store, task_id)
    assert state is not None
    assert state.completed_indexes == [1, 2]
    # 关闭退出 ≠ 取消：会话归一为「已暂停（中断）」而非删除，重启后
    # was_active=False，由用户手动继续而非自动续传。
    assert state.status == "已暂停"
    assert "应用中断" in state.error and "2/3" in state.error

    with pytest.raises(FileBrowserUploadCancelledError):
        service.upload_file("folder-1", local_path, cancel_requested=lambda: False)

    assert _state(store, task_id) is None


def test_service_upload_restarts_fresh_when_session_rejected_with_5xx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """服务端拒绝续传会话（5xx 且零新分片）：丢弃旧会话整文件重传一次。

    UAT 2026-10-06：130/131 分片的会话续传最后一片时服务端持续回
    HTTP 500，原路径反复用同一会话重试永远失败。
    """
    monkeypatch.setattr("openwopan.wopan.client.BYTES_PER_MB", 1)

    class _RejectResumeClient(ResumeAwareUploadClient):
        def __init__(self) -> None:
            super().__init__()
            self.reject_resumes = False
            self._request = httpx.Request(
                "POST", "https://upload.example/openapi/client/upload2C"
            )
            self._response = httpx.Response(500, request=self._request)

        def upload_file(self, parent_id: str, local_path: Path, **kwargs: object) -> WopanItem:
            resume = kwargs.get("resume")
            if (
                self.reject_resumes
                and isinstance(resume, UploadResumeContext)
                and resume.completed_indexes
            ):
                self.uploaded_files.append((parent_id, local_path))
                self.upload_kwargs.append(kwargs)
                raise httpx.HTTPStatusError(
                    "Server Error", request=self._request, response=self._response
                )
            return super().upload_file(parent_id, local_path, **kwargs)

    client = _RejectResumeClient()
    service, client, store = _resume_service(tmp_path, client=client)
    client.part_results = [(1, "fid-1")]
    client.upload_failure = WopanBusinessError("9999", "busy")
    local_path = _three_part_file(tmp_path)

    with pytest.raises(FileBrowserError):
        service.upload_file("folder-1", local_path)

    client.upload_failure = None
    client.reject_resumes = True
    client.part_results = []  # 本次尝试零新分片确认
    item = service.upload_file("folder-1", local_path)

    assert item.item_id == "uploaded-file"
    assert len(client.upload_kwargs) == 3
    second_resume = client.upload_kwargs[1]["resume"]
    third_resume = client.upload_kwargs[2]["resume"]
    assert isinstance(second_resume, UploadResumeContext)
    assert isinstance(third_resume, UploadResumeContext)
    assert second_resume.completed_indexes == frozenset({1})
    assert third_resume.completed_indexes == frozenset()
    assert third_resume.unique_id != second_resume.unique_id
    task_id = make_upload_task_id("folder-1", local_path, None)
    assert _state(store, task_id) is None


def test_restart_rejected_session_keeps_parts_when_stat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """会话重传启动前文件消失：报可读错误且旧会话分片保留（不被删除）。"""
    monkeypatch.setattr("openwopan.wopan.client.BYTES_PER_MB", 1)

    class _RejectAndVanishClient(ResumeAwareUploadClient):
        def __init__(self) -> None:
            super().__init__()
            self.reject = False
            self._request = httpx.Request("POST", "https://upload.example/x")
            self._response = httpx.Response(500, request=self._request)

        def upload_file(self, parent_id: str, local_path: Path, **kwargs: object) -> WopanItem:
            resume = kwargs.get("resume")
            if (
                self.reject
                and isinstance(resume, UploadResumeContext)
                and resume.completed_indexes
            ):
                local_path.unlink()
                raise httpx.HTTPStatusError(
                    "Server Error", request=self._request, response=self._response
                )
            return super().upload_file(parent_id, local_path, **kwargs)

    client = _RejectAndVanishClient()
    service, client, store = _resume_service(tmp_path, client=client)
    local_path = tmp_path / "report.bin"
    local_path.write_bytes(b"012345678901234")
    client.part_results = [(1, "fid-1")]
    client.upload_failure = WopanBusinessError("9999", "busy")
    with pytest.raises(FileBrowserError):
        service.upload_file("folder-1", local_path)

    client.upload_failure = None
    client.reject = True
    client.part_results = []
    with pytest.raises(FileBrowserError, match="无法读取本地文件"):
        service.upload_file("folder-1", local_path)

    state = store.load(make_upload_task_id("folder-1", local_path, None))
    assert state is not None
    assert state.completed_indexes == [1]  # 旧会话断点未丢


def test_service_upload_keeps_session_when_new_parts_confirmed_before_5xx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5xx 前已确认新分片：保留会话走正常失败-续传，不整文件重传。"""
    monkeypatch.setattr("openwopan.wopan.client.BYTES_PER_MB", 1)
    service, client, store = _resume_service(tmp_path)
    client.part_results = [(1, "fid-1")]
    client.upload_failure = WopanBusinessError("9999", "busy")
    local_path = _three_part_file(tmp_path)

    with pytest.raises(FileBrowserError):
        service.upload_file("folder-1", local_path)

    request = httpx.Request("POST", "https://upload.example/openapi/client/upload2C")
    response = httpx.Response(503, request=request)
    client.upload_failure = httpx.HTTPStatusError(
        "Service Unavailable", request=request, response=response
    )
    client.part_results = [(2, "fid-2")]  # 本次尝试确认了新分片
    with pytest.raises(FileBrowserError, match="503"):
        service.upload_file("folder-1", local_path)

    assert len(client.upload_kwargs) == 2  # 没有第三次整文件重传
    task_id = make_upload_task_id("folder-1", local_path, None)
    state = _state(store, task_id)
    assert state is not None
    assert state.completed_indexes == [1, 2]
    assert state.status == "失败"


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
    assert record.status == "已暂停"
    assert record.resumable is True
    assert record.completed_parts == 1
    assert record.total_parts == 3
    assert "应用中断" in record.error
    assert "已暂停" in record.error
    assert "1/3" in record.error
    assert _state(store, "kept-task") is not None
    assert _state(store, "kept-task").status == "已暂停"  # type: ignore[union-attr]
    assert _state(store, "finished-task") is None
    assert _state(store, "missing-task") is None


def test_recover_uploads_skips_session_deleted_mid_recovery(tmp_path: Path) -> None:
    """恢复循环中会话被并发删除（保留期清理竞态）时不炸、不产记录。"""
    store = UploadTaskStore(root_path=tmp_path)
    local_path = tmp_path / "kept.bin"
    local_path.write_bytes(b"data")
    store.save(
        UploadTaskState(
            task_id="kept-task",
            file_name="kept.bin",
            local_path=local_path,
            parent_id="folder-1",
            upload_name=None,
            file_size=3,
            file_mtime=1.0,
            part_size=5,
            total_parts=1,
            unique_id="u1",
            batch_no="b1",
        )
    )
    service = FileBrowserService(FakeClient(), upload_store=store)  # type: ignore[arg-type]

    original_update = store.update

    def update_then_delete(task_id: str, change: object) -> object:
        # 模拟清理线程在 load_all 之后、update 之前删除了会话
        store.delete(task_id)
        return original_update(task_id, change)  # type: ignore[arg-type,return-value]

    store.update = update_then_delete  # type: ignore[method-assign]

    assert service.recover_uploads() == ()


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


def test_discard_upload_sessions_deletes_states(tmp_path: Path) -> None:
    """B6 R6：按 task_id 丢弃持久化会话；无 store 时为 no-op。"""
    store = UploadTaskStore(root_path=tmp_path)
    local_path = tmp_path / "a.bin"
    local_path.write_bytes(b"data")
    for task_id in ("task-a", "task-b"):
        store.save(
            UploadTaskState(
                task_id=task_id,
                file_name=f"{task_id}.bin",
                local_path=local_path,
                parent_id="folder-1",
                upload_name=None,
                file_size=1,
                file_mtime=1.0,
                part_size=5,
                total_parts=1,
                unique_id=f"u-{task_id}",
                batch_no="b1",
            )
        )
    service = FileBrowserService(FakeClient(), upload_store=store)  # type: ignore[arg-type]

    service.discard_upload_sessions(["task-a", "", "task-b"])

    assert _state(store, "task-a") is None
    assert _state(store, "task-b") is None


def test_discard_upload_sessions_without_store_is_noop() -> None:
    service = FileBrowserService(FakeClient())  # type: ignore[arg-type]

    service.discard_upload_sessions(["task-a"])

    assert service.recover_uploads() == ()
