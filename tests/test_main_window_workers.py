"""Background worker/thread, prompt dialog, and transfer-center UI tests."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtCore import QPoint, QThread
from PySide6.QtWidgets import QApplication, QDialog, QWidget

import openwopan.ui.main_window as main_window_module
from openwopan.app.file_browser import FileBrowserError, FileBrowserLoginRequiredError
from openwopan.storage.settings import AppSettings
from openwopan.tasks.download import DownloadTaskControl
from openwopan.ui.main_window import (
    DownloadWorker,
    NameInputDialog,
    PlaceholderInterface,
    TransferInterface,
    TransferRecord,
    UploadWorker,
    MainWindow,
)
from openwopan.wopan.client import ROOT_DIRECTORY_ID
from openwopan.wopan.models import WopanCloudUsage, WopanItem, WopanItemKind


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
        # emit_progress=False avoids the production mixed direct/queued signal
        # ordering between progress (queued bound slot) and terminal signals
        # (direct lambdas) when driven from a real QThread in tests.
        self.emit_progress = False
        self.requested_parent_ids: list[str] = []
        self.uploaded_files: list[tuple[str, Path]] = []
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

    def upload_file(self, parent_id: str, local_path: Path) -> WopanItem:
        self.uploaded_files.append((parent_id, local_path))
        uploaded = WopanItem(
            item_id="uploaded-file",
            name=local_path.name,
            kind=WopanItemKind.FILE,
            parent_id=parent_id,
            download_id="uploaded-fid",
            size=1,
        )
        self.items_by_parent[parent_id] = [*self.items_by_parent[parent_id], uploaded]
        return uploaded

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


class SyncQThread(QThread):
    """QThread stand-in that runs the worker synchronously on the main thread.

    The production code connects worker signals to plain lambdas, which Qt
    delivers in the emitting thread; with a real QThread that would render
    widgets off the GUI thread and crash offscreen tests. Running the same
    wiring synchronously keeps every connection line exercised deterministically.
    """

    def start(self, *args: object, **kwargs: object) -> None:
        self.started.emit()

    def quit(self) -> None:
        self.finished.emit()


@pytest.fixture
def sync_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_window_module, "QThread", SyncQThread)
    monkeypatch.setattr(
        main_window_module.DownloadWorker, "moveToThread", lambda self, thread: None
    )
    monkeypatch.setattr(
        main_window_module.UploadWorker, "moveToThread", lambda self, thread: None
    )


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
        worker.progress.connect(lambda done, total: self.events["progress"].append((done, total)))
        worker.status_changed.connect(lambda status: self.events["status_changed"].append((status,)))
        worker.connections_changed.connect(
            lambda active, maximum: self.events["connections_changed"].append((active, maximum))
        )
        worker.succeeded.connect(
            lambda name, path: self.events["succeeded"].append((name, path))
        )
        worker.stopped.connect(lambda status: self.events["stopped"].append((status,)))
        worker.failed.connect(lambda message: self.events["failed"].append((message,)))
        worker.login_required.connect(
            lambda message: self.events["login_required"].append((message,))
        )


class _UploadCollector:
    def __init__(self, worker: UploadWorker) -> None:
        self.events: dict[str, list[tuple]] = {
            "succeeded": [],
            "failed": [],
            "login_required": [],
        }
        worker.succeeded.connect(lambda item: self.events["succeeded"].append((item,)))
        worker.failed.connect(lambda message: self.events["failed"].append((message,)))
        worker.login_required.connect(
            lambda message: self.events["login_required"].append((message,))
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

    assert collector.events["progress"] == [(512, 1024)]
    assert collector.events["status_changed"] == [("下载中",)]
    assert collector.events["connections_changed"] == [(2, 4)]
    assert collector.events["succeeded"] == [
        ("report.txt", str(tmp_path / "report.txt"))
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
        ("report.txt", str(tmp_path / "report.txt"))
    ]


def test_download_worker_reraises_unrelated_type_error(
    qapp: QApplication, tmp_path: Path
) -> None:
    worker = _make_download_worker(UnrelatedTypeErrorFileBrowser(), tmp_path)
    collector = _SignalCollector(worker)

    with pytest.raises(TypeError):
        worker.run()

    assert collector.events["succeeded"] == []


@pytest.mark.parametrize(
    ("browser_factory", "expected_signal", "expected_payload"),
    [
        (lambda: _StoppedResultBrowser(), "stopped", ("已暂停",)),
        (
            lambda: _RaisingBrowser(FileBrowserError("network down")),
            "failed",
            ("network down",),
        ),
        (
            lambda: _RaisingBrowser(FileBrowserLoginRequiredError("登录已过期，请重新登录")),
            "login_required",
            ("登录已过期，请重新登录",),
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


def test_download_worker_defaults_to_completed_without_status_object(
    qapp: QApplication, tmp_path: Path
) -> None:
    worker = _make_download_worker(_NoStatusResultBrowser(), tmp_path)
    collector = _SignalCollector(worker)

    worker.run()

    assert collector.events["succeeded"] == [
        ("report.txt", str(tmp_path / "report.txt"))
    ]


def test_upload_worker_emits_success(qapp: QApplication, tmp_path: Path) -> None:
    browser = WorkerFileBrowser()
    local_path = tmp_path / "upload.txt"
    local_path.write_text("content")
    worker = UploadWorker(browser, ROOT_DIRECTORY_ID, local_path)  # type: ignore[arg-type]
    collector = _UploadCollector(worker)

    worker.run()

    assert len(collector.events["succeeded"]) == 1
    assert collector.events["succeeded"][0][0].name == "upload.txt"


@pytest.mark.parametrize(
    ("error", "expected_signal", "expected_payload"),
    [
        (FileBrowserError("upload failed"), "failed", ("upload failed",)),
        (
            FileBrowserLoginRequiredError("登录已过期，请重新登录"),
            "login_required",
            ("登录已过期，请重新登录",),
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
    worker = UploadWorker(_RaisingBrowser(error), ROOT_DIRECTORY_ID, tmp_path / "u.txt")  # type: ignore[arg-type]
    collector = _UploadCollector(worker)

    worker.run()

    assert collector.events[expected_signal] == [expected_payload]
    assert collector.events["succeeded"] == []


# ---------------------------------------------------------------------------
# Background download / upload task lifecycle
# ---------------------------------------------------------------------------


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
    browser.upload_file = lambda parent_id, local_path: (_ for _ in ()).throw(
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


def test_start_upload_task_reports_busy_state(qapp: QApplication) -> None:
    window = MainWindow(WorkerFileBrowser())
    window._upload_thread = QThread(window)

    window._start_upload_task(ROOT_DIRECTORY_ID, Path("/tmp/x.txt"), "upload-1")

    assert window.status_message() == "已有上传任务正在进行"
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


def test_remove_transfer_records_delegates_to_browser_and_ui(qapp: QApplication) -> None:
    browser = WorkerFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    _register_download_task(
        window, "download-1", target_path=Path("/tmp/report.txt"), item=_file_item()
    )

    window.transfer_interface.remove_records_requested.emit("download", {"download-1"})
    window.transfer_interface.remove_records_requested.emit("upload", ["not-a-set"])

    assert browser.removed_download_records == ["download-1"]
    assert window.transfer_interface.download_records == []


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
    assert window.status_message() == "正在下载：512 B / 1.0 KB"

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


def test_upload_success_handler_rejects_invalid_payload(qapp: QApplication) -> None:
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

    def exec(self) -> int:
        return type(self).accept_result

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

    def exec(self) -> int:
        return type(self).accept_result

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

    def exec(self) -> int:
        return type(self).accept_result

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

    def exec(self, *args: object, **kwargs: object) -> object:
        return None


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

    window.setting_interface.download_threads_spin_box.setValue(8)

    assert window.setting_interface.settings().max_download_threads == 8
    assert browser.update_settings_calls[-1].max_download_threads == 8


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


def test_transfer_delete_selected_only_removes_terminal_rows(qapp: QApplication) -> None:
    transfer = TransferInterface()
    removed: list[tuple[str, set[str]]] = []
    transfer.remove_records_requested.connect(
        lambda direction, ids: removed.append((direction, set(ids)))
    )
    transfer.add_download_record(_make_record("d-1", status="下载中"))
    transfer.add_download_record(_make_record("d-2", status="已完成"))

    transfer.download_table.selectAll()
    transfer._request_delete_selected("download")

    assert removed == [("download", {"d-2"})]

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

    def upload_file(self, parent_id: str, local_path: Path) -> WopanItem:
        if self.error is not None:
            raise self.error
        return super().upload_file(parent_id, local_path)


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
