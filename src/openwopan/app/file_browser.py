from __future__ import annotations

import logging
from collections.abc import Callable
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
    DownloadTaskRecord,
    DownloadTaskStore,
    download_url,
    make_download_task_id,
)
from openwopan.wopan.client import ORIGIN, REFERER, ROOT_DIRECTORY_ID, WopanClient
from openwopan.wopan.errors import WopanAuthenticationError, WopanError
from openwopan.wopan.models import WopanCloudUsage, WopanItem, WopanItemKind

LOGGER = logging.getLogger(__name__)
DownloadProgressCallback = Callable[[int, int | None], None]
DownloadStatusCallback = Callable[[DownloadStatus], None]
DownloadConnectionCallback = Callable[[int, int], None]


class FileBrowserError(Exception):
    """Base error for UI-facing file browser failures."""


class FileBrowserLoginRequiredError(FileBrowserError):
    """Raised when the file browser needs the user to log in again."""


class FileBrowserBackend(Protocol):
    """UI-facing file browser boundary."""

    def list_directory(self, parent_id: str = ROOT_DIRECTORY_ID) -> list[WopanItem]:
        """Return file items for a directory."""

    def create_folder(self, parent_id: str, name: str) -> WopanItem:
        """Create a folder and return the created item."""

    def rename_item(self, item: WopanItem, new_name: str) -> None:
        """Rename a file or folder."""

    def delete_item(self, item: WopanItem) -> None:
        """Delete a file or folder."""

    def move_item(self, item: WopanItem, target_parent_id: str) -> None:
        """Move a file or folder."""

    def download_file(
        self,
        item: WopanItem,
        local_path: Path,
        progress_callback: DownloadProgressCallback | None = None,
        *,
        status_callback: DownloadStatusCallback | None = None,
        connection_callback: DownloadConnectionCallback | None = None,
        control: DownloadTaskControl | None = None,
        task_id: str | None = None,
    ) -> DownloadResult | None:
        """Download one file to a local path."""

    def upload_file(self, parent_id: str, local_path: Path) -> WopanItem:
        """Upload one local file to a directory."""

    def get_cloud_usage(self, account_id: str) -> WopanCloudUsage:
        """Return cloud storage usage for the current account."""


class FileBrowserService:
    """Application service that exposes WoPan file browsing through OpenWoPan models."""

    def __init__(
        self,
        client: WopanClient,
        http_client: httpx.Client | None = None,
        settings: AppSettings | None = None,
        download_store: DownloadTaskStore | None = None,
    ) -> None:
        self._client = client
        self._settings = settings or AppSettings()
        self._download_store = download_store or DownloadTaskStore()
        self._http_client = http_client or httpx.Client(
            headers={"Origin": ORIGIN, "Referer": REFERER},
            follow_redirects=True,
            timeout=httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0),
        )

    def list_directory(self, parent_id: str = ROOT_DIRECTORY_ID) -> list[WopanItem]:
        """List a directory and map protocol authentication failures to UI state."""
        LOGGER.info("file_browser.list_directory.start parent_id=%s", parent_id)
        items = self._call(lambda: self._client.list_files(parent_id))
        LOGGER.info(
            "file_browser.list_directory.success parent_id=%s item_count=%s",
            parent_id,
            len(items),
        )
        return items

    def create_folder(self, parent_id: str, name: str) -> WopanItem:
        """Create a folder in a directory."""
        LOGGER.info(
            "file_browser.create_folder.start parent_id=%s name_length=%s",
            parent_id,
            len(name),
        )
        item = self._call(lambda: self._client.create_folder(parent_id, name))
        LOGGER.info(
            "file_browser.create_folder.success parent_id=%s item_id=%s",
            parent_id,
            item.item_id,
        )
        return item

    def rename_item(self, item: WopanItem, new_name: str) -> None:
        """Rename a file or folder."""
        LOGGER.info("file_browser.rename_item.start item_id=%s kind=%s", item.item_id, item.kind)
        self._call(lambda: self._client.rename(item.item_id, new_name, item.kind, item.file_type))
        LOGGER.info("file_browser.rename_item.success item_id=%s kind=%s", item.item_id, item.kind)

    def delete_item(self, item: WopanItem) -> None:
        """Delete a file or folder."""
        LOGGER.info("file_browser.delete_item.start item_id=%s kind=%s", item.item_id, item.kind)
        self._call(lambda: self._client.delete(item.item_id, item.kind))
        LOGGER.info("file_browser.delete_item.success item_id=%s kind=%s", item.item_id, item.kind)

    def move_item(self, item: WopanItem, target_parent_id: str) -> None:
        """Move a file or folder to another directory."""
        LOGGER.info(
            "file_browser.move_item.start item_id=%s kind=%s target_parent_id=%s",
            item.item_id,
            item.kind,
            target_parent_id,
        )
        self._call(lambda: self._client.move(item.item_id, item.kind, target_parent_id))
        LOGGER.info(
            "file_browser.move_item.success item_id=%s kind=%s target_parent_id=%s",
            item.item_id,
            item.kind,
            target_parent_id,
        )

    def download_file(
        self,
        item: WopanItem,
        local_path: Path,
        progress_callback: DownloadProgressCallback | None = None,
        *,
        status_callback: DownloadStatusCallback | None = None,
        connection_callback: DownloadConnectionCallback | None = None,
        control: DownloadTaskControl | None = None,
        task_id: str | None = None,
    ) -> DownloadResult:
        """Download one file to a local path."""
        if item.kind is not WopanItemKind.FILE:
            raise FileBrowserError("只能下载文件")
        if not local_path.name:
            raise FileBrowserError("保存路径不能为空")
        if not item.download_id:
            raise FileBrowserError("文件缺少下载标识，请刷新后重试")
        download_id = item.download_id

        LOGGER.info(
            "file_browser.download_file.start item_id=%s download_id_present=%s name_length=%s",
            item.item_id,
            bool(item.download_id),
            len(item.name),
        )
        download_info = self._call(lambda: self._client.get_download_info(download_id))
        resolved_task_id = task_id or make_download_task_id(download_id, local_path)

        def refresh_download_url() -> str:
            return self._call(lambda: self._client.get_download_info(download_id)).url

        try:
            result = download_url(
                self._http_client,
                download_info.url,
                local_path,
                settings=self._settings,
                store=self._download_store,
                task_id=resolved_task_id,
                file_name=item.name,
                download_id=download_id,
                refresh_url=refresh_download_url,
                callbacks=DownloadCallbacks(
                    progress=progress_callback,
                    status=status_callback,
                    connections=connection_callback,
                ),
                control=control,
            )
        except DownloadError as exc:
            LOGGER.warning("file_browser.download_file.download_error error=%s", exc)
            raise FileBrowserError(str(exc)) from exc
        LOGGER.info(
            "file_browser.download_file.success item_id=%s download_id_present=%s name_length=%s",
            item.item_id,
            bool(item.download_id),
            len(item.name),
        )
        return result

    def upload_file(self, parent_id: str, local_path: Path) -> WopanItem:
        """Upload one local file to a directory."""
        if not parent_id:
            raise FileBrowserError("目标文件夹不能为空")
        if not local_path.exists():
            raise FileBrowserError("本地文件不存在")
        if not local_path.is_file():
            raise FileBrowserError("只能上传文件")

        LOGGER.info(
            "file_browser.upload_file.start parent_id=%s file_name_length=%s",
            parent_id,
            len(local_path.name),
        )
        try:
            item = self._call(
                lambda: self._client.upload_file(
                    parent_id,
                    local_path,
                    upload_part_size_mb=self._settings.upload_part_size_mb,
                    max_upload_threads=self._settings.max_upload_threads,
                    retry_max_attempts=self._settings.retry_max_attempts,
                )
            )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            LOGGER.warning("file_browser.upload_file.http_status_error status=%s", status_code)
            raise FileBrowserError(f"HTTP {status_code}") from exc
        except httpx.HTTPError as exc:
            LOGGER.warning(
                "file_browser.upload_file.http_error error_type=%s",
                type(exc).__name__,
            )
            raise FileBrowserError("网络错误") from exc
        except OSError as exc:
            LOGGER.warning("file_browser.upload_file.read_error error=%s", exc)
            raise FileBrowserError(f"无法读取本地文件：{exc}") from exc
        LOGGER.info(
            "file_browser.upload_file.success parent_id=%s item_id=%s file_name_length=%s",
            parent_id,
            item.item_id,
            len(item.name),
        )
        return item

    def get_cloud_usage(self, account_id: str) -> WopanCloudUsage:
        """Return cloud storage usage for the current account."""
        LOGGER.info("file_browser.get_cloud_usage.start account_id_present=%s", bool(account_id))
        usage = self._call(lambda: self._client.query_cloud_usage(account_id))
        LOGGER.info(
            "file_browser.get_cloud_usage.success used_bytes=%s total_bytes=%s",
            usage.used_bytes,
            usage.total_bytes,
        )
        return usage

    def _call[T](self, action: Callable[[], T]) -> T:
        """Map protocol errors to UI-facing file browser errors."""
        try:
            return action()
        except WopanAuthenticationError as exc:
            LOGGER.info("file_browser.login_required")
            raise FileBrowserLoginRequiredError("登录已过期，请重新登录") from exc
        except WopanError as exc:
            LOGGER.warning("file_browser.protocol_error error=%s", exc)
            raise FileBrowserError(str(exc)) from exc

    def update_settings(self, settings: AppSettings) -> None:
        """Apply updated transfer settings to future operations."""
        self._settings = settings

    def download_records(self) -> tuple[DownloadTaskRecord, ...]:
        """Return persisted non-active download records."""
        return self._download_store.list_records()

    def remove_download_record(self, task_id: str) -> None:
        """Remove a persisted download record and temporary state."""
        self._download_store.delete(task_id)


def build_file_browser_service(
    cookie_header: str,
    settings: AppSettings | None = None,
) -> FileBrowserService:
    """Build a file browser service for a validated Cookie header."""
    return FileBrowserService(WopanClient(cookie_header), settings=settings)
