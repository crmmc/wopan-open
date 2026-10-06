from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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
from openwopan.tasks.scheduler import (
    DownloadEventCallback as SchedulerEventCallback,
)
from openwopan.tasks.scheduler import (
    DownloadScheduler,
    DownloadTaskInput,
)
from openwopan.tasks.upload import (
    UPLOAD_SESSION_MAX_AGE_SECONDS,
    FolderUploadJob,
    PlannedUploadFile,
    UploadTaskRecord,
    UploadTaskState,
    UploadTaskStore,
    make_upload_task_id,
    next_available_name,
    scan_folder_tree,
)
from openwopan.wopan.client import (
    ORIGIN,
    REFERER,
    ROOT_DIRECTORY_ID,
    UploadResumeContext,
    WopanClient,
    build_uploaded_file_item,
    resolve_upload_part_plan,
)
from openwopan.wopan.errors import (
    WopanAuthenticationError,
    WopanError,
    WopanUploadCancelledError,
)
from openwopan.wopan.models import (
    DownloadInfo,
    WopanCloudUsage,
    WopanItem,
    WopanItemKind,
    WopanRecycleItem,
)

LOGGER = logging.getLogger(__name__)
DownloadProgressCallback = Callable[[int, int | None], None]
UploadProgressCallback = Callable[[int, int], None]
DownloadStatusCallback = Callable[[DownloadStatus], None]
DownloadConnectionCallback = Callable[[int, int], None]
DownloadEventCallback = SchedulerEventCallback


class FileBrowserError(Exception):
    """Base error for UI-facing file browser failures."""


class FileBrowserUploadCancelledError(FileBrowserError):
    """Raised when an upload was cooperatively cancelled."""


class FileBrowserLoginRequiredError(FileBrowserError):
    """Raised when the file browser needs the user to log in again."""


@dataclass(frozen=True)
class TransferBatchPlan:
    """Outcome of comparing a transfer batch against the target directory."""

    transfer_items: tuple[WopanItem, ...]
    conflict_names: tuple[str, ...]
    noop_ids: frozenset[str]


def plan_transfer_batch(
    items: Sequence[WopanItem], target_items: Sequence[WopanItem]
) -> TransferBatchPlan:
    """Split a move/copy batch into transferable items and name conflicts.

    A target item sharing a name but a different id is a conflict that the
    user must skip or cancel on; sharing both name and id means the batch
    references the target item itself, which is a no-op and drops out
    silently.
    """
    target_by_name = {item.name: item for item in target_items}
    transfer_items: list[WopanItem] = []
    conflicts: list[str] = []
    noop_ids: set[str] = set()
    for item in items:
        target = target_by_name.get(item.name)
        if target is None:
            transfer_items.append(item)
        elif target.item_id == item.item_id:
            noop_ids.add(item.item_id)
        else:
            conflicts.append(item.name)
    return TransferBatchPlan(
        transfer_items=tuple(transfer_items),
        conflict_names=tuple(conflicts),
        noop_ids=frozenset(noop_ids),
    )


class FileBrowserBackend(Protocol):
    """UI-facing file browser boundary."""

    def list_directory(self, parent_id: str = ROOT_DIRECTORY_ID) -> list[WopanItem]:
        """Return file items for a directory."""

    def search_files(self, keyword: str, page_no: int = 1, page_size: int = 50) -> list[WopanItem]:
        """Search personal-space files by keyword across directories."""

    def resolve_directory_path(self, directory_id: str) -> list[tuple[str, str]]:
        """Resolve a directory id to its root-relative id/name path."""

    def create_folder(self, parent_id: str, name: str) -> WopanItem:
        """Create a folder and return the created item."""

    def rename_item(self, item: WopanItem, new_name: str) -> None:
        """Rename a file or folder."""

    def delete_item(self, item: WopanItem) -> None:
        """Delete a file or folder."""

    def delete_items(self, items: Sequence[WopanItem]) -> None:
        """Delete one or more files or folders in a single request."""

    def move_item(self, item: WopanItem, target_parent_id: str) -> None:
        """Move a file or folder."""

    def move_items(self, items: Sequence[WopanItem], target_parent_id: str) -> None:
        """Move one or more files or folders in a single request."""

    def copy_items(self, items: Sequence[WopanItem], target_parent_id: str) -> None:
        """Copy one or more files or folders in a single request."""

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

    def set_download_event_callback(self, callback: DownloadEventCallback | None) -> None:
        """Attach the UI observer without exposing scheduler internals."""
        ...

    def submit_download(self, item: WopanItem, local_path: Path) -> str:
        """Queue one file download without exposing its URL to callers."""

    def pause_download(self, task_id: str) -> bool:
        """Pause one download task."""

    def resume_download(self, task_id: str) -> bool:
        """Resume one download task."""

    def cancel_download(self, task_id: str, *, cleanup: bool = False) -> bool:
        """Cancel one download task."""

    def recover_downloads(self) -> tuple[DownloadTaskRecord, ...]:
        """Restore persisted download tasks."""

    def download_records(self) -> tuple[DownloadTaskRecord, ...]:
        """Return persisted download records."""

    def upload_file(
        self,
        parent_id: str,
        local_path: Path,
        *,
        upload_name: str | None = None,
        progress_callback: UploadProgressCallback | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> WopanItem:
        """Upload one local file to a directory."""

    def recover_uploads(self) -> tuple[UploadTaskRecord, ...]:
        """Normalize persisted upload states and surface resumable rows."""

    def prepare_folder_upload(
        self,
        parent_id: str,
        local_root: Path,
        *,
        root_name: str | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> FolderUploadJob:
        """Create the cloud directory tree for a local folder upload."""

    def get_cloud_usage(self, account_id: str) -> WopanCloudUsage:
        """Return cloud storage usage for the current account."""

    def list_recycle_items(self) -> list[WopanRecycleItem]:
        """Return recycle-bin entries."""

    def restore_recycle_items(self, delete_nos: Sequence[str]) -> None:
        """Restore recycle-bin entries to their original locations."""

    def purge_recycle_items(self, delete_nos: Sequence[str]) -> None:
        """Permanently delete recycle-bin entries."""

    def empty_recycle_bin(self) -> None:
        """Permanently delete every recycle-bin entry."""


class FileBrowserService:
    """Application service that exposes WoPan file browsing through OpenWoPan models."""

    def __init__(
        self,
        client: WopanClient,
        http_client: httpx.Client | None = None,
        settings: AppSettings | None = None,
        download_store: DownloadTaskStore | None = None,
        download_event_callback: DownloadEventCallback | None = None,
        download_scheduler: DownloadScheduler | None = None,
        upload_store: UploadTaskStore | None = None,
    ) -> None:
        self._client = client
        self._settings = settings or AppSettings()
        self._download_store = download_store or DownloadTaskStore()
        self._download_scheduler = download_scheduler or DownloadScheduler(
            max_concurrent_downloads=self._settings.max_concurrent_downloads,
            on_event=download_event_callback,
        )
        # None (legacy callers / tests) disables upload persistence;
        # the production factory injects a real store so uploads resume.
        self._upload_store = upload_store
        self._http_client = http_client or httpx.Client(
            headers={"Origin": ORIGIN, "Referer": REFERER},
            follow_redirects=True,
            timeout=httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0),
        )
        # Session-wide id -> path chain cache for search-result location
        # resolution; GetDirectoryPath returns the full chain in one call and
        # the cache keeps repeated jumps into the same folder free. The root
        # entry mirrors the server naming ("个人云") observed in live chains.
        self._directory_path_cache: dict[str, list[tuple[str, str]]] = {
            ROOT_DIRECTORY_ID: [(ROOT_DIRECTORY_ID, "个人云")]
        }

    def resolve_directory_path(self, directory_id: str) -> list[tuple[str, str]]:
        """Resolve a directory id to its root-relative id/name path.

        Uses the single-request GetDirectoryPath endpoint and caches results
        per directory id.
        """
        if not directory_id:
            raise ValueError("directory_id must not be empty")
        cached = self._directory_path_cache.get(directory_id)
        if cached is not None:
            return list(cached)
        LOGGER.info("file_browser.resolve_directory_path.start directory_id=%s", directory_id)
        chain = self._call(lambda: self._client.get_directory_path(directory_id))
        self._directory_path_cache[directory_id] = chain
        LOGGER.info(
            "file_browser.resolve_directory_path.success directory_id=%s depth=%s",
            directory_id,
            len(chain),
        )
        return list(chain)

    def set_download_event_callback(self, callback: DownloadEventCallback | None) -> None:
        """Attach the UI observer without exposing scheduler internals."""
        self._download_scheduler.set_event_callback(callback)

    def submit_download(self, item: WopanItem, local_path: Path) -> str:
        """Resolve one file's download information and enqueue it."""
        download_id = self._validate_download(item, local_path)
        download_info = self._resolve_download_info(download_id)
        task_id = make_download_task_id(download_id, local_path)
        if self._download_store.load(task_id) is not None:
            task_id = f"{task_id}-{uuid.uuid4().hex}"
        task = self._build_download_task(
            task_id=task_id,
            file_name=item.name,
            local_path=local_path,
            download_id=download_id,
            url=download_info.url,
        )
        try:
            return self._download_scheduler.submit(task)
        except ValueError:
            task_id = f"{task_id}-{uuid.uuid4().hex}"
            task = self._build_download_task(
                task_id=task_id,
                file_name=item.name,
                local_path=local_path,
                download_id=download_id,
                url=download_info.url,
            )
            try:
                return self._download_scheduler.submit(task)
            except ValueError as exc:
                raise FileBrowserError("下载任务已存在") from exc

    def pause_download(self, task_id: str) -> bool:
        """Pause one queued or active download task."""
        return self._download_scheduler.pause(task_id)

    def resume_download(self, task_id: str) -> bool:
        """Resume one paused or failed download task."""
        return self._download_scheduler.resume(task_id)

    def cancel_download(self, task_id: str, *, cleanup: bool = False) -> bool:
        """Cancel one queued or active download task."""
        return self._download_scheduler.cancel(task_id, cleanup=cleanup)

    def recover_downloads(self) -> tuple[DownloadTaskRecord, ...]:
        """Resolve persisted download ids and requeue recoverable tasks."""
        tasks: list[DownloadTaskInput] = []
        for state in self._download_store.load_all():
            if state.status in {"已完成", "已取消"}:
                continue
            if not state.download_id:
                state.status = "失败"
                state.error = "文件缺少下载标识，请刷新后重试"
                self._download_store.save(state)
                continue
            try:
                download_info = self._resolve_download_info(state.download_id)
            except FileBrowserError as exc:
                state.status = "失败"
                state.error = str(exc)
                self._download_store.save(state)
                continue
            tasks.append(
                self._build_download_task(
                    task_id=state.task_id,
                    file_name=state.file_name,
                    local_path=state.save_path,
                    download_id=state.download_id,
                    url=download_info.url,
                )
            )
        self._download_scheduler.recover(tasks)
        return self.download_records()

    def close_downloads(self, *, wait: bool = True) -> None:
        """Close the download scheduler and its worker pool."""
        self._download_scheduler.close(wait=wait)

    def _resolve_download_info(self, download_id: str) -> DownloadInfo:
        return self._call(lambda: self._client.get_download_info(download_id))

    def _build_download_task(
        self,
        *,
        task_id: str,
        file_name: str,
        local_path: Path,
        download_id: str,
        url: str,
    ) -> DownloadTaskInput:
        def refresh_download_url() -> str:
            return self._call(lambda: self._client.get_download_info(download_id)).url

        return DownloadTaskInput(
            task_id=task_id,
            file_name=file_name,
            local_path=local_path,
            url=url,
            download_id=download_id,
            settings=self._settings,
            store=self._download_store,
            http_client=self._http_client,
            refresh_url=refresh_download_url,
        )

    @staticmethod
    def _validate_download(item: WopanItem, local_path: Path) -> str:
        if item.kind is not WopanItemKind.FILE:
            raise FileBrowserError("只能下载文件")
        if not local_path.name:
            raise FileBrowserError("保存路径不能为空")
        if not item.download_id:
            raise FileBrowserError("文件缺少下载标识，请刷新后重试")
        return item.download_id

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

    def search_files(self, keyword: str, page_no: int = 1, page_size: int = 50) -> list[WopanItem]:
        """Search personal-space files and map authentication failures to UI state."""
        LOGGER.info(
            "file_browser.search_files.start keyword_length=%s page_no=%s page_size=%s",
            len(keyword),
            page_no,
            page_size,
        )
        items = self._call(lambda: self._client.search_files(keyword, page_no, page_size))
        LOGGER.info(
            "file_browser.search_files.success keyword_length=%s item_count=%s",
            len(keyword),
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
        self.delete_items([item])

    def delete_items(self, items: Sequence[WopanItem]) -> None:
        """Delete one or more files or folders in a single request."""
        LOGGER.info("file_browser.delete_items.start count=%s", len(items))
        self._call(lambda: self._client.delete_many([(item.item_id, item.kind) for item in items]))
        LOGGER.info("file_browser.delete_items.success count=%s", len(items))

    def move_item(self, item: WopanItem, target_parent_id: str) -> None:
        """Move a file or folder to another directory."""
        self.move_items([item], target_parent_id)

    def move_items(self, items: Sequence[WopanItem], target_parent_id: str) -> None:
        """Move one or more files or folders in a single request."""
        LOGGER.info(
            "file_browser.move_items.start count=%s target_parent_id=%s",
            len(items),
            target_parent_id,
        )
        self._call(
            lambda: self._client.move_many(
                [(item.item_id, item.kind) for item in items], target_parent_id
            )
        )
        LOGGER.info(
            "file_browser.move_items.success count=%s target_parent_id=%s",
            len(items),
            target_parent_id,
        )

    def copy_item(self, item: WopanItem, target_parent_id: str) -> None:
        """Copy a file or folder to another directory."""
        self.copy_items([item], target_parent_id)

    def copy_items(self, items: Sequence[WopanItem], target_parent_id: str) -> None:
        """Copy one or more files or folders in a single request."""
        LOGGER.info(
            "file_browser.copy_items.start count=%s target_parent_id=%s",
            len(items),
            target_parent_id,
        )
        self._call(
            lambda: self._client.copy_many(
                [(item.item_id, item.kind) for item in items], target_parent_id
            )
        )
        LOGGER.info(
            "file_browser.copy_items.success count=%s target_parent_id=%s",
            len(items),
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
                expected_sha256=item.sha256,
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

    def upload_file(
        self,
        parent_id: str,
        local_path: Path,
        *,
        upload_name: str | None = None,
        progress_callback: UploadProgressCallback | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> WopanItem:
        """Upload one local file to a directory, resuming a persisted session."""
        if not parent_id:
            raise FileBrowserError("目标文件夹不能为空")
        if not local_path.exists():
            raise FileBrowserError("本地文件不存在")
        if not local_path.is_file():
            raise FileBrowserError("只能上传文件")
        if cancel_requested is not None and cancel_requested():
            raise FileBrowserUploadCancelledError("上传已取消")
        if upload_name is not None:
            if not upload_name:
                raise FileBrowserError("上传文件名称不能为空")
            if upload_name in self._existing_names(parent_id):
                raise FileBrowserError("上传目标已存在，请刷新后重试")
        if cancel_requested is not None and cancel_requested():
            raise FileBrowserUploadCancelledError("上传已取消")

        LOGGER.info(
            "file_browser.upload_file.start parent_id=%s file_name_length=%s",
            parent_id,
            len(local_path.name),
        )
        store = self._upload_store
        resume: UploadResumeContext | None = None
        upload_task_id = ""
        if store is not None:
            try:
                stat_result = local_path.stat()
            except OSError as exc:
                LOGGER.warning(
                    "file_browser.upload_file.stat_failed error_type=%s",
                    type(exc).__name__,
                )
                raise FileBrowserError(f"无法读取本地文件：{exc}") from exc
            part_size, total_parts = resolve_upload_part_plan(
                stat_result.st_size, self._settings.upload_part_size_mb
            )
            upload_task_id = make_upload_task_id(parent_id, local_path, upload_name)
            state = self._prepare_upload_state(
                store,
                task_id=upload_task_id,
                parent_id=parent_id,
                local_path=local_path,
                upload_name=upload_name,
                file_size=stat_result.st_size,
                file_mtime=stat_result.st_mtime,
                part_size=part_size,
                total_parts=total_parts,
            )
            completed = frozenset(state.completed_indexes)
            resume = UploadResumeContext(
                unique_id=state.unique_id,
                batch_no=state.batch_no,
                completed_indexes=completed,
                known_fid=state.fid,
                on_part_result=self._make_upload_part_recorder(store, upload_task_id),
            )
            if state.fid and completed == frozenset(range(1, total_parts + 1)):
                item = build_uploaded_file_item(
                    file_name=state.file_name,
                    parent_id=parent_id,
                    file_size=state.file_size,
                    fid=state.fid,
                )
                store.delete(upload_task_id)
                LOGGER.info(
                    "file_browser.upload_file.resume_complete parent_id=%s item_id=%s",
                    parent_id,
                    item.item_id,
                )
                return item

        try:
            item = self._invoke_upload_client(
                parent_id,
                local_path,
                upload_name=upload_name,
                progress_callback=progress_callback,
                cancel_requested=cancel_requested,
                resume=resume,
            )
        except FileBrowserUploadCancelledError:
            if store is not None:
                store.delete(upload_task_id)
            raise
        except Exception as exc:
            if store is not None and self._should_restart_rejected_session(
                store, upload_task_id, resume=resume, error=exc
            ):
                # The server rejected the reused session outright (e.g. HTTP
                # 500 on the one remaining part, UAT 2026-10-06: retrying the
                # same session hits the same wall forever). Discard it and
                # re-upload the whole file once under a fresh session.
                item = self._restart_rejected_upload_session(
                    store,
                    upload_task_id,
                    parent_id=parent_id,
                    local_path=local_path,
                    upload_name=upload_name,
                    progress_callback=progress_callback,
                    cancel_requested=cancel_requested,
                )
                return item
            if store is not None:
                message = str(exc)
                try:
                    store.update(
                        upload_task_id,
                        lambda state: _mark_upload_failed(state, message),
                    )
                except KeyError:
                    LOGGER.debug("file_browser.upload_file.record_after_delete")
            raise
        if store is not None:
            store.delete(upload_task_id)
        LOGGER.info(
            "file_browser.upload_file.success parent_id=%s item_id=%s file_name_length=%s",
            parent_id,
            item.item_id,
            len(item.name),
        )
        return item

    def _invoke_upload_client(
        self,
        parent_id: str,
        local_path: Path,
        *,
        upload_name: str | None,
        progress_callback: UploadProgressCallback | None,
        cancel_requested: Callable[[], bool] | None,
        resume: UploadResumeContext | None,
    ) -> WopanItem:
        """Call the client, degrading kwargs for backends without new parameters."""
        kwargs: dict[str, object] = {
            "upload_part_size_mb": self._settings.upload_part_size_mb,
            "max_upload_threads": self._settings.max_upload_threads,
            "retry_max_attempts": self._settings.retry_max_attempts,
            "upload_name": upload_name,
        }
        if progress_callback is not None:
            kwargs["progress_callback"] = progress_callback
        if cancel_requested is not None:
            kwargs["cancel_requested"] = cancel_requested
        if resume is not None:
            kwargs["resume"] = resume
        try:
            while True:
                try:
                    return self._call(
                        lambda: self._client.upload_file(
                            parent_id,
                            local_path,
                            **kwargs,  # type: ignore[arg-type]
                        )
                    )
                except TypeError:
                    for key in ("resume", "cancel_requested", "progress_callback"):
                        if key in kwargs:
                            del kwargs[key]
                            break
                    else:
                        raise
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

    def _prepare_upload_state(
        self,
        store: UploadTaskStore,
        *,
        task_id: str,
        parent_id: str,
        local_path: Path,
        upload_name: str | None,
        file_size: int,
        file_mtime: float,
        part_size: int,
        total_parts: int,
    ) -> UploadTaskState:
        """Reuse a persisted upload session or start a fresh one."""
        state = store.load(task_id)
        if state is not None and _upload_state_reusable(
            state,
            parent_id=parent_id,
            file_size=file_size,
            file_mtime=file_mtime,
            part_size=part_size,
            total_parts=total_parts,
        ):
            return store.update(task_id, _reset_upload_session)
        if state is not None:
            store.delete(task_id)
        fresh = _new_upload_state(
            task_id=task_id,
            parent_id=parent_id,
            local_path=local_path,
            upload_name=upload_name,
            file_size=file_size,
            file_mtime=file_mtime,
            part_size=part_size,
            total_parts=total_parts,
        )
        store.save(fresh)
        return fresh

    def _should_restart_rejected_session(
        self,
        store: UploadTaskStore,
        task_id: str,
        *,
        resume: UploadResumeContext | None,
        error: Exception,
    ) -> bool:
        """Whether a failed resume should be retried once with a fresh session.

        Only when the server rejected the REUSED session outright — an HTTP
        5xx from the upload endpoint with zero newly confirmed parts — is a
        full re-upload worthwhile; the same session would keep hitting the
        same wall (UAT 2026-10-06: last remaining part of a 130/131 session
        answered HTTP 500 on every retry). Mid-transfer network failures
        still keep their parts and follow the normal fail-then-resume path.
        """
        if resume is None or not resume.completed_indexes:
            return False
        cause = error.__cause__
        if not (
            isinstance(error, FileBrowserError)
            and isinstance(cause, httpx.HTTPStatusError)
            and 500 <= cause.response.status_code < 600
        ):
            return False
        state = store.load(task_id)
        if state is None:
            return False
        return frozenset(state.completed_indexes) == frozenset(resume.completed_indexes)

    def _restart_rejected_upload_session(
        self,
        store: UploadTaskStore,
        task_id: str,
        *,
        parent_id: str,
        local_path: Path,
        upload_name: str | None,
        progress_callback: UploadProgressCallback | None,
        cancel_requested: Callable[[], bool] | None,
    ) -> WopanItem:
        """Discard a server-rejected session and re-upload the file once."""
        LOGGER.warning(
            "file_browser.upload_file.session_rejected_restart parent_id=%s "
            "file_name_length=%s",
            parent_id,
            len(local_path.name),
        )
        store.delete(task_id)
        stat_result = local_path.stat()
        part_size, total_parts = resolve_upload_part_plan(
            stat_result.st_size, self._settings.upload_part_size_mb
        )
        fresh = _new_upload_state(
            task_id=task_id,
            parent_id=parent_id,
            local_path=local_path,
            upload_name=upload_name,
            file_size=stat_result.st_size,
            file_mtime=stat_result.st_mtime,
            part_size=part_size,
            total_parts=total_parts,
        )
        store.save(fresh)
        resume = UploadResumeContext(
            unique_id=fresh.unique_id,
            batch_no=fresh.batch_no,
            completed_indexes=frozenset(),
            known_fid="",
            on_part_result=self._make_upload_part_recorder(store, task_id),
        )
        try:
            item = self._invoke_upload_client(
                parent_id,
                local_path,
                upload_name=upload_name,
                progress_callback=progress_callback,
                cancel_requested=cancel_requested,
                resume=resume,
            )
        except FileBrowserUploadCancelledError:
            store.delete(task_id)
            raise
        except Exception as exc:
            message = str(exc)
            try:
                store.update(task_id, lambda state: _mark_upload_failed(state, message))
            except KeyError:
                LOGGER.debug("file_browser.upload_file.record_after_delete")
            raise
        store.delete(task_id)
        LOGGER.info(
            "file_browser.upload_file.success parent_id=%s item_id=%s file_name_length=%s",
            parent_id,
            item.item_id,
            len(item.name),
        )
        return item

    def _make_upload_part_recorder(
        self, store: UploadTaskStore, task_id: str
    ) -> Callable[[int, str], None]:
        """Record each confirmed part under the store lock (worker-thread safe)."""

        def record_part_result(part_index: int, fid: str) -> None:
            try:
                store.update(
                    task_id,
                    lambda state: _record_upload_part(state, part_index, fid),
                )
            except KeyError:
                LOGGER.debug(
                    "file_browser.upload_file.record_after_delete part_index=%s",
                    part_index,
                )

        return record_part_result

    def recover_uploads(self) -> tuple[UploadTaskRecord, ...]:
        """Normalize persisted upload states after an application restart."""
        store = self._upload_store
        if store is None:
            return ()
        records: list[UploadTaskRecord] = []
        for state in store.load_all():
            if state.status == "已完成" or not state.local_path.exists():
                store.delete(state.task_id)
                continue
            try:
                updated = store.update(state.task_id, _mark_upload_interrupted)
            except KeyError:
                # The session was deleted concurrently (retention purge racing
                # this recovery loop); nothing left to recover for it.
                continue
            records.append(_upload_state_record(updated))
        return tuple(records)

    def discard_upload_sessions(self, task_ids: Sequence[str]) -> None:
        """Delete persisted upload session states by task id.

        Used by the startup retention purge: dropping a terminal transfer
        record must also drop its session state, otherwise the next restart
        would resurrect the record row. A no-op without an injected store.
        """
        store = self._upload_store
        if store is None:
            return
        for task_id in task_ids:
            if task_id:
                store.delete(task_id)

    def prepare_folder_upload(
        self,
        parent_id: str,
        local_root: Path,
        *,
        root_name: str | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> FolderUploadJob:
        """Create the cloud directory tree for a local folder upload.

        Creates every directory (root first, parent before child, empty dirs
        included), deduplicates every cloud name against existing siblings,
        and returns the per-file upload plan. On any failure the whole
        preparation fails without partial results; directories already
        created stay on the cloud (no rollback).
        """
        if not parent_id:
            raise FileBrowserError("目标文件夹不能为空")
        if not local_root.name:
            raise FileBrowserError("上传文件夹不能为空")
        if cancel_requested is not None and cancel_requested():
            raise FileBrowserUploadCancelledError("上传已取消")
        LOGGER.info(
            "file_browser.prepare_folder_upload.start parent_id=%s root_name_length=%s",
            parent_id,
            len(local_root.name),
        )
        try:
            plan = scan_folder_tree(local_root)
        except (OSError, ValueError) as exc:
            LOGGER.warning(
                "file_browser.prepare_folder_upload.scan_failed errno=%s error_type=%s",
                getattr(exc, "errno", None),
                type(exc).__name__,
            )
            raise FileBrowserError(f"扫描本地文件夹失败：{exc}") from exc

        try:
            if cancel_requested is not None and cancel_requested():
                raise FileBrowserUploadCancelledError("上传已取消")
            existing_root_names = self._existing_names(parent_id)
            if root_name is None:
                resolved_root_name = next_available_name(plan.root_name, existing_root_names)
            else:
                if not root_name:
                    raise FileBrowserError("上传文件夹名称不能为空")
                if root_name in existing_root_names:
                    raise FileBrowserError("上传目标已存在，请刷新后重试")
                resolved_root_name = root_name
            if cancel_requested is not None and cancel_requested():
                raise FileBrowserUploadCancelledError("上传已取消")
            root_item = self.create_folder(parent_id, resolved_root_name)
            dir_ids = {"": root_item.item_id}
            used_names: dict[str, set[str]] = {}

            def taken_names(rel_dir: str) -> set[str]:
                if rel_dir not in used_names:
                    used_names[rel_dir] = self._existing_names(dir_ids[rel_dir])
                return used_names[rel_dir]

            for rel_path in plan.folders:
                if cancel_requested is not None and cancel_requested():
                    raise FileBrowserUploadCancelledError("上传已取消")
                rel_parent, _, local_name = rel_path.rpartition("/")
                parent_names = taken_names(rel_parent)
                if root_name is not None and local_name in parent_names:
                    raise FileBrowserError("上传目标已存在，请刷新后重试")
                folder_name = next_available_name(local_name, parent_names)
                if cancel_requested is not None and cancel_requested():
                    raise FileBrowserUploadCancelledError("上传已取消")
                created = self.create_folder(dir_ids[rel_parent], folder_name)
                parent_names.add(folder_name)
                dir_ids[rel_path] = created.item_id

            planned_files: list[PlannedUploadFile] = []
            for planned in plan.files:
                if cancel_requested is not None and cancel_requested():
                    raise FileBrowserUploadCancelledError("上传已取消")
                names = taken_names(planned.rel_dir)
                if root_name is not None and planned.name in names:
                    raise FileBrowserError("上传目标已存在，请刷新后重试")
                upload_name = next_available_name(planned.name, names)
                names.add(upload_name)
                planned_files.append(
                    PlannedUploadFile(
                        local_path=planned.local_path,
                        target_dir_id=dir_ids[planned.rel_dir],
                        name=upload_name,
                        size=planned.size,
                    )
                )
        except FileBrowserLoginRequiredError:
            raise
        except FileBrowserUploadCancelledError:
            raise
        except FileBrowserError as exc:
            LOGGER.warning(
                "file_browser.prepare_folder_upload.failed error_type=%s",
                type(exc).__name__,
            )
            raise FileBrowserError(f"创建目录失败：{exc}") from exc

        if cancel_requested is not None and cancel_requested():
            raise FileBrowserUploadCancelledError("上传已取消")
        total_bytes = sum(planned.size for planned in planned_files)
        LOGGER.info(
            "file_browser.prepare_folder_upload.success parent_id=%s folder_count=%s "
            "file_count=%s total_bytes=%s",
            parent_id,
            len(plan.folders),
            len(planned_files),
            total_bytes,
        )
        return FolderUploadJob(
            root_item_id=root_item.item_id,
            root_name=resolved_root_name,
            files=tuple(planned_files),
            total_bytes=total_bytes,
        )

    def _existing_names(self, directory_id: str) -> set[str]:
        return {item.name for item in self.list_directory(directory_id)}

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

    def list_recycle_items(self) -> list[WopanRecycleItem]:
        """List recycle-bin entries and map authentication failures to UI state."""
        LOGGER.info("file_browser.list_recycle_items.start")
        items = self._call(lambda: self._client.list_recycle_items())
        LOGGER.info("file_browser.list_recycle_items.success item_count=%s", len(items))
        return items

    def restore_recycle_items(self, delete_nos: Sequence[str]) -> None:
        """Restore one or more recycle-bin entries to their original locations."""
        LOGGER.info("file_browser.restore_recycle_items.start count=%s", len(delete_nos))
        self._call(lambda: self._client.restore_recycle_items(delete_nos))
        LOGGER.info("file_browser.restore_recycle_items.success count=%s", len(delete_nos))

    def purge_recycle_items(self, delete_nos: Sequence[str]) -> None:
        """Permanently delete one or more recycle-bin entries."""
        LOGGER.info("file_browser.purge_recycle_items.start count=%s", len(delete_nos))
        self._call(lambda: self._client.purge_recycle_items(delete_nos))
        LOGGER.info("file_browser.purge_recycle_items.success count=%s", len(delete_nos))

    def empty_recycle_bin(self) -> None:
        """Permanently delete every recycle-bin entry."""
        LOGGER.info("file_browser.empty_recycle_bin.start")
        self._call(lambda: self._client.empty_recycle_bin())
        LOGGER.info("file_browser.empty_recycle_bin.success")

    def _call[T](self, action: Callable[[], T]) -> T:
        """Map protocol errors to UI-facing file browser errors."""
        try:
            return action()
        except WopanAuthenticationError as exc:
            LOGGER.info("file_browser.login_required")
            raise FileBrowserLoginRequiredError("登录已过期，请重新登录") from exc
        except WopanUploadCancelledError as exc:
            raise FileBrowserUploadCancelledError("上传已取消") from exc
        except WopanError as exc:
            LOGGER.warning("file_browser.protocol_error error=%s", exc)
            raise FileBrowserError(str(exc)) from exc

    def update_settings(self, settings: AppSettings) -> None:
        """Apply updated transfer settings to future operations."""
        self._download_scheduler.set_max_concurrent_downloads(settings.max_concurrent_downloads)
        self._settings = settings

    def download_records(self) -> tuple[DownloadTaskRecord, ...]:
        """Return persisted non-active download records."""
        return self._download_store.list_records()

    def remove_download_record(self, task_id: str) -> None:
        """Remove a paused or finished download and its temporary state."""
        if not self._download_scheduler.remove_record(task_id):
            self._download_store.delete(task_id)


def build_file_browser_service(
    cookie_header: str,
    settings: AppSettings | None = None,
) -> FileBrowserService:
    """Build a file browser service for a validated Cookie header."""
    return FileBrowserService(
        WopanClient(cookie_header),
        settings=settings,
        upload_store=UploadTaskStore(),
    )


def _upload_state_reusable(
    state: UploadTaskState,
    *,
    parent_id: str,
    file_size: int,
    file_mtime: float,
    part_size: int,
    total_parts: int,
) -> bool:
    """Return True when a persisted upload session still matches this upload."""
    return (
        state.status != "已完成"
        and state.parent_id == parent_id
        and state.file_size == file_size
        and state.file_mtime == file_mtime
        and state.part_size == part_size
        and state.total_parts == total_parts
        and time.time() - state.updated_at <= UPLOAD_SESSION_MAX_AGE_SECONDS
    )


def _reset_upload_session(state: UploadTaskState) -> None:
    """Mark a reused upload session as active again."""
    state.status = "进行中"
    state.error = ""


def _new_upload_state(
    *,
    task_id: str,
    parent_id: str,
    local_path: Path,
    upload_name: str | None,
    file_size: int,
    file_mtime: float,
    part_size: int,
    total_parts: int,
) -> UploadTaskState:
    """Build a fresh session with new server-side aggregation identifiers."""
    return UploadTaskState(
        task_id=task_id,
        file_name=upload_name if upload_name is not None else local_path.name,
        local_path=local_path,
        parent_id=parent_id,
        upload_name=upload_name,
        file_size=file_size,
        file_mtime=file_mtime,
        part_size=part_size,
        total_parts=total_parts,
        unique_id=str(int(time.time() * 1000)),
        batch_no=time.strftime("%Y%m%d%H%M%S"),
    )


def _record_upload_part(state: UploadTaskState, part_index: int, fid: str) -> None:
    """Merge one confirmed part into the persisted upload state."""
    if part_index not in state.completed_indexes:
        state.completed_indexes = [*state.completed_indexes, part_index]
    if fid and not state.fid:
        state.fid = fid


def _mark_upload_failed(state: UploadTaskState, message: str) -> None:
    """Persist a failed upload while keeping its completed parts."""
    state.status = "失败"
    state.error = message


def _mark_upload_interrupted(state: UploadTaskState) -> None:
    """Normalize a state left behind by an interrupted application run.

    被打断 ≠ 出错：恢复语义是「已暂停」，续传由用户手动触发。
    """
    state.status = "已暂停"
    state.error = (
        f"应用中断，已暂停（已完成 {len(state.completed_indexes)}/{state.total_parts} 分片）"
    )


def _upload_state_record(state: UploadTaskState) -> UploadTaskRecord:
    """Project a persisted upload state onto the transfer-center record."""
    return UploadTaskRecord(
        task_id=state.task_id,
        name=state.file_name,
        local_path=state.local_path,
        target_parent_id=state.parent_id,
        status=state.status,
        completed_parts=len(state.completed_indexes),
        total_parts=state.total_parts,
        file_size=state.file_size,
        upload_name=state.upload_name,
        error=state.error,
        resumable=True,
    )
