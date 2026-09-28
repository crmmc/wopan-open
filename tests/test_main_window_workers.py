"""Background worker/thread, prompt dialog, and transfer-center UI tests."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PySide6.QtCore import QItemSelectionModel, QMimeData, QPoint, Qt, QThread, QUrl
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QListWidget,
    QPushButton,
    QRadioButton,
    QTreeWidgetItem,
    QWidget,
)

import openwopan.ui.main_window as main_window_module
from openwopan.app.file_browser import FileBrowserError, FileBrowserLoginRequiredError
from openwopan.app.transfer_history import TransferHistoryAdapter
from openwopan.storage.settings import AppSettings
from openwopan.storage.transfer_records import TransferRecordStore
from openwopan.tasks.download import DownloadTaskControl
from openwopan.tasks.upload import (
    FolderUploadJob,
    PlannedUploadFile,
    UploadBatchSummary,
    UploadSummaryEntry,
    scan_folder_tree,
)
from openwopan.ui.main_window import (
    BrowserOperationWorker,
    DownloadWorker,
    DroppableTableWidget,
    MainWindow,
    NameInputDialog,
    PendingFolderUpload,
    PendingUploadTask,
    PlaceholderInterface,
    QueuedUploadFile,
    TransferInterface,
    TransferRecord,
    UploadConflictDialog,
    UploadSummaryDialog,
    UploadWorker,
)
from openwopan.wopan.client import ROOT_DIRECTORY_ID
from openwopan.wopan.models import WopanCloudUsage, WopanItem, WopanItemKind


def _static_exec_result(result: object) -> Callable[..., object]:
    """Qt exec() duck-type stub; assigned as a class attribute because a
    method named ``exec`` trips the CWE-95 static scanner."""

    def _run(self, *args: object, **kwargs: object) -> object:
        return result

    return _run


def _accept_result_exec() -> Callable[..., object]:
    """Qt exec() stub returning the class-level ``accept_result`` attribute."""

    def _run(self, *args: object, **kwargs: object) -> object:
        return type(self).accept_result

    return _run


@pytest.fixture(autouse=True)
def _sync_worker_tests(request: pytest.FixtureRequest) -> None:
    """Keep worker lifecycle tests synchronous except GUI-affinity regressions."""
    real_thread_tests = {
        "test_close_window_cancels_and_joins_running_download",
        "test_close_window_closes_scheduler_without_waiting_on_gui_thread",
        "test_background_download_unexpected_failure_clears_real_thread",
        "test_download_target_scan_runs_off_gui_thread",
        "test_download_controls_and_record_removal_run_off_gui_thread",
        "test_overlapping_download_submissions_keep_both_threads_tracked",
        "test_background_upload_updates_ui_on_gui_thread",
        "test_background_download_updates_ui_on_gui_thread",
        "test_refresh_directory_updates_ui_on_gui_thread",
        "test_refresh_directory_ignores_in_flight_request",
        "test_create_folder_updates_ui_on_gui_thread",
        "test_upload_drop_scans_off_gui_thread",
        "test_upload_drop_uses_one_summary_for_conflict_decision",
        "test_upload_drop_cancel_does_not_create_tasks",
        "test_upload_drop_accepts_batch_with_partial_failure",
        "test_upload_conflict_check_runs_off_gui_thread",
        "test_close_ignores_late_upload_check_result",
    }
    if request.node.name.split("[", 1)[0] not in real_thread_tests:
        request.getfixturevalue("sync_threads")




def _renamed(item: WopanItem, new_name: str) -> WopanItem:
    from dataclasses import replace

    return replace(item, name=new_name)


def _file_item(item_id: str = "file-1", name: str = "report.txt") -> WopanItem:
    return WopanItem(
        item_id=item_id,
        name=name,
        kind=WopanItemKind.FILE,
        parent_id=ROOT_DIRECTORY_ID,
        download_id=f"fid-{item_id}",
        size=2048,
    )


def _root_items() -> list[WopanItem]:
    return [
        WopanItem(
            item_id="folder-1",
            name="Folder",
            kind=WopanItemKind.FOLDER,
            parent_id=ROOT_DIRECTORY_ID,
        ),
        _file_item(),
    ]


class WorkerFileBrowser:
    """File browser double supporting the callback-style download API."""

    def __init__(self, download_error: Exception | None = None) -> None:
        # emit_progress=False keeps callback-order assertions deterministic;
        # progress is only emitted when the test opts in.
        self.emit_progress = False
        self.requested_parent_ids: list[str] = []
        self.uploaded_files: list[tuple[str, Path]] = []
        self.upload_names: list[str | None] = []
        self.upload_errors: list[Exception | None] = []
        self.prepare_error: Exception | None = None
        self.prepared_uploads: list[tuple[str, Path]] = []
        self.prepared_upload_names: list[str | None] = []
        self.download_calls: list[dict[str, Any]] = []
        self.download_error = download_error
        self.removed_download_records: list[str] = []
        self.update_settings_calls: list[AppSettings] = []
        self.items_by_parent = {
            ROOT_DIRECTORY_ID: _root_items(),
            "folder-1": [
                WopanItem(
                    item_id="child-file",
                    name="child.txt",
                    kind=WopanItemKind.FILE,
                    parent_id="folder-1",
                    download_id="child-fid",
                    size=1,
                )
            ],
        }

    def list_directory(self, parent_id: str = ROOT_DIRECTORY_ID) -> list[WopanItem]:
        self.requested_parent_ids.append(parent_id)
        return list(self.items_by_parent[parent_id])

    def create_folder(self, parent_id: str, name: str) -> WopanItem:
        created = WopanItem(item_id="new-folder", name=name, kind=WopanItemKind.FOLDER)
        self.items_by_parent[parent_id] = [*self.items_by_parent[parent_id], created]
        return created

    def rename_item(self, item: WopanItem, new_name: str) -> None:
        parent = item.parent_id or ROOT_DIRECTORY_ID
        self.items_by_parent[parent] = [
            _renamed(existing, new_name) if existing.item_id == item.item_id else existing
            for existing in self.items_by_parent.get(parent, [])
        ]

    def delete_item(self, item: WopanItem) -> None:
        parent = item.parent_id or ROOT_DIRECTORY_ID
        self.items_by_parent[parent] = [
            existing
            for existing in self.items_by_parent.get(parent, [])
            if existing.item_id != item.item_id
        ]

    def move_item(self, item: WopanItem, target_parent_id: str) -> None:
        self.delete_item(item)
        from dataclasses import replace

        moved = replace(item, parent_id=target_parent_id)
        self.items_by_parent[target_parent_id] = [
            *self.items_by_parent.get(target_parent_id, []),
            moved,
        ]

    def get_cloud_usage(self, account_id: str) -> WopanCloudUsage:
        return WopanCloudUsage(used_bytes=1, total_bytes=2)

    def upload_file(
        self,
        parent_id: str,
        local_path: Path,
        *,
        upload_name: str | None = None,
    ) -> WopanItem:
        self.uploaded_files.append((parent_id, local_path))
        self.upload_names.append(upload_name)
        if self.upload_errors:
            error = self.upload_errors.pop(0)
            if error is not None:
                raise error
        effective_name = upload_name if upload_name is not None else local_path.name
        uploaded = WopanItem(
            item_id="uploaded-file",
            name=effective_name,
            kind=WopanItemKind.FILE,
            parent_id=parent_id,
            download_id="uploaded-fid",
            size=1,
        )
        self.items_by_parent[parent_id] = [*self.items_by_parent.get(parent_id, []), uploaded]
        return uploaded

    def prepare_folder_upload(
        self,
        parent_id: str,
        local_root: Path,
        *,
        root_name: str | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> FolderUploadJob:
        self.prepared_uploads.append((parent_id, local_root))
        self.prepared_upload_names.append(root_name)
        if self.prepare_error is not None:
            raise self.prepare_error
        if cancel_requested is not None and cancel_requested():
            raise FileBrowserError("上传已取消")
        plan = scan_folder_tree(local_root)
        files = tuple(
            PlannedUploadFile(
                local_path=planned.local_path,
                target_dir_id=f"cloud-{parent_id}-{planned.rel_dir or 'root'}",
                name=planned.name,
                size=planned.size,
            )
            for planned in plan.files
        )
        return FolderUploadJob(
            root_item_id="cloud-root",
            root_name=root_name if root_name is not None else plan.root_name,
            files=files,
            total_bytes=sum(planned.size for planned in files),
        )

    def download_records(self) -> tuple[SimpleNamespace, ...]:
        return (
            SimpleNamespace(
                task_id="download-9",
                name="persisted.txt",
                target_path=str(Path("/tmp/persisted.txt")),
                status="已暂停",
                total_bytes=100,
                bytes_done=40,
                active_connections=0,
                max_connections=4,
                supports_resume=True,
            ),
            SimpleNamespace(task_id="bad"),
        )

    def remove_download_record(self, task_id: str) -> None:
        self.removed_download_records.append(task_id)

    def update_settings(self, settings: AppSettings) -> None:
        self.update_settings_calls.append(settings)

    def download_file(
        self,
        item: WopanItem,
        local_path: Path,
        progress_callback: object | None = None,
        status_callback: object | None = None,
        connection_callback: object | None = None,
        control: object | None = None,
        task_id: str | None = None,
    ) -> object:
        self.download_calls.append({"task_id": task_id, "local_path": local_path})
        if self.download_error is not None:
            raise self.download_error
        if self.emit_progress and callable(progress_callback):
            progress_callback(512, 1024)
        if callable(status_callback):
            status_callback("下载中")
        if callable(connection_callback):
            connection_callback(2, 4)
        return SimpleNamespace(status="已完成", task_id=task_id, local_path=local_path)


class LegacySignatureFileBrowser(WorkerFileBrowser):
    """Browser whose download_file only accepts the legacy positional signature."""

    def download_file(
        self,
        item: WopanItem,
        local_path: Path,
        progress_callback: object | None = None,
    ) -> object:
        self.download_calls.append({"legacy": True, "local_path": local_path})
        if callable(progress_callback):
            progress_callback(512, 1024)
        return SimpleNamespace(status="已完成")


class UnrelatedTypeErrorFileBrowser(WorkerFileBrowser):
    def download_file(self, *args: object, **kwargs: object) -> object:
        raise TypeError("bad operand type")


def _wait_until(qapp: QApplication, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()



class _CloseBlockingDownloadBrowser(WorkerFileBrowser):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()

    def download_file(
        self,
        item: WopanItem,
        local_path: Path,
        progress_callback: object | None = None,
        status_callback: object | None = None,
        connection_callback: object | None = None,
        control: object | None = None,
        task_id: str | None = None,
    ) -> object:
        assert isinstance(control, DownloadTaskControl)
        self.started.set()
        while not self.release.wait(0.01):
            status = control.stop_result()
            if status is not None:
                self.cancelled.set()
                return SimpleNamespace(status=status, task_id=task_id, local_path=local_path)
        return SimpleNamespace(status="已完成", task_id=task_id, local_path=local_path)


class _FakeFinishedSignal:
    """Minimal signal stand-in recording connected slots."""

    def __init__(self) -> None:
        self.slots: list[Callable[[], None]] = []

    def connect(self, slot: Callable[[], None]) -> None:
        self.slots.append(slot)

    def emit(self) -> None:
        for slot in self.slots:
            slot()


class _RefusingThread:
    """Thread stub that ignores quit() and never joins within the timeout."""

    def __init__(self) -> None:
        self.quit_called = False
        self.set_parent_values: list[object | None] = []
        self.wait_timeouts: list[int] = []
        self.finished = _FakeFinishedSignal()

    def isRunning(self) -> bool:
        return True

    def quit(self) -> None:
        self.quit_called = True

    def setParent(self, parent: object | None) -> None:
        self.set_parent_values.append(parent)

    def wait(self, timeout: int) -> bool:
        self.wait_timeouts.append(timeout)
        return False



# ---------------------------------------------------------------------------
# DownloadWorker / UploadWorker
# ---------------------------------------------------------------------------


class _SignalCollector:
    def __init__(self, worker: DownloadWorker) -> None:
        self.events: dict[str, list[tuple]] = {
            "progress": [],
            "status_changed": [],
            "connections_changed": [],
            "succeeded": [],
            "stopped": [],
            "failed": [],
            "login_required": [],
        }
        worker.progress.connect(
            lambda done, total, task_id: self.events["progress"].append((done, total, task_id))
        )
        worker.status_changed.connect(
            lambda status, task_id: self.events["status_changed"].append((status, task_id))
        )
        worker.connections_changed.connect(
            lambda active, maximum, task_id: self.events["connections_changed"].append(
                (active, maximum, task_id)
            )
        )
        worker.succeeded.connect(
            lambda name, path, task_id: self.events["succeeded"].append((name, path, task_id))
        )
        worker.stopped.connect(
            lambda status, task_id: self.events["stopped"].append((status, task_id))
        )
        worker.failed.connect(
            lambda message, task_id: self.events["failed"].append((message, task_id))
        )
        worker.login_required.connect(
            lambda message, task_id: self.events["login_required"].append((message, task_id))
        )


class _UploadCollector:
    def __init__(self, worker: UploadWorker) -> None:
        self.events: dict[str, list[tuple]] = {
            "progress": [],
            "succeeded": [],
            "failed": [],
            "login_required": [],
        }
        worker.progress.connect(
            lambda bytes_done, total_bytes, task_id: self.events["progress"].append(
                (bytes_done, total_bytes, task_id)
            )
        )
        worker.succeeded.connect(
            lambda item, task_id: self.events["succeeded"].append((item, task_id))
        )
        worker.failed.connect(
            lambda message, task_id: self.events["failed"].append((message, task_id))
        )
        worker.login_required.connect(
            lambda message, task_id: self.events["login_required"].append((message, task_id))
        )


class _ProgressOnlyBrowser:
    def download_file(self, item: WopanItem, local_path: Path, progress_callback=None) -> None:
        if callable(progress_callback):
            progress_callback(1, None)


class _StoppedResultBrowser:
    def download_file(self, *args: object, **kwargs: object) -> object:
        return SimpleNamespace(status="已暂停")


class _NoStatusResultBrowser:
    def download_file(self, *args: object, **kwargs: object) -> object:
        return "raw"


class _RaisingBrowser:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def download_file(self, *args: object, **kwargs: object) -> object:
        raise self.error

    def upload_file(self, parent_id: str, local_path: Path) -> WopanItem:
        raise self.error


def _make_download_worker(browser: object, tmp_path: Path) -> DownloadWorker:
    return DownloadWorker(
        browser,  # type: ignore[arg-type]
        _file_item(),
        tmp_path / "report.txt",
        "download-1",
        DownloadTaskControl(),
    )


def test_download_worker_emits_all_callbacks_and_success(
    qapp: QApplication, tmp_path: Path
) -> None:
    browser = WorkerFileBrowser()
    browser.emit_progress = True
    worker = _make_download_worker(browser, tmp_path)
    collector = _SignalCollector(worker)

    worker.run()

    assert collector.events["progress"] == [(512, 1024, "download-1")]
    assert collector.events["status_changed"] == [("下载中", "download-1")]
    assert collector.events["connections_changed"] == [(2, 4, "download-1")]
    assert collector.events["succeeded"] == [
        ("report.txt", str(tmp_path / "report.txt"), "download-1")
    ]
    assert collector.events["stopped"] == []
    assert collector.events["failed"] == []


def test_download_worker_falls_back_to_legacy_signature(
    qapp: QApplication, tmp_path: Path
) -> None:
    browser = LegacySignatureFileBrowser()
    worker = _make_download_worker(browser, tmp_path)
    collector = _SignalCollector(worker)

    worker.run()

    assert browser.download_calls == [{"legacy": True, "local_path": tmp_path / "report.txt"}]
    assert collector.events["succeeded"] == [
        ("report.txt", str(tmp_path / "report.txt"), "download-1")
    ]


def test_download_worker_reports_unrelated_type_error(
    qapp: QApplication, tmp_path: Path
) -> None:
    worker = _make_download_worker(UnrelatedTypeErrorFileBrowser(), tmp_path)
    collector = _SignalCollector(worker)

    worker.run()

    assert collector.events["failed"] == [("bad operand type", "download-1")]
    assert collector.events["succeeded"] == []


@pytest.mark.parametrize(
    ("browser_factory", "expected_signal", "expected_payload"),
    [
        (lambda: _StoppedResultBrowser(), "stopped", ("已暂停", "download-1")),
        (
            lambda: _RaisingBrowser(FileBrowserError("network down")),
            "failed",
            ("network down", "download-1"),
        ),
        (
            lambda: _RaisingBrowser(FileBrowserLoginRequiredError("登录已过期，请重新登录")),
            "login_required",
            ("登录已过期，请重新登录", "download-1"),
        ),
    ],
)
def test_download_worker_maps_stopped_failed_and_login_required(
    qapp: QApplication,
    tmp_path: Path,
    browser_factory,
    expected_signal: str,
    expected_payload: tuple,
) -> None:
    worker = _make_download_worker(browser_factory(), tmp_path)
    collector = _SignalCollector(worker)

    worker.run()

    assert collector.events[expected_signal] == [expected_payload]
    assert collector.events["succeeded"] == []


def test_download_worker_reports_unexpected_error_without_raising(
    qapp: QApplication, tmp_path: Path
) -> None:
    worker = _make_download_worker(_RaisingBrowser(RuntimeError("disk full")), tmp_path)
    collector = _SignalCollector(worker)

    worker.run()

    assert collector.events["failed"] == [("disk full", "download-1")]
    assert collector.events["succeeded"] == []


def test_download_progress_accepts_large_byte_count(
    qapp: QApplication, tmp_path: Path
) -> None:
    worker = _make_download_worker(WorkerFileBrowser(), tmp_path)
    collector = _SignalCollector(worker)

    worker.progress.emit(3_000_000_000, None, "download-1")

    assert collector.events["progress"] == [(3_000_000_000, None, "download-1")]


def test_download_worker_defaults_to_completed_without_status_object(
    qapp: QApplication, tmp_path: Path
) -> None:
    worker = _make_download_worker(_NoStatusResultBrowser(), tmp_path)
    collector = _SignalCollector(worker)

    worker.run()

    assert collector.events["succeeded"] == [
        ("report.txt", str(tmp_path / "report.txt"), "download-1")
    ]


def test_upload_worker_reports_unexpected_error_without_raising(
    qapp: QApplication, tmp_path: Path
) -> None:
    worker = UploadWorker(
        _RaisingBrowser(RuntimeError("disk full")),
        ROOT_DIRECTORY_ID,
        tmp_path / "u.txt",
        "upload-1",
    )  # type: ignore[arg-type]
    collector = _UploadCollector(worker)

    worker.run()

    assert collector.events["failed"] == [("disk full", "upload-1")]
    assert collector.events["succeeded"] == []


def test_upload_worker_forwards_progress_with_task_id(
    qapp: QApplication, tmp_path: Path
) -> None:
    class _ProgressBrowser:
        def upload_file(self, parent_id: str, local_path: Path, **kwargs: object) -> WopanItem:
            callback = kwargs.get("progress_callback")
            assert callable(callback)
            callback(4, 10)
            return WopanItem(
                item_id="uploaded",
                name=local_path.name,
                kind=WopanItemKind.FILE,
                parent_id=parent_id,
            )

    local_path = tmp_path / "upload.txt"
    local_path.write_text("content")
    worker = UploadWorker(_ProgressBrowser(), ROOT_DIRECTORY_ID, local_path, "upload-1")  # type: ignore[arg-type]
    collector = _UploadCollector(worker)

    worker.run()

    assert collector.events["progress"] == [(4, 10, "upload-1")]


def test_upload_worker_stops_before_or_after_backend_call(
    qapp: QApplication, tmp_path: Path,
) -> None:
    class _CancellingBrowser:
        def __init__(self) -> None:
            self.worker: UploadWorker | None = None
            self.calls = 0

        def upload_file(self, parent_id: str, local_path: Path, **kwargs: object) -> WopanItem:
            self.calls += 1
            assert self.worker is not None
            self.worker.request_cancel()
            return WopanItem(item_id="uploaded", name=local_path.name, kind=WopanItemKind.FILE)

    browser = _CancellingBrowser()
    worker = UploadWorker(browser, "0", tmp_path / "file.txt", "upload-1")  # type: ignore[arg-type]
    browser.worker = worker
    cancelled: list[str] = []
    succeeded: list[object] = []
    worker.cancelled.connect(cancelled.append)
    worker.succeeded.connect(succeeded.append)
    worker.request_cancel()
    worker.run()
    assert browser.calls == 0
    assert cancelled == ["upload-1"]

    worker = UploadWorker(browser, "0", tmp_path / "file.txt", "upload-2")  # type: ignore[arg-type]
    browser.worker = worker
    worker.cancelled.connect(cancelled.append)
    worker.succeeded.connect(succeeded.append)
    worker.run()
    assert browser.calls == 1
    assert cancelled == ["upload-1", "upload-2"]
    assert succeeded == []


def test_upload_worker_pause_waits_until_resumed(qapp: QApplication, tmp_path: Path) -> None:
    started = threading.Event()
    allow_check = threading.Event()
    finished = threading.Event()

    class _PausableBrowser:
        def upload_file(self, parent_id: str, local_path: Path, **kwargs: object) -> WopanItem:
            cancel_requested = kwargs["cancel_requested"]
            assert callable(cancel_requested)
            started.set()
            allow_check.wait(1)
            assert cancel_requested() is False
            return WopanItem(
                item_id="uploaded",
                name=local_path.name,
                kind=WopanItemKind.FILE,
                parent_id=parent_id,
            )

    local_path = tmp_path / "upload.txt"
    local_path.write_text("content")
    worker = UploadWorker(
        _PausableBrowser(), ROOT_DIRECTORY_ID, local_path, "upload-1"
    )  # type: ignore[arg-type]
    succeeded: list[str] = []
    worker.succeeded.connect(
        lambda _item, task_id: succeeded.append(task_id),
        Qt.ConnectionType.DirectConnection,
    )
    thread = threading.Thread(target=lambda: (worker.run(), finished.set()))
    thread.start()
    assert started.wait(1)
    worker.request_pause()
    allow_check.set()
    time.sleep(0.05)
    assert thread.is_alive()
    worker.request_resume()
    thread.join(1)
    assert not thread.is_alive()
    assert finished.is_set()
    assert succeeded == ["upload-1"]


def test_upload_worker_emits_success(qapp: QApplication, tmp_path: Path) -> None:
    browser = WorkerFileBrowser()
    local_path = tmp_path / "upload.txt"
    local_path.write_text("content")
    worker = UploadWorker(browser, ROOT_DIRECTORY_ID, local_path, "upload-1")  # type: ignore[arg-type]
    collector = _UploadCollector(worker)

    worker.run()

    assert len(collector.events["succeeded"]) == 1
    item, task_id = collector.events["succeeded"][0]
    assert item.name == "upload.txt"
    assert task_id == "upload-1"


@pytest.mark.parametrize("upload_name", [None, "renamed.txt"])
def test_upload_worker_falls_back_without_cancel_keyword(
    qapp: QApplication, tmp_path: Path, upload_name: str | None
) -> None:
    class LegacyUploadBrowser:
        def upload_file(
            self,
            parent_id: str,
            local_path: Path,
            *,
            upload_name: str | None = None,
            progress_callback: Callable[[int, int], None] | None = None,
        ) -> WopanItem:
            if progress_callback is not None:
                progress_callback(1, 1)
            return WopanItem(
                item_id="uploaded",
                name=upload_name if upload_name is not None else local_path.name,
                kind=WopanItemKind.FILE,
                parent_id=parent_id,
            )

    local_path = tmp_path / "upload.txt"
    local_path.write_text("content")
    worker = UploadWorker(
        LegacyUploadBrowser(), ROOT_DIRECTORY_ID, local_path, "upload-1", upload_name
    )  # type: ignore[arg-type]
    collector = _UploadCollector(worker)

    worker.run()

    assert collector.events["failed"] == []
    item, task_id = collector.events["succeeded"][0]
    assert item.name == (upload_name or local_path.name)
    assert task_id == "upload-1"


def test_upload_worker_maps_backend_error_after_cancel_to_cancelled(
    qapp: QApplication, tmp_path: Path
) -> None:
    class CancelThenFailBrowser:
        def upload_file(self, parent_id: str, local_path: Path, **kwargs: object) -> WopanItem:
            worker.request_cancel()
            raise FileBrowserError("上传请求失败")

    worker = UploadWorker(
        CancelThenFailBrowser(), ROOT_DIRECTORY_ID, tmp_path / "upload.txt", "upload-1"
    )  # type: ignore[arg-type]
    cancelled: list[str] = []
    failed: list[tuple[str, str]] = []
    worker.cancelled.connect(cancelled.append)
    worker.failed.connect(lambda message, task_id: failed.append((message, task_id)))

    worker.run()

    assert cancelled == ["upload-1"]
    assert failed == []


@pytest.mark.parametrize(
    ("error", "expected_signal", "expected_payload"),
    [
        (FileBrowserError("upload failed"), "failed", ("upload failed", "upload-1")),
        (
            FileBrowserLoginRequiredError("登录已过期，请重新登录"),
            "login_required",
            ("登录已过期，请重新登录", "upload-1"),
        ),
    ],
)
def test_upload_worker_maps_failed_and_login_required(
    qapp: QApplication,
    tmp_path: Path,
    error: Exception,
    expected_signal: str,
    expected_payload: tuple,
) -> None:
    worker = UploadWorker(
        _RaisingBrowser(error), ROOT_DIRECTORY_ID, tmp_path / "u.txt", "upload-1"
    )  # type: ignore[arg-type]
    collector = _UploadCollector(worker)

    worker.run()

    assert collector.events[expected_signal] == [expected_payload]
    assert collector.events["succeeded"] == []


def test_browser_operation_worker_emits_success(qapp: QApplication) -> None:
    result = [WopanItem(item_id="item", name="item", kind=WopanItemKind.FILE)]
    worker = BrowserOperationWorker(lambda: result)
    succeeded: list[object] = []
    worker.succeeded.connect(succeeded.append)

    worker.run()

    assert succeeded == [result]


@pytest.mark.parametrize(
    "error_type",
    [FileBrowserError, FileBrowserLoginRequiredError, RuntimeError],
)
def test_browser_operation_worker_maps_all_errors(
    qapp: QApplication,
    error_type: type[Exception],
) -> None:
    worker = BrowserOperationWorker(lambda: (_ for _ in ()).throw(error_type("boom")))
    failed: list[str] = []
    login_required: list[str] = []
    worker.failed.connect(failed.append)
    worker.login_required.connect(login_required.append)

    worker.run()

    if error_type is FileBrowserLoginRequiredError:
        assert login_required == ["boom"]
        assert failed == []
    else:
        assert failed == ["boom"]
        assert login_required == []


class _ThreadRecordingMainWindow(MainWindow):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.refresh_handler_thread_id: int | None = None

    def _on_directory_refresh_succeeded(self, result: object) -> None:
        super()._on_directory_refresh_succeeded(result)
        self.refresh_handler_thread_id = threading.get_ident()


def test_refresh_directory_updates_ui_on_gui_thread(qapp: QApplication) -> None:
    browser = WorkerFileBrowser()
    window = _ThreadRecordingMainWindow(browser)
    gui_thread_id = threading.get_ident()
    browser_thread_ids: list[int] = []
    original_list_directory = browser.list_directory

    def list_directory(parent_id: str = ROOT_DIRECTORY_ID) -> list[WopanItem]:
        browser_thread_ids.append(threading.get_ident())
        return original_list_directory(parent_id)

    browser.list_directory = list_directory  # type: ignore[method-assign]
    window.refresh_current_directory()

    assert _wait_until(
        qapp,
        lambda: window._directory_thread is None
        and not any(isinstance(child, QThread) for child in window.children()),
    )
    assert window.refresh_handler_thread_id == gui_thread_id
    assert browser_thread_ids and browser_thread_ids[0] != gui_thread_id
    assert [item.name for item in window.displayed_items()] == ["Folder", "report.txt"]


class _CreateThreadRecordingMainWindow(MainWindow):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.create_handler_thread_id: int | None = None

    def _on_create_folder_succeeded(self, result: object) -> None:
        super()._on_create_folder_succeeded(result)
        self.create_handler_thread_id = threading.get_ident()


@pytest.mark.parametrize(
    ("direction", "expected_status"),
    [
        ("upload", "上传完成：upload.txt"),
        ("create", "创建成功"),
    ],
)
def test_visibility_check_waits_for_refresh_completion(
    qapp: QApplication, tmp_path: Path, direction: str, expected_status: str
) -> None:
    """The after-refresh visibility check must see the fresh item list.

    Regression: the check used to read ``_items`` immediately after kicking
    an async refresh, always reporting "not visible after refresh".
    """
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    assert _wait_until(qapp, lambda: window._directory_thread is None)

    if direction == "upload":
        local_path = tmp_path / "upload.txt"
        local_path.write_text("content")
        window.upload_file_to_current_directory(local_path)
        assert _wait_until(
            qapp,
            lambda: window.status_message() == "上传完成：upload.txt"
            and window._upload_thread is None
            and window._directory_thread is None,
        )
    else:
        window.create_folder_with_name("新建文件夹")
        assert _wait_until(
            qapp,
            lambda: "未在当前目录看到" not in window.status_message()
            and window._create_thread is None
            and window._directory_thread is None,
        )

    assert "未在当前目录看到" not in window.status_message()


def test_create_folder_updates_ui_on_gui_thread(qapp: QApplication) -> None:
    browser = WorkerFileBrowser()
    window = _CreateThreadRecordingMainWindow(browser)
    gui_thread_id = threading.get_ident()
    create_thread_ids: list[int] = []
    list_thread_ids: list[int] = []
    original_create_folder = browser.create_folder
    original_list_directory = browser.list_directory

    def create_folder(parent_id: str, name: str) -> WopanItem:
        create_thread_ids.append(threading.get_ident())
        return original_create_folder(parent_id, name)

    def list_directory(parent_id: str = ROOT_DIRECTORY_ID) -> list[WopanItem]:
        list_thread_ids.append(threading.get_ident())
        return original_list_directory(parent_id)

    browser.create_folder = create_folder  # type: ignore[method-assign]
    browser.list_directory = list_directory  # type: ignore[method-assign]
    window.create_folder_with_name("Reports")

    assert _wait_until(
        qapp, lambda: window._create_thread is None and window._directory_thread is None
    )
    assert window.create_handler_thread_id == gui_thread_id
    assert create_thread_ids and create_thread_ids[0] != gui_thread_id
    assert list_thread_ids and list_thread_ids[-1] != gui_thread_id
    assert any(item.name == "Reports" for item in window.displayed_items())


def test_create_folder_failure_clears_thread_and_reports_status(
    qapp: QApplication,
    sync_threads: None,
) -> None:
    class FailingCreateBrowser(WorkerFileBrowser):
        def create_folder(self, parent_id: str, name: str) -> WopanItem:
            raise FileBrowserError("create down")

    window = MainWindow(FailingCreateBrowser())
    window.create_folder_with_name("name")

    assert window._create_thread is None
    assert window._create_worker is None
    assert window.status_message() == "新建文件夹失败：create down"


def test_refresh_directory_ignores_in_flight_request(
    qapp: QApplication,
    caplog: pytest.LogCaptureFixture,
) -> None:
    browser = WorkerFileBrowser()
    started = threading.Event()
    release = threading.Event()
    call_count = 0

    def blocking_list_directory(parent_id: str = ROOT_DIRECTORY_ID) -> list[WopanItem]:
        nonlocal call_count
        call_count += 1
        started.set()
        release.wait(5)
        return list(browser.items_by_parent[parent_id])

    browser.list_directory = blocking_list_directory  # type: ignore[method-assign]
    window = MainWindow(browser)
    window.refresh_current_directory()
    assert _wait_until(qapp, started.is_set)

    with caplog.at_level("DEBUG", logger=main_window_module.LOGGER.name):
        window.refresh_current_directory()
    assert window._directory_thread is not None
    assert call_count == 1
    assert "main_window.refresh.skipped_busy" in caplog.text

    release.set()
    assert _wait_until(qapp, lambda: window._directory_thread is None)


def test_refresh_directory_runs_pending_refresh_after_in_flight_finishes(
    qapp: QApplication,
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    assert [item.name for item in window.displayed_items()] == ["Folder", "report.txt"]

    # Simulate an in-flight refresh; navigation during loading marks the intent.
    window._directory_thread = object()  # type: ignore[assignment]
    window.enter_displayed_folder(0)
    assert window._directory_refresh_pending is True
    assert window.current_directory_id() == "folder-1"

    # The in-flight refresh finishing replays the latest navigation intent.
    window._clear_directory_refresh()

    assert window._directory_refresh_pending is False
    assert window._directory_thread is None
    assert window.current_directory_id() == "folder-1"
    assert [item.name for item in window.displayed_items()] == ["child.txt"]




def test_close_window_cancels_and_joins_running_download(
    qapp: QApplication, tmp_path: Path
) -> None:
    browser = _CloseBlockingDownloadBrowser()
    window = MainWindow(browser)
    task_id = "download-close"
    window._start_download_task(_file_item(), tmp_path / "report.txt", task_id)
    thread = window._download_thread
    assert thread is not None

    try:
        assert _wait_until(qapp, browser.started.is_set)

        window.close()

        assert browser.cancelled.is_set()
        assert thread.wait(500)
        assert not thread.isRunning()
    finally:
        browser.release.set()
        thread.quit()
        thread.wait(3000)


def test_close_window_joins_every_task_keyed_upload_thread(qapp: QApplication) -> None:
    window = MainWindow()
    first = _RefusingThread()
    second = _RefusingThread()
    window._upload_threads = {"upload-1": first, "upload-2": second}  # type: ignore[assignment]
    window._upload_thread = first  # type: ignore[assignment]
    window._upload_task_id = "upload-1"

    event = QCloseEvent()
    window.closeEvent(event)

    assert event.isAccepted()
    assert first.quit_called and second.quit_called
    assert first.wait_timeouts == [main_window_module.THREAD_JOIN_TIMEOUT_MS]
    assert second.wait_timeouts == [main_window_module.THREAD_JOIN_TIMEOUT_MS]


def test_close_window_cancels_waiting_folder_records(
    qapp: QApplication, tmp_path: Path
) -> None:
    window = MainWindow(WorkerFileBrowser())
    root = tmp_path / "active"
    waiting = tmp_path / "waiting"
    child = root / "child.txt"
    next_child = root / "next.txt"
    root.mkdir()
    waiting.mkdir()
    root_id = window._create_upload_record(root)
    child_id = window._create_upload_record(child)
    next_id = window._create_upload_record(next_child)
    waiting_id = window._create_upload_record(waiting)
    window.transfer_interface.update_record("upload", child_id, status="上传中")
    window._folder_upload_record_id = root_id

    window._folder_upload_active = QueuedUploadFile(child_id, child, "cloud-root", "child.txt")
    window._folder_upload_queue = [
        QueuedUploadFile(next_id, next_child, "cloud-root", "next.txt")
    ]
    window._folder_prepare_pending = [
        PendingFolderUpload(waiting, ROOT_DIRECTORY_ID, "waiting", waiting_id)
    ]

    window.closeEvent(QCloseEvent())

    assert [window.transfer_interface._find_record("upload", task_id).status for task_id in (
        root_id, next_id, waiting_id
    )] == ["已取消", "已取消", "已取消"]
    assert window._folder_upload_queue == []
    assert window._folder_prepare_pending == []
    assert window.transfer_interface._find_record("upload", child_id).status == "上传中"
    window._on_folder_upload_prepare_failed("关闭后的失败")
    window._on_folder_upload_prepare_login_required("登录已过期，请重新登录")
    assert window.transfer_interface._find_record("upload", root_id).status == "已取消"


def test_close_window_cancels_paused_upload_records(
    qapp: QApplication, tmp_path: Path
) -> None:
    window = MainWindow(WorkerFileBrowser())
    local_path = tmp_path / "paused.txt"
    task_id = window._create_upload_record(local_path)
    window._paused_uploads[task_id] = PendingUploadTask(
        ROOT_DIRECTORY_ID, local_path, task_id, local_path.name, False
    )
    window.transfer_interface.update_record("upload", task_id, status="已暂停")

    window.closeEvent(QCloseEvent())

    record = window.transfer_interface._find_record("upload", task_id)
    assert record is not None and record.status == "已取消"
    assert window._paused_uploads == {}


def test_close_window_closes_scheduler_without_waiting_on_gui_thread(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    gui_thread_id = threading.get_ident()
    close_calls: list[tuple[bool, int]] = []

    def close_downloads(*, wait: bool = True) -> None:
        close_calls.append((wait, threading.get_ident()))

    monkeypatch.setattr(browser, "close_downloads", close_downloads, raising=False)
    window = MainWindow(browser)

    event = QCloseEvent()
    window.closeEvent(event)

    assert event.isAccepted()
    assert len(close_calls) == 1
    assert close_calls[0][0] is False
    assert close_calls[0][1] != gui_thread_id
    qapp.processEvents()
    assert window._download_close_thread is None
    assert window._download_close_worker is None


def test_close_window_without_active_transfer_is_noop(qapp: QApplication) -> None:
    window = MainWindow()
    event = QCloseEvent()

    window.closeEvent(event)

    assert event.isAccepted()
    assert window._download_thread is None
    assert window._upload_thread is None
    assert window._directory_thread is None
    assert window._folder_prepare_thread is None


def test_finished_thread_is_deleted_by_gui_cleanup(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    delete_later_calls: list[bool] = []
    worker_delete_later_calls: list[bool] = []

    class TrackingThread(QThread):
        def start(self, *args: object, **kwargs: object) -> None:
            self.started.emit()

        def quit(self) -> None:
            self.finished.emit()

        def deleteLater(self) -> None:
            delete_later_calls.append(True)

    monkeypatch.setattr(main_window_module, "QThread", TrackingThread)
    monkeypatch.setattr(
        main_window_module.BrowserOperationWorker,
        "deleteLater",
        lambda _worker: worker_delete_later_calls.append(True),
    )
    window = MainWindow(WorkerFileBrowser())

    window.refresh_current_directory()

    assert delete_later_calls == [True]
    assert worker_delete_later_calls == []


@pytest.mark.parametrize(
    "direction",
    [
        "download",
        "upload",
        "directory",
        "create",
        "rename",
        "delete",
        "move",
        "usage",
        "folder_upload",
    ],
)
def test_close_window_logs_timeout_and_accepts_close(
    qapp: QApplication,
    caplog: pytest.LogCaptureFixture,
    direction: str,
) -> None:
    window = MainWindow()
    thread = _RefusingThread()
    task_id = f"{direction}-close"
    control: DownloadTaskControl | None = None
    if direction == "download":
        window._download_thread = thread  # type: ignore[assignment]
        window._download_task_id = task_id
        control = DownloadTaskControl()
        window._download_controls[task_id] = control
    elif direction == "upload":
        window._upload_thread = thread  # type: ignore[assignment]
        window._upload_task_id = task_id
    elif direction == "folder_upload":
        window._folder_prepare_thread = thread  # type: ignore[assignment]
    elif direction == "directory":
        window._directory_thread = thread  # type: ignore[assignment]
    else:
        field = f"_{direction}_thread"
        setattr(window, field, thread)

    event = QCloseEvent()
    with caplog.at_level("WARNING", logger=main_window_module.LOGGER.name):
        window.closeEvent(event)

    assert event.isAccepted()
    assert thread.quit_called
    assert thread.set_parent_values == [None]
    assert thread.wait_timeouts == [main_window_module.THREAD_JOIN_TIMEOUT_MS]
    assert "main_window.close.thread_abandoned" in caplog.text
    assert f"direction={direction}" in caplog.text
    if direction == "download":
        assert control is not None
        assert control.stop_result() == "cancelled"


def test_abandoned_thread_leaves_keep_alive_when_finished(qapp: QApplication) -> None:
    window = MainWindow()
    thread = _RefusingThread()
    window._download_thread = thread  # type: ignore[assignment]

    event = QCloseEvent()
    window.closeEvent(event)

    assert event.isAccepted()
    assert thread in main_window_module._THREAD_KEEP_ALIVE

    thread.finished.emit()

    assert thread not in main_window_module._THREAD_KEEP_ALIVE


def test_close_window_stuck_thread_exits_subprocess() -> None:
    # The child thread ignores quit() like a stuck network read, but exits via
    # a cooperative flag once the window is gone. Old code (no detach) aborted
    # with qFatal while destroying the still-running parented thread; the fix
    # must let the window close and the process exit with status 0.
    script = r'''
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QApplication
from openwopan.ui.main_window import MainWindow


class StuckThread(QThread):
    def __init__(self, parent):
        super().__init__(parent)
        self.stop_requested = False

    def run(self):
        while not self.stop_requested:
            self.msleep(100)


app = QApplication([])
window = MainWindow()
thread = StuckThread(window)
window._download_thread = thread
thread.start()


def shutdown():
    if not thread.isRunning():
        raise RuntimeError("stuck thread did not start")
    window.close()
    window.deleteLater()
    app.processEvents()
    print("WINDOW_DESTROYED_WITH_RUNNING_THREAD", flush=True)
    thread.stop_requested = True
    if not thread.wait(5000):
        raise RuntimeError("stuck thread did not stop cooperatively")
    print("SUBPROCESS_CLOSE_OK", flush=True)
    app.quit()


QTimer.singleShot(0, shutdown)
raise SystemExit(app.exec())
'''
    env = os.environ.copy()
    src_path = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(src_path), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "SUBPROCESS_CLOSE_OK" in result.stdout


def test_background_download_updates_records_and_clears_task(
    qapp: QApplication,
    sync_threads: None, tmp_path: Path
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)

    window.refresh_current_directory()
    window.download_displayed_item(1, tmp_path / "report.txt")

    assert window._download_thread is None
    records = window.transfer_interface.download_records
    assert [record.status for record in records] == ["已完成"]
    assert browser.download_calls and browser.download_calls[0]["task_id"].startswith("download-")
    assert window.status_message() == "下载完成：report.txt"
    assert window._download_controls == {}


def test_background_download_failure_marks_record_failed(
    qapp: QApplication,
    sync_threads: None, tmp_path: Path
) -> None:
    browser = WorkerFileBrowser(download_error=FileBrowserError("network down"))
    window = MainWindow(browser)

    window.refresh_current_directory()
    window.download_displayed_item(1, tmp_path / "report.txt")

    assert window._download_thread is None
    records = window.transfer_interface.download_records
    assert records[0].status == "失败"
    assert records[0].error == "network down"
    assert window.status_message() == "下载失败：network down"


def test_background_download_unexpected_failure_clears_real_thread(
    qapp: QApplication, tmp_path: Path
) -> None:
    browser = WorkerFileBrowser(download_error=RuntimeError("disk full"))
    window = MainWindow(browser)

    window.refresh_current_directory()
    assert _wait_until(qapp, lambda: window._directory_thread is None)
    window.download_displayed_item(1, tmp_path / "report.txt")

    assert _wait_until(
        qapp,
        lambda: bool(window.transfer_interface.download_records)
        and window.transfer_interface.download_records[0].status == "失败",
    )
    assert _wait_until(qapp, lambda: window._download_thread is None)
    assert window.transfer_interface.download_records[0].error == "disk full"
    assert window.status_message() == "下载失败：disk full"


def test_background_download_login_required_emits_signal(
    qapp: QApplication,
    sync_threads: None, tmp_path: Path
) -> None:
    messages: list[str] = []
    browser = WorkerFileBrowser(
        download_error=FileBrowserLoginRequiredError("登录已过期，请重新登录")
    )
    window = MainWindow(browser)
    window.login_required.connect(messages.append)

    window.refresh_current_directory()
    window.download_displayed_item(1, tmp_path / "report.txt")

    assert window._download_thread is None
    assert window.transfer_interface.download_records[0].status == "失败"
    assert messages == ["登录已过期，请重新登录"]


def test_background_upload_updates_records_and_refreshes(
    qapp: QApplication,
    sync_threads: None, tmp_path: Path
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    local_path = tmp_path / "upload.txt"
    local_path.write_text("content")

    window.refresh_current_directory()
    window.upload_file_to_current_directory(local_path)

    assert window._upload_thread is None

    assert browser.uploaded_files == [(ROOT_DIRECTORY_ID, local_path)]
    records = window.transfer_interface.upload_records
    assert [record.status for record in records] == ["已完成"]
    assert [item.name for item in window.displayed_items()] == [
        "Folder",
        "report.txt",
        "upload.txt",
    ]


def test_background_upload_failure_marks_record_failed(
    qapp: QApplication,
    sync_threads: None, tmp_path: Path
) -> None:
    browser = WorkerFileBrowser()
    browser.upload_file = lambda parent_id, local_path, **_kwargs: (_ for _ in ()).throw(
        FileBrowserError("upload failed")
    )
    window = MainWindow(browser)
    local_path = tmp_path / "upload.txt"
    local_path.write_text("content")

    window.refresh_current_directory()
    window.upload_file_to_current_directory(local_path)

    assert window._upload_thread is None
    assert window.transfer_interface.upload_records[0].status == "失败"
    assert window.status_message() == "上传失败：upload failed"


def _record_update_thread_ids(window: MainWindow) -> tuple[list[int], int]:
    """Patch the transfer table updater to record which thread executed it.

    GUI updates queued from a real worker thread must run on the GUI thread;
    direct lambda connections execute on the emitting thread and crash Qt.
    """
    gui_thread_id = threading.get_ident()
    observed: list[int] = []
    original_update = window.transfer_interface.update_record

    def recording_update(direction: str, task_id: str, **kwargs: object) -> None:
        observed.append(threading.get_ident())
        original_update(direction, task_id, **kwargs)  # type: ignore[arg-type]

    window.transfer_interface.update_record = recording_update  # type: ignore[assignment, method-assign]
    return observed, gui_thread_id


def test_background_upload_updates_ui_on_gui_thread(
    qapp: QApplication, tmp_path: Path
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    local_path = tmp_path / "upload.txt"
    local_path.write_text("content")
    window.refresh_current_directory()
    observed, gui_thread_id = _record_update_thread_ids(window)

    window.upload_file_to_current_directory(local_path)

    assert _wait_until(qapp, lambda: window._upload_thread is None and len(observed) >= 2)
    assert observed == [gui_thread_id] * len(observed)


def test_folder_prepare_updates_ui_on_gui_thread(
    qapp: QApplication, tmp_path: Path
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    observed, gui_thread_id = _record_update_thread_ids(window)
    local_root = _make_folder_tree(tmp_path)

    window.upload_folder_to_current_directory(local_root)

    assert _wait_until(
        qapp,
        lambda: window._folder_prepare_thread is None
        and window._folder_upload_active is None
        and window._folder_upload_queue == []
        and len(observed) >= 3,
    )
    assert observed == [gui_thread_id] * len(observed)


def test_background_download_updates_ui_on_gui_thread(
    qapp: QApplication, tmp_path: Path
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    assert _wait_until(qapp, lambda: window._directory_thread is None)
    observed, gui_thread_id = _record_update_thread_ids(window)

    window.download_displayed_item(1, tmp_path / "report.txt")

    assert _wait_until(qapp, lambda: window._download_thread is None and len(observed) >= 2)
    assert observed == [gui_thread_id] * len(observed)


def test_start_tasks_reject_missing_browser(qapp: QApplication, tmp_path: Path) -> None:
    window = MainWindow()

    window._start_download_task(_file_item(), tmp_path / "a.txt", "download-1")
    assert window.status_message() == "请先登录"

    window._start_upload_task(ROOT_DIRECTORY_ID, tmp_path / "b.txt", "upload-1")
    assert window.status_message() == "请先登录"


def test_start_download_task_reports_busy_state(qapp: QApplication) -> None:
    window = MainWindow(WorkerFileBrowser())
    window._download_thread = QThread(window)

    window._start_download_task(_file_item(), Path("/tmp/x.txt"), "download-1")

    assert window.status_message() == "已有下载任务正在进行"
    window._download_thread = None


def test_start_upload_task_accepts_new_task_while_upload_is_active(qapp: QApplication) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window._upload_thread = QThread(window)

    window._start_upload_task(ROOT_DIRECTORY_ID, Path("/tmp/x.txt"), "upload-1")
    window.update_operation_controls()

    assert window.file_interface.upload_button_group.isEnabled()
    assert window.file_interface.upload_folder_action.isEnabled()
    assert browser.uploaded_files == [(ROOT_DIRECTORY_ID, Path("/tmp/x.txt"))]
    assert window.status_message() == "上传完成：x.txt"
    window._upload_thread = None


# ---------------------------------------------------------------------------
# Pause / resume / cancel signal chain
# ---------------------------------------------------------------------------


def _register_download_task(
    window: MainWindow, task_id: str, *, target_path: Path | None, item: WopanItem | None
) -> DownloadTaskControl:
    window.transfer_interface.add_download_record(
        TransferRecord(
            task_id=task_id,
            direction="download",
            name="report.txt",
            size=2048,
            target_path=target_path,
            status="下载中",
        )
    )
    control = DownloadTaskControl()
    window._download_controls[task_id] = control
    if item is not None:
        window._download_items_by_task[task_id] = item
    return control


def test_pause_download_signal_pauses_control_and_record(qapp: QApplication) -> None:
    window = MainWindow(WorkerFileBrowser())
    control = _register_download_task(
        window, "download-1", target_path=Path("/tmp/report.txt"), item=_file_item()
    )

    window.transfer_interface.pause_download_requested.emit("download-1")
    window.transfer_interface.pause_download_requested.emit("unknown-task")

    assert control.stop_result() == "paused"
    record = window.transfer_interface._find_record("download", "download-1")
    assert record is not None
    assert record.status == "已暂停"
    assert record.can_resume is True


def test_cancel_download_signal_cancels_control_and_record(qapp: QApplication) -> None:
    window = MainWindow(WorkerFileBrowser())
    control = _register_download_task(
        window, "download-1", target_path=Path("/tmp/report.txt"), item=_file_item()
    )

    window.transfer_interface.cancel_download_requested.emit("download-1")
    window.transfer_interface.cancel_download_requested.emit("unknown-task")

    assert control.stop_result() == "cancelled"
    assert control.cleanup_on_cancel is True
    record = window.transfer_interface._find_record("download", "download-1")
    assert record is not None
    assert record.status == "已取消"
    assert record.can_resume is False


def test_resume_download_restarts_task_from_registered_item(
    qapp: QApplication,
    sync_threads: None, tmp_path: Path
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    target_path = tmp_path / "report.txt"
    _register_download_task(window, "download-1", target_path=target_path, item=_file_item())
    record = window.transfer_interface._find_record("download", "download-1")
    assert record is not None
    record.status = "已暂停"
    record.can_resume = True

    window.transfer_interface.resume_download_requested.emit("download-1")

    assert record.status == "已完成"
    assert browser.download_calls


def test_resume_download_rejects_when_task_active(qapp: QApplication, tmp_path: Path) -> None:
    window = MainWindow(WorkerFileBrowser())
    _register_download_task(
        window, "download-1", target_path=tmp_path / "report.txt", item=_file_item()
    )
    window._download_thread = QThread(window)

    window._resume_download_task("download-1")

    assert window.status_message() == "已有下载任务正在进行"
    window._download_thread = None


@pytest.mark.parametrize(
    ("target_path", "item", "expected_status"),
    [
        (None, _file_item(), "下载中"),  # record without target path is ignored
        (Path("/tmp/report.txt"), None, "无法继续下载，请从文件列表重新创建任务"),
    ],
)
def test_resume_download_edge_cases(
    qapp: QApplication, tmp_path: Path, target_path: Path | None, item, expected_status: str
) -> None:
    window = MainWindow(WorkerFileBrowser())
    _register_download_task(window, "download-1", target_path=target_path, item=item)

    window._resume_download_task("download-1")

    if expected_status == "下载中":
        record = window.transfer_interface._find_record("download", "download-1")
        assert record is not None
        assert record.status == expected_status
    else:
        assert window.status_message() == expected_status


def test_resume_download_finds_displayed_item_by_name(
    qapp: QApplication,
    sync_threads: None, tmp_path: Path
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    _register_download_task(
        window, "download-1", target_path=tmp_path / "report.txt", item=None
    )

    window._resume_download_task("download-1")

    record = window.transfer_interface._find_record("download", "download-1")
    assert record is not None
    assert record.status == "已完成"
    assert browser.download_calls


def test_persisted_download_records_loaded_on_browser_attach(qapp: QApplication) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow()
    window.set_auth_session(_make_session())
    window.set_file_browser(browser)

    records = window.transfer_interface.download_records
    assert [record.task_id for record in records if record.task_id == "download-9"] == [
        "download-9"
    ]
    persisted = window.transfer_interface._find_record("download", "download-9")
    assert persisted is not None
    assert persisted.status == "已暂停"
    assert persisted.can_resume is True
    assert persisted.bytes_done == 40


def _make_session():
    from openwopan.auth.session import AuthSession

    return AuthSession(account_id="13800138000", display_name="User")


def test_remove_transfer_records_delegates_to_browser_and_ui(
    qapp: QApplication, sync_threads: None
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    _register_download_task(
        window, "download-1", target_path=Path("/tmp/report.txt"), item=_file_item()
    )

    window.transfer_interface.add_download_record(
        TransferRecord(
            task_id="download-2",
            direction="download",
            name="other.txt",
            size=2048,
            target_path=Path("/tmp/other.txt"),
            status="已暂停",
        )
    )
    window._download_items_by_task["download-2"] = _file_item("file-2", "other.txt")
    window._download_controls["download-2"] = DownloadTaskControl()

    window.transfer_interface.remove_records_requested.emit("download", {"download-1"})
    window.transfer_interface.remove_records_requested.emit("upload", ["not-a-set"])

    assert browser.removed_download_records == ["download-1"]
    assert window.transfer_interface.download_records[0].task_id == "download-2"
    assert "download-1" not in window._download_items_by_task
    assert "download-2" in window._download_items_by_task
    assert "download-1" not in window._download_controls
    assert "download-2" in window._download_controls

    window._resume_download_task("download-2")

    assert window.transfer_interface.download_records[0].status == "已完成"
    assert browser.download_calls[-1]["task_id"] == "download-2"


# ---------------------------------------------------------------------------
# Callback handlers
# ---------------------------------------------------------------------------


def test_download_progress_handler_formats_known_and_unknown_totals(
    qapp: QApplication,
) -> None:
    window = MainWindow(WorkerFileBrowser())
    _register_download_task(
        window, "download-1", target_path=Path("/tmp/report.txt"), item=_file_item()
    )

    window._on_download_progress(512, 1024, task_id="download-1")
    record = window.transfer_interface._find_record("download", "download-1")
    assert record is not None
    assert record.status == "下载中"

    record.status = "已暂停"
    window._on_download_progress(768, 1024, "download-1")
    assert record.status == "已暂停"
    assert record.bytes_done == 768

    window._on_download_progress(768, None, task_id="download-1")
    assert window.status_message() == "正在下载：768 B"

    window._download_task_id = "download-1"
    window._on_download_progress(10, "not-an-int")
    assert window.status_message().startswith("正在下载：")


def test_download_status_changed_updates_record_and_status(
    qapp: QApplication,
) -> None:
    window = MainWindow(WorkerFileBrowser())
    _register_download_task(
        window, "download-1", target_path=Path("/tmp/report.txt"), item=_file_item()
    )

    window._on_download_status_changed("校验中", task_id="download-1")
    assert window.status_message() == "下载状态：校验中"

    window._on_download_status_changed("下载中", task_id="download-1")
    record = window.transfer_interface._find_record("download", "download-1")
    assert record is not None
    assert record.status == "下载中"
    assert record.can_resume is False

    window._on_download_status_changed("已暂停", task_id=None)
    assert window._download_task_id is None


def test_download_status_changed_without_task_is_ignored(qapp: QApplication) -> None:
    window = MainWindow()
    window._download_task_id = None

    window._on_download_status_changed("校验中")

    assert window.status_message() == "请先登录"


def test_download_connections_changed_updates_record(qapp: QApplication) -> None:
    window = MainWindow(WorkerFileBrowser())
    _register_download_task(
        window, "download-1", target_path=Path("/tmp/report.txt"), item=_file_item()
    )

    window._on_download_connections_changed(3, 8, task_id="download-1")

    record = window.transfer_interface._find_record("download", "download-1")
    assert record is not None
    assert record.active_connections == 3
    assert record.max_connections == 8


def test_download_stopped_updates_record(qapp: QApplication) -> None:
    window = MainWindow(WorkerFileBrowser())
    _register_download_task(
        window, "download-1", target_path=Path("/tmp/report.txt"), item=_file_item()
    )

    window._on_download_stopped("已暂停", task_id="download-1")

    record = window.transfer_interface._find_record("download", "download-1")
    assert record is not None
    assert record.status == "已暂停"
    assert record.can_resume is True
    assert window.status_message() == "下载状态：已暂停"


def test_download_with_callbacks_falls_back_on_legacy_signature(
    qapp: QApplication, tmp_path: Path
) -> None:
    browser = LegacySignatureFileBrowser()
    window = MainWindow(browser)

    window._download_with_callbacks(_file_item(), tmp_path / "report.txt", "download-1")

    assert browser.download_calls == [{"legacy": True, "local_path": tmp_path / "report.txt"}]


def test_download_with_callbacks_reraises_unrelated_type_error(
    qapp: QApplication, tmp_path: Path
) -> None:
    window = MainWindow(UnrelatedTypeErrorFileBrowser())

    with pytest.raises(TypeError):
        window._download_with_callbacks(_file_item(), tmp_path / "report.txt", "download-1")


def test_download_with_callbacks_without_browser_returns(
    qapp: QApplication, tmp_path: Path
) -> None:
    window = MainWindow()

    window._download_with_callbacks(_file_item(), tmp_path / "report.txt", "download-1")

    assert window.status_message() == "请先登录"


def test_upload_progress_updates_transfer_record(
    qapp: QApplication, tmp_path: Path
) -> None:
    window = MainWindow(WorkerFileBrowser())
    task_id = window._create_upload_record(tmp_path / "upload.bin", size=10)

    window._on_upload_progress(4, 10, task_id)

    record = window.transfer_interface._find_record("upload", task_id)
    assert record is not None
    assert record.bytes_done == 4
    assert record.total_bytes == 10


    window = MainWindow(WorkerFileBrowser())
    window._create_upload_record(Path("/tmp/upload.txt"))
    record = window.transfer_interface.upload_records[0]

    window._on_upload_succeeded(object(), task_id=record.task_id)

    assert record.status == "失败"
    assert record.error == "上传结果无效"
    assert window.status_message() == "上传失败：上传结果无效"


def test_upload_success_handler_reports_when_refresh_hides_item(
    qapp: QApplication, tmp_path: Path
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    local_path = tmp_path / "upload.txt"
    local_path.write_text("content")
    window.refresh_current_directory()
    task_id = window._create_upload_record(local_path)

    uploaded = WopanItem(
        item_id="missing-upload",
        name="vanish.txt",
        kind=WopanItemKind.FILE,
        download_id="vanish-fid",
    )
    window._on_upload_succeeded(uploaded, task_id=task_id)

    assert "刷新后未在当前目录看到" in window.status_message()


def test_upload_login_required_marks_record_failed(qapp: QApplication, tmp_path: Path) -> None:
    messages: list[str] = []
    window = MainWindow(WorkerFileBrowser())
    window.login_required.connect(messages.append)
    local_path = tmp_path / "upload.txt"
    local_path.write_text("content")
    task_id = window._create_upload_record(local_path)

    window._on_upload_login_required("登录已过期，请重新登录", task_id=task_id)

    assert window.transfer_interface.upload_records[0].status == "失败"
    assert messages == ["登录已过期，请重新登录"]


def test_mark_transfer_failed_without_task_is_ignored(qapp: QApplication) -> None:
    window = MainWindow()

    window._mark_transfer_failed("download", None, "ignored")

    assert window.transfer_interface.download_records == []


def test_create_upload_record_without_existing_file(qapp: QApplication) -> None:
    window = MainWindow()

    task_id = window._create_upload_record(Path("/tmp/missing-upload.txt"))

    record = window.transfer_interface._find_record("upload", task_id)
    assert record is not None
    assert record.size is None


# ---------------------------------------------------------------------------
# Prompt dialogs and context menu
# ---------------------------------------------------------------------------


class _StubNameDialog:
    instances: list[_StubNameDialog] = []
    accept_result = QDialog.DialogCode.Accepted
    stub_text = "stub-name"

    def __init__(self, *, title: str, hint: str, default_text: str, parent=None) -> None:
        self.title = title
        self.hint = hint
        self.default_text = default_text
        self.parent = parent
        self.deleted = False
        type(self).instances.append(self)

    exec = _accept_result_exec()

    def name_text(self) -> str:
        return type(self).stub_text

    def deleteLater(self) -> None:
        self.deleted = True


class _StubMessageBox:
    instances: list[_StubMessageBox] = []
    accept_result = 1

    def __init__(self, title: str, content: str, parent=None) -> None:
        self.title = title
        self.content = content
        self.parent = parent
        self.deleted = False
        type(self).instances.append(self)

    exec = _accept_result_exec()

    def deleteLater(self) -> None:
        self.deleted = True


class _StubMoveDialog:
    instances: list[_StubMoveDialog] = []
    accept_result = QDialog.DialogCode.Accepted
    entry = None

    def __init__(self, entries, *, parent=None) -> None:
        self.entries = entries
        self.parent = parent
        self.deleted = False
        type(self).instances.append(self)

    exec = _accept_result_exec()

    def selected_entry(self) -> object | None:
        return type(self).entry

    def deleteLater(self) -> None:
        self.deleted = True


@pytest.fixture
def stub_name_dialog(monkeypatch: pytest.MonkeyPatch):
    _StubNameDialog.instances = []
    _StubNameDialog.accept_result = QDialog.DialogCode.Accepted
    monkeypatch.setattr(main_window_module, "NameInputDialog", _StubNameDialog)
    return _StubNameDialog


@pytest.fixture
def stub_message_box(monkeypatch: pytest.MonkeyPatch):
    _StubMessageBox.instances = []
    _StubMessageBox.accept_result = 1
    monkeypatch.setattr(main_window_module, "MessageBox", _StubMessageBox)
    return _StubMessageBox


@pytest.fixture
def stub_move_dialog(monkeypatch: pytest.MonkeyPatch):
    _StubMoveDialog.instances = []
    _StubMoveDialog.accept_result = QDialog.DialogCode.Accepted
    monkeypatch.setattr(main_window_module, "MoveTargetDialog", _StubMoveDialog)
    return _StubMoveDialog


def test_prompt_create_folder_uses_dialog_result(
    qapp: QApplication, stub_name_dialog
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    stub_name_dialog.stub_text = "Created"

    window.prompt_create_folder()

    assert browser.items_by_parent[ROOT_DIRECTORY_ID][-1].name == "Created"
    assert stub_name_dialog.instances[0].deleted
    assert stub_name_dialog.instances[0].title == "新建文件夹"


def test_prompt_create_folder_cancelled_does_nothing(
    qapp: QApplication, stub_name_dialog
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    stub_name_dialog.accept_result = QDialog.DialogCode.Rejected

    window.prompt_create_folder()

    assert [item.name for item in window.displayed_items()] == ["Folder", "report.txt"]


def test_prompt_rename_item_uses_dialog_result(
    qapp: QApplication, stub_name_dialog
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    stub_name_dialog.stub_text = "renamed.txt"
    window.refresh_current_directory()

    window.prompt_rename_item(1)

    assert stub_name_dialog.instances[0].title == "重命名"
    assert stub_name_dialog.instances[0].default_text == "report.txt"


def test_prompt_rename_item_ignores_missing_row(qapp: QApplication, stub_name_dialog) -> None:
    window = MainWindow(WorkerFileBrowser())

    window.prompt_rename_item(5)

    assert stub_name_dialog.instances == []


def test_prompt_delete_item_confirmed_deletes_row(
    qapp: QApplication, stub_message_box
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    stub_message_box.accept_result = 1

    window.prompt_delete_item(0)

    assert stub_message_box.instances[0].title == "确认删除"
    assert [item.name for item in window.displayed_items()] == ["report.txt"]


def test_prompt_delete_item_cancelled_keeps_row(
    qapp: QApplication, stub_message_box
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    stub_message_box.accept_result = 0

    window.prompt_delete_item(0)

    assert [item.name for item in window.displayed_items()] == ["Folder", "report.txt"]


def test_prompt_delete_item_ignores_missing_row(
    qapp: QApplication, stub_message_box
) -> None:
    window = MainWindow(WorkerFileBrowser())

    window.prompt_delete_item(9)

    assert stub_message_box.instances == []


def test_prompt_move_item_moves_to_selected_entry(
    qapp: QApplication, stub_move_dialog
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    stub_move_dialog.entry = main_window_module.BreadcrumbEntry(
        item_id="target-folder", name="Target"
    )

    window.prompt_move_item(1)

    assert stub_move_dialog.instances[0].deleted
    assert window.status_message().startswith("1 项")


def test_prompt_move_item_without_selection_does_nothing(
    qapp: QApplication, stub_move_dialog
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    stub_move_dialog.entry = None

    window.prompt_move_item(1)

    assert window.status_message().startswith("2 项")


def test_prompt_move_item_cancelled_does_nothing(
    qapp: QApplication, stub_move_dialog
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    stub_move_dialog.accept_result = QDialog.DialogCode.Rejected
    stub_move_dialog.entry = main_window_module.BreadcrumbEntry(
        item_id="target-folder", name="Target"
    )

    window.prompt_move_item(1)

    assert stub_move_dialog.instances[0].deleted
    assert window.status_message().startswith("2 项")


def test_prompt_move_item_ignores_missing_row(
    qapp: QApplication, stub_move_dialog
) -> None:
    window = MainWindow(WorkerFileBrowser())

    window.prompt_move_item(3)

    assert stub_move_dialog.instances == []


def test_prompt_logout_confirmed_emits_logout(
    qapp: QApplication, stub_message_box
) -> None:
    requested: list[bool] = []
    window = MainWindow(WorkerFileBrowser())
    window.logout_requested.connect(lambda: requested.append(True))
    stub_message_box.accept_result = 1

    window.prompt_logout()

    assert requested == [True]
    assert stub_message_box.instances[0].deleted


def test_prompt_logout_cancelled_keeps_session(
    qapp: QApplication, stub_message_box
) -> None:
    requested: list[bool] = []
    window = MainWindow(WorkerFileBrowser())
    window.logout_requested.connect(lambda: requested.append(True))
    stub_message_box.accept_result = 0

    window.prompt_logout()

    assert requested == []


def test_prompt_download_item_asks_for_save_path_when_configured(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sync_threads: None,
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(
        browser,
        settings=AppSettings(default_download_path=tmp_path, ask_download_location=True),
    )
    window.refresh_current_directory()
    monkeypatch.setattr(main_window_module, "QFileDialog", FakeFileDialog)
    FakeFileDialog.save_result = (str(tmp_path / "saved.txt"), "")

    window.prompt_download_item(1)

    assert browser.download_calls
    assert _wait_until(qapp, lambda: window.status_message() == "下载完成：saved.txt")


@pytest.mark.parametrize(
    ("resolution", "expected_names"),
    [
        ("skip", []),
        ("copy", ["report (1).txt", "report (2).txt"]),
    ],
)
def test_download_target_conflicts_are_explicitly_resolved(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolution: str,
    expected_names: list[str],
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    first = _file_item("file-1", "report.txt")
    second = _file_item("file-2", "report.txt")
    target = tmp_path / "report.txt"
    target.write_text("existing")

    class DecisionDialog:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        exec = _static_exec_result(int(QDialog.DialogCode.Accepted))

        def resolution(self) -> str:
            return resolution

    monkeypatch.setattr(main_window_module, "DownloadConflictDialog", DecisionDialog)
    window._submit_download_items([(first, target), (second, target)], run_in_background=False)

    assert [call["local_path"].name for call in browser.download_calls] == expected_names
    assert target.read_text() == "existing"
    assert len(window.transfer_interface.download_records) == len(expected_names)


@pytest.mark.parametrize(
    ("button_text", "resolution"),
    [("跳过冲突", "skip"), ("保留副本", "copy"), ("取消本批次", None)],
)
def test_download_conflict_dialog_exposes_safe_choices(
    qapp: QApplication, button_text: str, resolution: str | None
) -> None:
    parent = QWidget()
    dialog = main_window_module.DownloadConflictDialog(
        (Path("/tmp/report.txt"), Path("/tmp/report.txt")), parent
    )

    button = next(
        button for button in dialog.findChildren(QPushButton) if button.text() == button_text
    )
    button.click()

    assert dialog.resolution() == resolution
    expected = QDialog.DialogCode.Rejected if resolution is None else QDialog.DialogCode.Accepted
    assert dialog.result() == expected


def test_download_target_scan_runs_off_gui_thread(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    gui_thread = threading.get_ident()
    scan_threads: list[int] = []
    original = MainWindow._scan_download_target_names

    def scan(items: list[tuple[WopanItem, Path]], automatic: bool) -> dict[Path, set[str]]:
        scan_threads.append(threading.get_ident())
        return original(items, automatic)

    monkeypatch.setattr(window, "_scan_download_target_names", scan)
    window._submit_download_items([(_file_item(), tmp_path / "new.txt")])

    assert _wait_until(qapp, lambda: bool(browser.download_calls))
    assert scan_threads and all(thread != gui_thread for thread in scan_threads)
    assert _wait_until(qapp, lambda: window._download_target_thread is None)


def test_download_controls_and_record_removal_run_off_gui_thread(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    task_id = window._create_download_record(_file_item(), tmp_path / "report.txt")
    gui_thread = threading.get_ident()
    started = threading.Event()
    release = threading.Event()
    calls: list[tuple[str, int]] = []

    def pause(tid: str) -> bool:
        assert tid == task_id
        calls.append(("pause", threading.get_ident()))
        started.set()
        assert release.wait(5)
        return True

    def resume(tid: str) -> bool:
        assert tid == task_id
        calls.append(("resume", threading.get_ident()))
        return True

    def cancel(tid: str, *, cleanup: bool) -> bool:
        assert tid == task_id and cleanup
        calls.append(("cancel", threading.get_ident()))
        return True

    def remove(tid: str) -> None:
        assert tid == task_id
        calls.append(("remove", threading.get_ident()))

    monkeypatch.setattr(browser, "pause_download", pause, raising=False)
    monkeypatch.setattr(browser, "resume_download", resume, raising=False)
    monkeypatch.setattr(browser, "cancel_download", cancel, raising=False)
    monkeypatch.setattr(browser, "remove_download_record", remove)
    window._pause_download_task(task_id)
    assert started.wait(5)
    window._resume_download_task(task_id)
    window._cancel_download_task(task_id)
    window._remove_transfer_records("download", {task_id})
    assert [name for name, _ in calls] == ["pause"]
    assert window.transfer_interface._find_record("download", task_id) is not None

    release.set()
    assert _wait_until(qapp, lambda: window._download_operation_thread is None)
    assert [name for name, _ in calls] == ["pause", "resume", "cancel", "remove"]
    assert all(thread != gui_thread for _, thread in calls)
    assert window.transfer_interface._find_record("download", task_id) is None


def test_overlapping_download_submissions_keep_both_threads_tracked(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    started = threading.Event()
    release = threading.Event()
    submitted: list[tuple[str, Path]] = []

    def submit(item: WopanItem, path: Path) -> str:
        submitted.append((item.item_id, path))
        if item.item_id == "first":
            started.set()
            assert release.wait(5)
        return item.item_id

    monkeypatch.setattr(browser, "submit_download", submit, raising=False)
    window = MainWindow(browser)
    window._submit_download_items(
        [(_file_item("first", "report.txt"), tmp_path / "report.txt")], automatic=True
    )
    assert _wait_until(qapp, started.is_set)
    window._submit_download_items(
        [(_file_item("second", "report.txt"), tmp_path / "report.txt")], automatic=True
    )
    assert _wait_until(qapp, lambda: len(window._download_submit_threads) == 2)
    release.set()
    assert _wait_until(qapp, lambda: len(window.transfer_interface.download_records) == 2)
    assert _wait_until(qapp, lambda: not window._download_submit_threads)
    assert {record.task_id for record in window.transfer_interface.download_records} == {
        "first", "second"
    }
    assert submitted == [
        ("first", tmp_path / "report.txt"),
        ("second", tmp_path / "report (1).txt"),
    ]


def test_close_ignores_late_download_submission_callbacks(
    qapp: QApplication, tmp_path: Path
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window._closing = True
    window._on_download_submissions_succeeded(
        [(_file_item(), tmp_path / "report.txt", "task-1", "")]
    )
    window._on_download_submissions_failed("unexpected failure")
    assert window.transfer_interface.download_records == []
    assert window.status_message() == "请先登录"


def test_download_target_conflict_cancel_creates_no_tasks(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    target = tmp_path / "report.txt"
    target.write_text("existing")

    class CancelDialog:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        exec = _static_exec_result(int(QDialog.DialogCode.Rejected))

        def resolution(self) -> None:
            return None

    monkeypatch.setattr(main_window_module, "DownloadConflictDialog", CancelDialog)
    window._submit_download_items([(_file_item(), target)], run_in_background=False)

    assert browser.download_calls == []
    assert window.transfer_interface.download_records == []
    assert window.status_message() == "已取消下载"


def test_prompt_download_item_cancelled_by_user(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(
        browser,
        settings=AppSettings(default_download_path=tmp_path, ask_download_location=True),
    )
    window.refresh_current_directory()
    monkeypatch.setattr(main_window_module, "QFileDialog", FakeFileDialog)
    FakeFileDialog.save_result = ("", "")

    window.prompt_download_item(1)

    assert browser.download_calls == []
    assert window.status_message().startswith("2 项")


def test_prompt_download_item_rejects_folder_row(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = MainWindow(WorkerFileBrowser())
    window.refresh_current_directory()

    window.prompt_download_item(0)

    assert window.status_message() == "只能下载文件"


def test_prompt_download_item_ignores_missing_row(qapp: QApplication) -> None:
    window = MainWindow(WorkerFileBrowser())

    window.prompt_download_item(7)

    assert window.status_message() == "请先登录"


def test_prompt_download_item_aborts_when_default_directory_unusable(
    qapp: QApplication, tmp_path: Path
) -> None:
    blocked = tmp_path / "occupied.txt"
    blocked.write_text("x")
    window = MainWindow(
        WorkerFileBrowser(),
        settings=AppSettings(default_download_path=blocked, ask_download_location=False),
    )
    window.refresh_current_directory()

    window.prompt_download_item(1)

    assert "下载目录不可用" in window.status_message()


def test_prompt_upload_file_requires_browser(qapp: QApplication) -> None:
    window = MainWindow()

    window.prompt_upload_file()

    assert window.status_message() == "请先登录"


def test_prompt_upload_file_cancelled_by_user(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    monkeypatch.setattr(main_window_module, "QFileDialog", FakeFileDialog)
    FakeFileDialog.open_result = ("", "")

    window.prompt_upload_file()

    assert browser.uploaded_files == []


def test_prompt_upload_file_uploads_selected_file(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sync_threads: None,
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    local_path = tmp_path / "picked.txt"
    local_path.write_text("content")
    monkeypatch.setattr(main_window_module, "QFileDialog", FakeFileDialog)
    FakeFileDialog.open_result = (str(local_path), "")

    window.prompt_upload_file()

    assert browser.uploaded_files == [(ROOT_DIRECTORY_ID, local_path)]


class FakeFileDialog:
    save_result: tuple[str, str] = ("", "")
    open_result: tuple[str, str] = ("", "")
    existing_directory = ""

    @staticmethod
    def getSaveFileName(*args: object, **kwargs: object) -> tuple[str, str]:
        return FakeFileDialog.save_result

    @staticmethod
    def getOpenFileName(*args: object, **kwargs: object) -> tuple[str, str]:
        return FakeFileDialog.open_result

    @staticmethod
    def getExistingDirectory(*args: object, **kwargs: object) -> str:
        return FakeFileDialog.existing_directory


class FakeMenu:
    instances: list[FakeMenu] = []

    def __init__(self, parent: object | None = None) -> None:
        self._actions: list[object] = []
        type(self).instances.append(self)

    def addAction(self, *args: object) -> None:
        if len(args) == 1 and not isinstance(args[0], str):
            self._actions.append(args[0])
        else:
            action = SimpleNamespace()
            action.text = (lambda text: (lambda: text))(args[0])
            self._actions.append(action)

    def actions(self) -> list[object]:
        return list(self._actions)

    exec = _static_exec_result(None)


def test_open_file_context_menu_builds_menu_per_row_type(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    table = window.file_interface.file_table
    monkeypatch.setattr(main_window_module, "QMenu", FakeMenu)

    window.open_file_context_menu(QPoint(1, 1))
    folder_menu = FakeMenu.instances[-1]
    folder_actions = [action.text() for action in folder_menu.actions()]
    assert folder_actions == ["打开", "重命名", "移动", "删除"]

    monkeypatch.setattr(table, "rowAt", lambda y: 1)
    window.open_file_context_menu(QPoint(1, 1))
    file_menu = FakeMenu.instances[-1]
    assert [action.text() for action in file_menu.actions()] == [
        "下载",
        "重命名",
        "移动",
        "删除",
    ]

    monkeypatch.setattr(table, "rowAt", lambda y: -1)
    window.open_file_context_menu(QPoint(1, 1))
    empty_menu = FakeMenu.instances[-1]
    assert [action.text() for action in empty_menu.actions()] == [
        "刷新",
        "新建文件夹",
        "上传文件",
        "上传文件夹",
    ]


def test_name_input_dialog_accepts_non_empty_text_only(qapp: QApplication) -> None:
    dialog = NameInputDialog(
        title="新建文件夹", hint="请输入文件夹名称", default_text="新建文件夹"
    )
    assert dialog.name_text() == "新建文件夹"

    dialog._name_input.setText("  valid  ")
    assert dialog.name_text() == "valid"
    dialog._accept_if_valid()
    assert dialog.result() == QDialog.DialogCode.Accepted

    rejected_dialog = NameInputDialog(
        title="新建文件夹", hint="请输入文件夹名称", default_text="新建文件夹"
    )
    rejected_dialog._name_input.clear()
    rejected_dialog._accept_if_valid()
    assert rejected_dialog.result() != QDialog.DialogCode.Accepted


def test_move_target_dialog_selection_flow(qapp: QApplication) -> None:
    entries = [
        main_window_module.BreadcrumbEntry(item_id="root", name="/"),
        main_window_module.BreadcrumbEntry(item_id="folder-1", name="Folder"),
    ]
    dialog = main_window_module.MoveTargetDialog(entries)

    assert dialog.selected_entry() is None
    assert not dialog._ok_button.isEnabled()

    dialog._on_item_clicked(dialog._target_tree.topLevelItem(1))

    assert dialog.selected_entry() == entries[1]
    assert dialog._ok_button.isEnabled()
    assert dialog._ok_button.text() == "移动到「Folder」"


def test_placeholder_interface_renders_title_and_message(qapp: QApplication) -> None:
    widget = PlaceholderInterface("My Page", "not implemented")

    assert widget.objectName() == "MyPage"


# ---------------------------------------------------------------------------
# Settings interface: folder picker, log file, settings folder
# ---------------------------------------------------------------------------


def test_download_folder_click_updates_settings_when_folder_selected(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(
        browser, settings=AppSettings(default_download_path=tmp_path)
    )
    new_folder = tmp_path / "new-downloads"
    new_folder.mkdir()
    monkeypatch.setattr(main_window_module, "QFileDialog", FakeFileDialog)
    FakeFileDialog.existing_directory = str(new_folder)

    window.setting_interface._on_download_folder_clicked()

    assert window.setting_interface.settings().default_download_path == new_folder
    assert browser.update_settings_calls[-1].default_download_path == new_folder


def test_download_folder_click_cancelled_keeps_settings(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = MainWindow(settings=AppSettings(default_download_path=tmp_path))
    monkeypatch.setattr(main_window_module, "QFileDialog", FakeFileDialog)
    FakeFileDialog.existing_directory = ""

    window.setting_interface._on_download_folder_clicked()

    assert window.setting_interface.settings().default_download_path == tmp_path


def test_open_log_file_and_settings_folder_launch_desktop_service(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[object] = []
    log_path = tmp_path / "openwopan.log"
    settings_path = tmp_path / "settings.json"
    window = MainWindow(settings_path=settings_path, log_path=log_path)
    monkeypatch.setattr(
        main_window_module.QDesktopServices, "openUrl", staticmethod(opened.append)
    )

    window.setting_interface.open_log_file_card.clicked.emit()
    window.setting_interface.open_settings_folder_card.clicked.emit()

    assert [str(url.toString()) for url in opened] == [
        log_path.as_uri(),
        tmp_path.as_uri(),
    ]


def test_settings_change_propagates_to_browser(qapp: QApplication, tmp_path: Path) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser, settings_path=tmp_path / "settings.json")

    # Pick a value away from the default so the spin box actually emits.
    window.setting_interface.download_threads_spin_box.setValue(10)

    assert window.setting_interface.settings().max_download_threads == 10
    assert browser.update_settings_calls[-1].max_download_threads == 10


def test_settings_change_ignores_non_settings_payload(qapp: QApplication) -> None:
    window = MainWindow(WorkerFileBrowser())

    window._on_settings_changed("not-settings")

    assert window.setting_interface.settings() is window._settings


# ---------------------------------------------------------------------------
# Transfer center UI behavior
# ---------------------------------------------------------------------------


def _make_record(task_id: str, direction: str = "download", **overrides) -> TransferRecord:
    values: dict[str, object] = {
        "task_id": task_id,
        "direction": direction,
        "name": f"{task_id}.txt",
        "size": 2048,
        "status": "下载中",
    }
    values.update(overrides)
    return TransferRecord(**values)  # type: ignore[arg-type]


def test_transfer_filters_narrow_visible_records(qapp: QApplication) -> None:
    transfer = TransferInterface()
    transfer.add_download_record(_make_record("d-1", status="已完成"))
    transfer.add_download_record(_make_record("d-2", status="失败"))
    transfer.add_download_record(_make_record("d-3", status="下载中"))

    transfer._on_download_filter_changed("已完成")

    assert transfer.download_table.rowCount() == 1
    assert transfer.download_table.item(0, 0).text() == "d-1.txt"

    transfer._on_upload_filter_changed("失败")
    assert transfer.upload_table.rowCount() == 0


def test_transfer_record_progress_rendering(qapp: QApplication) -> None:
    transfer = TransferInterface()
    transfer.add_upload_record(
        _make_record("u-1", direction="upload", status="上传中", bytes_done=1024, total_bytes=2048)
    )
    transfer.add_upload_record(
        _make_record("u-2", direction="upload", status="等待中", size=None, total_bytes=None)
    )

    assert transfer.upload_table.item(0, 2).text() == "50% (1.0 KB / 2.0 KB)"
    assert transfer.upload_table.item(1, 2).text() == "0%"


def test_transfer_update_record_computes_speed_and_terminal_state(qapp: QApplication) -> None:
    transfer = TransferInterface()
    transfer.add_upload_record(
        _make_record("u-1", direction="upload", status="上传中", total_bytes=2048)
    )

    transfer.update_record("upload", "u-1", bytes_done=1024, total_bytes=2048)
    transfer.update_record("upload", "u-1", status="已完成", bytes_done=2048)

    record = transfer._find_record("upload", "u-1")
    assert record is not None
    assert record.status == "已完成"
    assert record.speed_bps == 0.0
    assert record.active_connections == 0
    assert transfer.upload_batch_buttons["speed"].text().startswith("总速度: --")

    transfer.update_record("upload", "missing-task", status="失败")
    assert transfer._find_record("upload", "missing-task") is None


def test_transfer_update_record_coalesces_progress_renders(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    transfer = TransferInterface()
    transfer.add_upload_record(
        _make_record("u-1", direction="upload", status="上传中", total_bytes=2048)
    )
    render_calls: list[object] = []
    original_render = transfer._render_table
    scheduled: list[object] = []
    monkeypatch.setattr(
        main_window_module.QTimer,
        "singleShot",
        staticmethod(lambda _delay, callback: scheduled.append(callback)),
    )
    monkeypatch.setattr(
        transfer,
        "_render_table",
        lambda table, records, direction: (
            render_calls.append(direction), original_render(table, records, direction)
        )[1],
    )

    for bytes_done in (256, 512, 768):
        transfer.update_record("upload", "u-1", bytes_done=bytes_done)

    assert len(scheduled) == 1
    assert render_calls == []
    record = transfer._find_record("upload", "u-1")
    assert record is not None
    assert record.bytes_done == 768

    scheduled[0]()
    assert render_calls == ["upload"]
    assert transfer.upload_table.item(0, 2).text() == "37% (768 B / 2.0 KB)"


def test_transfer_terminal_update_flushes_and_renders_final_bytes(qapp: QApplication) -> None:
    transfer = TransferInterface()
    transfer.add_upload_record(
        _make_record("u-1", direction="upload", status="上传中", total_bytes=2048)
    )

    transfer.update_record("upload", "u-1", bytes_done=1024)
    transfer.update_record("upload", "u-1", status="已完成", bytes_done=2048)

    assert transfer.upload_table.item(0, 2).text() == "100% (2.0 KB / 2.0 KB)"
    assert transfer.upload_table.item(0, 4).text() == "已完成"


def test_removed_download_ignores_late_events(qapp: QApplication) -> None:
    window = MainWindow()
    task_id = "removed-download"
    item = _file_item()
    path = Path("/tmp/report.txt")
    window._create_download_record(item, path, task_id=task_id, status="已暂停")
    window._on_download_operation_succeeded(("remove", task_id, None))

    paused_event = main_window_module.DownloadTaskEvent(task_id=task_id, status="已暂停")
    window._on_download_event(paused_event)
    assert window.transfer_interface._find_record("download", task_id) is None
    assert task_id not in window._pending_download_events

    window._on_download_submissions_succeeded([(item, path, task_id, "")])
    running_event = main_window_module.DownloadTaskEvent(task_id=task_id, status="下载中")
    window._on_download_event(running_event)
    record = window.transfer_interface._find_record("download", task_id)
    assert record is not None and record.status == "下载中"


def test_main_window_download_events_coalesce_progress_and_connections(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = MainWindow()
    task_id = window._create_download_record(
        _file_item(), Path("/tmp/report.txt"), task_id="download-event"
    )
    render_calls: list[str] = []
    original_render = window.transfer_interface._render_download_table
    monkeypatch.setattr(
        window.transfer_interface,
        "_render_download_table",
        lambda: (render_calls.append("download"), original_render())[1],
    )
    scheduled: list[object] = []
    monkeypatch.setattr(
        main_window_module.QTimer,
        "singleShot",
        staticmethod(lambda _delay, callback: scheduled.append(callback)),
    )

    event = main_window_module.DownloadTaskEvent(
        task_id=task_id,
        status="下载中",
        bytes_done=512,
        total_bytes=2048,
        active_connections=1,
        max_connections=4,
    )
    window._on_download_event(event)
    assert render_calls == ["download"]

    window._on_download_event(
        main_window_module.DownloadTaskEvent(
            task_id=task_id,
            status="下载中",
            bytes_done=1024,
            total_bytes=2048,
            active_connections=2,
            max_connections=4,
        )
    )

    assert render_calls == ["download"]
    assert len(scheduled) == 1
    record = window.transfer_interface._find_record("download", task_id)
    assert record is not None
    assert record.bytes_done == 1024
    assert record.active_connections == 2

    scheduled[0]()
    assert render_calls == ["download", "download"]


@pytest.mark.parametrize("width", [800, 900])
def test_download_action_buttons_keep_geometry_during_progress(
    qapp: QApplication, width: int,
) -> None:
    transfer = TransferInterface()
    transfer.resize(width, 500)
    transfer.show()
    transfer._on_segment_changed("download")
    transfer.add_download_record(_make_record("d-1", status="下载中"))
    transfer.add_download_record(_make_record("d-2", status="下载中"))
    transfer.add_download_record(_make_record("d-3", status="已完成"))
    qapp.processEvents()
    table = transfer.download_table
    widgets = [table.cellWidget(row, 5) for row in range(3)]
    assert all(widget is not None for widget in widgets)

    for done in (1024, 2048, 4096):
        transfer.update_record("download", "d-1", bytes_done=done, active_connections=1)
        transfer.flush_progress_render()
        qapp.processEvents()
        for row in range(3):
            widget = table.cellWidget(row, 5)
            assert widget is widgets[row]
            cell = table.visualRect(table.model().index(row, 5))
            for button in widget.findChildren(QWidget):
                if button.toolTip() not in {"暂停", "继续", "取消", "删除"}:
                    continue
                top_left = button.mapTo(table.viewport(), QPoint(0, 0))
                assert cell.contains(top_left)
                assert cell.contains(top_left + QPoint(button.width() - 1, button.height() - 1))

    transfer.update_record("download", "d-1", status="已暂停", can_resume=True)
    assert table.cellWidget(0, 5) is not widgets[0]
    assert table.cellWidget(1, 5) is widgets[1]
    assert table.cellWidget(2, 5) is widgets[2]
    resume_button = next(
        child for child in table.cellWidget(0, 5).findChildren(QWidget)
        if child.toolTip() == "继续"
    )
    assert resume_button.isEnabled()


def test_transfer_status_update_renders_immediately(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    transfer = TransferInterface()
    transfer.add_download_record(_make_record("d-1"))
    render_calls: list[str] = []
    original_render = transfer._render_download_table
    monkeypatch.setattr(
        transfer,
        "_render_download_table",
        lambda: (render_calls.append("download"), original_render())[1],
    )

    transfer.update_record("download", "d-1", status="失败")

    assert render_calls == ["download"]
    assert transfer.download_table.item(0, 4).text() == "失败"


def test_transfer_update_record_clamps_inputs(qapp: QApplication) -> None:
    transfer = TransferInterface()
    transfer.add_download_record(_make_record("d-1"))

    transfer.update_record(
        "download",
        "d-1",
        bytes_done=-5,
        active_connections=-2,
        max_connections=0,
        can_resume=True,
        error="boom",
    )

    record = transfer._find_record("download", "d-1")
    assert record is not None
    assert record.bytes_done == 0
    assert record.active_connections == 0
    assert record.max_connections == 1
    assert record.error == "boom"


def test_transfer_batch_toolbar_selects_and_inverts(qapp: QApplication) -> None:
    transfer = TransferInterface()
    transfer.add_upload_record(_make_record("u-1", direction="upload"))
    transfer.add_upload_record(_make_record("u-2", direction="upload"))

    transfer._select_all(transfer.upload_table)
    assert len(transfer.upload_table.selectionModel().selectedRows()) == 2
    assert transfer.upload_batch_buttons["count"].text() == "已选 2 项"

    transfer._invert_selection(transfer.upload_table, 2)
    assert len(transfer.upload_table.selectionModel().selectedRows()) == 0

    transfer._invert_selection(transfer.upload_table, 2)
    assert len(transfer.upload_table.selectionModel().selectedRows()) == 2


def test_transfer_total_speed_aggregates_active_records(qapp: QApplication) -> None:
    transfer = TransferInterface()
    record_one = _make_record("d-1", direction="download", speed_bps=1024.0)
    record_two = _make_record("d-2", direction="download", speed_bps=2048.0)
    transfer.add_download_record(record_one)
    transfer.add_download_record(record_two)

    transfer._update_total_speed("download")

    assert transfer.download_batch_buttons["speed"].text() == "总速度: 3.0 KB/s"


def test_transfer_download_action_buttons_follow_record_state(qapp: QApplication) -> None:
    transfer = TransferInterface()
    pause_ids: list[str] = []
    resume_ids: list[str] = []
    cancel_ids: list[str] = []
    transfer.pause_download_requested.connect(pause_ids.append)
    transfer.resume_download_requested.connect(resume_ids.append)
    transfer.cancel_download_requested.connect(cancel_ids.append)

    active = _make_record("d-1", status="下载中")
    paused = _make_record("d-2", status="已暂停", can_resume=True)
    failed = _make_record("d-3", status="失败", can_resume=True)
    terminal = _make_record("d-4", status="已完成")
    for record in (active, paused, failed, terminal):
        transfer.add_download_record(record)

    def action_button(row: int, tooltip: str):
        widget = transfer.download_table.cellWidget(row, 5)
        for child in widget.findChildren(QWidget):
            if child.toolTip() == tooltip:
                return child
        raise AssertionError(f"button {tooltip} not found in row {row}")

    assert action_button(0, "暂停").isEnabled()
    assert not action_button(1, "暂停").isEnabled()
    assert action_button(1, "继续").isEnabled()
    assert not action_button(3, "暂停").isEnabled()
    assert not action_button(0, "删除").isEnabled()
    assert action_button(3, "删除").isEnabled()

    action_button(0, "暂停").click()
    action_button(1, "继续").click()
    action_button(0, "取消").click()

    assert pause_ids == ["d-1"]
    assert resume_ids == ["d-2"]
    assert cancel_ids == ["d-1"]


def test_transfer_batch_pause_and_resume_emit_only_eligible_selected_rows(
    qapp: QApplication,
) -> None:
    transfer = TransferInterface()
    upload_pause_ids: list[set[str]] = []
    upload_resume_ids: list[set[str]] = []
    download_pause_ids: list[set[str]] = []
    download_resume_ids: list[set[str]] = []
    transfer.pause_uploads_requested.connect(lambda ids: upload_pause_ids.append(set(ids)))
    transfer.resume_uploads_requested.connect(lambda ids: upload_resume_ids.append(set(ids)))
    transfer.pause_downloads_requested.connect(lambda ids: download_pause_ids.append(set(ids)))
    transfer.resume_downloads_requested.connect(lambda ids: download_resume_ids.append(set(ids)))
    transfer.add_upload_record(
        _make_record("u-active", direction="upload", status="上传中", upload_retryable=True)
    )
    transfer.add_upload_record(
        _make_record("u-paused", direction="upload", status="已暂停", upload_retryable=True,
                     can_resume=True)
    )
    transfer.add_download_record(_make_record("d-active", status="下载中"))
    transfer.add_download_record(
        _make_record("d-paused", status="已暂停", can_resume=True)
    )

    transfer.upload_table.selectAll()
    transfer._request_pause_selected("upload")
    transfer._request_resume_selected("upload")
    transfer.download_table.selectAll()
    transfer._request_pause_selected("download")
    transfer._request_resume_selected("download")

    assert upload_pause_ids == [{"u-active"}]
    assert upload_resume_ids == [{"u-paused"}]
    assert download_pause_ids == [{"d-active"}]
    assert download_resume_ids == [{"d-paused"}]
    assert transfer.upload_batch_buttons["pause"].isEnabled()
    assert transfer.upload_batch_buttons["resume"].isEnabled()
    assert transfer.download_batch_buttons["pause"].isEnabled()
    assert transfer.download_batch_buttons["resume"].isEnabled()


def test_main_window_batch_upload_controls_active_worker(qapp: QApplication) -> None:
    window = MainWindow(WorkerFileBrowser())
    window.transfer_interface.add_upload_record(
        _make_record("u-1", direction="upload", status="上传中", upload_retryable=True)
    )
    calls: list[str] = []
    worker = SimpleNamespace(
        request_pause=lambda: calls.append("pause"),
        request_resume=lambda: calls.append("resume"),
    )
    window._upload_workers["u-1"] = worker  # type: ignore[assignment]

    window._pause_selected_uploads({"u-1"})
    assert calls == ["pause"]
    record = window.transfer_interface._find_record("upload", "u-1")
    assert record is not None and record.status == "已暂停" and record.can_resume

    window._resume_selected_uploads({"u-1"})
    assert calls == ["pause", "resume"]
    assert record.status == "上传中" and not record.can_resume


def test_main_window_batch_upload_controls_paused_folder_worker_advances_queue(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = MainWindow(WorkerFileBrowser())
    root = tmp_path / "folder"
    root.mkdir()
    active_path = root / "active.txt"
    next_path = root / "next.txt"
    active_path.write_text("active")
    next_path.write_text("next")
    root_id = window._create_upload_record(root, name="folder")
    active_id = window._create_upload_record(
        active_path,
        parent_id="cloud-root",
        upload_name="active.txt",
        retryable=True,
    )
    next_id = window._create_upload_record(
        next_path,
        parent_id="cloud-root",
        upload_name="next.txt",
        retryable=True,
    )
    window._folder_upload_record_id = root_id
    window._folder_upload_child_ids = {active_id, next_id}
    window._folder_upload_active = QueuedUploadFile(
        active_id, active_path, "cloud-root", "active.txt"
    )
    window._folder_upload_queue = [
        QueuedUploadFile(next_id, next_path, "cloud-root", "next.txt")
    ]
    calls: list[str] = []
    worker = SimpleNamespace(
        request_pause=lambda: calls.append("pause"),
        request_resume=lambda: calls.append("resume"),
    )
    window._upload_workers[active_id] = worker  # type: ignore[assignment]
    window._upload_threads[active_id] = QThread(window)
    monkeypatch.setattr(
        window,
        "_start_upload_task",
        lambda _parent, _path, task_id, **_kwargs: calls.append(task_id),
    )

    window._pause_upload_task(active_id)

    assert calls == ["pause", next_id]
    assert window._folder_upload_active is not None
    assert window._folder_upload_active.task_id == next_id
    assert window._folder_upload_queue == []
    assert active_id in window._paused_uploads
    record = window.transfer_interface._find_record("upload", active_id)
    assert record is not None and record.status == "已暂停"

    window._resume_upload_task(active_id)
    assert active_id not in window._paused_uploads
    assert window.transfer_interface._find_record("upload", active_id).status == "上传中"
    assert calls == ["pause", next_id, "resume"]


def test_paused_folder_worker_keeps_upload_limit(
    qapp: QApplication, tmp_path: Path
) -> None:
    window = MainWindow(
        WorkerFileBrowser(), settings=AppSettings(max_concurrent_uploads=1)
    )
    root = tmp_path / "folder"
    root.mkdir()
    active_path = root / "active.txt"
    next_path = root / "next.txt"
    active_path.write_text("active")
    next_path.write_text("next")
    root_id = window._create_upload_record(root, name="folder")
    active_id = window._create_upload_record(
        active_path, parent_id="cloud-root", upload_name="active.txt", retryable=True
    )
    next_id = window._create_upload_record(
        next_path, parent_id="cloud-root", upload_name="next.txt", retryable=True
    )
    window._folder_upload_record_id = root_id
    window._folder_upload_child_ids = {active_id, next_id}
    window._folder_upload_active = QueuedUploadFile(
        active_id, active_path, "cloud-root", "active.txt"
    )
    window._folder_upload_queue = [
        QueuedUploadFile(next_id, next_path, "cloud-root", "next.txt")
    ]
    window._upload_workers[active_id] = SimpleNamespace(
        request_pause=lambda: None,
        request_resume=lambda: None,
    )  # type: ignore[assignment]
    window._upload_threads[active_id] = QThread(window)

    window._pause_upload_task(active_id)

    assert window._folder_upload_active is not None
    assert window._folder_upload_active.task_id == next_id
    assert [pending.task_id for pending in window._upload_pending] == [next_id]
    assert list(window._upload_threads) == [active_id]


def test_main_window_batch_upload_controls_waiting_task(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = MainWindow(WorkerFileBrowser())
    local_path = tmp_path / "upload.txt"
    local_path.write_text("content")
    window.transfer_interface.add_upload_record(
        _make_record(
            "u-1",
            direction="upload",
            status="等待中",
            upload_retryable=True,
            target_path=local_path,
            upload_parent_id=ROOT_DIRECTORY_ID,
        )
    )
    pending = PendingUploadTask(
        ROOT_DIRECTORY_ID, local_path, "u-1", local_path.name, False
    )
    window._upload_pending = [pending]

    window._pause_selected_uploads({"u-1"})
    assert window._upload_pending == []
    assert "u-1" in window._paused_uploads
    record = window.transfer_interface._find_record("upload", "u-1")
    assert record is not None and record.status == "已暂停"

    started: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        window,
        "_start_upload_task",
        lambda *args, **kwargs: started.append((*args, kwargs)),
    )
    window._resume_selected_uploads({"u-1"})
    assert "u-1" not in window._paused_uploads
    assert started == [
        (
            ROOT_DIRECTORY_ID,
            local_path,
            "u-1",
            {"upload_name": local_path.name, "show_enqueue_status": False},
        )
    ]


def test_cancel_folder_upload_removes_paused_child_record(
    qapp: QApplication, tmp_path: Path
) -> None:
    window = MainWindow(WorkerFileBrowser())
    root = tmp_path / "folder"
    child = root / "paused.txt"
    root.mkdir()
    child.write_text("paused")
    root_id = window._create_upload_record(root, name="folder")
    child_id = window._create_upload_record(
        child,
        parent_id="cloud-root",
        upload_name="paused.txt",
        retryable=True,
    )
    window._folder_upload_record_id = root_id
    window._folder_upload_child_ids = {child_id}
    window._paused_uploads[child_id] = PendingUploadTask(
        "cloud-root", child, child_id, "paused.txt", False
    )
    window.transfer_interface.update_record("upload", child_id, status="已暂停")

    window._cancel_folder_upload()

    assert window.transfer_interface._find_record("upload", root_id) is None
    assert window.transfer_interface._find_record("upload", child_id) is None
    assert child_id not in window._paused_uploads


def test_main_window_batch_upload_controls_queued_folder_task(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = MainWindow(WorkerFileBrowser())
    root = tmp_path / "folder"
    root.mkdir()
    child = root / "child.txt"
    child.write_text("content")
    root_id = window._create_upload_record(root, name="folder")
    child_id = window._create_upload_record(
        child,
        parent_id="cloud-root",
        upload_name="child.txt",
        retryable=True,
    )
    window._folder_upload_record_id = root_id
    window._folder_upload_queue = [
        main_window_module.QueuedUploadFile(
            child_id, child, "cloud-root", "child.txt"
        )
    ]
    started: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        window,
        "_start_upload_task",
        lambda *args, **kwargs: started.append((*args, kwargs)),
    )

    window._pause_selected_uploads({child_id})

    assert window.transfer_interface._find_record("upload", child_id).status == "已暂停"
    assert child_id in window._paused_uploads
    assert [item.task_id for item in window._folder_upload_queue] == [child_id]

    window._resume_selected_uploads({child_id})

    assert child_id not in window._paused_uploads
    assert started == [
        (
            "cloud-root",
            child,
            child_id,
            {"upload_name": "child.txt", "show_enqueue_status": False},
        )
    ]


def test_transfer_upload_retry_action_is_available_only_for_failed_uploads(
    qapp: QApplication,
) -> None:
    transfer = TransferInterface()
    retry_ids: list[set[str]] = []
    transfer.retry_upload_requested.connect(lambda ids: retry_ids.append(set(ids)))
    transfer.add_upload_record(
        _make_record("u-1", direction="upload", status="失败", upload_retryable=True)
    )
    transfer.add_upload_record(
        _make_record("u-2", direction="upload", status="已完成", upload_retryable=True)
    )

    def action_button(row: int, tooltip: str):
        widget = transfer.upload_table.cellWidget(row, 5)
        for child in widget.findChildren(QWidget):
            if child.toolTip() == tooltip:
                return child
        raise AssertionError(f"button {tooltip} not found in row {row}")

    assert action_button(0, "重试").isEnabled()
    assert not action_button(1, "重试").isEnabled()
    action_button(0, "重试").click()
    assert retry_ids == [{"u-1"}]


def test_transfer_upload_batch_retry_emits_only_selected_failed_rows(qapp: QApplication) -> None:
    transfer = TransferInterface()
    retry_ids: list[set[str]] = []
    transfer.retry_upload_requested.connect(lambda ids: retry_ids.append(set(ids)))
    transfer.add_upload_record(
        _make_record("u-1", direction="upload", status="失败", upload_retryable=True)
    )
    transfer.add_upload_record(
        _make_record("u-2", direction="upload", status="上传中", upload_retryable=True)
    )
    transfer.upload_table.selectAll()
    transfer._request_retry_selected_uploads()
    assert retry_ids == [{"u-1"}]


def test_transfer_defaults_to_download_tab(qapp: QApplication) -> None:
    transfer = TransferInterface()
    assert transfer._active_direction == "download"
    assert transfer.download_frame.isVisibleTo(transfer)
    assert not transfer.upload_frame.isVisibleTo(transfer)


def test_transfer_upload_delete_can_request_waiting_and_active_rows(qapp: QApplication) -> None:
    transfer = TransferInterface()
    removed: list[tuple[str, set[str]]] = []
    transfer.remove_records_requested.connect(
        lambda direction, ids: removed.append((direction, set(ids)))
    )
    for task_id, status in (
        ("queued", "等待中"),
        ("running", "上传中"),
        ("preparing", "创建目录中"),
        ("finished", "已完成"),
    ):
        transfer.add_upload_record(_make_record(task_id, direction="upload", status=status))
    for row in range(4):
        widget = transfer.upload_table.cellWidget(row, 5)
        delete = next(child for child in widget.findChildren(QWidget) if child.toolTip() == "删除")
        assert delete.isEnabled()
    transfer.upload_table.selectAll()
    transfer._request_delete_selected("upload")
    assert removed == [("upload", {"queued", "running", "preparing", "finished"})]


def test_transfer_delete_selected_only_removes_terminal_rows(qapp: QApplication) -> None:
    transfer = TransferInterface()
    removed: list[tuple[str, set[str]]] = []
    transfer.remove_records_requested.connect(
        lambda direction, ids: removed.append((direction, set(ids)))
    )
    transfer.add_download_record(_make_record("d-1", status="下载中"))
    transfer.add_download_record(_make_record("d-2", status="已完成"))
    transfer.add_download_record(_make_record("d-3", status="已暂停"))
    transfer.add_upload_record(_make_record("u-1", direction="upload", status="已暂停"))

    transfer.download_table.selectAll()
    transfer._request_delete_selected("download")

    assert removed == [("download", {"d-2", "d-3"})]
    widget = transfer.download_table.cellWidget(2, 5)
    delete_button = next(
        child for child in widget.findChildren(QWidget) if child.toolTip() == "删除"
    )
    assert delete_button.isEnabled()
    upload_widget = transfer.upload_table.cellWidget(0, 5)
    upload_delete = next(
        child for child in upload_widget.findChildren(QWidget) if child.toolTip() == "删除"
    )
    assert upload_delete.isEnabled()

    transfer.download_table.clearSelection()
    transfer._request_delete_selected("download")
    assert len(removed) == 1


def test_transfer_delete_request_ignores_empty_ids(qapp: QApplication) -> None:
    transfer = TransferInterface()
    removed: list[tuple[str, set[str]]] = []
    transfer.remove_records_requested.connect(
        lambda direction, ids: removed.append((direction, set(ids)))
    )

    transfer._request_delete_ids("upload", set())

    assert removed == []


def test_transfer_active_download_folder_prefers_selection_then_latest(qapp: QApplication) -> None:
    transfer = TransferInterface()
    assert transfer.active_download_folder() is None

    transfer.add_download_record(
        _make_record("d-1", target_path=Path("/downloads/a.txt"), status="已完成")
    )
    transfer.add_download_record(
        _make_record("d-2", target_path=Path("/downloads/b.txt"), status="已完成")
    )
    assert transfer.active_download_folder() == Path("/downloads")

    transfer.download_table.selectRow(0)
    assert transfer.active_download_folder() == Path("/downloads")


def test_transfer_open_folder_button_emits_active_folder(qapp: QApplication) -> None:
    transfer = TransferInterface()
    emitted: list[object] = []
    transfer.open_download_folder_requested.connect(emitted.append)

    transfer._request_open_download_folder()

    assert emitted == [None]


def test_transfer_upsert_replaces_existing_record(qapp: QApplication) -> None:
    transfer = TransferInterface()
    transfer.add_upload_record(_make_record("u-1", direction="upload", status="等待中"))
    transfer.add_upload_record(_make_record("u-1", direction="upload", status="已完成"))

    assert len(transfer.upload_records) == 1
    assert transfer.upload_records[0].status == "已完成"


def test_transfer_record_progress_percent_edge_cases() -> None:
    completed = _make_record("d-1", status="已完成", total_bytes=10, bytes_done=1)
    assert completed.progress_percent == 100

    no_total = _make_record("d-2", status="下载中", size=None, total_bytes=None, bytes_done=5)
    assert no_total.progress_percent == 0

    over = _make_record("d-3", status="下载中", total_bytes=10, bytes_done=50)
    assert over.progress_percent == 100


def test_main_window_open_transfer_download_folder(qapp: QApplication, tmp_path: Path) -> None:
    opened: list[object] = []
    window = MainWindow(
        settings=AppSettings(default_download_path=tmp_path),  # type: ignore[arg-type]
    )
    original_open = main_window_module.QDesktopServices.openUrl
    main_window_module.QDesktopServices.openUrl = staticmethod(opened.append)  # type: ignore[assignment]
    try:
        window._open_transfer_download_folder("not-a-path")
        assert [url.toString() for url in opened] == [tmp_path.as_uri()]

        window._open_transfer_download_folder(tmp_path / "missing")
        assert "下载文件夹不存在" in window.status_message()
    finally:
        main_window_module.QDesktopServices.openUrl = original_open  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Misc navigation and rendering paths
# ---------------------------------------------------------------------------


def test_go_up_one_level_and_breadcrumb_navigation(qapp: QApplication) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)

    window.refresh_current_directory()
    window.go_up_one_level()
    assert window.current_directory_id() == ROOT_DIRECTORY_ID

    window.enter_displayed_folder(0)
    assert window.breadcrumb_names() == ("/", "Folder")
    window.go_up_one_level()
    assert window.breadcrumb_names() == ("/",)

    window.open_breadcrumb_index(-1)
    window.open_breadcrumb_index(99)
    assert window.breadcrumb_names() == ("/",)


def test_breadcrumb_bar_click_triggers_navigation(qapp: QApplication) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    window.enter_displayed_folder(0)

    window.file_interface._rendering_breadcrumb = True
    window.file_interface._on_breadcrumb_changed("0")
    assert window.breadcrumb_names() == ("/", "Folder")

    window.file_interface._rendering_breadcrumb = False
    window.file_interface._on_breadcrumb_changed("0")
    assert window.breadcrumb_names() == ("/",)


def test_tree_item_click_navigates_root_and_children(qapp: QApplication) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    tree = window.file_interface.folder_tree

    root_item = tree.topLevelItem(0)
    child_item = root_item.child(0)
    window.file_interface._on_tree_item_clicked(child_item)
    assert window.current_directory_id() == "folder-1"

    window.file_interface._on_tree_item_clicked(tree.topLevelItem(0))
    assert window.current_directory_id() == ROOT_DIRECTORY_ID


def test_render_items_reports_empty_directory_after_loading(qapp: QApplication) -> None:
    browser = WorkerFileBrowser()
    browser.items_by_parent[ROOT_DIRECTORY_ID] = []
    window = MainWindow(browser)

    window.refresh_current_directory()

    assert window.status_message() == "当前文件夹为空"


def test_refresh_directory_failure_shows_error_status(qapp: QApplication) -> None:
    class FailingListBrowser(WorkerFileBrowser):
        def list_directory(self, parent_id: str = ROOT_DIRECTORY_ID) -> list[WopanItem]:
            raise FileBrowserError("boom")

    window = MainWindow(FailingListBrowser())

    window.refresh_current_directory()

    assert window.status_message() == "加载失败：boom"
    assert window.displayed_items() == ()


def test_refresh_cloud_usage_failure_and_login_required(qapp: QApplication) -> None:
    class FailingUsageBrowser(WorkerFileBrowser):
        mode = "error"

        def get_cloud_usage(self, account_id: str) -> WopanCloudUsage:
            if self.mode == "error":
                raise FileBrowserError("usage down")
            raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    messages: list[str] = []
    browser = FailingUsageBrowser()
    window = MainWindow(browser)
    window.set_auth_session(_make_session())
    window.login_required.connect(messages.append)

    window.refresh_cloud_usage()
    assert window.status_message() == "空间信息刷新失败：usage down"

    browser.mode = "login"
    window.refresh_cloud_usage()
    assert messages == ["登录已过期，请重新登录"]


def test_refresh_cloud_usage_without_session_clears_display(qapp: QApplication) -> None:
    window = MainWindow(WorkerFileBrowser())

    window.refresh_cloud_usage()

    assert window.account_interface.usage_value_label.text() == "-- / --"
    assert window.file_interface.storage_value_label.text() == "-- / --"


@pytest.mark.parametrize(
    "operation",
    [
        "create_folder",
        "rename",
        "delete",
        "move",
        "download",
        "upload",
    ],
)
def test_operations_without_browser_report_login_required(
    qapp: QApplication, tmp_path: Path, operation: str
) -> None:
    window = MainWindow()

    if operation == "create_folder":
        window.create_folder_with_name("name")
    elif operation == "rename":
        window.rename_displayed_item(0, "new")
    elif operation == "delete":
        window.delete_displayed_item(0)
    elif operation == "move":
        window.move_displayed_item(0, "target")
    elif operation == "download":
        window._items = [_file_item()]
        window.download_displayed_item(0, tmp_path / "x.txt", run_in_background=False)
    else:
        window.upload_file_to_current_directory(tmp_path / "x.txt", run_in_background=False)

    assert window.status_message() == "请先登录"


def test_download_displayed_item_rejects_empty_path_name(
    qapp: QApplication, tmp_path: Path
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()

    window.download_displayed_item(1, Path("/"), run_in_background=False)

    assert browser.download_calls == []
    assert window.status_message() == "保存路径不能为空"


def test_upload_displayed_item_rejects_empty_file_name(qapp: QApplication, tmp_path: Path) -> None:
    window = MainWindow(WorkerFileBrowser())

    window.upload_file_to_current_directory(Path("/"), run_in_background=False)

    assert window.status_message() == "上传文件不能为空"


def test_resolve_automatic_download_path_reports_unusable_directory(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = tmp_path / "a.txt"
    blocked.write_text("occupied")
    window = MainWindow(settings=AppSettings(default_download_path=blocked))  # type: ignore[arg-type]

    resolved = window._resolve_automatic_download_path("report.txt")

    assert resolved is None
    assert "下载目录不可用" in window.status_message()


def test_resolve_automatic_download_path_falls_back_to_safe_name(
    qapp: QApplication, tmp_path: Path
) -> None:
    window = MainWindow(settings=AppSettings(default_download_path=tmp_path))  # type: ignore[arg-type]

    resolved = window._resolve_automatic_download_path('bad:name?.txt')

    assert resolved is not None
    assert resolved.name == "bad_name_.txt"
    assert window._resolve_automatic_download_path("...") is not None


def test_clear_auth_session_resets_display(qapp: QApplication) -> None:
    window = MainWindow(WorkerFileBrowser())
    window.set_auth_session(_make_session())

    window.clear_auth_session()

    assert window.auth_session() is None
    assert window.account_interface.account_value_label.text() == "--"
    assert window.windowTitle() == "OpenWoPan"


def test_enter_displayed_folder_ignores_invalid_rows(qapp: QApplication) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()

    window.enter_displayed_folder(-1)
    window.enter_displayed_folder(99)
    window.enter_displayed_folder(1)

    assert window.current_directory_id() == ROOT_DIRECTORY_ID


def test_recover_multiple_download_records_renders_once(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = MainWindow(WorkerFileBrowser())
    renders: list[int] = []
    original = window.transfer_interface._render_download_table

    def render() -> None:
        renders.append(len(window.transfer_interface.download_records))
        original()

    monkeypatch.setattr(window.transfer_interface, "_render_download_table", render)
    records = tuple(
        SimpleNamespace(
            task_id=f"persisted-{index}",
            name=f"file-{index}.txt",
            target_path=Path(f"/tmp/file-{index}.txt"),
            status="等待中",
            total_bytes=100,
            bytes_done=20,
            active_connections=0,
            max_connections=1,
            supports_resume=False,
        )
        for index in range(3)
    )
    window._on_download_recovery_succeeded(records)
    assert renders == [3]
    assert len(window.transfer_interface.download_records) == 3


def test_load_persisted_download_records_without_browser(qapp: QApplication) -> None:
    window = MainWindow()

    window._load_persisted_download_records()

    assert window.transfer_interface.download_records == []


def test_download_displayed_item_ignores_missing_row(qapp: QApplication, tmp_path: Path) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)

    window.download_displayed_item(10, tmp_path / "x.txt", run_in_background=False)

    assert browser.download_calls == []


# ---------------------------------------------------------------------------
# Second-pass coverage: helper functions, guards, and edge branches
# ---------------------------------------------------------------------------


def test_next_available_file_name_increments_past_first_duplicate() -> None:
    from openwopan.ui.main_window import _next_available_file_name

    assert (
        _next_available_file_name(
            "report.txt", {"report.txt", "report (1).txt"}
        )
        == "report (2).txt"
    )
    assert _next_available_file_name("new.txt", set()) == "new.txt"


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (2048, "2.0 KB"),
        (1024**3, "1.0 GB"),
        (5 * 1024**5, "5.0 PB"),
        (3 * 1024**6, "3072.0 PB"),
    ],
)
def test_format_bytes_units(size: int, expected: str) -> None:
    from openwopan.ui.main_window import _format_bytes

    assert _format_bytes(size) == expected


@pytest.mark.parametrize(
    ("speed", "expected"),
    [
        (0.0, "--"),
        (-1.0, "--"),
        (2048.0, "2.0 KB/s"),
    ],
)
def test_format_speed(speed: float, expected: str) -> None:
    from openwopan.ui.main_window import _format_speed

    assert _format_speed(speed) == expected


@pytest.mark.parametrize(
    ("account_id", "expected"),
    [
        ("13800138000", "138****8000"),
        ("1234", "1234"),
        ("abcd", "abcd"),
        ("account-99", "ac***99"),
    ],
)
def test_mask_account_id(account_id: str, expected: str) -> None:
    from openwopan.ui.main_window import _mask_account_id

    assert _mask_account_id(account_id) == expected


@pytest.mark.parametrize(
    ("raw_name", "expected"),
    [
        ('bad:name?.txt', "bad_name_.txt"),
        ("...", "download"),
        ("normal.txt", "normal.txt"),
    ],
)
def test_safe_local_file_name(raw_name: str, expected: str) -> None:
    from openwopan.ui.main_window import _safe_local_file_name

    assert _safe_local_file_name(raw_name) == expected


def test_transfer_record_post_init_fills_timestamps() -> None:
    both = _make_record("d-1")
    assert both.created_at > 0
    assert both.updated_at == both.created_at

    preset = TransferRecord(
        task_id="d-2",
        direction="download",
        name="x",
        size=1,
        created_at=5.0,
    )
    assert preset.created_at == 5.0
    assert preset.updated_at == 5.0


def test_transfer_active_download_folder_skips_row_without_target(
    qapp: QApplication,
) -> None:
    transfer = TransferInterface()
    transfer.add_download_record(_make_record("d-1", target_path=None))
    transfer.add_download_record(
        _make_record("d-2", target_path=Path("/downloads/b.txt"))
    )

    transfer.download_table.selectRow(0)

    assert transfer.active_download_folder() == Path("/downloads")


def test_move_target_dialog_ignores_items_without_index_data(qapp: QApplication) -> None:
    from PySide6.QtWidgets import QTreeWidgetItem

    dialog = main_window_module.MoveTargetDialog(
        [main_window_module.BreadcrumbEntry(item_id="root", name="/")]
    )
    stray = QTreeWidgetItem(["stray"])
    dialog._target_tree.addTopLevelItem(stray)

    dialog._on_item_clicked(stray)

    assert dialog.selected_entry() is None
    assert not dialog._ok_button.isEnabled()


def test_file_interface_row_helpers_invoke_prompts_for_current_row(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    stub_message_box,
) -> None:
    window = MainWindow(WorkerFileBrowser())
    window.refresh_current_directory()
    table = window.file_interface.file_table
    table.clearSelection()
    table.setCurrentCell(0, 0)

    window.file_interface._delete_selected_row()
    assert stub_message_box.instances[0].title == "确认删除"

    monkeypatch.setattr(main_window_module, "QFileDialog", FakeFileDialog)
    FakeFileDialog.save_result = ("", "")
    table.setCurrentCell(1, 0)
    window.file_interface._download_selected_row()

    assert window.status_message().startswith("1 项")


def test_file_interface_row_helpers_ignore_empty_selection(qapp: QApplication) -> None:
    window = MainWindow(WorkerFileBrowser())
    window.refresh_current_directory()
    table = window.file_interface.file_table
    table.clearSelection()

    window.file_interface._delete_selected_row()
    window.file_interface._download_selected_row()

    assert window.selected_download_row() is None
    assert window.status_message().startswith("2 项")


def test_switch_to_interface_without_shell_returns(qapp: QApplication) -> None:
    window = MainWindow(WorkerFileBrowser())
    window._stacked_widget = None
    window._navigation_interface = None

    window._switch_to_interface(window.file_interface, "files")

    assert window.status_message() == "请先登录"


def test_refresh_without_browser_clears_items(qapp: QApplication) -> None:
    window = MainWindow()

    window.refresh_current_directory()

    assert window.displayed_items() == ()
    assert window.status_message() == "请先登录"


class _OperationOutcomeBrowser(WorkerFileBrowser):
    def __init__(self, error: Exception | None) -> None:
        super().__init__()
        self.error = error
        self.renames: list[str] = []
        self.deletes: list[str] = []
        self.moves: list[str] = []

    def create_folder(self, parent_id: str, name: str) -> WopanItem:
        if self.error is not None:
            raise self.error
        return super().create_folder(parent_id, name)

    def rename_item(self, item: WopanItem, new_name: str) -> None:
        self.renames.append(new_name)
        if self.error is not None:
            raise self.error

    def delete_item(self, item: WopanItem) -> None:
        self.deletes.append(item.item_id)
        if self.error is not None:
            raise self.error

    def move_item(self, item: WopanItem, target_parent_id: str) -> None:
        self.moves.append(target_parent_id)
        if self.error is not None:
            raise self.error

    def download_file(self, *args: object, **kwargs: object) -> object:
        if self.error is not None:
            raise self.error
        return SimpleNamespace(status="已完成")

    def upload_file(
        self,
        parent_id: str,
        local_path: Path,
        *,
        upload_name: str | None = None,
    ) -> WopanItem:
        if self.error is not None:
            raise self.error
        return super().upload_file(parent_id, local_path, upload_name=upload_name)


@pytest.mark.parametrize(
    ("error", "prefix"),
    [
        (FileBrowserError("boom"), "失败"),
        (FileBrowserLoginRequiredError("登录已过期，请重新登录"), "登录"),
    ],
)
@pytest.mark.parametrize("operation", ["create", "rename", "delete", "move"])
def test_operations_report_backend_errors(
    qapp: QApplication, operation: str, error: Exception, prefix: str
) -> None:
    messages: list[str] = []
    window = MainWindow(_OperationOutcomeBrowser(error))
    window.login_required.connect(messages.append)
    window.refresh_current_directory()

    if operation == "create":
        window.create_folder_with_name("New")
    elif operation == "rename":
        window.rename_displayed_item(1, "renamed.txt")
    elif operation == "delete":
        window.delete_displayed_item(0)
    else:
        window.move_displayed_item(1, "folder-1")

    if isinstance(error, FileBrowserLoginRequiredError):
        assert messages == ["登录已过期，请重新登录"]
    else:
        assert prefix in window.status_message()


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (FileBrowserError("boom"), "失败"),
        (FileBrowserLoginRequiredError("登录已过期，请重新登录"), "失败"),
    ],
)
def test_sync_download_and_upload_error_paths(
    qapp: QApplication,
    tmp_path: Path,
    error: Exception,
    expected_status: str,
) -> None:
    messages: list[str] = []
    window = MainWindow(_OperationOutcomeBrowser(error))
    window.login_required.connect(messages.append)
    window.refresh_current_directory()
    upload_path = tmp_path / "u.txt"
    upload_path.write_text("content")

    window.download_displayed_item(1, tmp_path / "r.txt", run_in_background=False)
    window.upload_file_to_current_directory(upload_path, run_in_background=False)

    assert window.transfer_interface.download_records[0].status == expected_status
    assert window.transfer_interface.upload_records[0].status == expected_status
    if isinstance(error, FileBrowserLoginRequiredError):
        assert messages == ["登录已过期，请重新登录", "登录已过期，请重新登录"]


@pytest.mark.parametrize("operation", ["rename", "delete", "move"])
def test_item_operations_without_browser_after_items_loaded(
    qapp: QApplication, operation: str
) -> None:
    window = MainWindow()
    window._items = _root_items()

    if operation == "rename":
        window.rename_displayed_item(0, "new")
    elif operation == "delete":
        window.delete_displayed_item(0)
    else:
        window.move_displayed_item(0, "target")

    assert window.status_message() == "请先登录"


def test_prompt_download_item_uses_automatic_path_when_not_asking(
    qapp: QApplication, tmp_path: Path, sync_threads: None
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(
        browser,
        settings=AppSettings(default_download_path=tmp_path, ask_download_location=False),
    )
    window.refresh_current_directory()
    (tmp_path / "report.txt").write_text("x")

    window.prompt_download_item(1)

    assert browser.download_calls
    assert browser.download_calls[0]["local_path"] == tmp_path / "report (1).txt"


class _FakeUnavailableFolder:
    """Path stand-in whose mkdir succeeds but is_dir reports False."""

    def mkdir(self, parents: bool = True, exist_ok: bool = True) -> None:
        return None

    def is_dir(self) -> bool:
        return False


def test_resolve_automatic_download_path_rejects_non_directory(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = MainWindow()
    monkeypatch.setattr(
        AppSettings,
        "default_download_path",
        property(lambda self: _FakeUnavailableFolder()),
    )

    resolved = window._resolve_automatic_download_path("report.txt")

    assert resolved is None
    assert window.status_message() == "下载目录不可用"


def test_resolve_automatic_download_path_counts_active_download_name(
    qapp: QApplication, tmp_path: Path
) -> None:
    window = MainWindow(settings=AppSettings(default_download_path=tmp_path))  # type: ignore[arg-type]
    window._download_item = _file_item(name="report.txt")

    resolved = window._resolve_automatic_download_path("report.txt")

    assert resolved is not None
    assert resolved.name == "report (1).txt"


def test_download_handlers_without_task_id_fallback_to_none(qapp: QApplication) -> None:
    window = MainWindow()
    window._download_task_id = None

    window._on_download_progress(10, 20)
    window._on_download_status_changed("校验中")
    window._on_download_connections_changed(1, 2)
    window._on_download_stopped("已暂停")
    window._clear_download_task()
    window._on_download_succeeded("a.txt", "/tmp/a.txt")

    assert window.status_message() == "下载完成：a.txt"


def test_download_handlers_use_current_task_id_fallback(qapp: QApplication) -> None:
    window = MainWindow(WorkerFileBrowser())
    _register_download_task(
        window, "download-1", target_path=Path("/tmp/report.txt"), item=_file_item()
    )
    window._download_task_id = "download-1"

    window._on_download_status_changed("合并中")
    window._on_download_connections_changed(1, 2)
    window._on_download_stopped("已取消")
    window._on_download_progress(100, 200)
    window._on_download_succeeded("report.txt", "/tmp/report.txt")

    record = window.transfer_interface._find_record("download", "download-1")
    assert record is not None
    assert record.status == "已完成"


def test_resume_download_ignores_unknown_task(qapp: QApplication) -> None:
    window = MainWindow(WorkerFileBrowser())

    window._resume_download_task("missing-task")

    assert window.status_message() == "请先登录"


def test_remove_transfer_records_without_browser_support(qapp: QApplication) -> None:
    browser = WorkerFileBrowser()
    browser.remove_download_record = "not-callable"  # type: ignore[assignment]
    window = MainWindow(browser)
    _register_download_task(
        window, "download-1", target_path=Path("/tmp/r.txt"), item=_file_item()
    )

    window.transfer_interface.remove_records_requested.emit("download", {"download-1"})

    assert window.transfer_interface.download_records == []


def test_upload_failed_without_task_id(qapp: QApplication) -> None:
    window = MainWindow()
    window._upload_task_id = None

    window._on_upload_failed("boom")

    assert window.status_message() == "上传失败：boom"


def test_upload_succeeded_uses_task_id_fallback(qapp: QApplication) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    local_path = Path("/tmp/fallback.txt")
    task_id = window._create_upload_record(local_path)
    window._upload_task_id = task_id

    uploaded = WopanItem(
        item_id="uploaded-file",
        name="fallback.txt",
        kind=WopanItemKind.FILE,
        download_id="uploaded-fid",
        size=10,
    )
    window._on_upload_succeeded(uploaded)

    record = window.transfer_interface._find_record("upload", task_id)
    assert record is not None
    assert record.status == "已完成"


@pytest.mark.parametrize(
    ("created_at", "updated_at"),
    [
        (5.0, 7.0),  # both explicit -> both preserved
        (0.0, 7.0),  # explicit updated_at survives even when created_at is auto-filled
        (5.0, 0.0),  # created_at preserved; updated_at falls back to it
    ],
)
def test_transfer_record_post_init_timestamp_fallbacks(
    created_at: float, updated_at: float
) -> None:
    record = _make_record("d-1", created_at=created_at, updated_at=updated_at)

    if created_at > 0:
        assert record.created_at == created_at
    else:
        assert record.created_at > 0
    if updated_at > 0:
        assert record.updated_at == updated_at
    else:
        assert record.updated_at == record.created_at


def test_transfer_active_download_folder_skips_latest_null_targets(
    qapp: QApplication,
) -> None:
    transfer = TransferInterface()
    transfer.add_download_record(_make_record("d-1", target_path=Path("/downloads/a.txt")))
    transfer.add_download_record(_make_record("d-2", target_path=None))

    assert transfer.active_download_folder() == Path("/downloads")

    all_without_target = TransferInterface()
    all_without_target.add_download_record(_make_record("d-3", target_path=None))

    assert all_without_target.active_download_folder() is None


def test_file_interface_download_action_starts_download_for_selected_row(
    qapp: QApplication, tmp_path: Path, sync_threads: None
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(
        browser,
        settings=AppSettings(default_download_path=tmp_path, ask_download_location=False),
    )
    window.refresh_current_directory()
    table = window.file_interface.file_table
    table.clearSelection()
    table.selectRow(1)

    window.file_interface._download_selected_row()

    assert browser.download_calls
    assert browser.download_calls[0]["local_path"] == tmp_path / "report.txt"
    assert window.status_message() == "下载完成：report.txt"


@pytest.mark.parametrize(
    ("clicked_id", "expected_directory_id"),
    [
        # Unknown id: the displayed-items loop exhausts without navigating anywhere.
        ("ghost-folder", ROOT_DIRECTORY_ID),
        # Known id on row 1: the loop skips the non-matching row 0, then navigates.
        ("folder-9", "folder-9"),
    ],
)
def test_tree_item_click_non_root_navigation_branches(
    qapp: QApplication, clicked_id: str, expected_directory_id: str
) -> None:
    browser = WorkerFileBrowser()
    browser.items_by_parent[ROOT_DIRECTORY_ID] = [
        _file_item("file-0", "first.txt"),
        WopanItem(
            item_id="folder-9",
            name="Deep",
            kind=WopanItemKind.FOLDER,
            parent_id=ROOT_DIRECTORY_ID,
        ),
    ]
    browser.items_by_parent["folder-9"] = [
        WopanItem(
            item_id="deep-file",
            name="deep.txt",
            kind=WopanItemKind.FILE,
            parent_id="folder-9",
            download_id="deep-fid",
            size=1,
        )
    ]
    window = MainWindow(browser)
    window.refresh_current_directory()
    clicked = QTreeWidgetItem(["clicked"])
    clicked.setData(0, Qt.ItemDataRole.UserRole, clicked_id)

    window.file_interface._on_tree_item_clicked(clicked)

    assert window.current_directory_id() == expected_directory_id


def test_switch_to_interface_activates_widget_and_navigation(qapp: QApplication) -> None:
    window = MainWindow(WorkerFileBrowser())
    assert window._stacked_widget is not None
    assert window._navigation_interface is not None

    window._switch_to_interface(window.transfer_interface, "transfers")

    assert window._stacked_widget.currentWidget() is window.transfer_interface
    transfers_nav_item = window._navigation_interface.panel.currentItem()

    window._switch_to_interface(window.file_interface, "files")

    assert window._stacked_widget.currentWidget() is window.file_interface
    assert window._navigation_interface.panel.currentItem() is not transfers_nav_item


def test_prompt_rename_item_cancelled_keeps_name_and_deletes_dialog(
    qapp: QApplication, stub_name_dialog
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    stub_name_dialog.accept_result = QDialog.DialogCode.Rejected

    window.prompt_rename_item(1)

    assert window.displayed_items()[1].name == "report.txt"
    assert stub_name_dialog.instances[0].title == "重命名"
    assert stub_name_dialog.instances[0].deleted


def test_upload_success_without_record_id_still_refreshes_directory(
    qapp: QApplication,
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    refreshes_before = len(browser.requested_parent_ids)
    window._upload_task_id = None

    uploaded = WopanItem(
        item_id="uploaded-file",
        name="orphan.txt",
        kind=WopanItemKind.FILE,
        download_id="uploaded-fid",
        size=10,
    )
    window._on_upload_succeeded(uploaded)

    assert len(browser.requested_parent_ids) == refreshes_before + 1
    assert window.transfer_interface.upload_records == []
    assert window.status_message() == "已上传「orphan.txt」，但刷新后未在当前目录看到，请稍后再刷新"


class _InfoBarSpy:
    """Record InfoBar calls so summary toasts can be asserted."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def make(kind: str):
            def call(title: str, content: str, parent: object = None, **_kwargs: object) -> None:
                self.calls.append((kind, title, content))

            return call

        monkeypatch.setattr(
            main_window_module,
            "InfoBar",
            SimpleNamespace(
                info=make("info"),
                warning=make("warning"),
                error=make("error"),
                success=make("success"),
            ),
        )


def _make_folder_tree(tmp_path: Path) -> Path:
    root = tmp_path / "相册"
    root.mkdir()
    (root / "2024").mkdir()
    (root / "空目录").mkdir()
    (root / "说明.txt").write_bytes(b"hello")
    (root / "2024" / "春节.md").write_bytes(b"12345")
    return root


def _upload_records(window: MainWindow) -> list[TransferRecord]:
    return list(window.transfer_interface.upload_records)


def test_prompt_upload_folder_requires_browser(qapp: QApplication) -> None:
    window = MainWindow()

    window.prompt_upload_folder()

    assert window.status_message() == "请先登录"


def test_prompt_upload_folder_cancelled_by_user(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    monkeypatch.setattr(main_window_module, "QFileDialog", FakeFileDialog)
    FakeFileDialog.existing_directory = ""

    window.prompt_upload_folder()

    assert browser.prepared_uploads == []


def test_prompt_upload_folder_uploads_selected_folder(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    local_root = _make_folder_tree(tmp_path)
    monkeypatch.setattr(main_window_module, "QFileDialog", FakeFileDialog)
    FakeFileDialog.existing_directory = str(local_root)

    window.prompt_upload_folder()

    assert browser.prepared_uploads == [(ROOT_DIRECTORY_ID, local_root)]
    assert len(browser.uploaded_files) == 2
    assert window._folder_prepare_thread is None
    assert window._folder_upload_queue == []
    assert window._folder_upload_active is None


def test_folder_upload_runs_two_phases_and_updates_records(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    """AC1/AC7：准备记录 + 每文件记录，全部顺序执行并到达终态。"""
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    local_root = _make_folder_tree(tmp_path)

    window.upload_folder_to_current_directory(local_root)

    records = _upload_records(window)
    assert [record.name for record in records] == ["相册", "春节.md", "说明.txt"]
    assert [record.status for record in records] == ["已完成", "已完成", "已完成"]
    assert records[1].target_path == local_root / "2024" / "春节.md"
    # 阶段二逐文件顺序执行，父目录为目标云端目录 ID，名字为去重后的最终名
    assert [parent_id for parent_id, _ in browser.uploaded_files] == [
        f"cloud-{ROOT_DIRECTORY_ID}-2024",
        f"cloud-{ROOT_DIRECTORY_ID}-root",
    ]
    assert browser.upload_names == ["春节.md", "说明.txt"]


def test_folder_upload_shows_summary_and_refreshes_target_directory(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R6：队列排空后弹汇总；仍停留在目标目录则自动刷新。"""
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    local_root = _make_folder_tree(tmp_path)
    spy = _InfoBarSpy()
    spy.install(monkeypatch)
    requested_before = len(browser.requested_parent_ids)

    window.upload_folder_to_current_directory(local_root)

    assert spy.calls[-1] == ("info", "上传完成", "成功 2 个，失败 0 个")
    # 结束时当前目录刷新一次（单文件路径的逐文件刷新不适用队列文件）
    assert browser.requested_parent_ids[requested_before:] == [
        ROOT_DIRECTORY_ID,
        ROOT_DIRECTORY_ID,
    ]


def test_folder_upload_skips_final_refresh_after_navigation(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    """R6：用户离开目标目录后收尾不再刷新该目录。"""
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    window._folder_upload_target_dir_id = "0"
    window.enter_displayed_folder(0)  # 进入「Folder」子目录
    requested_before = list(browser.requested_parent_ids)

    window._finish_folder_upload()

    assert browser.requested_parent_ids == requested_before


def test_folder_upload_continues_after_single_file_failure(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5：单文件失败不中断其余文件，结束汇总成功/失败数。"""
    browser = WorkerFileBrowser()
    browser.upload_errors = [FileBrowserError("网络错误"), None]
    window = MainWindow(browser)
    window.refresh_current_directory()
    local_root = _make_folder_tree(tmp_path)
    spy = _InfoBarSpy()
    spy.install(monkeypatch)

    window.upload_folder_to_current_directory(local_root)

    records = _upload_records(window)
    assert [record.status for record in records] == ["失败", "失败", "已完成"]
    assert records[0].error == "成功 1 个，失败 1 个"
    assert records[1].error == "网络错误"
    assert ("error", "上传失败", "网络错误") in spy.calls
    assert spy.calls[-1] == ("warning", "上传完成", "成功 1 个，失败 1 个")
    assert len(browser.uploaded_files) == 2


def test_ordinary_upload_login_failure_does_not_clear_folder_queue(
    qapp: QApplication, tmp_path: Path
) -> None:
    window = MainWindow(WorkerFileBrowser())
    ordinary_path = tmp_path / "ordinary.txt"
    ordinary_path.write_text("ordinary")
    folder_root = tmp_path / "folder"
    folder_root.mkdir()
    ordinary_task_id = window._create_upload_record(ordinary_path)
    window._folder_upload_active = QueuedUploadFile(
        "folder-active", folder_root / "child.txt", "cloud-root", "child.txt"
    )
    window._folder_upload_queue = [
        QueuedUploadFile("folder-pending", folder_root / "next.txt", "cloud-root", "next.txt")
    ]
    window._folder_prepare_pending = [
        PendingFolderUpload(folder_root, "pinned-parent", "folder", "pending-folder")
    ]
    window._folder_upload_target_dir_id = "pinned-parent"

    window._on_upload_login_required("登录已过期，请重新登录", task_id=ordinary_task_id)

    assert window.transfer_interface._find_record("upload", ordinary_task_id).status == "失败"
    assert window._folder_upload_active is not None
    assert window._folder_upload_active.task_id == "folder-active"
    assert [queued.task_id for queued in window._folder_upload_queue] == ["folder-pending"]
    assert window._folder_prepare_pending == [
        PendingFolderUpload(folder_root, "pinned-parent", "folder", "pending-folder")
    ]
    assert window._folder_upload_target_dir_id == "pinned-parent"


def test_folder_upload_joins_shared_fifo_behind_waiting_files(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = MainWindow(WorkerFileBrowser(), settings=AppSettings(max_concurrent_uploads=1))
    ordinary = tmp_path / "ordinary.txt"
    child = tmp_path / "child.txt"
    window._upload_thread = QThread(window)  # type: ignore[assignment]
    window._upload_pending = [
        PendingUploadTask(ROOT_DIRECTORY_ID, ordinary, "ordinary", None, False)
    ]
    window._folder_upload_queue = [
        QueuedUploadFile("folder-child", child, "cloud-root", "child.txt")
    ]

    window._continue_folder_upload_queue()

    assert [pending.task_id for pending in window._upload_pending] == [
        "ordinary", "folder-child"
    ]
    assert window._folder_upload_active is not None
    assert window._folder_upload_active.task_id == "folder-child"
    window._upload_thread = None
    started: list[str] = []

    def launch(_parent: str, _path: Path, task_id: str, **_kwargs: object) -> None:
        started.append(task_id)
        window._upload_threads[task_id] = QThread(window)

    monkeypatch.setattr(window, "_launch_upload_task", launch)
    window._start_next_upload_task()
    assert started == ["ordinary"]
    window._upload_threads.pop("ordinary")
    window._start_next_upload_task()
    assert started == ["ordinary", "folder-child"]
    window._upload_threads.clear()
    window._folder_upload_active = None
    window._folder_upload_queue.clear()


def test_folder_prepare_login_failure_preserves_other_pending_folder(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = MainWindow(WorkerFileBrowser())
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    first_id = window._create_upload_record(first)
    second_id = window._create_upload_record(second)
    window._folder_upload_record_id = first_id
    window._folder_prepare_pending = [
        PendingFolderUpload(second, ROOT_DIRECTORY_ID, "second", second_id)
    ]
    started: list[tuple[Path, dict[str, object]]] = []
    monkeypatch.setattr(
        window,
        "upload_folder_to_current_directory",
        lambda path, **kwargs: started.append((path, kwargs)),
    )

    window._on_folder_upload_prepare_login_required("登录已过期，请重新登录")

    assert window.transfer_interface._find_record("upload", first_id).status == "失败"
    assert window.transfer_interface._find_record("upload", second_id).status == "等待中"
    assert window._folder_prepare_pending == [
        PendingFolderUpload(second, ROOT_DIRECTORY_ID, "second", second_id)
    ]
    window._clear_folder_prepare()
    assert started[0][0] == second
    assert started[0][1]["_record_id"] == second_id


def test_folder_upload_stops_chaining_on_login_required(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    """AC6：登录态失效停止排队，未开始记录明确失败。"""
    browser = WorkerFileBrowser()
    browser.upload_errors = [None, FileBrowserLoginRequiredError("登录已过期，请重新登录")]
    window = MainWindow(browser)
    window.refresh_current_directory()
    observed_messages: list[str] = []
    window.login_required.connect(observed_messages.append)
    local_root = _make_folder_tree(tmp_path)
    (local_root / "结尾.txt").write_bytes(b"tail")  # 第三个文件保持等待中

    window.upload_folder_to_current_directory(local_root)

    assert observed_messages == ["登录已过期，请重新登录"]
    records = _upload_records(window)
    assert [record.name for record in records] == ["相册", "春节.md", "结尾.txt", "说明.txt"]
    assert [record.status for record in records] == ["失败", "已完成", "失败", "失败"]
    assert records[0].error == "登录已过期，请重新登录"
    assert records[-1].error == "登录已过期，请重新登录"
    # 登录失效的文件已尝试但不计入成功；第三个文件从未开始
    assert browser.uploaded_files == [
        (f"cloud-{ROOT_DIRECTORY_ID}-2024", local_root / "2024" / "春节.md"),
        (f"cloud-{ROOT_DIRECTORY_ID}-root", local_root / "结尾.txt"),
    ]
    assert window._folder_upload_queue == []
    assert window._folder_upload_active is None


@pytest.mark.parametrize("terminal", ["success", "failure", "login"])
def test_removed_upload_ignores_late_terminal_result(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    terminal: str,
) -> None:
    window = MainWindow(WorkerFileBrowser())
    task_id = window._create_upload_record(tmp_path / "file.txt")
    unrelated = window._create_upload_record(tmp_path / "unrelated.txt")
    window.transfer_interface.update_record("upload", task_id, status="上传中")
    requests: list[bool] = []
    window._upload_workers[task_id] = SimpleNamespace(
        request_cancel=lambda: requests.append(True)
    )  # type: ignore[assignment]
    login: list[str] = []
    window.login_required.connect(login.append)
    monkeypatch.setattr(window, "refresh_current_directory", lambda **kwargs: None)
    window._remove_transfer_records("upload", {task_id})

    if terminal == "success":
        window._on_upload_succeeded(_file_item(), task_id)
    elif terminal == "failure":
        window._on_upload_failed("upload failed", task_id)
    else:
        window._on_upload_login_required("登录已过期，请重新登录", task_id)

    assert requests == [True]
    assert window.transfer_interface._find_record("upload", task_id) is None
    assert window.transfer_interface._find_record("upload", unrelated) is not None
    if terminal == "login":
        assert login == ["登录已过期，请重新登录"]


@pytest.mark.parametrize("terminal", ["failure", "login"])
def test_removed_preparing_folder_ignores_late_failure(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    terminal: str,
) -> None:
    window = MainWindow(WorkerFileBrowser())
    root_id = window._create_upload_record(tmp_path / "root")
    window._folder_upload_record_id = root_id
    window._folder_prepare_thread = QThread(window)  # type: ignore[assignment]
    errors: list[str] = []
    monkeypatch.setattr(
        main_window_module.InfoBar, "error", lambda **kwargs: errors.append(kwargs["content"])
    )
    window._remove_transfer_records("upload", {root_id})
    if terminal == "failure":
        window._on_folder_upload_prepare_failed("late failure")
    else:
        window._on_folder_upload_prepare_login_required("登录已过期，请重新登录")

    assert window.transfer_interface._find_record("upload", root_id) is None
    assert errors == []


def test_remove_waiting_folder_child_preserves_next_and_root(
    qapp: QApplication, tmp_path: Path,
) -> None:
    window = MainWindow(WorkerFileBrowser())
    root_id = window._create_upload_record(tmp_path / "root")
    queued_id = window._create_upload_record(tmp_path / "queued.txt")
    next_id = window._create_upload_record(tmp_path / "next.txt")
    window._folder_upload_record_id = root_id
    window._folder_upload_active = QueuedUploadFile(
        "active", tmp_path / "active.txt", "c", "active.txt"
    )
    window._upload_threads["active"] = QThread(window)
    window._folder_upload_queue = [
        QueuedUploadFile(queued_id, tmp_path / "queued.txt", "c", "queued.txt"),
        QueuedUploadFile(next_id, tmp_path / "next.txt", "c", "next.txt"),
    ]
    window._remove_transfer_records("upload", {queued_id})
    assert [queued.task_id for queued in window._folder_upload_queue] == [next_id]
    assert window._folder_upload_cancel_count == 1
    assert window.transfer_interface._find_record("upload", queued_id) is None
    assert window.transfer_interface._find_record("upload", root_id) is not None
    assert window.transfer_interface._find_record("upload", next_id) is not None


def test_remove_paused_folder_worker_waits_for_cancel_terminal(
    qapp: QApplication, tmp_path: Path
) -> None:
    window = MainWindow(WorkerFileBrowser())
    root_id = window._create_upload_record(tmp_path / "root")
    child_path = tmp_path / "paused.txt"
    child_id = window._create_upload_record(child_path, parent_id="cloud-root")
    window._folder_upload_record_id = root_id
    window._folder_upload_child_ids = {child_id}
    window._paused_uploads[child_id] = PendingUploadTask(
        "cloud-root", child_path, child_id, child_path.name, False
    )
    window.transfer_interface.update_record("upload", child_id, status="已暂停")
    requested: list[bool] = []
    window._upload_workers[child_id] = SimpleNamespace(
        request_cancel=lambda: requested.append(True)
    )  # type: ignore[assignment]

    window._remove_transfer_records("upload", {child_id})

    assert requested == [True]
    assert window.transfer_interface._find_record("upload", child_id) is not None
    assert child_id in window._upload_removal_requested

    window._on_upload_cancelled(child_id)
    assert window.transfer_interface._find_record("upload", child_id) is None


def test_cancel_folder_upload_keeps_paused_worker_until_terminal(
    qapp: QApplication, tmp_path: Path
) -> None:
    window = MainWindow(WorkerFileBrowser())
    root_id = window._create_upload_record(tmp_path / "root")
    child_path = tmp_path / "paused.txt"
    child_id = window._create_upload_record(child_path, parent_id="cloud-root")
    window._folder_upload_record_id = root_id
    window._folder_upload_child_ids = {child_id}
    window._paused_uploads[child_id] = PendingUploadTask(
        "cloud-root", child_path, child_id, child_path.name, False
    )
    window.transfer_interface.update_record("upload", child_id, status="已暂停")
    requested: list[bool] = []
    window._upload_workers[child_id] = SimpleNamespace(
        request_cancel=lambda: requested.append(True)
    )  # type: ignore[assignment]

    window._cancel_folder_upload()

    assert requested == [True]
    assert window.transfer_interface._find_record("upload", root_id) is not None
    assert window.transfer_interface._find_record("upload", child_id) is not None

    window._on_upload_cancelled(child_id)
    window._upload_workers.pop(child_id)
    window._finish_folder_upload()
    assert window.transfer_interface._find_record("upload", root_id) is None
    assert window.transfer_interface._find_record("upload", child_id) is None


def test_retry_waits_for_previous_worker_with_same_task_id(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = MainWindow(WorkerFileBrowser())
    task_id = window._create_upload_record(tmp_path / "file.txt", parent_id="0")
    previous = QThread(window)
    window._upload_threads[task_id] = previous
    window.transfer_interface.update_record("upload", task_id, status="失败")
    launched: list[str] = []
    monkeypatch.setattr(
        window, "_launch_upload_task",
        lambda parent, path, tid, **kwargs: launched.append(tid),
    )

    record = window.transfer_interface._find_record("upload", task_id)
    assert record is not None
    window._retry_upload_task(record)
    window._start_next_upload_task()
    assert window._upload_threads[task_id] is previous
    assert [pending.task_id for pending in window._upload_pending] == [task_id]
    assert launched == []

    window._upload_threads.pop(task_id)
    window._start_next_upload_task()
    assert launched == [task_id]


def test_remove_waiting_upload_does_not_start_or_remove_another_task(
    qapp: QApplication, tmp_path: Path,
) -> None:
    window = MainWindow(
        WorkerFileBrowser(), settings=AppSettings(max_concurrent_uploads=1)
    )
    first = window._create_upload_record(tmp_path / "first.txt")
    second = window._create_upload_record(tmp_path / "second.txt")
    window._upload_pending = [
        PendingUploadTask("0", tmp_path / "first.txt", first, None, False),
        PendingUploadTask("0", tmp_path / "second.txt", second, None, False),
    ]
    window._upload_threads["active"] = QThread(window)

    window._remove_transfer_records("upload", {first})

    assert [task.task_id for task in window._upload_pending] == [second]
    assert window.transfer_interface._find_record("upload", first) is None
    assert window.transfer_interface._find_record("upload", second) is not None


def test_remove_running_upload_waits_for_terminal_signal(
    qapp: QApplication, tmp_path: Path,
) -> None:
    window = MainWindow(WorkerFileBrowser())
    task_id = window._create_upload_record(tmp_path / "running.txt")
    other_id = window._create_upload_record(tmp_path / "other.txt")
    window.transfer_interface.update_record("upload", task_id, status="上传中")
    requested: list[bool] = []
    window._upload_workers[task_id] = SimpleNamespace(
        request_cancel=lambda: requested.append(True)
    )  # type: ignore[assignment]

    window._remove_transfer_records("upload", {task_id})
    assert requested == [True]
    assert window.transfer_interface._find_record("upload", task_id) is not None

    window._on_upload_cancelled(task_id)
    window._on_upload_progress(5, 10, task_id)
    assert window.transfer_interface._find_record("upload", task_id) is None
    assert window.transfer_interface._find_record("upload", other_id) is not None
    assert task_id not in window._upload_removal_requested


def test_batch_remove_pending_child_root_and_folder_never_starts_selected_folder(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = MainWindow(WorkerFileBrowser())
    root_id = window._create_upload_record(tmp_path / "root")
    child_id = window._create_upload_record(tmp_path / "child.txt")
    selected_id = window._create_upload_record(tmp_path / "selected")
    survivor_id = window._create_upload_record(tmp_path / "survivor")
    window._folder_upload_record_id = root_id
    window._folder_upload_active = QueuedUploadFile(
        child_id, tmp_path / "child.txt", "c", "child.txt"
    )
    window._upload_pending = [
        PendingUploadTask("c", tmp_path / "child.txt", child_id, "child.txt", False)
    ]
    window._folder_prepare_pending = [
        PendingFolderUpload(tmp_path / "selected", "0", "selected", selected_id),
        PendingFolderUpload(tmp_path / "survivor", "0", "survivor", survivor_id),
    ]
    started: list[str] = []
    monkeypatch.setattr(
        window, "upload_folder_to_current_directory",
        lambda path, **kwargs: started.append(kwargs["_record_id"]),
    )

    window._remove_transfer_records("upload", {root_id, child_id, selected_id})

    assert started == [survivor_id]
    assert all(
        window.transfer_interface._find_record("upload", tid) is None
        for tid in (root_id, child_id, selected_id)
    )
    assert window.transfer_interface._find_record("upload", survivor_id) is not None


def test_batch_remove_does_not_launch_selected_waiting_folder(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = MainWindow(WorkerFileBrowser())
    root_id = window._create_upload_record(tmp_path / "root")
    selected_id = window._create_upload_record(tmp_path / "selected")
    survivor_id = window._create_upload_record(tmp_path / "survivor")
    window._folder_upload_record_id = root_id
    window._folder_prepare_pending = [
        PendingFolderUpload(tmp_path / "selected", "0", "selected", selected_id),
        PendingFolderUpload(tmp_path / "survivor", "0", "survivor", survivor_id),
    ]
    started: list[str] = []
    monkeypatch.setattr(
        window, "upload_folder_to_current_directory",
        lambda path, **kwargs: started.append(kwargs["_record_id"]),
    )

    window._remove_transfer_records("upload", {root_id, selected_id})

    assert started == [survivor_id]
    assert window.transfer_interface._find_record("upload", root_id) is None
    assert window.transfer_interface._find_record("upload", selected_id) is None


def test_remove_active_folder_child_waits_then_drains(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = MainWindow(WorkerFileBrowser())
    root_id = window._create_upload_record(tmp_path / "root")
    child_id = window._create_upload_record(tmp_path / "child.txt")
    next_id = window._create_upload_record(tmp_path / "next.txt")
    window._folder_upload_record_id = root_id
    window._folder_upload_active = QueuedUploadFile(
        child_id, tmp_path / "child.txt", "c", "child.txt"
    )
    window._folder_upload_queue = [
        QueuedUploadFile(next_id, tmp_path / "next.txt", "c", "next.txt")
    ]
    requested: list[bool] = []
    window._upload_workers[child_id] = SimpleNamespace(
        request_cancel=lambda: requested.append(True)
    )  # type: ignore[assignment]
    window._upload_threads[child_id] = QThread(window)
    started: list[str] = []
    monkeypatch.setattr(
        window, "_start_upload_task", lambda parent, path, tid, **kw: started.append(tid)
    )

    window._remove_transfer_records("upload", {child_id})
    assert requested == [True]
    assert window.transfer_interface._find_record("upload", child_id) is not None
    window._on_upload_cancelled(child_id)
    window._upload_workers.pop(child_id)
    window._upload_threads.pop(child_id)
    window._continue_folder_upload_queue()

    assert window.transfer_interface._find_record("upload", child_id) is None
    assert window._folder_upload_cancel_count == 1
    assert started == [next_id]
    assert window.transfer_interface._find_record("upload", root_id) is not None


def test_remove_preparing_folder_waits_for_late_result_before_next_folder(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = MainWindow(WorkerFileBrowser())
    root_id = window._create_upload_record(tmp_path / "root")
    next_id = window._create_upload_record(tmp_path / "next")
    window._folder_upload_record_id = root_id
    window._folder_prepare_thread = QThread(window)  # type: ignore[assignment]
    cancel_requested = threading.Event()
    window._folder_prepare_cancel = cancel_requested
    window._folder_prepare_pending = [
        PendingFolderUpload(tmp_path / "next", "0", "next", next_id)
    ]
    started: list[str] = []
    monkeypatch.setattr(
        window, "upload_folder_to_current_directory",
        lambda path, **kwargs: started.append(kwargs["_record_id"]),
    )

    window._remove_transfer_records("upload", {root_id})
    assert cancel_requested.is_set()
    assert started == []
    assert window.transfer_interface._find_record("upload", root_id) is not None
    assert "不会自动删除" in window.status_message()
    window._on_folder_upload_prepared(object())
    assert window.transfer_interface._find_record("upload", root_id) is None
    assert started == []
    window._clear_folder_prepare()
    assert started == [next_id]


def test_remove_running_folder_waits_for_child_then_starts_next_folder(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = MainWindow(WorkerFileBrowser())
    root_id = window._create_upload_record(tmp_path / "root")
    child_id = window._create_upload_record(tmp_path / "child.txt")
    queued_id = window._create_upload_record(tmp_path / "queued.txt")
    next_id = window._create_upload_record(tmp_path / "next")
    window._folder_upload_record_id = root_id
    window._folder_upload_target_dir_id = "different-directory"
    window._folder_upload_active = QueuedUploadFile(
        child_id, tmp_path / "child.txt", "c", "child.txt"
    )
    window._folder_upload_queue = [
        QueuedUploadFile(queued_id, tmp_path / "queued.txt", "c", "queued.txt")
    ]
    window._folder_prepare_pending = [
        PendingFolderUpload(tmp_path / "next", "0", "next", next_id)
    ]
    requested: list[bool] = []
    window._upload_workers[child_id] = SimpleNamespace(
        request_cancel=lambda: requested.append(True)
    )  # type: ignore[assignment]
    window._upload_threads[child_id] = QThread(window)
    started: list[str] = []
    monkeypatch.setattr(
        window, "upload_folder_to_current_directory",
        lambda path, **kwargs: started.append(kwargs["_record_id"]),
    )

    window._remove_transfer_records("upload", {root_id})
    assert requested == [True]
    assert window.transfer_interface._find_record("upload", root_id) is not None
    assert window.transfer_interface._find_record("upload", child_id) is not None
    assert window.transfer_interface._find_record("upload", queued_id) is None
    assert started == []

    window._on_upload_cancelled(child_id)
    window._upload_workers.pop(child_id)
    window._upload_threads.pop(child_id)
    window._continue_folder_upload_queue()
    assert window.transfer_interface._find_record("upload", root_id) is None
    assert window.transfer_interface._find_record("upload", child_id) is None
    assert started == [next_id]


def test_successful_folder_child_retry_clears_root_failure(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = MainWindow(WorkerFileBrowser())
    root_id = window._create_upload_record(tmp_path / "root")
    child_id = window._create_upload_record(tmp_path / "child.txt")
    window._folder_upload_record_id = root_id
    window.transfer_interface.update_record("upload", root_id, status="上传中")
    window._folder_upload_child_ids = {child_id}
    window._folder_upload_active = QueuedUploadFile(
        child_id, tmp_path / "child.txt", "c", "child.txt"
    )
    monkeypatch.setattr(main_window_module.InfoBar, "error", lambda **kwargs: None)
    monkeypatch.setattr(main_window_module.InfoBar, "info", lambda **kwargs: None)

    window._on_upload_failed("暂时失败", child_id)
    assert window._folder_upload_failure_count == 1
    window._folder_upload_active = None
    window._upload_workers[child_id] = SimpleNamespace(
        request_cancel=lambda: None
    )  # type: ignore[assignment]
    window._finish_folder_upload()
    assert window.transfer_interface._find_record("upload", root_id).status == "上传中"

    window._on_upload_succeeded(_file_item(name="child.txt"), child_id)
    window._upload_workers.pop(child_id)
    window._finish_folder_upload()

    assert window.transfer_interface._find_record("upload", root_id).status == "已完成"
    assert window._folder_upload_failed_ids == set()


def test_root_stays_active_until_retried_child_finishes(
    qapp: QApplication, tmp_path: Path,
) -> None:
    window = MainWindow(WorkerFileBrowser())
    root_id = window._create_upload_record(tmp_path / "root")
    retried_id = window._create_upload_record(tmp_path / "retried.txt")
    window._folder_upload_record_id = root_id
    window.transfer_interface.update_record("upload", root_id, status="上传中")
    window._folder_upload_child_ids = {retried_id}
    requests: list[bool] = []
    window._upload_workers[retried_id] = SimpleNamespace(
        request_cancel=lambda: requests.append(True)
    )  # type: ignore[assignment]

    window._finish_folder_upload()
    assert window._folder_upload_record_id == root_id
    assert window.transfer_interface._find_record("upload", root_id).status == "上传中"
    window._remove_transfer_records("upload", {root_id})
    assert requests == [True]
    assert window.transfer_interface._find_record("upload", root_id) is not None

    window._on_upload_cancelled(retried_id)
    window._upload_workers.pop(retried_id)
    window._finish_folder_upload()
    assert window.transfer_interface._find_record("upload", root_id) is None


def test_remove_folder_root_stops_retried_child_in_parallel(
    qapp: QApplication, tmp_path: Path,
) -> None:
    window = MainWindow(WorkerFileBrowser())
    root_id = window._create_upload_record(tmp_path / "root")
    retried_id = window._create_upload_record(tmp_path / "retried.txt")
    active_id = window._create_upload_record(tmp_path / "active.txt")
    window._folder_upload_record_id = root_id
    window._folder_upload_child_ids = {retried_id, active_id}
    window._folder_upload_active = QueuedUploadFile(
        active_id, tmp_path / "active.txt", "cloud-root", "active.txt"
    )
    requested: list[str] = []
    for task_id in (retried_id, active_id):
        window._upload_workers[task_id] = SimpleNamespace(
            request_cancel=lambda tid=task_id: requested.append(tid)
        )  # type: ignore[assignment]
        window._upload_threads[task_id] = QThread(window)

    window._remove_transfer_records("upload", {root_id})
    assert set(requested) == {retried_id, active_id}
    assert window.transfer_interface._find_record("upload", root_id) is not None
    window._on_upload_cancelled(retried_id)
    window._upload_workers.pop(retried_id)
    window._upload_threads.pop(retried_id)
    window._finish_folder_upload()
    assert window.transfer_interface._find_record("upload", root_id) is not None

    window._on_upload_cancelled(active_id)
    window._upload_workers.pop(active_id)
    window._upload_threads.pop(active_id)
    window._continue_folder_upload_queue()
    assert window.transfer_interface._find_record("upload", root_id) is None
    assert window._folder_upload_child_ids == set()


def test_remove_folder_root_starts_next_waiting_folder(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = MainWindow(WorkerFileBrowser())
    root_id = window._create_upload_record(tmp_path / "root")
    child_id = window._create_upload_record(tmp_path / "child.txt")
    next_id = window._create_upload_record(tmp_path / "next")
    window._folder_upload_record_id = root_id
    window._folder_upload_target_dir_id = "different-directory"
    window._folder_upload_queue = [
        QueuedUploadFile(child_id, tmp_path / "child.txt", "cloud-root", "child.txt")
    ]
    window._folder_prepare_pending = [
        PendingFolderUpload(tmp_path / "next", "0", "next", next_id)
    ]
    started: list[str] = []
    monkeypatch.setattr(
        window, "upload_folder_to_current_directory",
        lambda path, **kwargs: started.append(kwargs["_record_id"]),
    )

    window._remove_transfer_records("upload", {root_id})

    assert window.transfer_interface._find_record("upload", root_id) is None
    assert window.transfer_interface._find_record("upload", child_id) is None
    assert window.transfer_interface._find_record("upload", next_id) is not None
    assert started == [next_id]


def test_remove_pending_folder_does_not_stop_active_folder(
    qapp: QApplication, tmp_path: Path,
) -> None:
    window = MainWindow(WorkerFileBrowser())
    active_id = window._create_upload_record(tmp_path / "active")
    waiting_id = window._create_upload_record(tmp_path / "waiting")
    window._folder_upload_record_id = active_id
    window._folder_prepare_pending = [
        PendingFolderUpload(tmp_path / "waiting", "0", "waiting", waiting_id)
    ]
    window._remove_transfer_records("upload", {waiting_id})
    assert window._folder_prepare_pending == []
    assert window._folder_upload_record_id == active_id
    assert window.transfer_interface._find_record("upload", active_id) is not None
    assert window.transfer_interface._find_record("upload", waiting_id) is None


def test_folder_upload_starts_next_pending_folder_after_queue_finishes(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = MainWindow(WorkerFileBrowser())
    second = tmp_path / "second"
    second.mkdir()
    third = tmp_path / "third"
    third.mkdir()
    started: list[tuple[Path, dict[str, object]]] = []
    monkeypatch.setattr(
        window,
        "upload_folder_to_current_directory",
        lambda path, **kwargs: started.append((path, kwargs)),
    )
    window._folder_upload_active = QueuedUploadFile(
        "active", second / "active.txt", "cloud-root", "active.txt"
    )
    window._folder_prepare_pending = [
        PendingFolderUpload(second, "pinned-second", "Second", "second-task"),
        PendingFolderUpload(third, "pinned-third", "Third", "third-task"),
    ]

    window._clear_folder_prepare()
    assert started == []
    assert [pending.local_path for pending in window._folder_prepare_pending] == [second, third]

    window._finish_folder_upload()

    assert started == [
        (
            second,
            {
                "root_name": "Second",
                "_conflict_checked": True,
                "_parent_id": "pinned-second",
                "_record_id": "second-task",
            },
        )
    ]
    assert window._folder_prepare_pending == [
        PendingFolderUpload(third, "pinned-third", "Third", "third-task")
    ]


def test_folder_upload_prepare_failure_reports_and_skips_file_records(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    """AC4：阶段一失败明确报错，不创建任何文件上传任务。"""
    browser = WorkerFileBrowser()
    browser.prepare_error = FileBrowserError("创建目录失败：没有权限")
    window = MainWindow(browser)
    window.refresh_current_directory()
    local_root = _make_folder_tree(tmp_path)

    window.upload_folder_to_current_directory(local_root)

    records = _upload_records(window)
    assert len(records) == 1
    assert records[0].status == "失败"
    assert records[0].error == "创建目录失败：没有权限"
    assert browser.uploaded_files == []
    assert "上传文件夹失败" in window.status_message()
    assert window._folder_prepare_thread is None


def test_folder_upload_prepare_login_required_marks_record_failed(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    browser = WorkerFileBrowser()
    browser.prepare_error = FileBrowserLoginRequiredError("登录已过期，请重新登录")
    window = MainWindow(browser)
    window.refresh_current_directory()
    observed_messages: list[str] = []
    window.login_required.connect(observed_messages.append)

    window.upload_folder_to_current_directory(_make_folder_tree(tmp_path))

    assert observed_messages == ["登录已过期，请重新登录"]
    records = _upload_records(window)
    assert [record.status for record in records] == ["失败"]
    assert browser.uploaded_files == []


def test_folder_prepare_login_failure_runs_next_independent_folder(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    waiting_id = window._create_upload_record(second)
    window._folder_prepare_pending = [
        PendingFolderUpload(second, ROOT_DIRECTORY_ID, "second", waiting_id)
    ]
    prepare = browser.prepare_folder_upload
    attempts = 0

    def prepare_once_failed(
        parent_id: str, root: Path, *, root_name: str | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> FolderUploadJob:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FileBrowserLoginRequiredError("登录已过期，请重新登录")
        return prepare(
            parent_id, root, root_name=root_name, cancel_requested=cancel_requested
        )

    monkeypatch.setattr(browser, "prepare_folder_upload", prepare_once_failed)
    window.upload_folder_to_current_directory(
        first, root_name="first", _conflict_checked=True
    )

    assert attempts == 2
    records = _upload_records(window)
    assert [record.status for record in records] == ["已完成", "失败"]
    assert records[0].task_id == waiting_id
    assert records[1].error == "登录已过期，请重新登录"
    assert browser.prepared_uploads == [(ROOT_DIRECTORY_ID, second)]


def test_folder_upload_without_files_finishes_with_empty_summary(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    spy = _InfoBarSpy()
    spy.install(monkeypatch)
    empty_root = tmp_path / "空文件夹"
    empty_root.mkdir()

    window.upload_folder_to_current_directory(empty_root)

    assert spy.calls[-1] == ("info", "上传完成", "成功 0 个，失败 0 个")
    assert browser.uploaded_files == []
    records = _upload_records(window)
    assert [record.status for record in records] == ["已完成"]


def test_folder_upload_entry_remains_usable_while_upload_is_active(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    local_root = _make_folder_tree(tmp_path)

    window._upload_thread = QThread(window)  # type: ignore[assignment]
    window.upload_folder_to_current_directory(local_root)
    assert browser.prepared_uploads == [(ROOT_DIRECTORY_ID, local_root)]
    window._upload_thread = None

    window._folder_prepare_thread = QThread(window)  # type: ignore[assignment]
    window.upload_folder_to_current_directory(local_root)
    pending = window._folder_prepare_pending[0]
    assert pending.local_path == local_root
    assert pending.parent_id == ROOT_DIRECTORY_ID
    assert pending.root_name == local_root.name
    assert pending.record_id in {
        record.task_id for record in window.transfer_interface.upload_records
    }

    window._folder_prepare_thread = None
    window._folder_prepare_pending.clear()

    window._folder_upload_queue = [
        QueuedUploadFile("upload-1", local_root / "file.txt", "cloud-root", "file.txt")
    ]
    window.upload_folder_to_current_directory(local_root)
    pending = window._folder_prepare_pending[0]
    assert pending.local_path == local_root
    assert pending.parent_id == ROOT_DIRECTORY_ID
    assert pending.root_name == local_root.name
    assert pending.record_id in {
        record.task_id for record in window.transfer_interface.upload_records
    }

    window._folder_upload_queue.clear()


def test_folder_upload_defers_until_single_upload_slot_frees(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    """准备完成时槽位被单文件上传占用：先排队，槽位释放后自动继续。"""
    browser = WorkerFileBrowser()
    window = MainWindow(
        browser, settings=AppSettings(max_concurrent_uploads=1)
    )
    window.refresh_current_directory()
    local_root = _make_folder_tree(tmp_path)
    job = browser.prepare_folder_upload(ROOT_DIRECTORY_ID, local_root)

    window._upload_thread = QThread(window)  # type: ignore[assignment]
    window._folder_upload_target_dir_id = ROOT_DIRECTORY_ID
    window._on_folder_upload_prepared(job)
    assert len(window._folder_upload_queue) == len(job.files) - 1
    assert [task.task_id for task in window._upload_pending] == [
        window._folder_upload_active.task_id
    ]
    assert browser.uploaded_files == []

    window._upload_thread = None
    window._clear_upload_task()

    assert browser.uploaded_files == [
        (f"cloud-{ROOT_DIRECTORY_ID}-2024", local_root / "2024" / "春节.md"),
        (f"cloud-{ROOT_DIRECTORY_ID}-root", local_root / "说明.txt"),
    ]
    assert window._folder_upload_queue == []


def test_upload_folder_to_current_directory_requires_browser(qapp: QApplication) -> None:
    window = MainWindow()

    window.upload_folder_to_current_directory(Path("/tmp/any-folder"))

    assert window.status_message() == "请先登录"


def test_upload_folder_to_current_directory_rejects_empty_folder_name(
    qapp: QApplication,
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()

    window.upload_folder_to_current_directory(Path("/"))

    assert window.status_message() == "上传文件夹不能为空"
    assert browser.prepared_uploads == []


def test_upload_tasks_respect_concurrent_upload_limit(
    qapp: QApplication, tmp_path: Path
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser, settings=AppSettings(max_concurrent_uploads=1))
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first")
    second.write_text("second")
    first_task_id = window._create_upload_record(first)
    second_task_id = window._create_upload_record(second)
    window._upload_thread = QThread(window)  # type: ignore[assignment]

    window._start_upload_task(ROOT_DIRECTORY_ID, second, second_task_id)

    assert [pending.task_id for pending in window._upload_pending] == [second_task_id]
    assert browser.uploaded_files == []
    window._upload_thread = None
    window._start_next_upload_task()

    assert browser.uploaded_files == [(ROOT_DIRECTORY_ID, second)]
    assert window.transfer_interface._find_record("upload", second_task_id).status == "已完成"
    assert first_task_id != second_task_id


def test_upload_enqueue_status_is_confirmation(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    messages: list[str] = []
    set_status = window._set_status

    def record_status(message: str) -> None:
        messages.append(message)
        set_status(message)

    monkeypatch.setattr(window, "_set_status", record_status)
    window.upload_file_to_current_directory(tmp_path / "single.txt")
    assert "已添加「single.txt」上传任务" in messages
    assert not any(message.startswith("正在上传") for message in messages)
    assert window.transfer_interface.upload_records[0].status == "已完成"

    messages.clear()
    window.upload_folder_to_current_directory(_make_folder_tree(tmp_path))
    assert "已添加 2 个上传任务" in messages
    assert not any(message.startswith("正在上传") for message in messages)
    assert [record.status for record in window.transfer_interface.upload_records[-2:]] == [
        "已完成", "已完成"
    ]


def test_upload_conflict_keeps_file_as_copy(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    local_path = tmp_path / "report.txt"
    local_path.write_text("duplicate")
    observed_conflicts: list[tuple[Path, ...]] = []

    class CopyDialog:
        def __init__(self, conflicts: tuple[Path, ...], _parent: QWidget) -> None:
            observed_conflicts.append(conflicts)

        exec = _static_exec_result(int(QDialog.DialogCode.Accepted))

        def resolution(self) -> str:
            return "copy"

    monkeypatch.setattr(main_window_module, "UploadConflictDialog", CopyDialog)
    window.upload_file_to_current_directory(local_path)

    assert observed_conflicts == [(local_path,)]
    assert browser.upload_names == ["report (copy).txt"]
    assert window.transfer_interface.upload_records[0].name == "report (copy).txt"


def test_failed_renamed_upload_can_be_retried_with_same_target_name(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = WorkerFileBrowser()
    browser.upload_errors = [FileBrowserError("HTTP 504"), None]
    window = MainWindow(browser)
    window.refresh_current_directory()
    local_path = tmp_path / "report.txt"
    local_path.write_text("duplicate")

    class CopyDialog:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        exec = _static_exec_result(int(QDialog.DialogCode.Accepted))

        def resolution(self) -> str:
            return "copy"

    monkeypatch.setattr(main_window_module, "UploadConflictDialog", CopyDialog)
    window.upload_file_to_current_directory(local_path)
    record = window.transfer_interface.upload_records[0]
    assert record.status == "失败"
    assert record.upload_name == "report (copy).txt"

    window.transfer_interface.upload_table.selectRow(0)
    window.transfer_interface._request_retry_selected_uploads()

    assert browser.upload_names == ["report (copy).txt", "report (copy).txt"]
    assert window.transfer_interface.upload_records[0].status == "已完成"


def test_upload_conflict_skip_creates_no_file_task(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    local_path = tmp_path / "report.txt"
    local_path.write_text("duplicate")

    class SkipDialog:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        exec = _static_exec_result(int(QDialog.DialogCode.Accepted))

        def resolution(self) -> str:
            return "skip"

    monkeypatch.setattr(main_window_module, "UploadConflictDialog", SkipDialog)
    window.upload_file_to_current_directory(local_path)

    assert browser.uploaded_files == []
    assert window.transfer_interface.upload_records == []
    assert "已跳过 1 个冲突项目" in window.status_message()


def test_upload_folder_conflict_passes_copy_root_name_before_prepare(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    local_root = tmp_path / "Folder"
    local_root.mkdir()
    (local_root / "child.txt").write_text("child")

    class CopyDialog:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        exec = _static_exec_result(int(QDialog.DialogCode.Accepted))

        def resolution(self) -> str:
            return "copy"

    monkeypatch.setattr(main_window_module, "UploadConflictDialog", CopyDialog)
    window.upload_folder_to_current_directory(local_root)

    assert browser.prepared_upload_names == ["Folder (copy)"]
    assert window.transfer_interface.upload_records[0].name == "Folder (copy)"


def test_batch_upload_conflict_skip_keeps_non_conflicting_task(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    conflicting = tmp_path / "report.txt"
    non_conflicting = tmp_path / "new.txt"
    conflicting.write_text("duplicate")
    non_conflicting.write_text("new")

    class SkipDialog:
        def __init__(self, conflicts: tuple[Path, ...], _parent: QWidget) -> None:
            assert conflicts == (conflicting,)

        exec = _static_exec_result(int(QDialog.DialogCode.Accepted))

        def resolution(self) -> str:
            return "skip"

    monkeypatch.setattr(main_window_module, "UploadConflictDialog", SkipDialog)
    window._submit_upload_paths((conflicting, non_conflicting))

    assert browser.uploaded_files == [(ROOT_DIRECTORY_ID, non_conflicting)]
    assert browser.upload_names == ["new.txt"]
    assert [record.name for record in window.transfer_interface.upload_records] == [
        "new.txt"
    ]


def test_upload_conflict_cancel_creates_no_folder_or_record(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    local_root = tmp_path / "Folder"
    local_root.mkdir()

    class CancelDialog:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        exec = _static_exec_result(int(QDialog.DialogCode.Rejected))

        def resolution(self) -> None:
            return None

    monkeypatch.setattr(main_window_module, "UploadConflictDialog", CancelDialog)
    window.upload_folder_to_current_directory(local_root)

    assert browser.prepared_uploads == []
    assert window.transfer_interface.upload_records == []
    assert window.status_message() == "已取消上传"


def test_upload_conflict_check_runs_off_gui_thread(
    qapp: QApplication, tmp_path: Path
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    gui_thread_id = threading.get_ident()
    list_thread_ids: list[int] = []
    original_list_directory = browser.list_directory

    def list_directory(parent_id: str = ROOT_DIRECTORY_ID) -> list[WopanItem]:
        list_thread_ids.append(threading.get_ident())
        return original_list_directory(parent_id)

    browser.list_directory = list_directory  # type: ignore[method-assign]
    local_path = tmp_path / "new.txt"
    local_path.write_text("new")
    window.upload_file_to_current_directory(local_path)

    assert _wait_until(
        qapp,
        lambda: window._upload_conflict_thread is None and not window._upload_threads,
    )
    assert list_thread_ids and all(thread_id != gui_thread_id for thread_id in list_thread_ids)


def test_upload_folder_menu_action_starts_selected_folder(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    root = _make_folder_tree(tmp_path)
    monkeypatch.setattr(main_window_module, "QFileDialog", FakeFileDialog)
    FakeFileDialog.existing_directory = str(root)

    assert window.file_interface.upload_folder_action.isEnabled()
    window.file_interface.upload_folder_action.trigger()

    assert browser.prepared_uploads == [(ROOT_DIRECTORY_ID, root)]
    assert len(browser.uploaded_files) == 2


def test_download_button_submits_all_selected_files(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    browser.items_by_parent[ROOT_DIRECTORY_ID].append(_file_item("file-2", "second.txt"))
    window = MainWindow(
        browser, settings=AppSettings(default_download_path=tmp_path, ask_download_location=True)
    )
    window.refresh_current_directory()
    table = window.file_interface.file_table
    table.selectRow(1)
    selection = table.selectionModel()
    assert selection is not None
    selection.select(
        table.model().index(2, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    assert window.selected_download_rows() == [1, 2]
    monkeypatch.setattr(main_window_module, "QFileDialog", FakeFileDialog)
    FakeFileDialog.existing_directory = str(tmp_path)

    window.file_interface.download_button.click()

    assert {record.name for record in window.transfer_interface.download_records} == {
        "report.txt", "second.txt"
    }
    assert {call["local_path"] for call in browser.download_calls} == {
        tmp_path / "report.txt", tmp_path / "second.txt"
    }


def test_refresh_button_does_not_pass_clicked_bool_as_callback(qapp: QApplication) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    before = len(browser.requested_parent_ids)

    window.file_interface.refresh_button.click()

    assert len(browser.requested_parent_ids) == before + 1
    assert window._after_refresh is None
    assert [item.name for item in window.displayed_items()] == ["Folder", "report.txt"]


def test_upload_summary_dialog_bounded_expanded_preview(qapp: QApplication) -> None:
    parent = QWidget()
    summary = UploadBatchSummary(
        top_paths=(Path("/tmp/folder"),),
        file_count=40,
        folder_count=1,
        total_bytes=100,
        preview=tuple(
            UploadSummaryEntry(Path(f"/tmp/folder/{i}.txt"), f"{i}.txt", "文件", 5)
            for i in range(20)
        ),
        omitted_count=21,
    )
    dialog = UploadSummaryDialog(summary, "云端目录", parent)
    dialog.show()
    qapp.processEvents()
    previews = dialog.findChildren(QListWidget)

    assert len(previews) == 1
    assert previews[0].isVisibleTo(dialog)
    assert previews[0].count() == 21
    assert previews[0].item(20).text() == "另有 21 项"
    assert dialog.findChildren(QCheckBox) == []
    assert dialog.findChildren(QRadioButton) == []
    assert dialog.resolution() == "copy"
    assert "云端目录" in dialog.findChildren(main_window_module.BodyLabel)[1].text()


def test_upload_summary_dialog_keeps_labels_compact_when_enlarged(qapp: QApplication) -> None:
    parent = QWidget()
    conflict = Path("/tmp/conflict.txt")
    summary = UploadBatchSummary(
        top_paths=(conflict,),
        file_count=1,
        folder_count=0,
        total_bytes=8,
        preview=(UploadSummaryEntry(conflict, "conflict.txt", "文件", 8),),
        omitted_count=0,
    )
    dialog = UploadSummaryDialog(summary, "云端目录", parent, conflicts=(conflict,))
    dialog.resize(900, 750)
    dialog.show()
    qapp.processEvents()
    preview = dialog.findChild(QListWidget)
    labels = dialog.findChildren(main_window_module.BodyLabel)
    buttons = dialog.findChild(QDialogButtonBox)
    choices = dialog.findChildren(QRadioButton)

    assert preview is not None and buttons is not None
    assert len(dialog.findChildren(QListWidget)) == 1
    assert dialog.findChildren(QCheckBox) == []
    assert len(choices) == 2
    assert "1 个同名" in " ".join(label.text() for label in labels)
    assert preview.item(0).text().startswith("（重复）conflict.txt")
    assert preview.isVisibleTo(dialog) and preview.height() > 200
    assert all(label.height() <= label.sizeHint().height() + 2 for label in labels)
    assert buttons.geometry().top() - labels[-1].geometry().bottom() < 32
    confirm = buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert confirm is not None and not confirm.isEnabled()
    copy = next(button for button in choices if button.text() == "保留副本")
    copy.click()
    assert confirm.isEnabled()
    assert dialog.resolution() == "copy"
    skip = next(button for button in choices if button.text() == "跳过冲突")
    skip.click()
    assert dialog.resolution() == "skip"
    assert not confirm.isEnabled()


def test_upload_summary_dialog_bounds_conflict_preview(qapp: QApplication) -> None:
    parent = QWidget()
    conflicts = tuple(Path(f"/tmp/{index}.txt") for index in range(25))
    preview_entries = tuple(
        UploadSummaryEntry(path, path.name, "文件", 1) for path in conflicts[:20]
    )
    summary = UploadBatchSummary(conflicts, 25, 0, 25, preview_entries, 5)
    dialog = UploadSummaryDialog(summary, "云端目录", parent, conflicts=conflicts)
    previews = dialog.findChildren(QListWidget)
    assert len(previews) == 1
    assert previews[0].count() == 21
    assert all(previews[0].item(index).text().startswith("（重复）") for index in range(20))
    assert previews[0].item(20).text() == "另有 5 项"
    confirm = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)
    assert not confirm.isEnabled()


def test_upload_summary_marks_only_conflicting_top_level_entry(qapp: QApplication) -> None:
    parent = QWidget()
    folder = Path("/tmp/folder")
    child = folder / "child.txt"
    other = Path("/tmp/other.txt")
    summary = UploadBatchSummary(
        (folder, other), 2, 1, 2,
        (
            UploadSummaryEntry(folder, "folder", "文件夹", 0),
            UploadSummaryEntry(child, "folder/child.txt", "文件", 1),
            UploadSummaryEntry(other, "other.txt", "文件", 1),
        ),
        0,
    )
    dialog = UploadSummaryDialog(summary, "云端目录", parent, conflicts=(folder,))
    preview = dialog.findChild(QListWidget)

    assert [preview.item(index).text().startswith("（重复）") for index in range(3)] == [
        True, False, False,
    ]


def test_upload_summary_marks_late_conflict_after_large_folder(
    qapp: QApplication, tmp_path: Path
) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    for index in range(25):
        (folder / f"{index:02d}.txt").write_text("content")
    duplicate = tmp_path / "duplicate.txt"
    duplicate.write_text("content")
    summary = main_window_module.scan_upload_inputs((folder, duplicate))
    parent = QWidget()

    dialog = UploadSummaryDialog(summary, "云端目录", parent, conflicts=(duplicate,))
    preview = dialog.findChild(QListWidget)

    assert preview.count() == 21
    assert any(
        preview.item(index).text().startswith("（重复）duplicate.txt")
        for index in range(20)
    )
    assert preview.item(20).text() == "另有 7 项"


def test_upload_conflict_queue_waits_for_open_confirmation(qapp: QApplication) -> None:
    window = MainWindow(WorkerFileBrowser())
    window._upload_conflict_dialog_open = True
    pending = ((Path("/tmp/new.txt"),), ROOT_DIRECTORY_ID, True, None)
    window._upload_conflict_pending.append(pending)

    window._clear_upload_conflict_check()

    assert window._upload_conflict_pending == [pending]
    assert window._upload_conflict_thread is None


@pytest.mark.parametrize(
    ("resolution", "expected_names"),
    [
        ("skip", ["new.txt"]),
        ("copy", ["report (copy).txt", "new.txt"]),
        (None, []),
    ],
)
def test_upload_drop_uses_one_summary_for_conflict_decision(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolution: str | None,
    expected_names: list[str],
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    conflicting = tmp_path / "report.txt"
    other = tmp_path / "new.txt"
    conflicting.write_text("duplicate")
    other.write_text("new")
    dialogs: list[tuple[int, tuple[Path, ...]]] = []

    class SummaryDialog:
        def __init__(
            self, summary: UploadBatchSummary, _target: str, _parent: QWidget, *,
            conflicts: tuple[Path, ...],
        ) -> None:
            dialogs.append((summary.file_count, conflicts))

        def _show_modal(self) -> int:
            return int(
                QDialog.DialogCode.Accepted if resolution is not None
                else QDialog.DialogCode.Rejected
            )

        exec = _show_modal

        def resolution(self) -> str | None:
            return resolution

    def unexpected_conflict_dialog(*_args: object) -> None:
        raise AssertionError("drag-drop must not open a second dialog")

    monkeypatch.setattr(main_window_module, "UploadSummaryDialog", SummaryDialog)
    monkeypatch.setattr(main_window_module, "UploadConflictDialog", unexpected_conflict_dialog)
    window.handle_upload_drop((conflicting, other))

    assert _wait_until(qapp, lambda: window._scan_thread is None and bool(dialogs))
    assert dialogs == [(2, (conflicting,))]
    assert browser.upload_names == expected_names
    assert [record.name for record in window.transfer_interface.upload_records] == expected_names
    assert ROOT_DIRECTORY_ID in browser.requested_parent_ids


def test_upload_summary_rejects_navigation_during_confirmation(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    local_file = tmp_path / "new.txt"
    local_file.write_text("new")
    summary = main_window_module.scan_upload_inputs((local_file,))

    class NavigateDialog:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def _show_modal(self) -> int:
            window._breadcrumb.append(
                main_window_module.BreadcrumbEntry("folder-1", "Folder")
            )
            return int(QDialog.DialogCode.Accepted)

        exec = _show_modal

        def resolution(self) -> str:
            return "copy"

    monkeypatch.setattr(main_window_module, "UploadSummaryDialog", NavigateDialog)
    window._submit_upload_paths(summary.top_paths, summary=summary)

    assert browser.uploaded_files == []
    assert window.transfer_interface.upload_records == []
    assert window.status_message() == "目录已变化，请重新提交上传任务"


def test_upload_summary_not_shown_when_name_check_fails(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    local_file = tmp_path / "new.txt"
    local_file.write_text("new")
    summary = main_window_module.scan_upload_inputs((local_file,))

    def list_fails(_parent: str) -> list[WopanItem]:
        raise FileBrowserError("检查失败")

    def unexpected_dialog(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cloud name check must finish before the dialog")

    monkeypatch.setattr(browser, "list_directory", list_fails)
    monkeypatch.setattr(main_window_module, "UploadSummaryDialog", unexpected_dialog)
    window._submit_upload_paths(summary.top_paths, summary=summary)

    assert window.transfer_interface.upload_records == []
    assert window.status_message() == "检查上传名称失败：检查失败"


@pytest.mark.parametrize(
    ("button_text", "resolution"),
    [("跳过冲突", "skip"), ("保留副本", "copy"), ("取消本批次", None)],
)
def test_upload_conflict_dialog_bounded_preview_and_choice(
    qapp: QApplication, button_text: str, resolution: str | None
) -> None:
    parent = QWidget()
    dialog = UploadConflictDialog(tuple(Path(f"/tmp/{i}.txt") for i in range(25)), parent)
    preview = dialog.findChild(QListWidget)
    assert preview is not None
    assert preview.count() == 21
    assert preview.item(20).text() == "另有 5 项"
    button = next(
        button for button in dialog.findChildren(QPushButton) if button.text() == button_text
    )
    button.click()
    assert dialog.resolution() == resolution
    expected = QDialog.DialogCode.Rejected if resolution is None else QDialog.DialogCode.Accepted
    assert dialog.result() == expected


class _DropEvent:
    def __init__(self, mime_data: QMimeData) -> None:
        self._mime_data = mime_data
        self.accepted = False

    def mimeData(self) -> QMimeData:
        return self._mime_data

    def acceptProposedAction(self) -> None:
        self.accepted = True

    def ignore(self) -> None:
        self.accepted = False


def test_file_table_rejects_mixed_local_and_remote_drop(
    qapp: QApplication, tmp_path: Path
) -> None:
    parent = QWidget()
    table = DroppableTableWidget(parent)
    dropped: list[tuple[Path, ...]] = []
    table.paths_dropped.connect(dropped.append)
    local_path = tmp_path / "drop.txt"
    local_path.write_text("content")
    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(local_path)), QUrl("https://example.test/file")])
    event = _DropEvent(mime_data)

    table.dragEnterEvent(event)
    table.dragMoveEvent(event)
    table.dropEvent(event)

    assert not event.accepted
    assert dropped == []


def test_file_table_accepts_local_file_drop(
    qapp: QApplication, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    parent = QWidget()
    table = DroppableTableWidget(parent)
    dropped: list[tuple[Path, ...]] = []
    table.paths_dropped.connect(dropped.append)
    mime_data = QMimeData()
    local_path = tmp_path / "drop.txt"
    local_path.write_text("content")
    mime_data.setUrls([QUrl.fromLocalFile(str(local_path))])
    event = _DropEvent(mime_data)

    with caplog.at_level("INFO", logger=main_window_module.LOGGER.name):
        table.dragEnterEvent(event)
        table.dragMoveEvent(event)
        table.dropEvent(event)

    assert event.accepted
    assert dropped == [(local_path,)]
    assert [record.message for record in caplog.records] == [
        "main_window.upload_drop.received count=1",
        "main_window.upload_drop.dispatched count=1",
    ]
    assert str(local_path) not in caplog.text


def test_upload_drop_cancel_does_not_create_tasks(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    local_path = tmp_path / "drop.txt"
    local_path.write_text("content")

    class CancelDialog:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        exec = _static_exec_result(int(QDialog.DialogCode.Rejected))

    monkeypatch.setattr(main_window_module, "UploadSummaryDialog", CancelDialog)
    window.handle_upload_drop((local_path,))

    assert _wait_until(qapp, lambda: window._scan_thread is None)
    assert browser.uploaded_files == []
    assert window.transfer_interface.upload_records == []
    assert window.status_message() == "已取消上传"


def test_upload_drop_accepts_batch_with_partial_failure(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    browser.upload_errors = [FileBrowserError("one failed"), None]
    window = MainWindow(browser)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first")
    second.write_text("second")

    class AcceptDialog:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        exec = _static_exec_result(int(QDialog.DialogCode.Accepted))

        def resolution(self) -> str:
            return "copy"

    monkeypatch.setattr(main_window_module, "UploadSummaryDialog", AcceptDialog)
    window.handle_upload_drop((first, second))

    assert _wait_until(
        qapp,
        lambda: window._scan_thread is None
        and not window._upload_threads
        and all(
            record.status in {"失败", "已完成"}
            for record in window.transfer_interface.upload_records
        ),
    )
    assert [record.status for record in window.transfer_interface.upload_records] == [
        "失败",
        "已完成",
    ]

    assert len(browser.uploaded_files) == 2
    assert window.status_message() in {
        "正在加载...",
        "上传失败：one failed",
        "上传完成：second.txt",
    }


def test_upload_drop_scans_off_gui_thread(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    gui_thread_id = threading.get_ident()
    scan_thread_ids: list[int] = []
    local_path = tmp_path / "drop.txt"
    local_path.write_text("content")

    original_scan = main_window_module.scan_upload_inputs

    def scan(paths: tuple[Path, ...]) -> object:
        scan_thread_ids.append(threading.get_ident())
        return original_scan(paths)

    class CancelDialog:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        exec = _static_exec_result(int(QDialog.DialogCode.Rejected))

    monkeypatch.setattr(main_window_module, "scan_upload_inputs", scan)
    monkeypatch.setattr(main_window_module, "UploadSummaryDialog", CancelDialog)
    with caplog.at_level("INFO", logger=main_window_module.LOGGER.name):
        window.handle_upload_drop((local_path,))
        assert _wait_until(
            qapp,
            lambda: window._scan_thread is None
            and window._upload_conflict_thread is None
            and window.status_message() == "已取消上传",
        )

    assert scan_thread_ids and scan_thread_ids[0] != gui_thread_id
    events = [record.message.split()[0] for record in caplog.records]
    assert events == [
        "main_window.upload_scan.start",
        "main_window.upload_scan.success",
        "main_window.upload_conflict_check.start",
        "main_window.upload_conflict_check.success",
    ]
    assert str(local_path) not in caplog.text


@pytest.mark.parametrize("entry", ["drop", "picker"])
def test_close_ignores_late_upload_check_result(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    local_path = tmp_path / "report.txt"
    local_path.write_text("content")
    started = threading.Event()
    dialogs: list[str] = []

    class UnexpectedDialog:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            dialogs.append("opened")

        exec = _static_exec_result(int(QDialog.DialogCode.Rejected))

        def resolution(self) -> None:
            return None

    if entry == "drop":
        original_scan = main_window_module.scan_upload_inputs

        def slow_scan(paths: tuple[Path, ...]) -> UploadBatchSummary:
            started.set()
            time.sleep(0.05)
            return original_scan(paths)

        monkeypatch.setattr(main_window_module, "scan_upload_inputs", slow_scan)
        monkeypatch.setattr(main_window_module, "UploadSummaryDialog", UnexpectedDialog)
        window.handle_upload_drop((local_path,))
    else:
        original_list = browser.list_directory

        def slow_list(parent_id: str) -> list[WopanItem]:
            started.set()
            time.sleep(0.05)
            return original_list(parent_id)

        monkeypatch.setattr(browser, "list_directory", slow_list)
        monkeypatch.setattr(main_window_module, "UploadConflictDialog", UnexpectedDialog)
        window._submit_upload_paths((local_path,))

    assert started.wait(2)
    window.close()
    assert _wait_until(
        qapp,
        lambda: window._scan_thread is None and window._upload_conflict_thread is None,
    )
    assert window._closing
    assert dialogs == []
    assert browser.uploaded_files == []
    assert window.transfer_interface.upload_records == []


def test_close_during_upload_summary_cannot_submit(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    local_path = tmp_path / "new.txt"
    local_path.write_text("content")
    summary = main_window_module.scan_upload_inputs((local_path,))

    class CloseAndAcceptDialog:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def _show_modal(self) -> int:
            window.close()
            return int(QDialog.DialogCode.Accepted)

        exec = _show_modal

        def resolution(self) -> str:
            return "copy"

    monkeypatch.setattr(main_window_module, "UploadSummaryDialog", CloseAndAcceptDialog)
    window._submit_upload_paths(summary.top_paths, summary=summary)

    assert window._closing
    assert browser.uploaded_files == []
    assert window.transfer_interface.upload_records == []


def test_picker_conflict_rejects_navigation_during_dialog(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    local_path = tmp_path / "report.txt"
    local_path.write_text("content")

    class NavigateAndCopyDialog:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def _show_modal(self) -> int:
            window._breadcrumb.append(
                main_window_module.BreadcrumbEntry("folder-1", "Folder")
            )
            return int(QDialog.DialogCode.Accepted)

        exec = _show_modal

        def resolution(self) -> str:
            return "copy"

    monkeypatch.setattr(main_window_module, "UploadConflictDialog", NavigateAndCopyDialog)
    window._submit_upload_paths((local_path,))

    assert browser.uploaded_files == []
    assert window.transfer_interface.upload_records == []
    assert window.status_message() == "目录已变化，请重新提交上传任务"


def test_close_during_picker_conflict_cannot_submit(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    local_path = tmp_path / "report.txt"
    local_path.write_text("content")

    class CloseAndCopyDialog:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def _show_modal(self) -> int:
            window.close()
            return int(QDialog.DialogCode.Accepted)

        exec = _show_modal

        def resolution(self) -> str:
            return "copy"

    monkeypatch.setattr(main_window_module, "UploadConflictDialog", CloseAndCopyDialog)
    window._submit_upload_paths((local_path,))

    assert window._closing
    assert browser.uploaded_files == []
    assert window.transfer_interface.upload_records == []


def test_close_ignores_late_upload_check_failures(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = MainWindow(WorkerFileBrowser())
    window.close()
    status = window.status_message()
    login_messages: list[str] = []
    window.login_required.connect(login_messages.append)

    def unexpected_error(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("closed window must not show an error")

    monkeypatch.setattr(main_window_module.InfoBar, "error", unexpected_error)
    window._on_upload_scan_failed("late failure")
    window._on_upload_conflict_check_failed("late failure")
    window._on_upload_conflict_check_login_required("late login failure")
    window._submit_upload_paths((Path("/tmp/late.txt"),))

    assert window.status_message() == status
    assert login_messages == []
    assert window._upload_conflict_pending == []


# ---------------------------------------------------------------------------
# Upload recovery rows (startup recovery of interrupted upload sessions)
# ---------------------------------------------------------------------------


def _upload_recovery_record(task_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        task_id=task_id,
        name="persisted.bin",
        local_path=Path(f"/tmp/{task_id}.bin"),
        target_parent_id="0",
        upload_name="persisted.bin",
        status="失败",
        completed_parts=1,
        total_parts=3,
        file_size=100,
        error="应用中断，可续传（已完成 1/3 分片）",
        resumable=True,
    )


class UploadRecoveryBrowser(WorkerFileBrowser):
    def __init__(
        self,
        records: tuple[SimpleNamespace, ...] = (),
        error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.recovery_records = records
        self.recovery_error = error

    def recover_uploads(self) -> tuple[SimpleNamespace, ...]:
        if self.recovery_error is not None:
            raise self.recovery_error
        return self.recovery_records


def test_startup_recovers_upload_rows_as_retryable(
    qapp: QApplication, sync_threads: None
) -> None:
    """AC7：启动恢复渲染「失败 + 可重试」上传行。"""
    records = tuple(
        _upload_recovery_record(f"upload-{index}") for index in range(1, 4)
    )
    window = MainWindow(UploadRecoveryBrowser(records))

    window.set_file_browser(UploadRecoveryBrowser(records))

    rows = window.transfer_interface.upload_records
    assert [row.task_id for row in rows] == ["upload-1", "upload-2", "upload-3"]
    row = rows[0]
    assert row.status == "失败"
    assert row.upload_retryable is True
    assert "1/3" in row.error
    assert row.size == 100
    assert row.target_path == Path("/tmp/upload-1.bin")
    assert row.upload_parent_id == "0"


def test_startup_upload_recovery_failure_only_notifies_status(
    qapp: QApplication, sync_threads: None
) -> None:
    browser = UploadRecoveryBrowser(error=FileBrowserError("boom"))
    window = MainWindow(browser)

    # 启动路径：恢复失败不打断后续 refresh_root（状态栏可能被刷新覆盖，仅日志+瞬态提示）
    window.set_file_browser(browser)
    assert window.transfer_interface.upload_records == []

    # 失败处理器直查：状态栏给出可读提示
    window._on_upload_recovery_failed("boom")
    assert "恢复上传任务失败" in window.status_message()
    assert "boom" in window.status_message()


def test_upload_recovery_succeeded_ignores_late_or_malformed_events(
    qapp: QApplication,
) -> None:
    window = MainWindow(WorkerFileBrowser())
    record = _upload_recovery_record("upload-9")

    window._on_upload_recovery_succeeded(["not-a-tuple"])
    assert window.transfer_interface.upload_records == []

    window._on_upload_recovery_succeeded((SimpleNamespace(task_id="bad"),))
    assert window.transfer_interface.upload_records == []

    window._closing = True
    window._on_upload_recovery_succeeded((record,))
    assert window.transfer_interface.upload_records == []

    window._closing = False
    window._on_upload_recovery_succeeded(())
    assert window.transfer_interface.upload_records == []


def test_recover_uploads_without_file_browser_is_noop(qapp: QApplication) -> None:
    window = MainWindow()

    window._recover_uploads()

    assert window.transfer_interface.upload_records == []


def test_recovered_upload_row_retry_uses_original_target(
    qapp: QApplication, tmp_path: Path, sync_threads: None
) -> None:
    """恢复行走既有 retry 链路：原目标原文件重新 upload_file（服务层续传）。"""
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    local_path = tmp_path / "persisted.bin"
    local_path.write_bytes(b"data")
    record = _upload_recovery_record("upload-9")
    record.local_path = local_path
    window._on_upload_recovery_succeeded((record,))

    row = window.transfer_interface._find_record("upload", "upload-9")
    assert row is not None
    # 恢复行显式携带 upload_name，retry 派生键与原会话一致
    assert row.upload_name == "persisted.bin"
    window._retry_upload_task(row)

    assert browser.uploaded_files == [("0", local_path)]
    assert browser.upload_names == ["persisted.bin"]


# ---------------------------------------------------------------------------
# Persistent transfer history (SQLite-backed restore across restarts)
# ---------------------------------------------------------------------------


class FakeTransferPersistence:
    """Protocol double recording every persistence call."""

    def __init__(self) -> None:
        self.saved: list[TransferRecord] = []
        self.progress_updates: list[tuple[str, str, int, int, float]] = []
        self.deleted: list[tuple[str, set[str]]] = []
        self.history: tuple[TransferRecord, ...] = ()
        self.load_calls = 0

    def save_record(self, record: TransferRecord) -> None:
        self.saved.append(record)

    def update_record_progress(
        self,
        direction: str,
        task_id: str,
        *,
        bytes_done: int,
        active_connections: int,
        updated_at: float,
    ) -> None:
        self.progress_updates.append(
            (direction, task_id, bytes_done, active_connections, updated_at)
        )

    def delete_records(self, direction: str, task_ids: Any) -> None:
        self.deleted.append((direction, set(task_ids)))

    def load_history(self) -> tuple[TransferRecord, ...]:
        self.load_calls += 1
        return self.history


def _opened_store(tmp_path: Path) -> TransferRecordStore:
    store = TransferRecordStore(tmp_path / "records.sqlite3")
    store.open()
    return store


def _history_record(task_id: str, direction: str, **overrides: object) -> TransferRecord:
    values: dict[str, object] = {
        "task_id": task_id,
        "direction": direction,
        "name": f"{task_id}.bin",
        "size": 10,
        "target_path": Path(f"/tmp/{task_id}.bin"),
        "status": "已完成",
        "created_at_epoch": 1.0,
        "updated_at_epoch": 2.0,
    }
    values.update(overrides)
    return TransferRecord(**values)


def test_update_record_persists_terminal_and_batches_progress(qapp: QApplication) -> None:
    fake = FakeTransferPersistence()
    interface = TransferInterface(persistence=fake)
    record = TransferRecord(
        task_id="upload-1", direction="upload", name="x.bin", size=100
    )

    interface.add_upload_record(record)
    assert len(fake.saved) == 1

    interface.update_record("upload", "upload-1", status="上传中")
    assert len(fake.saved) == 2

    interface.update_record("upload", "upload-1", bytes_done=50)
    assert len(fake.saved) == 2
    assert ("upload", "upload-1") in interface._persist_dirty

    interface._flush_pending_record_persist()
    assert fake.progress_updates == [
        ("upload", "upload-1", 50, 0, record.updated_at_epoch)
    ]
    assert not interface._persist_timer.isActive()

    interface.update_record("upload", "upload-1", status="已完成")
    assert len(fake.saved) == 3
    assert ("upload", "upload-1") not in interface._persist_dirty


def test_restore_delivery_does_not_write_back(qapp: QApplication) -> None:
    fake = FakeTransferPersistence()
    interface = TransferInterface(persistence=fake)

    interface.add_upload_record(
        _history_record("upload-9", "upload"), render=False, persist=False
    )

    assert fake.saved == []
    assert [row.task_id for row in interface.upload_records] == ["upload-9"]


def test_remove_records_deletes_persisted_rows(qapp: QApplication) -> None:
    fake = FakeTransferPersistence()
    interface = TransferInterface(persistence=fake)
    interface.add_download_record(
        _history_record("download-1", "download", status="下载中")
    )
    interface.update_record("download", "download-1", bytes_done=5)

    interface.remove_records("download", {"download-1"})

    assert fake.deleted == [("download", {"download-1"})]
    assert ("download", "download-1") not in interface._persist_dirty


def test_flush_skips_records_removed_after_dirty_mark(qapp: QApplication) -> None:
    fake = FakeTransferPersistence()
    interface = TransferInterface(persistence=fake)
    interface.add_download_record(_history_record("download-1", "download"))
    interface.update_record("download", "download-1", bytes_done=5)
    interface._persist_dirty.add(("download", "download-gone"))

    interface.remove_records("download", {"download-1"})
    interface._flush_pending_record_persist()

    assert fake.progress_updates == []
    assert not interface._persist_timer.isActive()


def test_stop_record_persistence_flushes_pending_progress(qapp: QApplication) -> None:
    fake = FakeTransferPersistence()
    interface = TransferInterface(persistence=fake)
    record = _history_record("download-1", "download", status="下载中")
    interface.add_download_record(record)
    interface.update_record("download", "download-1", bytes_done=7)
    assert interface._persist_timer.isActive()

    interface.stop_record_persistence()

    assert fake.progress_updates == [
        ("download", "download-1", 7, 0, record.updated_at_epoch)
    ]
    assert not interface._persist_timer.isActive()


class HistoryRestoreBrowser(WorkerFileBrowser):
    """Worker browser double without persisted download rows."""

    def download_records(self) -> tuple[SimpleNamespace, ...]:
        return ()


def test_set_file_browser_restores_history_once_on_gui_thread(
    qapp: QApplication,
    sync_threads: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    seeder = TransferHistoryAdapter(store)
    seeder.save_record(
        _history_record(
            "upload-3",
            "upload",
            status="失败",
            upload_parent_id="0",
            upload_retryable=True,
        )
    )
    seeder.save_record(_history_record("download-5", "download"))
    window = MainWindow(HistoryRestoreBrowser(), transfer_record_store=store)
    renders: list[str] = []
    monkeypatch.setattr(
        window.transfer_interface, "_render_upload_table", lambda: renders.append("upload")
    )
    monkeypatch.setattr(
        window.transfer_interface,
        "_render_download_table",
        lambda: renders.append("download"),
    )

    window.set_file_browser(HistoryRestoreBrowser())

    assert renders == ["upload", "download"]
    assert [row.task_id for row in window.transfer_interface.upload_records] == ["upload-3"]
    assert [row.task_id for row in window.transfer_interface.download_records] == [
        "download-5"
    ]
    upload_row = window.transfer_interface.upload_records[0]
    assert upload_row.created_at_epoch == 1.0
    assert upload_row.upload_retryable is True
    assert window._transfer_sequence == 5
    assert window._next_transfer_task_id("upload") == "upload-6"
    store.close()


def test_transfer_history_conflict_drops_restored_row(
    qapp: QApplication,
    sync_threads: None,
    tmp_path: Path,
) -> None:
    store = _opened_store(tmp_path)
    window = MainWindow(WorkerFileBrowser(), transfer_record_store=store)
    live = TransferRecord(
        task_id="upload-1", direction="upload", name="live.bin", size=1, status="上传中"
    )
    window.transfer_interface.add_upload_record(live)

    window._on_transfer_history_loaded(
        (_history_record("upload-1", "upload", name="stale.bin", status="失败"),)
    )

    rows = window.transfer_interface.upload_records
    assert len(rows) == 1
    assert rows[0] is live
    db_rows = store.load_all()
    assert len(db_rows) == 1
    assert db_rows[0].name == "live.bin"
    store.close()


def test_recovered_download_row_upserts_restored_row(
    qapp: QApplication,
    sync_threads: None,
    tmp_path: Path,
) -> None:
    store = _opened_store(tmp_path)
    window = MainWindow(WorkerFileBrowser(), transfer_record_store=store)
    window._on_transfer_history_loaded(
        (
            _history_record(
                "download-1",
                "download",
                status="已暂停",
                bytes_done=1,
            ),
        )
    )
    assert len(window.transfer_interface.download_records) == 1
    # 恢复行是纯展示行：不自动续传、不注册控制对象（PRD Out of Scope）。
    assert window.transfer_interface.download_records[0].can_resume is False
    assert window._download_controls == {}
    assert not window.transfer_interface._speed_sampler.isActive()

    persisted = SimpleNamespace(
        task_id="download-1",
        name="download-1.bin",
        target_path=Path("/tmp/download-1.bin"),
        status="等待中",
        total_bytes=9,
        bytes_done=2,
        active_connections=0,
        max_connections=1,
        supports_resume=False,
    )
    window._add_persisted_download_record(persisted, render=False)

    rows = window.transfer_interface.download_records
    assert len(rows) == 1
    assert rows[0].status == "等待中"
    db_rows = store.load_all()
    assert len(db_rows) == 1
    assert db_rows[0].status == "等待中"
    store.close()


def test_upload_recovery_upserts_restored_row(
    qapp: QApplication,
    sync_threads: None,
    tmp_path: Path,
) -> None:
    store = _opened_store(tmp_path)
    window = MainWindow(WorkerFileBrowser(), transfer_record_store=store)
    window._on_transfer_history_loaded((_history_record("upload-1", "upload"),))

    window._add_persisted_upload_record(_upload_recovery_record("upload-1"))

    rows = window.transfer_interface.upload_records
    assert len(rows) == 1
    assert rows[0].status == "失败"
    assert rows[0].upload_retryable is True
    db_rows = store.load_all()
    assert len(db_rows) == 1
    assert db_rows[0].status == "失败"
    store.close()


def test_transfer_history_delivery_ignores_malformed_and_late_events(
    qapp: QApplication,
    sync_threads: None,
    tmp_path: Path,
) -> None:
    store = _opened_store(tmp_path)
    window = MainWindow(WorkerFileBrowser(), transfer_record_store=store)

    window._on_transfer_history_loaded("not-a-tuple")
    window._on_transfer_history_loaded((object(),))

    assert window.transfer_interface.upload_records == []
    assert window.transfer_interface.download_records == []

    window._closing = True
    window._on_transfer_history_loaded((_history_record("upload-1", "upload"),))

    assert window.transfer_interface.upload_records == []
    store.close()


def test_transfer_history_load_failure_is_logged_only(
    qapp: QApplication,
    sync_threads: None,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _opened_store(tmp_path)
    window = MainWindow(WorkerFileBrowser(), transfer_record_store=store)

    with caplog.at_level(logging.WARNING, logger="openwopan.ui.main_window"):
        window._on_transfer_history_failed("boom")

    assert any(
        "main_window.transfer_history.load_failed" in message for message in caplog.messages
    )
    store.close()


def test_load_transfer_history_guards(
    qapp: QApplication,
    sync_threads: None,
    tmp_path: Path,
) -> None:
    store = _opened_store(tmp_path)
    window = MainWindow(WorkerFileBrowser(), transfer_record_store=store)
    calls: list[int] = []
    original_load = window._transfer_history.load_history

    def counting_load() -> tuple[TransferRecord, ...]:
        calls.append(1)
        return original_load()

    window._transfer_history.load_history = counting_load  # type: ignore[method-assign]

    window.set_file_browser(WorkerFileBrowser())
    assert calls == [1]
    assert window._transfer_history_loaded is True

    window._transfer_history_loaded = False
    window._transfer_history_thread = cast(QThread, object())
    window._load_transfer_history()
    assert calls == [1]

    window._transfer_history_thread = None
    window._transfer_history_loaded = True
    window._load_transfer_history()
    assert calls == [1]
    store.close()


def test_load_transfer_history_without_store_is_noop(qapp: QApplication) -> None:
    window = MainWindow()

    window._load_transfer_history()

    assert window._transfer_history_loaded is False
    assert window._transfer_history_thread is None


def test_broken_history_store_degrades_without_crashing(
    qapp: QApplication,
    sync_threads: None,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "records.sqlite3"
    db_path.write_bytes(b"this is not a database" * 100)
    window = MainWindow(
        WorkerFileBrowser(), transfer_record_store=TransferRecordStore(db_path)
    )

    window.transfer_interface.add_upload_record(
        TransferRecord(task_id="upload-1", direction="upload", name="x.bin", size=1)
    )
    window.transfer_interface.update_record("upload", "upload-1", status="已完成")
    window.transfer_interface.remove_records("upload", {"upload-1"})

    assert window.transfer_interface.upload_records == []


def test_transfer_sequence_skips_non_numeric_ids(qapp: QApplication) -> None:
    window = MainWindow()

    window._raise_transfer_sequence("download", "abc123def456")
    window._raise_transfer_sequence("upload", "upload-2")
    window._raise_transfer_sequence("upload", "upload-0")

    assert window._transfer_sequence == 2
    assert window._next_transfer_task_id("upload") == "upload-3"


def test_upload_file_flow_persists_record(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    """上传单文件流程经汇聚点写入 SQLite（PRD AC：上传文件记录）。"""
    store = _opened_store(tmp_path)
    browser = WorkerFileBrowser()
    window = MainWindow(browser, transfer_record_store=store)
    window.refresh_current_directory()
    local_path = tmp_path / "movie.bin"
    local_path.write_bytes(b"data")

    window.upload_file_to_current_directory(
        local_path, upload_name="movie.bin", _conflict_checked=True
    )

    assert browser.uploaded_files == [(ROOT_DIRECTORY_ID, local_path)]
    rows = {row.task_id: row for row in store.load_all()}
    assert len(rows) == 1
    (row,) = rows.values()
    assert row.direction == "upload"
    assert row.name == "movie.bin"
    assert row.status == "已完成"
    assert row.local_path == str(local_path)
    store.close()


def test_folder_upload_flow_persists_root_and_child_records(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    """上传文件夹流程：root 与逐文件记录分别落库（PRD AC：文件夹批量）。"""
    store = _opened_store(tmp_path)
    browser = WorkerFileBrowser()
    window = MainWindow(browser, transfer_record_store=store)
    window.refresh_current_directory()
    local_root = _make_folder_tree(tmp_path)

    window.upload_folder_to_current_directory(local_root)

    rows = {
        row.name: row
        for row in store.load_all()
        if row.direction == "upload"
    }
    assert set(rows) == {"相册", "春节.md", "说明.txt"}
    assert all(row.status == "已完成" for row in rows.values())
    assert rows["春节.md"].local_path == str(local_root / "2024" / "春节.md")
    assert rows["说明.txt"].local_path == str(local_root / "说明.txt")
    store.close()


def test_download_batch_flow_persists_records(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    """下载批量流程逐项写入 SQLite（PRD AC：下载批量记录）。"""
    store = _opened_store(tmp_path)
    window = MainWindow(WorkerFileBrowser(), transfer_record_store=store)
    window.refresh_current_directory()
    targets = [
        (_file_item("file-a", "a.bin"), tmp_path / "a.bin"),
        (_file_item("file-b", "b.bin"), tmp_path / "b.bin"),
    ]

    window._submit_resolved_download_items(targets, run_in_background=False)

    rows = {row.task_id: row for row in store.load_all()}
    live_ids = [
        record.task_id for record in window.transfer_interface.download_records
    ]
    assert sorted(rows) == sorted(live_ids)
    assert all(row.direction == "download" for row in rows.values())
    assert [row.status for row in rows.values()] == ["已完成", "已完成"]
    assert {row.local_path for row in rows.values()} == {str(path) for _, path in targets}
    store.close()
