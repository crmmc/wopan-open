from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from PySide6.QtCore import (
    QItemSelectionModel,
    QObject,
    QPoint,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QListWidget,
    QMainWindow,
    QMenu,
    QRadioButton,
    QSplitter,
    QStackedWidget,
    QTableWidgetItem,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    BreadcrumbBar,
    CardWidget,
    ComboBox,
    ExpandLayout,
    FluentIcon,
    FluentWindow,
    IconWidget,
    IndeterminateProgressBar,
    InfoBar,
    LineEdit,
    MessageBox,
    NavigationInterface,
    NavigationItemPosition,
    PrimaryPushButton,
    PrimaryPushSettingCard,
    ProgressBar,
    PushButton,
    PushSettingCard,
    RoundMenu,
    ScrollArea,
    SearchLineEdit,
    SegmentedWidget,
    SettingCard,
    SettingCardGroup,
    SpinBox,
    SplitPushButton,
    SwitchSettingCard,
    TableWidget,
    ToolButton,
    TreeWidget,
)

from openwopan import __version__
from openwopan.app.file_browser import (
    FileBrowserBackend,
    FileBrowserError,
    FileBrowserLoginRequiredError,
    FileBrowserUploadCancelledError,
    plan_transfer_batch,
)
from openwopan.app.logging_config import app_log_path, set_logging_level
from openwopan.auth.session import AuthSession
from openwopan.storage.settings import AppSettings, app_settings_path, save_app_settings
from openwopan.storage.transfer_records import TransferRecordStore
from openwopan.tasks.download import DownloadTaskControl, DownloadTaskRecord
from openwopan.tasks.scheduler import DownloadTaskEvent
from openwopan.tasks.transfer_rate import TransferRateEstimator
from openwopan.tasks.upload import (
    FolderUploadJob,
    UploadBatchSummary,
    UploadConflictResolution,
    UploadTaskRecord,
    find_upload_conflicts,
    format_upload_summary,
    resolve_upload_targets,
    scan_upload_inputs,
)
from openwopan.ui.formatting import format_bytes as _format_bytes
from openwopan.ui.formatting import format_items_summary as _format_items_summary
from openwopan.ui.formatting import format_kind as _format_kind
from openwopan.ui.formatting import format_optional_bytes as _format_optional_bytes
from openwopan.ui.formatting import format_size as _format_size
from openwopan.ui.recycle_interface import RecycleInterface
from openwopan.ui.search_window import SearchResultsWindow
from openwopan.ui.target_folder_dialog import (
    TargetEntry,
    TargetFolderDialog,
    TransferConflictDialog,
    TransferMode,
    mode_label,
)
from openwopan.wopan.client import ROOT_DIRECTORY_ID
from openwopan.wopan.models import WopanCloudUsage, WopanItem, WopanItemKind, WopanRecycleItem

MAIN_WINDOW_DEFAULT_SIZE = (900, 600)
MAIN_WINDOW_MINIMUM_SIZE = (800, 600)
FILE_SPLITTER_STRETCH_FACTORS = (1, 6)
ROOT_DISPLAY_NAME = "/"
TRANSFER_TABLE_HEADERS = ("名称", "大小", "进度", "速度", "状态", "操作")
TRANSFER_COL_NAME = 0
TRANSFER_COL_SIZE = 1
TRANSFER_COL_PROGRESS = 2
TRANSFER_COL_SPEED = 3
TRANSFER_COL_STATUS = 4
TRANSFER_COL_ACTION = 5
TRANSFER_ACTION_COLUMN_WIDTH = 156
TRANSFER_ACTION_BUTTON_SIZE = (32, 24)
THREAD_JOIN_TIMEOUT_MS = 3000
UPLOAD_STATUS_FILTERS = (
    "全部",
    "等待中",
    "上传中",
    "创建目录中",
    "已暂停",
    "已完成",
    "失败",
    "已取消",
)
DOWNLOAD_STATUS_FILTERS = (
    "全部",
    "等待中",
    "校验中",
    "下载中",
    "合并中",
    "已暂停",
    "已完成",
    "失败",
    "已取消",
)
TERMINAL_TRANSFER_STATUSES = frozenset({"已完成", "失败", "已取消"})
ACTIVE_DOWNLOAD_STATUSES = frozenset({"等待中", "校验中", "下载中", "合并中"})
ACTIVE_UPLOAD_STATUSES = frozenset({"等待中", "上传中"})
DownloadConflictResolution = Literal["skip", "copy"]
FRAME_STYLE = (
    "QFrame#frame, QFrame#listFrame {"
    "border: 1px solid rgba(0, 0, 0, 15);"
    "border-radius: 5px;"
    "background: transparent;"
    "}"
)
LOGGER = logging.getLogger(__name__)
# Keep abandoned (unjoinable) worker threads alive: dropping the last Python
# reference would delete a still-running QThread and abort the process.
_THREAD_KEEP_ALIVE: set[QThread] = set()
# Finished handlers release worker wrappers on the GUI thread; avoid direct
# finished -> worker.deleteLater, which can destroy PySide wrappers on macOS.
FIF = FluentIcon


def _is_offscreen_platform() -> bool:
    return os.environ.get("QT_QPA_PLATFORM") == "offscreen"


if TYPE_CHECKING:

    class _MainWindowBase(QMainWindow):
        """Type-checking base; runtime may use FluentWindow."""

elif _is_offscreen_platform():

    class _MainWindowBase(QMainWindow):
        """QMainWindow fallback avoids qframelesswindow offscreen crashes."""

else:  # pragma: no cover - docs/testing-exemptions.md

    class _MainWindowBase(FluentWindow):
        """Runtime base matching the sibling 123pan-open shell."""


@dataclass(frozen=True, slots=True)
class BreadcrumbEntry:
    """OpenWoPan-owned breadcrumb state."""

    item_id: str
    name: str


class NameInputDialog(QDialog):
    """Fluent-style name input dialog for create-folder and rename flows."""

    def __init__(
        self,
        *,
        title: str,
        hint: str,
        default_text: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(400, 180)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(20)

        title_label = BodyLabel(title, self)
        title_label.setObjectName("dialogTitle")
        layout.addWidget(title_label, alignment=Qt.AlignmentFlag.AlignCenter)

        hint_label = BodyLabel(hint, self)
        layout.addWidget(hint_label, alignment=Qt.AlignmentFlag.AlignCenter)

        self._name_input = LineEdit(self)
        self._name_input.setText(default_text)
        self._name_input.selectAll()
        self._name_input.returnPressed.connect(self._accept_if_valid)
        layout.addWidget(self._name_input)

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        cancel_button = PushButton("取消", self)
        cancel_button.setMinimumWidth(96)
        cancel_button.clicked.connect(self.reject)
        ok_button = PrimaryPushButton("确定", self)
        ok_button.setMinimumWidth(96)
        ok_button.clicked.connect(self._accept_if_valid)
        button_layout.addWidget(cancel_button)
        button_layout.addWidget(ok_button)
        layout.addLayout(button_layout)

    def name_text(self) -> str:
        """Return the normalized input text."""
        return self._name_input.text().strip()

    def _accept_if_valid(self) -> None:
        if self.name_text():
            self.accept()


class BrowserOperationWorker(QObject):
    """Run one blocking file-browser operation in a worker thread."""

    succeeded = Signal(object)
    failed = Signal(str)
    login_required = Signal(str)

    def __init__(self, operation: Callable[[], object]) -> None:
        super().__init__()
        self._operation = operation

    def run(self) -> None:
        """Run the operation and emit exactly one terminal signal."""
        try:
            result = self._operation()
        except FileBrowserLoginRequiredError as exc:
            self.login_required.emit(str(exc))
        except FileBrowserError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            LOGGER.exception("main_window.operation.unexpected_error")
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(result)


class DownloadWorker(QObject):
    """Background worker for one ordinary file download."""

    progress = Signal(object, object, str)
    status_changed = Signal(str, str)
    connections_changed = Signal(int, int, str)
    succeeded = Signal(str, str, str)
    stopped = Signal(str, str)
    failed = Signal(str, str)
    login_required = Signal(str, str)

    def __init__(
        self,
        file_browser: FileBrowserBackend,
        item: WopanItem,
        local_path: Path,
        task_id: str,
        control: DownloadTaskControl,
    ) -> None:
        super().__init__()
        self._file_browser = file_browser
        self._item = item
        self._local_path = local_path
        self._task_id = task_id
        self._control = control

    def run(self) -> None:
        """Run the blocking download in a worker thread."""
        try:
            try:
                result = self._file_browser.download_file(
                    self._item,
                    self._local_path,
                    lambda bytes_done, total_bytes: self.progress.emit(
                        bytes_done, total_bytes, self._task_id
                    ),
                    status_callback=lambda status: self.status_changed.emit(status, self._task_id),
                    connection_callback=lambda active, maximum: self.connections_changed.emit(
                        active, maximum, self._task_id
                    ),
                    control=self._control,
                    task_id=self._task_id,
                )
            except TypeError as exc:
                if "unexpected keyword argument" not in str(exc):
                    raise
                result = self._file_browser.download_file(
                    self._item,
                    self._local_path,
                    lambda bytes_done, total_bytes: self.progress.emit(
                        bytes_done, total_bytes, self._task_id
                    ),
                )
        except FileBrowserLoginRequiredError as exc:
            self.login_required.emit(str(exc), self._task_id)
        except FileBrowserError as exc:
            self.failed.emit(str(exc), self._task_id)
        except Exception as exc:
            LOGGER.exception("main_window.download.unexpected_error")
            self.failed.emit(str(exc), self._task_id)
        else:
            status = getattr(result, "status", "已完成")
            if status == "已完成":
                self.succeeded.emit(self._item.name, str(self._local_path), self._task_id)
                return
            self.stopped.emit(str(status), self._task_id)


class UploadScanWorker(QObject):
    """Scan dropped local paths without touching the GUI thread."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, paths: tuple[Path, ...]) -> None:
        super().__init__()
        self._paths = paths

    def run(self) -> None:
        try:
            self.succeeded.emit(scan_upload_inputs(self._paths))
        except Exception as exc:
            sanitized_error = RuntimeError(f"upload scan failed: {type(exc).__name__}")
            LOGGER.exception(
                "main_window.upload_scan.unexpected_error error_type=%s",
                type(exc).__name__,
                exc_info=(type(sanitized_error), sanitized_error, exc.__traceback__),
            )
            self.failed.emit(str(exc))


class UploadSummaryDialog(QDialog):
    """Confirm a bounded local upload summary and any cloud-name conflicts."""

    def __init__(
        self,
        summary: UploadBatchSummary,
        target_name: str,
        parent: QWidget,
        *,
        conflicts: tuple[Path, ...] = (),
    ) -> None:
        super().__init__(parent)
        self._skip_conflicts_button: QRadioButton | None = None
        self.setWindowTitle("确认上传")
        self.resize(640, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(BodyLabel(format_upload_summary(summary), self))
        layout.addWidget(BodyLabel(f"目标云端目录：{target_name}", self))
        if summary.folder_count > 0 and summary.file_count == 0:
            layout.addWidget(BodyLabel("所选文件夹为空，不会创建文件上传任务。", self))
        preview = QListWidget(self)
        conflict_paths = set(conflicts)
        for entry in summary.preview:
            marker = "（重复）" if entry.local_path in conflict_paths else ""
            preview.addItem(
                f"{marker}{entry.display_name}  ({entry.kind}, {_format_bytes(entry.size)})"
            )
        if summary.omitted_count:
            preview.addItem(f"另有 {summary.omitted_count} 项")
        layout.addWidget(preview, 1)

        if conflicts:
            layout.addWidget(BodyLabel(f"发现 {len(conflicts)} 个同名项目", self))
            skip_button = QRadioButton("跳过冲突", self)
            self._skip_conflicts_button = skip_button
            copy_button = QRadioButton("保留副本", self)
            skip_button.setChecked(True)
            layout.addWidget(skip_button)
            layout.addWidget(copy_button)
            decision_label = BodyLabel("", self)
            layout.addWidget(decision_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if conflicts:
            confirm_button = buttons.button(QDialogButtonBox.StandardButton.Ok)

            def update_choice(skip: bool) -> None:
                count = len(summary.top_paths) - len(conflicts) if skip else len(summary.top_paths)
                decision_label.setText(f"将添加 {count} 个上传任务")
                confirm_button.setEnabled(count > 0)

            skip_button.toggled.connect(update_choice)
            update_choice(True)

    def resolution(self) -> UploadConflictResolution:
        if self._skip_conflicts_button is not None and self._skip_conflicts_button.isChecked():
            return "skip"
        return "copy"


class UploadConflictDialog(QDialog):
    """Choose how to handle top-level upload name conflicts."""

    def __init__(self, conflicts: tuple[Path, ...], parent: QWidget) -> None:
        super().__init__(parent)
        self._resolution: UploadConflictResolution | None = None
        self.setWindowTitle("处理同名上传项目")
        layout = QVBoxLayout(self)
        layout.addWidget(
            BodyLabel(
                f"发现 {len(conflicts)} 个同名项目，请选择冲突处理方式。",
                self,
            )
        )
        preview = QListWidget(self)
        for path in conflicts[:20]:
            preview.addItem(path.name)
        if len(conflicts) > 20:
            preview.addItem(f"另有 {len(conflicts) - 20} 项")
        layout.addWidget(preview)

        buttons = QDialogButtonBox(self)
        skip_button = buttons.addButton("跳过冲突", QDialogButtonBox.ButtonRole.DestructiveRole)
        copy_button = buttons.addButton("保留副本", QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_button = buttons.addButton("取消本批次", QDialogButtonBox.ButtonRole.RejectRole)
        skip_button.clicked.connect(lambda: self._finish("skip"))
        copy_button.clicked.connect(lambda: self._finish("copy"))
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def resolution(self) -> UploadConflictResolution | None:
        """Return the selected resolution, or None when the batch was cancelled."""
        return self._resolution

    def _finish(self, resolution: UploadConflictResolution) -> None:
        self._resolution = resolution
        self.accept()


class DownloadConflictDialog(QDialog):
    """Choose how to handle existing local download targets."""

    def __init__(self, conflicts: tuple[Path, ...], parent: QWidget) -> None:
        super().__init__(parent)
        self._resolution: DownloadConflictResolution | None = None
        self.setWindowTitle("处理下载文件冲突")
        layout = QVBoxLayout(self)
        layout.addWidget(
            BodyLabel(
                f"发现 {len(conflicts)} 个本地目标冲突，请选择处理方式。",
                self,
            )
        )
        preview = QListWidget(self)
        for conflict in conflicts[:20]:
            preview.addItem(conflict.name)
        if len(conflicts) > 20:
            preview.addItem(f"另有 {len(conflicts) - 20} 项")
        layout.addWidget(preview)

        buttons = QDialogButtonBox(self)
        skip_button = buttons.addButton("跳过冲突", QDialogButtonBox.ButtonRole.DestructiveRole)
        copy_button = buttons.addButton("保留副本", QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_button = buttons.addButton("取消本批次", QDialogButtonBox.ButtonRole.RejectRole)
        skip_button.clicked.connect(lambda: self._finish("skip"))
        copy_button.clicked.connect(lambda: self._finish("copy"))
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def resolution(self) -> DownloadConflictResolution | None:
        """Return the selected resolution, or None when cancelled."""
        return self._resolution

    def _finish(self, resolution: DownloadConflictResolution) -> None:
        self._resolution = resolution
        self.accept()


class DroppableTableWidget(TableWidget):
    """Accept local file URLs and pass paths to the owning file page."""

    paths_dropped = Signal(object)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: object) -> None:
        mime_data = event.mimeData()  # type: ignore[attr-defined]
        urls = mime_data.urls()
        if urls and all(url.isLocalFile() for url in urls):
            event.acceptProposedAction()  # type: ignore[attr-defined]
        else:
            event.ignore()  # type: ignore[attr-defined]

    def dragMoveEvent(self, event: object) -> None:
        mime_data = event.mimeData()  # type: ignore[attr-defined]
        urls = mime_data.urls()
        if urls and all(url.isLocalFile() for url in urls):
            event.acceptProposedAction()  # type: ignore[attr-defined]
        else:
            event.ignore()  # type: ignore[attr-defined]

    def dropEvent(self, event: object) -> None:
        urls = event.mimeData().urls()  # type: ignore[attr-defined]
        if urls and all(url.isLocalFile() for url in urls):
            LOGGER.info("main_window.upload_drop.received count=%s", len(urls))
            self.paths_dropped.emit(tuple(Path(url.toLocalFile()) for url in urls))
            event.acceptProposedAction()  # type: ignore[attr-defined]
            LOGGER.info("main_window.upload_drop.dispatched count=%s", len(urls))
        else:
            event.ignore()  # type: ignore[attr-defined]


class UploadWorker(QObject):
    """Background worker for one ordinary file upload."""

    progress = Signal(object, object, str)
    succeeded = Signal(object, str)
    failed = Signal(str, str)
    login_required = Signal(str, str)
    cancelled = Signal(str)

    def __init__(
        self,
        file_browser: FileBrowserBackend,
        parent_id: str,
        local_path: Path,
        task_id: str,
        upload_name: str | None = None,
    ) -> None:
        super().__init__()
        self._file_browser = file_browser
        self._parent_id = parent_id
        self._local_path = local_path
        self._task_id = task_id
        self._upload_name = upload_name
        self._cancel_requested = threading.Event()
        self._pause_requested = threading.Event()
        self._resume_requested = threading.Event()
        self._resume_requested.set()

    def request_pause(self) -> None:
        """Pause after the current upload request reaches a safe check."""
        self._pause_requested.set()
        self._resume_requested.clear()

    def request_resume(self) -> None:
        """Allow a paused upload to continue."""
        self._pause_requested.clear()
        self._resume_requested.set()

    def request_cancel(self) -> None:
        self._cancel_requested.set()
        self._resume_requested.set()

    def _upload_stop_requested(self) -> bool:
        while self._pause_requested.is_set() and not self._cancel_requested.is_set():
            self._resume_requested.wait()
        return self._cancel_requested.is_set()

    def _upload_progress_callback(self, bytes_done: int, total_bytes: int) -> None:
        self._upload_stop_requested()
        self.progress.emit(bytes_done, total_bytes, self._task_id)

    def run(self) -> None:
        """Run the blocking upload in a worker thread."""
        try:
            if self._upload_stop_requested():
                raise FileBrowserUploadCancelledError("上传已取消")

            def progress_callback(bytes_done: int, total_bytes: int) -> None:
                self._upload_progress_callback(bytes_done, total_bytes)

            try:
                try:
                    if self._upload_name is None:
                        item = self._file_browser.upload_file(
                            self._parent_id,
                            self._local_path,
                            progress_callback=progress_callback,
                            cancel_requested=self._upload_stop_requested,
                        )
                    else:
                        item = self._file_browser.upload_file(
                            self._parent_id,
                            self._local_path,
                            upload_name=self._upload_name,
                            progress_callback=progress_callback,
                            cancel_requested=self._upload_stop_requested,
                        )
                except TypeError as exc:
                    if "unexpected keyword argument 'cancel_requested'" not in str(exc):
                        raise
                    if self._upload_name is None:
                        item = self._file_browser.upload_file(
                            self._parent_id, self._local_path, progress_callback=progress_callback
                        )
                    else:
                        item = self._file_browser.upload_file(
                            self._parent_id,
                            self._local_path,
                            upload_name=self._upload_name,
                            progress_callback=progress_callback,
                        )
            except TypeError as exc:
                if "unexpected keyword argument 'progress_callback'" not in str(exc):
                    raise
                if self._upload_name is None:
                    item = self._file_browser.upload_file(self._parent_id, self._local_path)
                else:
                    item = self._file_browser.upload_file(
                        self._parent_id, self._local_path, upload_name=self._upload_name
                    )
            if self._upload_stop_requested():
                raise FileBrowserUploadCancelledError("上传已取消")
        except FileBrowserUploadCancelledError:
            self.cancelled.emit(self._task_id)
        except FileBrowserLoginRequiredError as exc:
            self.login_required.emit(str(exc), self._task_id)
        except FileBrowserError as exc:
            if self._upload_stop_requested():
                self.cancelled.emit(self._task_id)
            else:
                self.failed.emit(str(exc), self._task_id)
        except Exception as exc:
            LOGGER.exception("main_window.upload.unexpected_error")
            self.failed.emit(str(exc), self._task_id)
        else:
            self.succeeded.emit(item, self._task_id)


class PlaceholderInterface(QWidget):
    """Navigation placeholder for stages that are visible but not implemented yet."""

    def __init__(self, title: str, message: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName(title.replace(" ", ""))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(12)
        title_label = BodyLabel(title, self)
        title_label.setObjectName("pageTitle")
        message_label = BodyLabel(message, self)
        message_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(message_label)
        layout.addStretch(1)


@dataclass(slots=True)
class PendingUploadTask:
    """One upload waiting for an ordinary upload worker slot."""

    parent_id: str
    local_path: Path
    task_id: str
    upload_name: str | None
    show_enqueue_status: bool


@dataclass(slots=True)
class TransferRecord:
    """UI-owned in-memory transfer task row."""

    task_id: str
    direction: str
    name: str
    size: int | None
    target_path: Path | None = None
    status: str = "等待中"
    bytes_done: int = 0
    total_bytes: int | None = None
    speed_bps: float = 0.0
    active_connections: int = 0
    max_connections: int = 1
    can_resume: bool = False
    error: str = ""
    upload_parent_id: str | None = None
    upload_name: str | None = None
    upload_retryable: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    # Wall-clock epochs persisted by the history store; the monotonic fields
    # above stay process-local and keep serving the speed sampler.
    created_at_epoch: float = 0.0
    updated_at_epoch: float = 0.0

    def __post_init__(self) -> None:
        now = time.monotonic()
        if self.created_at <= 0:
            self.created_at = now
        if self.updated_at <= 0:
            self.updated_at = self.created_at
        wall_now = time.time()
        if self.created_at_epoch <= 0:
            self.created_at_epoch = wall_now
        if self.updated_at_epoch <= 0:
            self.updated_at_epoch = self.created_at_epoch

    @property
    def progress_percent(self) -> int:
        if self.status == "已完成":
            return 100
        total = self.total_bytes or self.size
        if not total or total <= 0:
            return 0
        return max(0, min(100, int(self.bytes_done * 100 / total)))


class TransferRecordPersistence(Protocol):
    """Persistence contract for transfer records (UI-side, Qt-free boundary).

    Implementations live in the app layer and must never raise: persistence is
    best-effort, and a failed write degrades to the in-memory row.
    """

    def save_record(self, record: TransferRecord) -> None:
        """Upsert one full record row."""

    def update_record_progress(
        self,
        direction: str,
        task_id: str,
        *,
        bytes_done: int,
        active_connections: int,
        updated_at: float,
    ) -> None:
        """Persist progress-only fields of one row (no-op when the row is gone)."""

    def delete_records(self, direction: str, task_ids: Iterable[str]) -> None:
        """Delete rows by (direction, task id)."""

    def load_history(self) -> tuple[TransferRecord, ...]:
        """Load all persisted rows ordered by update time ascending."""


@dataclass(slots=True)
class QueuedUploadFile:
    """One folder-upload file waiting for the single upload slot."""

    task_id: str
    local_path: Path
    target_dir_id: str
    upload_name: str


@dataclass(frozen=True, slots=True)
class PendingFolderUpload:
    """One folder upload waiting for its preparation slot."""

    local_path: Path
    parent_id: str
    root_name: str | None
    record_id: str


class TransferInterface(QWidget):
    """Transfer center aligned with the sibling Fluent client."""

    PROGRESS_RENDER_INTERVAL_MS = 150
    SPEED_SAMPLE_INTERVAL_MS = 1000
    PROGRESS_PERSIST_INTERVAL_MS = 2000
    # Statuses during which bytes are expected to flow; only these are sampled.
    # Waiting/creating-dir/verifying/merging/paused/terminal tasks show no
    # active speed (R5).
    SAMPLING_STATUSES = frozenset({"上传中", "下载中"})

    remove_records_requested = Signal(str, object)
    open_download_folder_requested = Signal(object)
    pause_download_requested = Signal(str)
    resume_download_requested = Signal(str)
    cancel_download_requested = Signal(str)
    pause_uploads_requested = Signal(object)
    resume_uploads_requested = Signal(object)
    pause_downloads_requested = Signal(object)
    resume_downloads_requested = Signal(object)
    retry_upload_requested = Signal(object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        persistence: TransferRecordPersistence | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("TransferInterface")
        self.upload_records: list[TransferRecord] = []
        self.download_records: list[TransferRecord] = []
        self.upload_status_filter = "全部"
        self.download_status_filter = "全部"
        self._active_direction = "download"
        self._pending_progress_directions: set[str] = set()
        self._progress_render_scheduled = False
        self._speed_estimators: dict[tuple[str, str], TransferRateEstimator] = {}
        # Injectable for tests; must return a fresh TransferRateEstimator.
        self._new_speed_estimator: Callable[[], TransferRateEstimator] = TransferRateEstimator
        self._speed_sampler = QTimer(self)
        self._speed_sampler.setInterval(self.SPEED_SAMPLE_INTERVAL_MS)
        self._speed_sampler.timeout.connect(self._sample_speeds)
        # History persistence: progress-only writes coalesce behind a 2 s tick;
        # status/error/terminal/add/remove writes stay immediate (design.md 3).
        self._persistence = persistence
        self._persist_dirty: set[tuple[str, str]] = set()
        self._persist_timer = QTimer(self)
        self._persist_timer.setInterval(self.PROGRESS_PERSIST_INTERVAL_MS)
        self._persist_timer.timeout.connect(self._flush_pending_record_persist)
        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(24, 20, 24, 24)
        self._main_layout.setSpacing(12)

        self._build_top_bar()
        self._build_content()
        self._connect_signals()
        self._render_all()
        self._on_segment_changed("download")

    def add_upload_record(
        self, record: TransferRecord, *, render: bool = True, persist: bool = True
    ) -> None:
        """Add or replace an upload task row."""
        self._on_record_added("upload", record)
        self._upsert_record(self.upload_records, record)
        if persist:
            self._save_record(record)
        if render:
            self.flush_progress_render()
            self._render_upload_table()

    def add_download_record(
        self, record: TransferRecord, *, render: bool = True, persist: bool = True
    ) -> None:
        """Add or replace a download task row."""
        self._on_record_added("download", record)
        self._upsert_record(self.download_records, record)
        if persist:
            self._save_record(record)
        if render:
            self.flush_progress_render()
            self._render_download_table()

    def _on_record_added(self, direction: str, record: TransferRecord) -> None:
        """Reset speed state for a (re-)added record; recovered tasks re-baseline."""
        self._speed_estimators.pop((direction, record.task_id), None)
        if record.status in self.SAMPLING_STATUSES:
            self._ensure_speed_sampler()

    def update_record(
        self,
        direction: str,
        task_id: str,
        *,
        status: str | None = None,
        bytes_done: int | None = None,
        total_bytes: int | None = None,
        active_connections: int | None = None,
        max_connections: int | None = None,
        can_resume: bool | None = None,
        error: str | None = None,
    ) -> None:
        """Update a visible transfer record."""
        record = self._find_record(direction, task_id)
        if record is None:
            return
        previous_status = record.status
        now = time.monotonic()
        progress_only = (
            status is None
            and total_bytes is None
            and max_connections is None
            and can_resume is None
            and error is None
        )
        if status is not None:
            record.status = status
        if total_bytes is not None:
            record.total_bytes = total_bytes
            record.size = total_bytes
        if active_connections is not None:
            record.active_connections = max(0, active_connections)
        if max_connections is not None:
            record.max_connections = max(1, max_connections)
        if can_resume is not None:
            record.can_resume = can_resume
        if bytes_done is not None:
            # Progress callbacks only carry cumulative bytes; the speed comes
            # from the 1-second sampler (R1/R2), never from callback intervals.
            record.bytes_done = max(0, bytes_done)
        if error is not None:
            record.error = error
        if record.status in TERMINAL_TRANSFER_STATUSES:
            # Terminal: zero the speed and release estimator state (R5).
            self._speed_estimators.pop((direction, task_id), None)
            record.speed_bps = 0.0
            record.active_connections = 0
        elif status is not None and status != previous_status:
            # Status transition (pause/resume/waiting/...): re-baseline so the
            # paused or transitional span is never counted into a new rate.
            estimator = self._speed_estimators.get((direction, task_id))
            if estimator is not None:
                estimator.reset()
            record.speed_bps = 0.0
            if record.status in self.SAMPLING_STATUSES:
                self._ensure_speed_sampler()
        record.updated_at = now
        record.updated_at_epoch = time.time()
        if record.status in TERMINAL_TRANSFER_STATUSES:
            self._discard_pending_progress(direction)
            self._save_record(record)
            self._render_direction(direction)
        elif progress_only:
            self._mark_record_progress_dirty(direction, task_id)
            self._schedule_progress_render(direction)
        else:
            self._discard_pending_progress(direction)
            self._save_record(record)
            self._render_direction(direction)

    def _save_record(self, record: TransferRecord) -> None:
        """Write one full record row through the persistence boundary (best-effort)."""
        self._persist_dirty.discard((record.direction, record.task_id))
        if self._persistence is not None:
            self._persistence.save_record(record)

    def _mark_record_progress_dirty(self, direction: str, task_id: str) -> None:
        """Queue one progress-only write behind the 2-second persistence tick."""
        if self._persistence is None:
            return
        self._persist_dirty.add((direction, task_id))
        if not self._persist_timer.isActive():
            self._persist_timer.start()

    def _flush_pending_record_persist(self) -> None:
        """Write every pending progress-only row through the persistence boundary."""
        if not self._persist_dirty:
            self._persist_timer.stop()
            return
        pending = tuple(self._persist_dirty)
        self._persist_dirty.clear()
        for direction, task_id in pending:
            record = self._find_record(direction, task_id)
            # A removed record's row is already deleted from the database by
            # remove_records; a late flush must not resurrect it.
            if record is None or self._persistence is None:
                continue
            self._persistence.update_record_progress(
                direction,
                task_id,
                bytes_done=record.bytes_done,
                active_connections=record.active_connections,
                updated_at=record.updated_at_epoch,
            )
        if not self._persist_dirty:
            self._persist_timer.stop()

    def stop_record_persistence(self) -> None:
        """Flush pending progress writes and stop the tick; called on window close."""
        self._persist_timer.stop()
        self._flush_pending_record_persist()

    def _discard_pending_progress(self, direction: str) -> None:
        """Drop one direction's pending coalesced render; the caller renders it now."""
        self._pending_progress_directions.discard(direction)

    def flush_progress_render(self) -> None:
        """Render pending progress updates immediately; intended for tests and terminal updates."""
        if not self._pending_progress_directions:
            self._progress_render_scheduled = False
            return
        pending = tuple(self._pending_progress_directions)
        self._pending_progress_directions.clear()
        self._progress_render_scheduled = False
        for direction in pending:
            self._render_direction(direction)

    def _schedule_progress_render(self, direction: str) -> None:
        self._pending_progress_directions.add(direction)
        if self._progress_render_scheduled:
            return
        self._progress_render_scheduled = True
        QTimer.singleShot(self.PROGRESS_RENDER_INTERVAL_MS, self.flush_progress_render)

    def _render_direction(self, direction: str) -> None:
        if direction == "upload":
            self._render_upload_table()
        else:
            self._render_download_table()

    def _ensure_speed_sampler(self) -> None:
        """Start the 1-second speed tick; runs only while active tasks exist."""
        if not self._speed_sampler.isActive():
            self._speed_sampler.start()

    def stop_speed_sampler(self) -> None:
        """Stop the speed tick; called on window close so no timer outlives it."""
        self._speed_sampler.stop()

    def _sample_speeds(self) -> None:
        """Sampler tick (GUI thread): advance each active task's estimator.

        Updates only the speed cells and the direction total labels; it never
        rebuilds the table (R6). Stops itself when no task is transferring.
        """
        has_active = False
        for direction in ("upload", "download"):
            records = self.upload_records if direction == "upload" else self.download_records
            table = self.upload_table if direction == "upload" else self.download_table
            visible = (
                self._filtered_upload_records()
                if direction == "upload"
                else self._filtered_download_records()
            )
            for record in records:
                if record.status not in self.SAMPLING_STATUSES:
                    continue
                has_active = True
                key = (direction, record.task_id)
                estimator = self._speed_estimators.get(key)
                if estimator is None:
                    estimator = self._new_speed_estimator()
                    self._speed_estimators[key] = estimator
                record.speed_bps = max(0.0, estimator.sample(record.bytes_done))
            for row, record in enumerate(visible):
                if record.status not in self.SAMPLING_STATUSES:
                    continue
                speed_item = table.item(row, TRANSFER_COL_SPEED)
                if (
                    speed_item is not None
                    and speed_item.data(Qt.ItemDataRole.UserRole) == record.task_id
                ):
                    speed_item.setText(_format_speed(record.speed_bps))
            self._update_total_speed(direction)
        if not has_active:
            self._speed_sampler.stop()

    def remove_records(self, direction: str, task_ids: set[str]) -> None:
        """Remove task rows by id."""
        if direction == "upload":
            self.upload_records = [
                record for record in self.upload_records if record.task_id not in task_ids
            ]
            self.flush_progress_render()
            self._render_upload_table()
        else:
            self.download_records = [
                record for record in self.download_records if record.task_id not in task_ids
            ]
            self.flush_progress_render()
            self._render_download_table()
        for task_id in task_ids:
            self._speed_estimators.pop((direction, task_id), None)
            self._persist_dirty.discard((direction, task_id))
        if self._persistence is not None:
            self._persistence.delete_records(direction, task_ids)

    def active_download_folder(self) -> Path | None:
        """Return selected download folder or the latest download folder."""
        visible = self._filtered_download_records()
        row = self.download_table.currentRow()
        if 0 <= row < len(visible) and visible[row].target_path is not None:
            return visible[row].target_path.parent
        for record in reversed(self.download_records):
            if record.target_path is not None:
                return record.target_path.parent
        return None

    def _build_top_bar(self) -> None:
        top_bar = QFrame(self)
        top_bar.setObjectName("frame")
        top_bar.setStyleSheet(FRAME_STYLE)
        self.top_bar_frame = top_bar
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(12, 10, 12, 10)
        top_layout.setSpacing(8)

        self.title_label = BodyLabel("传输管理", top_bar)
        self.segmented_widget = SegmentedWidget(top_bar)
        self.segmented_widget.addItem("upload", "上传", icon=FIF.UP.icon())
        self.segmented_widget.addItem("download", "下载", icon=FIF.DOWNLOAD.icon())
        self.segmented_widget.setCurrentItem("download")

        self.upload_filter_label = BodyLabel("状态", top_bar)
        self.upload_filter_combo = ComboBox(top_bar)
        self.upload_filter_combo.addItems(list(UPLOAD_STATUS_FILTERS))
        self.upload_filter_combo.setCurrentText(self.upload_status_filter)
        self.upload_filter_combo.setMinimumWidth(120)

        self.download_filter_label = BodyLabel("状态", top_bar)
        self.download_filter_combo = ComboBox(top_bar)
        self.download_filter_combo.addItems(list(DOWNLOAD_STATUS_FILTERS))
        self.download_filter_combo.setCurrentText(self.download_status_filter)
        self.download_filter_combo.setMinimumWidth(120)
        self.open_download_folder_button = PushButton(
            FIF.FOLDER.icon(),
            "打开下载文件夹",
            top_bar,
        )

        top_layout.addWidget(self.title_label)
        top_layout.addWidget(self.segmented_widget)
        top_layout.addStretch(1)
        top_layout.addWidget(self.upload_filter_label)
        top_layout.addWidget(self.upload_filter_combo)
        top_layout.addWidget(self.download_filter_label)
        top_layout.addWidget(self.download_filter_combo)
        top_layout.addWidget(self.open_download_folder_button)
        self._main_layout.addWidget(top_bar)

    def _build_content(self) -> None:
        self.upload_frame = self._build_table_frame("upload")
        self.download_frame = self._build_table_frame("download")
        self._main_layout.addWidget(self.upload_frame, 1)
        self._main_layout.addWidget(self.download_frame, 1)

    def _build_table_frame(self, direction: str) -> QFrame:
        frame = QFrame(self)
        frame.setObjectName("frame")
        frame.setStyleSheet(FRAME_STYLE)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(0)

        batch_bar, buttons = self._build_batch_toolbar(frame)
        table = TableWidget(frame)
        table.setColumnCount(len(TRANSFER_TABLE_HEADERS))
        table.setHorizontalHeaderLabels(list(TRANSFER_TABLE_HEADERS))
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setBorderRadius(8)
        table.setBorderVisible(True)
        vertical_header = table.verticalHeader()
        if vertical_header is not None:  # pragma: no cover - docs/testing-exemptions.md
            vertical_header.hide()
        header = table.horizontalHeader()
        if header is not None:  # pragma: no cover - docs/testing-exemptions.md
            header.setSectionResizeMode(TRANSFER_COL_NAME, QHeaderView.ResizeMode.Stretch)
            for column in range(1, len(TRANSFER_TABLE_HEADERS)):
                if column == TRANSFER_COL_ACTION:
                    header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
                    header.resizeSection(column, TRANSFER_ACTION_COLUMN_WIDTH)
                else:
                    header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

        layout.addWidget(batch_bar)
        layout.addWidget(table)
        buttons["retry"].setVisible(direction == "upload")
        if direction == "upload":
            self.upload_batch_bar = batch_bar
            self.upload_batch_buttons = buttons
            self.upload_table = table
        else:
            self.download_batch_bar = batch_bar
            self.download_batch_buttons = buttons
            self.download_table = table
        return frame

    def _build_batch_toolbar(self, parent: QWidget) -> tuple[QFrame, dict[str, Any]]:
        frame = QFrame(parent)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(6)
        select_all_button = PushButton(FIF.CHECKBOX.icon(), "全选", frame)
        invert_button = PushButton(FIF.SYNC.icon(), "反选", frame)
        pause_button = PushButton(FIF.PAUSE.icon(), "暂停", frame)
        resume_button = PushButton(FIF.PLAY.icon(), "继续", frame)
        retry_button = PushButton(FIF.SYNC.icon(), "重试", frame)
        delete_button = PushButton(FIF.DELETE.icon(), "删除", frame)
        for button in (
            select_all_button,
            invert_button,
            pause_button,
            resume_button,
            retry_button,
            delete_button,
        ):
            button.setFixedHeight(28)
            layout.addWidget(button)
        layout.addStretch(1)
        count_label = BodyLabel("已选 0 项", frame)
        speed_label = BodyLabel("总速度: --", frame)
        layout.addWidget(count_label)
        layout.addSpacing(16)
        layout.addWidget(speed_label)
        return frame, {
            "select_all": select_all_button,
            "invert": invert_button,
            "pause": pause_button,
            "resume": resume_button,
            "retry": retry_button,
            "delete": delete_button,
            "count": count_label,
            "speed": speed_label,
        }

    def _connect_signals(self) -> None:
        self.segmented_widget.currentItemChanged.connect(self._on_segment_changed)
        self.upload_filter_combo.currentTextChanged.connect(self._on_upload_filter_changed)
        self.download_filter_combo.currentTextChanged.connect(self._on_download_filter_changed)
        self.open_download_folder_button.clicked.connect(self._request_open_download_folder)
        self.upload_table.itemSelectionChanged.connect(lambda: self._update_batch_bar("upload"))
        self.download_table.itemSelectionChanged.connect(lambda: self._update_batch_bar("download"))
        self.upload_batch_buttons["select_all"].clicked.connect(
            lambda: self._select_all(self.upload_table)
        )
        self.upload_batch_buttons["invert"].clicked.connect(
            lambda: self._invert_selection(self.upload_table, len(self._filtered_upload_records()))
        )
        self.upload_batch_buttons["delete"].clicked.connect(
            lambda: self._request_delete_selected("upload")
        )
        self.upload_batch_buttons["retry"].clicked.connect(self._request_retry_selected_uploads)
        self.upload_batch_buttons["pause"].clicked.connect(
            lambda: self._request_pause_selected("upload")
        )
        self.upload_batch_buttons["resume"].clicked.connect(
            lambda: self._request_resume_selected("upload")
        )
        self.download_batch_buttons["select_all"].clicked.connect(
            lambda: self._select_all(self.download_table)
        )
        self.download_batch_buttons["invert"].clicked.connect(
            lambda: self._invert_selection(
                self.download_table,
                len(self._filtered_download_records()),
            )
        )
        self.download_batch_buttons["delete"].clicked.connect(
            lambda: self._request_delete_selected("download")
        )
        self.download_batch_buttons["pause"].clicked.connect(
            lambda: self._request_pause_selected("download")
        )
        self.download_batch_buttons["resume"].clicked.connect(
            lambda: self._request_resume_selected("download")
        )

    def _render_all(self) -> None:
        self._render_upload_table()
        self._render_download_table()

    def _on_segment_changed(self, route_key: str) -> None:
        self._active_direction = route_key
        is_upload = route_key == "upload"
        self.upload_frame.setVisible(is_upload)
        self.download_frame.setVisible(not is_upload)
        self.upload_filter_label.setVisible(is_upload)
        self.upload_filter_combo.setVisible(is_upload)
        self.download_filter_label.setVisible(not is_upload)
        self.download_filter_combo.setVisible(not is_upload)
        self.open_download_folder_button.setVisible(not is_upload)

    def _on_upload_filter_changed(self, status: str) -> None:
        self.upload_status_filter = status
        self.flush_progress_render()
        self._render_upload_table()

    def _on_download_filter_changed(self, status: str) -> None:
        self.download_status_filter = status
        self.flush_progress_render()
        self._render_download_table()

    def _render_upload_table(self) -> None:
        self._render_table(
            self.upload_table,
            self._filtered_upload_records(),
            "upload",
        )
        self._update_batch_bar("upload")

    def _render_download_table(self) -> None:
        self._render_table(
            self.download_table,
            self._filtered_download_records(),
            "download",
        )
        self._update_batch_bar("download")

    def _render_table(
        self,
        table: TableWidget,
        records: list[TransferRecord],
        direction: str,
    ) -> None:
        # Contract 16 (transfer page): a refill that changes the row-to-task
        # mapping (record removal/addition/reorder/filter change) shifts row
        # numbers, so a stale row-number selection would silently point at
        # another task. Progress-only renders keep the same id sequence and
        # must preserve the user's selection, hence the conditional clear. It
        # runs before setRowCount so the synchronous itemSelectionChanged ->
        # _update_batch_bar reads the already-updated backing record list.
        rendered_ids: list[object] = []
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            rendered_ids.append(item.data(Qt.ItemDataRole.UserRole) if item is not None else None)
        if rendered_ids != [record.task_id for record in records]:
            table.clearSelection()
        if table.rowCount() != len(records):
            self._clear_action_widgets(table)
        table.setRowCount(len(records))
        for row, record in enumerate(records):
            values = (
                record.name,
                _format_optional_bytes(record.size),
                self._format_record_progress(record),
                _format_speed(record.speed_bps),
                record.status,
            )
            for column, value in enumerate(values):
                table_item = QTableWidgetItem(value)
                table_item.setData(Qt.ItemDataRole.UserRole, record.task_id)
                if column in (
                    TRANSFER_COL_SIZE,
                    TRANSFER_COL_PROGRESS,
                    TRANSFER_COL_SPEED,
                    TRANSFER_COL_STATUS,
                ):
                    table_item.setTextAlignment(
                        Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight
                    )
                table.setItem(row, column, table_item)
            action_key = (record.task_id, record.status, record.can_resume, record.upload_retryable)
            widget = table.cellWidget(row, TRANSFER_COL_ACTION)
            if widget is not None and widget.property("action_key") == action_key:
                continue
            if widget is not None:
                widget.hide()
                table.removeCellWidget(row, TRANSFER_COL_ACTION)
                widget.setParent(None)
                widget.deleteLater()
            widget = self._build_row_action_widget(record, direction, table)
            widget.setProperty("action_key", action_key)
            table.setCellWidget(row, TRANSFER_COL_ACTION, widget)
        self._update_total_speed(direction)

    def _build_row_action_widget(
        self,
        record: TransferRecord,
        direction: str,
        parent: QWidget,
    ) -> QWidget:
        widget = QWidget(parent)
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)
        layout.addStretch(1)
        if direction == "download":
            self._add_download_action_buttons(layout, record, widget)
        else:
            self._add_upload_action_buttons(layout, record, widget)
        delete_button = self._build_action_button(FIF.DELETE, "删除", widget)
        delete_button.setEnabled(
            direction == "upload"
            or record.status in TERMINAL_TRANSFER_STATUSES
            or record.status == "已暂停"
        )
        delete_button.clicked.connect(
            lambda _checked=False, task_id=record.task_id: self._request_delete_ids(
                direction,
                {task_id},
            )
        )
        layout.addWidget(delete_button)
        return widget

    def _add_download_action_buttons(
        self,
        layout: QHBoxLayout,
        record: TransferRecord,
        parent: QWidget,
    ) -> None:
        pause_button = self._build_action_button(FIF.PAUSE, "暂停", parent)
        pause_button.setEnabled(record.status in ACTIVE_DOWNLOAD_STATUSES)
        pause_button.clicked.connect(
            lambda _checked=False, task_id=record.task_id: self.pause_download_requested.emit(
                task_id
            )
        )
        resume_button = self._build_action_button(FIF.PLAY, "继续", parent)
        resume_button.setEnabled(record.can_resume and record.status in {"已暂停", "失败"})
        resume_button.clicked.connect(
            lambda _checked=False, task_id=record.task_id: self.resume_download_requested.emit(
                task_id
            )
        )
        cancel_button = self._build_action_button(FIF.CANCEL, "取消", parent)
        cancel_button.setEnabled(record.status in ACTIVE_DOWNLOAD_STATUSES)
        cancel_button.clicked.connect(
            lambda _checked=False, task_id=record.task_id: self.cancel_download_requested.emit(
                task_id
            )
        )
        layout.addWidget(pause_button)
        layout.addWidget(resume_button)
        layout.addWidget(cancel_button)

    def _add_upload_action_buttons(
        self,
        layout: QHBoxLayout,
        record: TransferRecord,
        parent: QWidget,
    ) -> None:
        pause_button = self._build_action_button(FIF.PAUSE, "暂停", parent)
        pause_button.setEnabled(record.upload_retryable and record.status in ACTIVE_UPLOAD_STATUSES)
        pause_button.clicked.connect(
            lambda _checked=False, task_id=record.task_id: self.pause_uploads_requested.emit(
                {task_id}
            )
        )
        resume_button = self._build_action_button(FIF.PLAY, "继续", parent)
        resume_button.setEnabled(record.upload_retryable and record.status == "已暂停")
        resume_button.clicked.connect(
            lambda _checked=False, task_id=record.task_id: self.resume_uploads_requested.emit(
                {task_id}
            )
        )
        retry_button = self._build_action_button(FIF.SYNC, "重试", parent)
        retry_button.setEnabled(record.upload_retryable and record.status == "失败")
        retry_button.clicked.connect(
            lambda _checked=False, task_id=record.task_id: self.retry_upload_requested.emit(
                {task_id}
            )
        )
        layout.addWidget(pause_button)
        layout.addWidget(resume_button)
        layout.addWidget(retry_button)

    @staticmethod
    def _build_action_button(icon: FluentIcon, tooltip: str, parent: QWidget) -> ToolButton:
        button = ToolButton(icon, parent)
        button.setToolTip(tooltip)
        button.setFixedSize(*TRANSFER_ACTION_BUTTON_SIZE)
        return button

    @staticmethod
    def _clear_action_widgets(table: TableWidget) -> None:
        for row in range(table.rowCount()):
            for column in range(table.columnCount()):
                widget = table.cellWidget(row, column)
                if widget is None:
                    continue
                widget.hide()
                table.removeCellWidget(row, column)
                widget.setParent(None)
                widget.deleteLater()

    def _filtered_upload_records(self) -> list[TransferRecord]:
        if self.upload_status_filter == "全部":
            return list(self.upload_records)
        return [
            record for record in self.upload_records if record.status == self.upload_status_filter
        ]

    def _filtered_download_records(self) -> list[TransferRecord]:
        if self.download_status_filter == "全部":
            return list(self.download_records)
        return [
            record
            for record in self.download_records
            if record.status == self.download_status_filter
        ]

    def _request_open_download_folder(self) -> None:
        self.open_download_folder_requested.emit(self.active_download_folder())

    def _request_delete_selected(self, direction: str) -> None:
        table = self.upload_table if direction == "upload" else self.download_table
        visible = (
            self._filtered_upload_records()
            if direction == "upload"
            else self._filtered_download_records()
        )
        rows = sorted({index.row() for index in table.selectionModel().selectedRows()})
        task_ids = {
            visible[row].task_id
            for row in rows
            if 0 <= row < len(visible)
            and (
                direction == "upload"
                or visible[row].status in TERMINAL_TRANSFER_STATUSES
                or visible[row].status == "已暂停"
            )
        }
        self._request_delete_ids(direction, task_ids)

    def _request_delete_ids(self, direction: str, task_ids: set[str]) -> None:
        if task_ids:
            self.remove_records_requested.emit(direction, task_ids)

    def _request_retry_selected_uploads(self) -> None:
        visible = self._filtered_upload_records()
        rows = sorted({index.row() for index in self.upload_table.selectionModel().selectedRows()})
        task_ids = {
            visible[row].task_id
            for row in rows
            if 0 <= row < len(visible)
            and visible[row].status == "失败"
            and visible[row].upload_retryable
        }
        if task_ids:
            self.retry_upload_requested.emit(task_ids)

    def _request_pause_selected(self, direction: str) -> None:
        visible = (
            self._filtered_upload_records()
            if direction == "upload"
            else self._filtered_download_records()
        )
        selected = self._selected_task_ids(direction)
        task_ids = {
            record.task_id
            for record in visible
            if record.task_id in selected
            and (
                (
                    direction == "upload"
                    and record.upload_retryable
                    and record.status in ACTIVE_UPLOAD_STATUSES
                )
                or (direction == "download" and record.status in ACTIVE_DOWNLOAD_STATUSES)
            )
        }
        if not task_ids:
            return
        if direction == "upload":
            self.pause_uploads_requested.emit(task_ids)
        else:
            self.pause_downloads_requested.emit(task_ids)

    def _request_resume_selected(self, direction: str) -> None:
        visible = (
            self._filtered_upload_records()
            if direction == "upload"
            else self._filtered_download_records()
        )
        selected = self._selected_task_ids(direction)
        task_ids = {
            record.task_id
            for record in visible
            if record.task_id in selected
            and (
                (direction == "upload" and record.upload_retryable and record.status == "已暂停")
                or (
                    direction == "download"
                    and record.can_resume
                    and record.status in {"已暂停", "失败"}
                )
            )
        }
        if not task_ids:
            return
        if direction == "upload":
            self.resume_uploads_requested.emit(task_ids)
        else:
            self.resume_downloads_requested.emit(task_ids)

    def _selected_task_ids(self, direction: str) -> set[str]:
        table = self.upload_table if direction == "upload" else self.download_table
        visible = (
            self._filtered_upload_records()
            if direction == "upload"
            else self._filtered_download_records()
        )
        return {
            visible[index.row()].task_id
            for index in table.selectionModel().selectedRows()
            if 0 <= index.row() < len(visible)
        }

    @staticmethod
    def _select_all(table: TableWidget) -> None:
        table.selectAll()

    @staticmethod
    def _invert_selection(table: TableWidget, row_count: int) -> None:
        selection_model = table.selectionModel()
        if selection_model is None:  # pragma: no cover - docs/testing-exemptions.md
            return
        selected = {index.row() for index in selection_model.selectedRows()}
        table.blockSignals(True)
        table.clearSelection()
        for row in range(row_count):
            if row in selected:
                continue
            index = table.model().index(row, 0)
            selection_model.select(
                index,
                QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
            )
        table.blockSignals(False)
        table.itemSelectionChanged.emit()

    def _update_batch_bar(self, direction: str) -> None:
        if direction == "upload":
            buttons = self.upload_batch_buttons
            table = self.upload_table
        else:
            buttons = self.download_batch_buttons
            table = self.download_table
        count = len(table.selectionModel().selectedRows())
        count_label = buttons["count"]
        if isinstance(count_label, BodyLabel):  # pragma: no cover - docs/testing-exemptions.md
            count_label.setText(f"已选 {count} 项")
        if direction == "upload":
            visible = self._filtered_upload_records()
            rows = {index.row() for index in table.selectionModel().selectedRows()}
            buttons["retry"].setEnabled(
                any(
                    0 <= row < len(visible)
                    and visible[row].status == "失败"
                    and visible[row].upload_retryable
                    for row in rows
                )
            )
            buttons["pause"].setEnabled(
                any(
                    0 <= row < len(visible)
                    and visible[row].upload_retryable
                    and visible[row].status in ACTIVE_UPLOAD_STATUSES
                    for row in rows
                )
            )
            buttons["resume"].setEnabled(
                any(
                    0 <= row < len(visible)
                    and visible[row].upload_retryable
                    and visible[row].status == "已暂停"
                    for row in rows
                )
            )
            return
        visible = self._filtered_download_records()
        rows = {index.row() for index in table.selectionModel().selectedRows()}
        buttons["pause"].setEnabled(
            any(
                0 <= row < len(visible) and visible[row].status in ACTIVE_DOWNLOAD_STATUSES
                for row in rows
            )
        )
        buttons["resume"].setEnabled(
            any(
                0 <= row < len(visible)
                and visible[row].can_resume
                and visible[row].status in {"已暂停", "失败"}
                for row in rows
            )
        )

    def _update_total_speed(self, direction: str) -> None:
        records = self.upload_records if direction == "upload" else self.download_records
        buttons = (
            self.upload_batch_buttons if direction == "upload" else self.download_batch_buttons
        )
        total_speed = sum(record.speed_bps for record in records if record.speed_bps > 0)
        speed_label = buttons["speed"]
        if isinstance(speed_label, BodyLabel):  # pragma: no cover - docs/testing-exemptions.md
            speed_label.setText(f"总速度: {_format_speed(total_speed)}")

    def _find_record(self, direction: str, task_id: str) -> TransferRecord | None:
        records = self.upload_records if direction == "upload" else self.download_records
        return next((record for record in records if record.task_id == task_id), None)

    @staticmethod
    def _upsert_record(records: list[TransferRecord], record: TransferRecord) -> None:
        for index, existing in enumerate(records):
            if existing.task_id == record.task_id:
                records[index] = record
                return
        records.append(record)

    @staticmethod
    def _format_record_progress(record: TransferRecord) -> str:
        total = record.total_bytes or record.size
        percent = record.progress_percent
        if total and total > 0:
            return f"{percent}% ({_format_bytes(record.bytes_done)} / {_format_bytes(total)})"
        return f"{percent}%"


class AccountInterface(QWidget):
    """Account page aligned with the sibling Fluent client."""

    refresh_all_requested = Signal()
    logout_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AccountInterface")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(12)

        self.account_group = SettingCardGroup("账户信息", self)
        self.account_card = SettingCard(
            FIF.PEOPLE,
            "账户",
            "当前登录的账户信息",
            self.account_group,
        )
        self.account_value_label = BodyLabel("--", self.account_card)
        self.account_card.hBoxLayout.addWidget(
            self.account_value_label,
            0,
            Qt.AlignmentFlag.AlignRight,
        )
        self.account_card.hBoxLayout.addSpacing(16)

        self.usage_card = SettingCard(
            FIF.CLOUD,
            "云盘空间",
            "当前账号的网盘容量",
            self.account_group,
        )
        self.usage_value_label = BodyLabel("-- / --", self.usage_card)
        self.usage_card.hBoxLayout.addWidget(
            self.usage_value_label,
            0,
            Qt.AlignmentFlag.AlignRight,
        )
        self.usage_card.hBoxLayout.addSpacing(16)

        self.refresh_all_card = PushSettingCard(
            "刷新",
            FIF.UPDATE,
            "刷新所有信息",
            "重新获取用户数据和当前文件列表",
            self.account_group,
        )
        self.logout_card = PushSettingCard(
            "退出登录",
            FIF.CLOSE,
            "退出登录",
            "清除当前登录状态并返回登录页",
            self.account_group,
        )

        self.account_group.addSettingCard(self.account_card)
        self.account_group.addSettingCard(self.usage_card)
        self.account_group.addSettingCard(self.refresh_all_card)
        self.account_group.addSettingCard(self.logout_card)
        layout.addWidget(self.account_group)
        layout.addStretch(1)

        self.refresh_all_card.clicked.connect(self.refresh_all_requested.emit)
        self.logout_card.clicked.connect(self.logout_requested.emit)

    def set_session(self, session: AuthSession | None) -> None:
        """Render the current session without exposing sensitive material."""
        if session is None:
            self.account_value_label.setText("--")
            return
        display_name = session.display_name or "未命名账户"
        self.account_value_label.setText(f"{display_name} / {_mask_account_id(session.account_id)}")

    def set_usage(self, usage: WopanCloudUsage | None) -> None:
        """Render cloud usage."""
        if usage is None:
            self.usage_value_label.setText("-- / --")
            return
        self.usage_value_label.setText(_format_usage_value(usage))


class SettingsInterface(ScrollArea):
    """Settings page using Fluent setting cards."""

    settings_changed = Signal(object)
    _LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    _RETRY_ATTEMPTS = ["0", "1", "2", "3", "4", "5"]

    def __init__(
        self,
        settings: AppSettings,
        *,
        settings_path: Path | None = None,
        log_path: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent=parent)
        self._settings = settings
        self._settings_path = settings_path or app_settings_path()
        self._log_path = log_path or app_log_path()
        self.scroll_widget = QWidget()
        self.expand_layout = ExpandLayout(self.scroll_widget)

        self.startup_group = SettingCardGroup("启动", self.scroll_widget)
        self.stay_logged_in_card = SwitchSettingCard(
            FIF.SYNC,
            "启动时自动恢复登录",
            "启动时尝试复用上次登录状态",
            parent=self.startup_group,
        )
        self.stay_logged_in_card.setChecked(settings.stay_logged_in)

        self.transfer_group = SettingCardGroup("传输设置", self.scroll_widget)
        self.download_folder_card = PushSettingCard(
            "选择文件夹",
            FIF.DOWNLOAD,
            "下载目录",
            str(settings.default_download_path),
            self.transfer_group,
        )
        self.ask_download_location_card = SwitchSettingCard(
            FIF.DOWNLOAD,
            "每次询问下载位置",
            "下载文件时是否每次都询问保存位置",
            parent=self.transfer_group,
        )
        self.ask_download_location_card.setChecked(settings.ask_download_location)
        self.download_threads_card = SettingCard(
            FIF.DOWNLOAD,
            "下载线程数",
            "单个下载任务的最大线程数（1-16）",
            self.transfer_group,
        )
        self.download_threads_spin_box = self._build_spin_box(
            self.download_threads_card,
            1,
            16,
            settings.max_download_threads,
        )
        self.upload_threads_card = SettingCard(
            FIF.UP,
            "上传线程数",
            "单个上传任务的最大线程数（1-16）",
            self.transfer_group,
        )
        self.upload_threads_spin_box = self._build_spin_box(
            self.upload_threads_card,
            1,
            16,
            settings.max_upload_threads,
        )
        self.concurrent_downloads_card = SettingCard(
            FIF.DOWNLOAD,
            "同时下载任务数",
            "允许同时进行的下载任务数（1-5）",
            self.transfer_group,
        )
        self.concurrent_downloads_spin_box = self._build_spin_box(
            self.concurrent_downloads_card,
            1,
            5,
            settings.max_concurrent_downloads,
        )
        self.concurrent_uploads_card = SettingCard(
            FIF.UP,
            "同时上传任务数",
            "允许同时进行的上传任务数（1-5）",
            self.transfer_group,
        )
        self.concurrent_uploads_spin_box = self._build_spin_box(
            self.concurrent_uploads_card,
            1,
            5,
            settings.max_concurrent_uploads,
        )
        self.retry_attempts_card = SettingCard(
            FIF.SYNC,
            "分块重试次数",
            "上传/下载分块失败后的重试次数",
            self.transfer_group,
        )
        self.retry_attempts_combo_box = ComboBox(self.retry_attempts_card)
        self.retry_attempts_combo_box.addItems(self._RETRY_ATTEMPTS)
        self.retry_attempts_combo_box.setCurrentIndex(settings.retry_max_attempts)
        self.retry_attempts_combo_box.setFixedWidth(120)
        self.retry_attempts_card.hBoxLayout.addWidget(self.retry_attempts_combo_box)
        self.retry_attempts_card.hBoxLayout.addSpacing(16)
        self.download_part_size_card = SettingCard(
            FIF.DOWNLOAD,
            "下载分片大小",
            "单个下载分片大小（4-32 MB）",
            self.transfer_group,
        )
        self.download_part_size_spin_box = self._build_spin_box(
            self.download_part_size_card,
            4,
            32,
            settings.download_part_size_mb,
        )
        self.download_part_mode_card = SettingCard(
            FIF.DOWNLOAD,
            "下载分片模式",
            "自动按文件大小选择分片，或使用固定分片大小",
            self.transfer_group,
        )
        self.download_part_mode_combo_box = ComboBox(self.download_part_mode_card)
        self.download_part_mode_combo_box.addItems(["自动", "固定大小"])
        self.download_part_mode_combo_box.setCurrentIndex(
            1 if settings.download_part_mode == "fixed" else 0
        )
        self.download_part_mode_combo_box.setFixedWidth(120)
        self.download_part_mode_card.hBoxLayout.addWidget(self.download_part_mode_combo_box)
        self.download_part_mode_card.hBoxLayout.addSpacing(16)
        self.upload_part_size_card = SettingCard(
            FIF.UP,
            "上传分片大小",
            "单个上传分片大小（5-16 MB）",
            self.transfer_group,
        )
        self.upload_part_size_spin_box = self._build_spin_box(
            self.upload_part_size_card,
            5,
            16,
            settings.upload_part_size_mb,
        )

        self.about_group = SettingCardGroup("关于", self.scroll_widget)
        self.log_level_card = SettingCard(
            FIF.DOCUMENT,
            "日志级别",
            "设置程序日志的详细程度",
            self.about_group,
        )
        self.log_level_combo_box = ComboBox(self.log_level_card)
        self.log_level_combo_box.addItems(self._LOG_LEVELS)
        self.log_level_combo_box.setCurrentIndex(self._LOG_LEVELS.index(settings.log_level))
        self.log_level_combo_box.setFixedWidth(120)
        self.log_level_card.hBoxLayout.addWidget(self.log_level_combo_box)
        self.log_level_card.hBoxLayout.addSpacing(16)

        self.open_log_file_card = PushSettingCard(
            "打开日志",
            FIF.DOCUMENT,
            "日志文件",
            str(self._log_path),
            self.about_group,
        )
        self.open_settings_folder_card = PushSettingCard(
            "打开位置",
            FIF.FOLDER,
            "配置文件",
            str(self._settings_path),
            self.about_group,
        )
        self.about_card = PrimaryPushSettingCard(
            "OpenWoPan",
            FIF.INFO,
            "关于",
            f"版本 {__version__}",
            self.about_group,
        )

        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setViewportMargins(0, 0, 0, 20)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(
            "SettingsInterface { background: transparent; border: none; }"
            "SettingsInterface > QWidget { background: transparent; }"
        )
        self.viewport().setAutoFillBackground(False)
        self.viewport().setStyleSheet("background: transparent;")
        self.scroll_widget.setAutoFillBackground(False)
        self.scroll_widget.setStyleSheet("background: transparent;")
        self.setWidget(self.scroll_widget)
        self.setWidgetResizable(True)
        self.setObjectName("SettingsInterface")

        self.startup_group.addSettingCard(self.stay_logged_in_card)
        self.transfer_group.addSettingCard(self.download_folder_card)
        self.transfer_group.addSettingCard(self.ask_download_location_card)
        self.transfer_group.addSettingCard(self.download_threads_card)
        self.transfer_group.addSettingCard(self.upload_threads_card)
        self.transfer_group.addSettingCard(self.concurrent_downloads_card)
        self.transfer_group.addSettingCard(self.concurrent_uploads_card)
        self.transfer_group.addSettingCard(self.retry_attempts_card)
        self.transfer_group.addSettingCard(self.download_part_size_card)
        self.transfer_group.addSettingCard(self.download_part_mode_card)
        self.transfer_group.addSettingCard(self.upload_part_size_card)
        self.about_group.addSettingCard(self.log_level_card)
        self.about_group.addSettingCard(self.open_log_file_card)
        self.about_group.addSettingCard(self.open_settings_folder_card)
        self.about_group.addSettingCard(self.about_card)
        self.expand_layout.setSpacing(28)
        self.expand_layout.setContentsMargins(36, 10, 36, 0)
        self.expand_layout.addWidget(self.startup_group)
        self.expand_layout.addWidget(self.transfer_group)
        self.expand_layout.addWidget(self.about_group)

        self.stay_logged_in_card.checkedChanged.connect(self._on_stay_logged_in_changed)
        self.download_folder_card.clicked.connect(self._on_download_folder_clicked)
        self.ask_download_location_card.checkedChanged.connect(
            self._on_ask_download_location_changed
        )
        self.download_threads_spin_box.valueChanged.connect(self._on_download_threads_changed)
        self.upload_threads_spin_box.valueChanged.connect(self._on_upload_threads_changed)
        self.concurrent_downloads_spin_box.valueChanged.connect(
            self._on_concurrent_downloads_changed
        )
        self.concurrent_uploads_spin_box.valueChanged.connect(self._on_concurrent_uploads_changed)
        self.retry_attempts_combo_box.currentIndexChanged.connect(self._on_retry_attempts_changed)
        self.download_part_size_spin_box.valueChanged.connect(self._on_download_part_size_changed)
        self.download_part_mode_combo_box.currentIndexChanged.connect(
            self._on_download_part_mode_changed
        )
        self.upload_part_size_spin_box.valueChanged.connect(self._on_upload_part_size_changed)
        self.log_level_combo_box.currentIndexChanged.connect(self._on_log_level_changed)
        self.open_log_file_card.clicked.connect(self._open_log_file)
        self.open_settings_folder_card.clicked.connect(self._open_settings_folder)

    def settings(self) -> AppSettings:
        """Return the current in-memory settings."""
        return self._settings

    def _build_spin_box(
        self,
        card: SettingCard,
        minimum: int,
        maximum: int,
        value: int,
    ) -> SpinBox:
        spin_box = SpinBox(card)
        spin_box.setRange(minimum, maximum)
        spin_box.setValue(value)
        spin_box.setFixedWidth(120)
        card.hBoxLayout.addWidget(spin_box)
        card.hBoxLayout.addSpacing(16)
        return spin_box

    def _replace_settings(self, **changes: Any) -> None:
        self._settings = replace(self._settings, **changes)
        save_app_settings(self._settings, self._settings_path)
        self.settings_changed.emit(self._settings)

    def _on_stay_logged_in_changed(self, checked: bool) -> None:
        self._replace_settings(stay_logged_in=checked)
        LOGGER.info("settings.stay_logged_in.changed value=%s", checked)

    def _on_download_folder_clicked(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "选择下载目录",
            str(self._settings.default_download_path),
        )
        if not folder:
            return
        download_path = Path(folder)
        self.download_folder_card.setContent(str(download_path))
        self._replace_settings(default_download_path=download_path)
        LOGGER.info(
            "settings.default_download_path.changed path_name_length=%s",
            len(download_path.name),
        )

    def _on_ask_download_location_changed(self, checked: bool) -> None:
        self._replace_settings(ask_download_location=checked)
        LOGGER.info("settings.ask_download_location.changed value=%s", checked)

    def _on_download_threads_changed(self, value: int) -> None:
        self._replace_settings(max_download_threads=value)

    def _on_upload_threads_changed(self, value: int) -> None:
        self._replace_settings(max_upload_threads=value)

    def _on_concurrent_downloads_changed(self, value: int) -> None:
        self._replace_settings(max_concurrent_downloads=value)

    def _on_concurrent_uploads_changed(self, value: int) -> None:
        self._replace_settings(max_concurrent_uploads=value)

    def _on_retry_attempts_changed(self, _index: int) -> None:
        self._replace_settings(retry_max_attempts=int(self.retry_attempts_combo_box.currentText()))

    def _on_download_part_size_changed(self, value: int) -> None:
        self._replace_settings(download_part_size_mb=value)

    def _on_download_part_mode_changed(self, index: int) -> None:
        self._replace_settings(download_part_mode="fixed" if index == 1 else "auto")

    def _on_upload_part_size_changed(self, value: int) -> None:
        self._replace_settings(upload_part_size_mb=value)

    def _on_log_level_changed(self, index: int) -> None:
        level = self._LOG_LEVELS[index]
        self._replace_settings(log_level=level)
        set_logging_level(level)
        LOGGER.info("settings.log_level.changed level=%s", level)

    def _open_log_file(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._log_path)))

    def _open_settings_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._settings_path.parent)))


class FileInterface(QWidget):
    """Fluent-style file browsing page."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.setObjectName("FileInterface")
        self._window = window
        self._rendering_breadcrumb = False
        self._operation_busy_count = 0

        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(24, 20, 24, 24)
        self._main_layout.setSpacing(12)

        self._build_top_bar()
        self._build_operation_busy_bar()
        self._build_content()
        self._connect_signals()

    def render_state(
        self,
        items: tuple[WopanItem, ...],
        breadcrumb: tuple[BreadcrumbEntry, ...],
    ) -> None:
        """Render file rows and breadcrumb from current window state."""
        self._render_breadcrumb(breadcrumb)
        self._render_table(items)

    def set_storage_usage(self, usage: WopanCloudUsage | None) -> None:
        """Render the storage card from cloud usage."""
        if usage is None:
            self.storage_value_label.setText("-- / --")
            self.storage_progress_bar.setValue(0)
            return
        self.storage_value_label.setText(_format_usage_value(usage))
        self.storage_progress_bar.setValue(_usage_percent(usage))

    def set_operations_enabled(self, enabled: bool) -> None:
        """Enable or disable operation controls."""
        for widget in (
            self.new_folder_button,
            self.refresh_button,
            self.back_button,
            self.search_bar,
            self.file_table,
            self.folder_tree,
        ):
            widget.setEnabled(enabled)
        self.upload_button_group.setEnabled(enabled)
        self.upload_file_action.setEnabled(enabled)
        self.delete_button.setEnabled(enabled)
        self._window.update_operation_controls()

    def _build_top_bar(self) -> None:
        top_bar = QFrame(self)
        top_bar.setObjectName("frame")
        top_bar.setStyleSheet(FRAME_STYLE)
        self.top_bar_frame = top_bar
        top_layout = QVBoxLayout(top_bar)
        top_layout.setContentsMargins(12, 10, 12, 10)
        top_layout.setSpacing(6)

        action_layout = QHBoxLayout()
        self.action_bar_layout = action_layout
        action_layout.setSpacing(8)
        self.new_folder_button = PushButton(FIF.FOLDER_ADD.icon(), "新建文件夹", top_bar)
        self.upload_button = SplitPushButton("上传文件", top_bar, FIF.DOCUMENT)
        self.upload_button.setEnabled(False)
        self.upload_button.setDropIcon(FIF.DOWN)
        self.upload_button.dropButton.setToolTip("更多上传方式")
        self.upload_menu = RoundMenu(parent=self)
        self.upload_file_action = Action(FIF.DOCUMENT.icon(), "上传文件", parent=self.upload_menu)
        self.upload_folder_action = Action(FIF.FOLDER.icon(), "上传文件夹", parent=self.upload_menu)
        self.upload_file_action.setEnabled(False)
        self.upload_folder_action.setEnabled(False)
        self.upload_menu.addAction(self.upload_file_action)
        self.upload_menu.addAction(self.upload_folder_action)
        self.upload_button.setFlyout(self.upload_menu)
        self.upload_button_group = self.upload_button
        self.download_button = PushButton(FIF.DOWNLOAD.icon(), "下载", top_bar)
        self.download_button.setEnabled(False)
        self.delete_button = PushButton(FIF.DELETE.icon(), "删除", top_bar)
        self.search_bar = SearchLineEdit(top_bar)
        self.search_bar.setPlaceholderText("搜索文件")
        self.search_bar.setFixedWidth(200)
        self.search_bar.setEnabled(False)

        action_layout.addWidget(self.new_folder_button)
        action_layout.addWidget(self.upload_button_group)
        action_layout.addWidget(self.download_button)
        action_layout.addWidget(self.delete_button)
        action_layout.addStretch(1)
        action_layout.addWidget(self.search_bar)

        nav_layout = QHBoxLayout()
        self.nav_bar_layout = nav_layout
        nav_layout.setSpacing(8)
        self.back_button = ToolButton(FIF.LEFT_ARROW, top_bar)
        self.back_button.setToolTip("返回上一级")
        self.breadcrumb_frame = QFrame(top_bar)
        self.breadcrumb_frame.setObjectName("frame")
        self.breadcrumb_frame.setStyleSheet(FRAME_STYLE)
        breadcrumb_layout = QHBoxLayout(self.breadcrumb_frame)
        breadcrumb_layout.setContentsMargins(8, 4, 8, 4)
        breadcrumb_layout.setSpacing(0)
        self.breadcrumb_bar = BreadcrumbBar(self.breadcrumb_frame)
        self.breadcrumb_bar.currentItemChanged.connect(self._on_breadcrumb_changed)
        breadcrumb_layout.addWidget(self.breadcrumb_bar)
        self.refresh_button = PushButton(FIF.UPDATE.icon(), "刷新", top_bar)
        nav_layout.addWidget(self.back_button)
        nav_layout.addWidget(self.breadcrumb_frame, 1)
        nav_layout.addWidget(self.refresh_button)

        top_layout.addLayout(action_layout)
        top_layout.addLayout(nav_layout)
        self._main_layout.addWidget(top_bar)

    def _build_operation_busy_bar(self) -> None:
        """Build the slim indeterminate busy bar for batch folder operations."""
        self.operation_busy_bar = IndeterminateProgressBar(self, start=False)
        self.operation_busy_bar.hide()
        self._main_layout.addWidget(self.operation_busy_bar)

    def set_operation_busy(self, busy: bool) -> None:
        """Show or hide the batch-operation busy indicator (reference counted).

        Move/copy/delete run on independent threads, so several can be in
        flight at once; the bar hides only when the last one reaches a
        terminal state.
        """
        self._operation_busy_count = max(0, self._operation_busy_count + (1 if busy else -1))
        active = self._operation_busy_count > 0
        self.operation_busy_bar.setVisible(active)
        if active:
            self.operation_busy_bar.start()
        else:
            self.operation_busy_bar.stop()

    def _build_content(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter = splitter
        splitter.setChildrenCollapsible(False)

        left_panel = QFrame(splitter)
        left_panel.setObjectName("frame")
        left_panel.setStyleSheet(FRAME_STYLE)
        self.tree_frame = left_panel
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 8, 0, 0)
        left_layout.setSpacing(8)
        self.folder_tree = TreeWidget(left_panel)
        self.folder_tree.setHeaderHidden(True)
        self.folder_tree.setUniformRowHeights(True)
        left_layout.addWidget(self.folder_tree)
        self.storage_card = CardWidget(left_panel)
        storage_layout = QVBoxLayout(self.storage_card)
        storage_layout.setContentsMargins(12, 8, 12, 8)
        storage_layout.setSpacing(8)
        storage_top_layout = QHBoxLayout()
        storage_top_layout.setSpacing(8)
        self.storage_icon = IconWidget(FIF.CLOUD.icon(), self.storage_card)
        self.storage_icon.setFixedSize(20, 20)
        self.storage_value_label = BodyLabel("-- / --", self.storage_card)
        self.storage_value_label.setStyleSheet("font-size: 12px; color: gray;")
        storage_top_layout.addWidget(self.storage_icon)
        storage_top_layout.addWidget(self.storage_value_label)
        storage_top_layout.addStretch(1)
        storage_layout.addLayout(storage_top_layout)
        self.storage_progress_bar = ProgressBar(self.storage_card)
        self.storage_progress_bar.setRange(0, 100)
        self.storage_progress_bar.setValue(0)
        self.storage_progress_bar.setFixedHeight(6)
        storage_layout.addWidget(self.storage_progress_bar)
        left_layout.addWidget(self.storage_card)

        right_panel = QFrame(splitter)
        right_panel.setObjectName("listFrame")
        right_panel.setStyleSheet(FRAME_STYLE)
        self.list_frame = right_panel
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 8, 0, 0)
        right_layout.setSpacing(0)
        self.file_table = DroppableTableWidget(right_panel)
        self.file_table.setColumnCount(3)
        self.file_table.setHorizontalHeaderLabels(["名称", "类型", "大小"])
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.file_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.file_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_table.setAlternatingRowColors(True)
        self.file_table.setBorderRadius(8)
        self.file_table.setBorderVisible(True)
        vertical_header = self.file_table.verticalHeader()
        if vertical_header is not None:  # pragma: no cover - docs/testing-exemptions.md
            vertical_header.hide()
        header = self.file_table.horizontalHeader()
        if header is not None:  # pragma: no cover - docs/testing-exemptions.md
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            for section in (1, 2):
                header.setSectionResizeMode(section, QHeaderView.ResizeMode.ResizeToContents)
        right_layout.addWidget(self.file_table)
        self.status_label = BodyLabel("", right_panel)
        self.status_label.setStyleSheet("font-size: 12px; color: gray; padding: 6px 8px;")
        right_layout.addWidget(self.status_label)

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, FILE_SPLITTER_STRETCH_FACTORS[0])
        splitter.setStretchFactor(1, FILE_SPLITTER_STRETCH_FACTORS[1])
        left_panel.setMinimumWidth(200)
        self._main_layout.addWidget(splitter, 1)

    def _connect_signals(self) -> None:
        self.new_folder_button.clicked.connect(self._window.prompt_create_folder)
        self.upload_button.clicked.connect(self._window.prompt_upload_file)
        self.upload_file_action.triggered.connect(self._window.prompt_upload_file)
        self.upload_folder_action.triggered.connect(self._window.prompt_upload_folder)
        self.refresh_button.clicked.connect(lambda: self._window.refresh_all_information())
        self.back_button.clicked.connect(self._window.go_up_one_level)
        self.download_button.clicked.connect(self._download_selected_row)
        self.delete_button.clicked.connect(self._delete_selected_row)
        self.folder_tree.itemClicked.connect(self._on_tree_item_clicked)
        self.file_table.itemDoubleClicked.connect(
            lambda item: self._window.enter_displayed_folder(item.row())
        )
        self.file_table.itemSelectionChanged.connect(self._window.update_operation_controls)
        self.file_table.paths_dropped.connect(self._window.handle_upload_drop)
        self.file_table.customContextMenuRequested.connect(self._window.open_file_context_menu)
        self.search_bar.returnPressed.connect(self._window.request_search)

    def _render_breadcrumb(self, breadcrumb: tuple[BreadcrumbEntry, ...]) -> None:
        self._rendering_breadcrumb = True
        self.breadcrumb_bar.clear()
        for index, entry in enumerate(breadcrumb):
            self.breadcrumb_bar.addItem(str(index), entry.name)
        self._rendering_breadcrumb = False

    def _on_breadcrumb_changed(self, key: str) -> None:
        if self._rendering_breadcrumb:
            return
        self._window.open_breadcrumb_index(int(key))

    def render_folder_tree(
        self,
        breadcrumb: tuple[BreadcrumbEntry, ...],
        levels: tuple[tuple[WopanItem, ...], ...],
    ) -> None:
        """Render the navigation tree expanded along the current path.

        ``levels[i]`` holds the folder entries of ``breadcrumb[i]``. Every
        level lists its folders (skipping the next path entry, which is
        attached with its own children instead); nodes along the path are
        expanded and the current folder is highlighted. Each node stores its
        full id/name path so deep nodes can navigate directly.
        """
        self.folder_tree.clear()
        if not breadcrumb:
            return

        def make_node(
            name: str,
            ids: tuple[str, ...],
            names: tuple[str, ...],
            folders: tuple[WopanItem, ...],
        ) -> QTreeWidgetItem:
            node = QTreeWidgetItem([name])
            node.setIcon(0, FIF.FOLDER.icon())
            node.setData(0, Qt.ItemDataRole.UserRole, ids)
            node.setData(0, Qt.ItemDataRole.UserRole + 1, names)
            for folder in folders:
                node.addChild(
                    make_node(folder.name, (*ids, folder.item_id), (*names, folder.name), ())
                )
            return node

        ids_chain: tuple[str, ...] = (breadcrumb[0].item_id,)
        names_chain: tuple[str, ...] = (breadcrumb[0].name,)
        current = make_node(breadcrumb[0].name, ids_chain, names_chain, ())
        self.folder_tree.addTopLevelItem(current)
        current.setExpanded(True)
        for depth in range(len(breadcrumb)):
            next_entry = breadcrumb[depth + 1] if depth + 1 < len(breadcrumb) else None
            folders = levels[depth] if depth < len(levels) else ()
            # Server listing order is unstable across calls; each sibling level
            # is sorted locally by name (case-insensitive, stable) so the tree
            # does not drift when directories are revisited. The breadcrumb
            # path node appended below is NOT part of this sort — path nodes
            # must stay in path order.
            for folder in sorted(folders, key=lambda entry: entry.name.casefold()):
                if next_entry is not None and folder.item_id == next_entry.item_id:
                    continue
                current.addChild(
                    make_node(
                        folder.name,
                        (*ids_chain, folder.item_id),
                        (*names_chain, folder.name),
                        (),
                    )
                )
            if next_entry is None:
                break
            ids_chain = (*ids_chain, next_entry.item_id)
            names_chain = (*names_chain, next_entry.name)
            child = make_node(next_entry.name, ids_chain, names_chain, ())
            current.addChild(child)
            current.setExpanded(True)
            current = child
        # The loop above only expands intermediate path nodes; the current
        # (last) node must be expanded too so its own subfolders are visible
        # right after a click navigates here (backlog B22).
        current.setExpanded(True)
        self.folder_tree.setCurrentItem(current)

    def _render_table(self, items: tuple[WopanItem, ...]) -> None:
        # A refill keeps still-in-range row selections alive, so a stale row
        # would silently point at a different entry; the window updates
        # ``self._items`` before rendering, so handlers read consistent state.
        self.file_table.clearSelection()
        self.file_table.setRowCount(len(items))
        for row, item in enumerate(items):
            values = (
                item.name,
                _format_kind(item.kind),
                _format_size(item.size, item.kind),
            )
            for column, value in enumerate(values):
                table_item = QTableWidgetItem(value)
                table_item.setData(Qt.ItemDataRole.UserRole, item.item_id)
                if column in (1, 2):
                    table_item.setTextAlignment(
                        Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight
                    )
                self.file_table.setItem(row, column, table_item)

    def _delete_selected_row(self) -> None:
        rows = self._window.selected_rows()
        if rows:
            self._window.prompt_delete_items(rows)

    def _download_selected_row(self) -> None:
        rows = self._window.selected_download_rows()
        if rows:
            self._window.prompt_download_item(rows[0])

    def _on_tree_item_clicked(self, item: QTreeWidgetItem) -> None:
        ids = item.data(0, Qt.ItemDataRole.UserRole)
        names = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if not isinstance(ids, tuple) or not isinstance(names, tuple):
            return
        self._window.open_tree_path(ids, names)


class MainWindow(_MainWindowBase):
    """Main OpenWoPan window aligned with the sibling Fluent desktop client."""

    login_required = Signal(str)
    _download_event_signal = Signal(object)
    logout_requested = Signal()

    def __init__(
        self,
        file_browser: FileBrowserBackend | None = None,
        *,
        settings: AppSettings | None = None,
        settings_path: Path | None = None,
        log_path: Path | None = None,
        transfer_record_store: TransferRecordStore | None = None,
    ) -> None:
        super().__init__()
        self._file_browser = file_browser
        self._auth_session: AuthSession | None = None
        self._cloud_usage: WopanCloudUsage | None = None
        self._settings = settings or AppSettings()
        self._breadcrumb: list[BreadcrumbEntry] = [
            BreadcrumbEntry(item_id=ROOT_DIRECTORY_ID, name=ROOT_DISPLAY_NAME)
        ]
        self._items: list[WopanItem] = []
        self._status_message = "请先登录"
        self._directory_thread: QThread | None = None
        self._directory_worker: BrowserOperationWorker | None = None
        self._directory_parent_id: str | None = None
        self._directory_refresh_pending = False
        self._after_refresh: tuple[str, Callable[[list[WopanItem], bool], None]] | None = None
        self._create_thread: QThread | None = None
        self._create_worker: BrowserOperationWorker | None = None
        self._create_parent_id: str | None = None
        self._create_folder_name: str | None = None
        self._rename_thread: QThread | None = None
        self._rename_worker: BrowserOperationWorker | None = None
        self._delete_thread: QThread | None = None
        self._delete_worker: BrowserOperationWorker | None = None
        self._move_thread: QThread | None = None
        self._move_worker: BrowserOperationWorker | None = None
        self._search_window: SearchResultsWindow | None = None
        self._tree_sync_thread: QThread | None = None
        self._tree_sync_worker: BrowserOperationWorker | None = None
        self._tree_sync_pending = False
        self._copy_thread: QThread | None = None
        self._copy_worker: BrowserOperationWorker | None = None
        self._recycle_list_thread: QThread | None = None
        self._recycle_list_worker: BrowserOperationWorker | None = None
        self._recycle_action_thread: QThread | None = None
        self._recycle_action_worker: BrowserOperationWorker | None = None
        # 回收站发生过影响云盘内容的操作后置位；切回文件页时消费一次完整刷新
        self._recycle_dirty = False
        self._target_dialog: TargetFolderDialog | None = None
        self._target_load_thread: QThread | None = None
        self._target_load_worker: BrowserOperationWorker | None = None
        self._target_create_thread: QThread | None = None
        self._target_create_worker: BrowserOperationWorker | None = None
        self._transfer_check_thread: QThread | None = None
        self._transfer_check_worker: BrowserOperationWorker | None = None
        self._transfer_pending: tuple[tuple[WopanItem, ...], str, TransferMode] | None = None
        self._usage_thread: QThread | None = None
        self._usage_worker: BrowserOperationWorker | None = None
        self._download_thread: QThread | None = None
        self._download_worker: DownloadWorker | None = None
        self._download_item: WopanItem | None = None
        self._download_task_id: str | None = None
        self._download_controls: dict[str, DownloadTaskControl] = {}
        self._download_items_by_task: dict[str, WopanItem] = {}
        self._pending_download_events: dict[str, DownloadTaskEvent] = {}
        self._removed_download_task_ids: set[str] = set()
        self._download_target_thread: QThread | None = None
        self._download_target_worker: BrowserOperationWorker | None = None
        self._download_target_pending: list[tuple[list[tuple[WopanItem, Path]], bool, bool]] = []
        self._download_submit_thread: QThread | None = None
        self._download_submit_worker: BrowserOperationWorker | None = None
        self._download_submit_threads: set[QThread] = set()
        self._download_submit_workers: dict[QThread, BrowserOperationWorker] = {}
        self._download_submit_paths: dict[QThread, set[Path]] = {}
        self._download_reserved_targets: set[Path] = set()
        self._download_recovery_thread: QThread | None = None
        self._upload_recovery_thread: QThread | None = None
        self._upload_recovery_worker: BrowserOperationWorker | None = None
        self._download_recovery_worker: BrowserOperationWorker | None = None
        self._download_close_thread: QThread | None = None
        self._download_close_worker: BrowserOperationWorker | None = None
        self._download_operation_thread: QThread | None = None
        self._download_operation_worker: BrowserOperationWorker | None = None
        self._download_operations: list[tuple[str, str, Callable[[], object]]] = []
        self._upload_thread: QThread | None = None
        self._upload_worker: UploadWorker | None = None
        self._upload_path: Path | None = None
        self._upload_task_id: str | None = None
        self._upload_threads: dict[str, QThread] = {}
        self._upload_workers: dict[str, UploadWorker] = {}
        self._upload_pending: list[PendingUploadTask] = []
        self._paused_uploads: dict[str, PendingUploadTask] = {}
        self._upload_removal_requested: set[str] = set()
        self._upload_delete_batch = False
        self._folder_prepare_thread: QThread | None = None
        self._folder_prepare_worker: BrowserOperationWorker | None = None
        self._folder_prepare_cancel: threading.Event | None = None
        self._folder_upload_record_id: str | None = None
        self._folder_upload_queue: list[QueuedUploadFile] = []
        self._folder_upload_active: QueuedUploadFile | None = None
        self._folder_upload_child_ids: set[str] = set()
        self._folder_upload_failed_ids: set[str] = set()
        self._folder_upload_success_count = 0
        self._folder_upload_failure_count = 0
        self._folder_upload_cancel_count = 0
        self._folder_upload_target_dir_id: str | None = None
        self._folder_prepare_pending: list[PendingFolderUpload] = []
        self._closing = False
        self._transfer_sequence = 0
        self._transfer_history: TransferRecordPersistence | None = None
        self._transfer_history_thread: QThread | None = None
        self._transfer_history_worker: BrowserOperationWorker | None = None
        self._transfer_history_loaded = False
        self._scan_thread: QThread | None = None
        self._scan_worker: UploadScanWorker | None = None
        self._upload_scan_pending: list[tuple[tuple[Path, ...], str]] = []
        self._upload_scan_parent_id: str | None = None
        self._upload_conflict_thread: QThread | None = None
        self._upload_conflict_worker: BrowserOperationWorker | None = None
        self._upload_conflict_pending: list[
            tuple[tuple[Path, ...], str, bool, UploadBatchSummary | None]
        ] = []
        self._upload_conflict_dialog_open = False
        self._upload_conflict_current: (
            tuple[tuple[Path, ...], str, bool, UploadBatchSummary | None] | None
        ) = None

        self.setWindowTitle("OpenWoPan")
        self.resize(*MAIN_WINDOW_DEFAULT_SIZE)
        self.setMinimumSize(*MAIN_WINDOW_MINIMUM_SIZE)

        self.file_interface = FileInterface(self)
        if transfer_record_store is not None:
            # Local import: app.transfer_history imports TransferRecord from this
            # module, so a module-level import would be circular.
            from openwopan.app.transfer_history import TransferHistoryAdapter

            self._transfer_history = TransferHistoryAdapter(transfer_record_store)
        self.transfer_interface = TransferInterface(self, persistence=self._transfer_history)
        self.account_interface = AccountInterface(self)
        self.setting_interface = SettingsInterface(
            self._settings,
            settings_path=settings_path,
            log_path=log_path,
            parent=self,
        )
        self.setting_interface.settings_changed.connect(self._on_settings_changed)
        self.account_interface.refresh_all_requested.connect(self.refresh_all_information)
        self.account_interface.logout_requested.connect(self.prompt_logout)
        self.transfer_interface.remove_records_requested.connect(self._remove_transfer_records)
        self.transfer_interface.open_download_folder_requested.connect(
            self._open_transfer_download_folder
        )
        self.transfer_interface.pause_download_requested.connect(self._pause_download_task)
        self.transfer_interface.resume_download_requested.connect(self._resume_download_task)
        self.transfer_interface.cancel_download_requested.connect(self._cancel_download_task)
        self.transfer_interface.pause_uploads_requested.connect(self._pause_selected_uploads)
        self.transfer_interface.resume_uploads_requested.connect(self._resume_selected_uploads)
        self.transfer_interface.pause_downloads_requested.connect(self._pause_selected_downloads)
        self.transfer_interface.resume_downloads_requested.connect(self._resume_selected_downloads)
        self.transfer_interface.retry_upload_requested.connect(self._retry_selected_uploads)
        self.recycle_interface = RecycleInterface(self)
        self.recycle_interface.refresh_requested.connect(self.refresh_recycle_items)
        self.recycle_interface.restore_requested.connect(self._restore_recycle_items)
        self.recycle_interface.purge_requested.connect(self._purge_recycle_items)
        self.recycle_interface.empty_requested.connect(self._empty_recycle_bin)
        self._download_event_signal.connect(self._on_download_event)

        self._stacked_widget: QStackedWidget | None = None
        self._navigation_interface: NavigationInterface | None = None
        self._init_navigation_shell()

        self._render_items()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Stop active background work before the window destroys its threads."""
        # Cooperative-stop and quit every running thread first, then wait for
        # all of them against one shared budget — waiting per thread in turn
        # would stack each THREAD_JOIN_TIMEOUT_MS on a pathological close.
        running: list[tuple[QThread, str, str | None]] = []
        self._closing = True
        self.transfer_interface.stop_speed_sampler()
        self.transfer_interface.stop_record_persistence()
        if self._folder_prepare_cancel is not None:
            self._folder_prepare_cancel.set()
        close_downloads = getattr(self._file_browser, "close_downloads", None)
        if callable(close_downloads):
            close_thread = QThread(self)
            close_worker = BrowserOperationWorker(lambda: close_downloads(wait=False))
            close_worker.moveToThread(close_thread)
            close_thread.started.connect(close_worker.run)
            close_worker.succeeded.connect(close_thread.quit)
            close_worker.failed.connect(close_thread.quit)
            close_worker.login_required.connect(close_thread.quit)
            close_thread.finished.connect(self._clear_download_close)
            self._download_close_thread = close_thread
            self._download_close_worker = close_worker
            close_thread.start()
        self._download_target_pending.clear()
        self._download_operations.clear()
        self._download_reserved_targets.clear()
        self._upload_conflict_pending.clear()
        self._upload_scan_pending.clear()
        for folder_pending in self._folder_prepare_pending:
            self.transfer_interface.update_record(
                "upload", folder_pending.record_id, status="已取消"
            )
        for paused_task_id in self._paused_uploads:
            self.transfer_interface.update_record("upload", paused_task_id, status="已取消")
        self._paused_uploads.clear()
        self._folder_prepare_pending.clear()
        for queued in self._folder_upload_queue:
            self.transfer_interface.update_record("upload", queued.task_id, status="已取消")
        self._folder_upload_queue.clear()
        if self._folder_upload_record_id is not None:
            self.transfer_interface.update_record(
                "upload", self._folder_upload_record_id, status="已取消"
            )
        for pending in self._upload_pending:
            self.transfer_interface.update_record("upload", pending.task_id, status="已取消")
        self._upload_pending.clear()
        thread_entries: list[tuple[QThread | None, str, str | None]] = [
            (self._directory_thread, "directory", None),
            (self._create_thread, "create", None),
            (self._rename_thread, "rename", None),
            (self._delete_thread, "delete", None),
            (self._move_thread, "move", None),
            (self._tree_sync_thread, "tree_sync", None),
            (self._copy_thread, "copy", None),
            (self._recycle_list_thread, "recycle_list", None),
            (self._recycle_action_thread, "recycle_action", None),
            (self._target_load_thread, "target_load", None),
            (self._target_create_thread, "target_create", None),
            (self._transfer_check_thread, "transfer_check", None),
            (self._usage_thread, "usage", None),
            (self._download_thread, "download", None),
            (self._download_target_thread, "download_target", None),
            (self._download_recovery_thread, "download_recovery", None),
            (self._upload_recovery_thread, "upload_recovery", None),
            (self._transfer_history_thread, "transfer_history", None),
            (self._download_close_thread, "download_close", None),
            (self._download_operation_thread, "download_operation", None),
            (self._scan_thread, "upload_scan", None),
            (self._upload_conflict_thread, "upload_conflict_check", None),
            (self._folder_prepare_thread, "folder_upload", None),
        ]
        thread_entries.extend(
            (thread, "download_submit", None) for thread in self._download_submit_threads
        )
        upload_threads: list[tuple[QThread, str, str | None]] = [
            (thread, "upload", task_id) for task_id, thread in self._upload_threads.items()
        ]
        if not upload_threads and self._upload_thread is not None:
            upload_threads.append((self._upload_thread, "upload", self._upload_task_id))
        thread_entries.extend(upload_threads)
        for thread, direction, upload_task_id in thread_entries:
            if thread is None or not thread.isRunning():
                continue
            if direction == "download":
                task_id = self._download_task_id
                if task_id is not None:
                    control = self._download_controls.get(task_id)
                    if control is not None:
                        control.request_cancel()
            elif direction == "upload":
                task_id = upload_task_id
                worker = self._upload_workers.get(task_id) if task_id is not None else None
                if worker is not None:
                    worker.request_cancel()
            else:
                task_id = None
            thread.quit()
            running.append((thread, direction, task_id))

        join_deadline = time.monotonic() + THREAD_JOIN_TIMEOUT_MS / 1000
        for thread, direction, task_id in running:
            if thread.isRunning():
                remaining_ms = max(
                    0,
                    math.ceil((join_deadline - time.monotonic()) * 1000),
                )
                if not thread.wait(remaining_ms):
                    self._abandon_thread_after_timeout(thread, direction, task_id)

        super().closeEvent(event)

    @staticmethod
    def _abandon_thread_after_timeout(thread: QThread, direction: str, task_id: str | None) -> None:
        # Detach so window destruction cannot delete a running QThread (qFatal),
        # and keep a reference so Python GC cannot either. No terminate(): it
        # can kill the thread mid-bytecode holding the GIL and deadlock.
        thread.setParent(None)
        _THREAD_KEEP_ALIVE.add(thread)
        thread.finished.connect(lambda kept=thread: _THREAD_KEEP_ALIVE.discard(kept))
        LOGGER.warning(
            "main_window.close.thread_abandoned direction=%s task_id=%s join_timeout_ms=%s",
            direction,
            task_id,
            THREAD_JOIN_TIMEOUT_MS,
        )

    def _add_sub_interface(
        self,
        widget: QWidget,
        route_key: str,
        icon: FluentIcon,
        text: str,
        *,
        position: NavigationItemPosition = NavigationItemPosition.TOP,
    ) -> None:
        if isinstance(self, FluentWindow):  # pragma: no cover - docs/testing-exemptions.md
            widget.setObjectName(route_key)
            self.addSubInterface(widget, icon, text, position=position)
            return

        if self._stacked_widget is None or self._navigation_interface is None:
            raise RuntimeError("fallback navigation shell is not initialized")  # pragma: no cover
        self._stacked_widget.addWidget(widget)
        self._navigation_interface.addItem(
            routeKey=route_key,
            icon=icon,
            text=text,
            onClick=lambda target=widget, key=route_key: self._switch_to_interface(target, key),
            position=position,
        )

    def _init_navigation_shell(self) -> None:
        if isinstance(self, FluentWindow):  # pragma: no cover - docs/testing-exemptions.md
            nav = self.navigationInterface
            nav.setExpandWidth(120)
            nav.setMinimumExpandWidth(0)
            nav.setCollapsible(False)
            nav.setMenuButtonVisible(False)
            self._add_sub_interface(self.file_interface, "files", FIF.FOLDER, "文件")
            self._add_sub_interface(self.transfer_interface, "transfers", FIF.SYNC, "传输")
            self._add_sub_interface(self.recycle_interface, "recycle", FIF.DELETE, "回收站")
            self._add_sub_interface(
                self.account_interface,
                "account",
                FIF.CLOUD,
                "账户",
                position=NavigationItemPosition.BOTTOM,
            )
            self._add_sub_interface(
                self.setting_interface,
                "settings",
                FIF.SETTING,
                "设置",
                position=NavigationItemPosition.BOTTOM,
            )
            self.stackedWidget.setCurrentWidget(self.file_interface)
            self.navigationInterface.setCurrentItem("files")
            self.stackedWidget.currentChanged.connect(self._on_navigation_page_changed)
            return

        self._stacked_widget = QStackedWidget(self)
        self._navigation_interface = NavigationInterface(
            self,
            showMenuButton=False,
            showReturnButton=False,
            collapsible=False,
        )
        self._navigation_interface.setExpandWidth(120)
        self._navigation_interface.setMinimumExpandWidth(0)
        self._add_sub_interface(self.file_interface, "files", FIF.FOLDER, "文件")
        self._add_sub_interface(self.transfer_interface, "transfers", FIF.SYNC, "传输")
        self._add_sub_interface(self.recycle_interface, "recycle", FIF.DELETE, "回收站")
        self._add_sub_interface(
            self.account_interface,
            "account",
            FIF.CLOUD,
            "账户",
            position=NavigationItemPosition.BOTTOM,
        )
        self._add_sub_interface(
            self.setting_interface,
            "settings",
            FIF.SETTING,
            "设置",
            position=NavigationItemPosition.BOTTOM,
        )

        central = QWidget(self)
        central_layout = QHBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self._navigation_interface)
        central_layout.addWidget(self._stacked_widget, 1)
        self.setCentralWidget(central)
        self._navigation_interface.setCurrentItem("files")
        self._stacked_widget.setCurrentWidget(self.file_interface)
        self._stacked_widget.currentChanged.connect(self._on_navigation_page_changed)

    def _on_navigation_page_changed(self, _index: int) -> None:
        """Reload the visible page's data on navigation.

        Recycle page: reload the bin. File page: if a recycle-bin mutation
        happened since the last visit, consume the dirty flag once and run a
        full refresh (list + cloud usage).
        """
        if not self.recycle_interface.isHidden():
            self.refresh_recycle_items()
            return
        if not self.file_interface.isHidden() and self._recycle_dirty:
            self._recycle_dirty = False
            self.refresh_all_information()

    def _switch_to_interface(self, widget: QWidget, route_key: str) -> None:
        if self._stacked_widget is None or self._navigation_interface is None:
            return
        self._stacked_widget.setCurrentWidget(widget)
        self._navigation_interface.setCurrentItem(route_key)

    def refresh_root(self) -> None:
        """Reset to the root directory and reload."""
        self._breadcrumb = [BreadcrumbEntry(item_id=ROOT_DIRECTORY_ID, name=ROOT_DISPLAY_NAME)]
        self.refresh_current_directory()

    def go_up_one_level(self) -> None:
        """Navigate to the parent breadcrumb entry."""
        if len(self._breadcrumb) <= 1:
            return
        self.open_breadcrumb_index(len(self._breadcrumb) - 2)

    def set_file_browser(self, file_browser: FileBrowserBackend) -> None:
        """Attach a UI-safe file browser backend and load the root directory."""
        self._file_browser = file_browser
        set_callback = getattr(file_browser, "set_download_event_callback", None)
        if callable(set_callback):
            set_callback(self._receive_download_event)
            self._load_transfer_history()
            self._recover_downloads()
        else:
            self._load_transfer_history()
            self._load_persisted_download_records()
        self._recover_uploads()
        self.refresh_root()
        self.refresh_cloud_usage()

    def _load_transfer_history(self) -> None:
        """Load persisted transfer history once per session on a worker thread."""
        if (
            self._transfer_history is None
            or self._transfer_history_loaded
            or self._transfer_history_thread is not None
        ):
            return
        self._transfer_history_loaded = True
        thread = QThread(self)
        worker = BrowserOperationWorker(self._transfer_history.load_history)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_transfer_history_loaded)
        worker.failed.connect(self._on_transfer_history_failed)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._clear_transfer_history)
        self._transfer_history_thread = thread
        self._transfer_history_worker = worker
        thread.start()

    def _on_transfer_history_loaded(self, result: object) -> None:
        if not isinstance(result, tuple) or self._closing:
            return
        uploads: list[TransferRecord] = []
        downloads: list[TransferRecord] = []
        for record in result:
            if not isinstance(record, TransferRecord):
                continue
            if self.transfer_interface._find_record(record.direction, record.task_id) is not None:
                # Same id is already alive this session (user-created race): the
                # restored row is dropped and its database row deleted; the live
                # record re-upserts its own row right away so its history is not
                # lost (design.md 4).
                if self._transfer_history is not None:
                    self._transfer_history.delete_records(record.direction, [record.task_id])
                    live_record = self.transfer_interface._find_record(
                        record.direction, record.task_id
                    )
                    if live_record is not None:
                        self.transfer_interface._save_record(live_record)
                LOGGER.warning(
                    "main_window.transfer_history.conflict_dropped direction=%s task_id=%s",
                    record.direction,
                    record.task_id,
                )
                continue
            self._raise_transfer_sequence(record.direction, record.task_id)
            if record.direction == "upload":
                uploads.append(record)
            else:
                downloads.append(record)
        for record in uploads:
            self.transfer_interface.add_upload_record(record, render=False, persist=False)
        for record in downloads:
            self.transfer_interface.add_download_record(record, render=False, persist=False)
        if uploads:
            self.transfer_interface._render_upload_table()
        if downloads:
            self.transfer_interface._render_download_table()

    def _raise_transfer_sequence(self, direction: str, task_id: str) -> None:
        """Lift the id sequence above restored numeric ids to avoid collisions."""
        match = re.fullmatch(rf"{direction}-(\d+)", task_id)
        if match is None:
            return
        number = int(match.group(1))
        if number > self._transfer_sequence:
            self._transfer_sequence = number

    def _on_transfer_history_failed(self, message: str) -> None:
        LOGGER.warning("main_window.transfer_history.load_failed error=%s", message)

    def _clear_transfer_history(self) -> None:
        self._delete_finished_thread()
        self._transfer_history_thread = None
        self._transfer_history_worker = None

    def set_auth_session(self, session: AuthSession) -> None:
        """Attach a safe authenticated-session summary to the UI."""
        self._auth_session = session
        self.account_interface.set_session(session)
        title_name = session.display_name or _mask_account_id(session.account_id)
        self.setWindowTitle(f"OpenWoPan - {title_name}")

    def clear_auth_session(self) -> None:
        """Clear account state from the UI."""
        self._auth_session = None
        self._cloud_usage = None
        self._recycle_dirty = False
        self.account_interface.set_session(None)
        self.account_interface.set_usage(None)
        self.file_interface.set_storage_usage(None)
        self.setWindowTitle("OpenWoPan")

    def auth_session(self) -> AuthSession | None:
        """Return the current safe session summary."""
        return self._auth_session

    def refresh_cloud_usage(self) -> None:
        """Refresh account cloud usage from the application service."""
        if self._file_browser is None or self._auth_session is None:
            self._cloud_usage = None
            self.account_interface.set_usage(None)
            self.file_interface.set_storage_usage(None)
            return
        if self._usage_thread is not None:
            LOGGER.debug("main_window.cloud_usage.refresh.skipped_busy")
            return
        LOGGER.info("main_window.cloud_usage.refresh.start")
        account_id = self._auth_session.account_id
        file_browser = self._file_browser
        thread = QThread(self)
        worker = BrowserOperationWorker(lambda: file_browser.get_cloud_usage(account_id))
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_cloud_usage_succeeded)
        worker.failed.connect(self._on_cloud_usage_failed)
        worker.login_required.connect(self._on_cloud_usage_login_required)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.login_required.connect(thread.quit)
        thread.finished.connect(self._clear_cloud_usage)

        self._usage_thread = thread
        self._usage_worker = worker
        thread.start()

    def _on_cloud_usage_succeeded(self, result: object) -> None:
        usage = cast(WopanCloudUsage, result)
        self._cloud_usage = usage
        self.account_interface.set_usage(usage)
        self.file_interface.set_storage_usage(usage)
        self._set_status("空间信息已刷新")
        LOGGER.info(
            "main_window.cloud_usage.refresh.success used_bytes=%s total_bytes=%s",
            usage.used_bytes,
            usage.total_bytes,
        )

    def _on_cloud_usage_failed(self, message: str) -> None:
        LOGGER.warning("main_window.cloud_usage.refresh.failed error=%s", message)
        self._set_status(f"空间信息刷新失败：{message}")
        InfoBar.warning(title="空间信息刷新失败", content=message, parent=self)

    def _on_cloud_usage_login_required(self, message: str) -> None:
        LOGGER.info("main_window.cloud_usage.login_required")
        self._show_login_required_error(message)

    def _delete_finished_thread(self) -> None:
        thread = self.sender()
        if isinstance(thread, QThread):
            thread.deleteLater()

    def _clear_download_close(self) -> None:
        self._delete_finished_thread()
        self._download_close_thread = None
        self._download_close_worker = None

    def _clear_cloud_usage(self) -> None:
        self._delete_finished_thread()
        self._usage_thread = None
        self._usage_worker = None

    def refresh_all_information(self) -> None:
        """Reload account-side information and the currently opened directory."""
        LOGGER.info("main_window.refresh_all.start")
        self.refresh_cloud_usage()
        self.refresh_current_directory()
        LOGGER.info("main_window.refresh_all.complete")

    def prompt_logout(self) -> None:
        """Confirm logout before returning to the login flow."""
        message = MessageBox("退出登录", "确定要退出当前账号并返回登录页吗？", self)
        accepted = message.exec()
        message.deleteLater()
        if accepted:
            self.logout_current_session()

    def logout_current_session(self) -> None:
        """Request application-level logout orchestration."""
        LOGGER.info("main_window.logout.requested has_session=%s", self._auth_session is not None)
        self.logout_requested.emit()

    def refresh_current_directory(
        self, after: Callable[[list[WopanItem], bool], None] | None = None
    ) -> None:
        """Load the current directory from the application file browser service.

        ``after`` runs on the GUI thread with the fresh item list once the
        refresh lands (including a trailing refresh after a busy skip); it is
        dropped when the refresh fails. The callback also receives
        ``still_current`` — False when the user navigated away after the
        refresh was requested, so directory-dependent checks must be skipped.
        Use this instead of reading ``_items`` right after this call — the
        refresh is asynchronous.
        """
        if self._file_browser is None:
            self._items = []
            self._set_status("请先登录")
            self._render_items()
            return
        if after is not None:
            self._after_refresh = (self.current_directory_id(), after)
        if self._directory_thread is not None:
            # A refresh is in flight; remember the latest intent so navigation
            # during loading still lands on the current breadcrumb target.
            self._directory_refresh_pending = True
            LOGGER.debug("main_window.refresh.skipped_busy")
            return

        parent_id = self.current_directory_id()
        LOGGER.info("main_window.refresh.start parent_id=%s", parent_id)
        self._set_status("正在加载...")

        file_browser = self._file_browser
        thread = QThread(self)
        worker = BrowserOperationWorker(lambda: file_browser.list_directory(parent_id))
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_directory_refresh_succeeded)
        worker.failed.connect(self._on_directory_refresh_failed)
        worker.login_required.connect(self._on_directory_refresh_login_required)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.login_required.connect(thread.quit)
        thread.finished.connect(self._clear_directory_refresh)

        self._directory_thread = thread
        self._directory_worker = worker
        self._directory_parent_id = parent_id
        thread.start()

    def _on_directory_refresh_succeeded(self, result: object) -> None:
        self._items = cast(list[WopanItem], result)
        LOGGER.info(
            "main_window.refresh.success parent_id=%s item_count=%s",
            self._directory_parent_id,
            len(self._items),
        )
        self._render_items()
        entry = self._after_refresh
        self._after_refresh = None
        if entry is not None:
            requested_parent_id, after = entry
            after(self._items, self.current_directory_id() == requested_parent_id)
        self._sync_folder_tree()

    def _sync_folder_tree(self) -> None:
        """Rebuild the navigation tree expanded along the current path."""
        file_browser = self._file_browser
        if file_browser is None:
            return
        if self._tree_sync_thread is not None:
            # A tree sync is in flight; remember the latest intent so the
            # pending request is replayed once it clears instead of being
            # dropped (trailing semantics, mirroring the directory refresh).
            self._tree_sync_pending = True
            LOGGER.debug("main_window.tree_sync.skipped_busy")
            return
        breadcrumb = tuple(self._breadcrumb)
        current_folders = tuple(item for item in self._items if item.kind is WopanItemKind.FOLDER)
        LOGGER.info("main_window.tree_sync.start depth=%s", len(breadcrumb))

        def operation() -> list[tuple[WopanItem, ...]]:
            levels: list[tuple[WopanItem, ...]] = []
            for entry in breadcrumb:
                if entry.item_id == breadcrumb[-1].item_id:
                    levels.append(current_folders)
                    continue
                listing = file_browser.list_directory(entry.item_id)
                levels.append(tuple(item for item in listing if item.kind is WopanItemKind.FOLDER))
            return levels

        thread = QThread(self)
        worker = BrowserOperationWorker(operation)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_tree_sync_succeeded)
        worker.failed.connect(self._on_tree_sync_failed)
        worker.login_required.connect(self._on_tree_sync_login_required)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.login_required.connect(thread.quit)
        thread.finished.connect(self._clear_tree_sync)
        self._tree_sync_thread = thread
        self._tree_sync_worker = worker
        thread.start()

    def _on_tree_sync_succeeded(self, result: object) -> None:
        if self._closing:
            return
        if not isinstance(result, list) or not all(
            isinstance(level, (list, tuple)) for level in result
        ):
            LOGGER.debug("main_window.tree_sync.invalid_result")
            return
        self.file_interface.render_folder_tree(
            tuple(self._breadcrumb), tuple(tuple(level) for level in result)
        )

    def _on_tree_sync_failed(self, message: str) -> None:
        # The tree is auxiliary navigation; the breadcrumb stays correct, so
        # a failed sync only degrades the pane and must not alarm the user.
        LOGGER.debug("main_window.tree_sync.failed error_length=%s", len(message))

    def _on_tree_sync_login_required(self, message: str) -> None:
        LOGGER.debug("main_window.tree_sync.login_required")

    def _clear_tree_sync(self) -> None:
        self._delete_finished_thread()
        self._tree_sync_thread = None
        self._tree_sync_worker = None
        if self._tree_sync_pending:
            self._tree_sync_pending = False
            self._sync_folder_tree()

    def _on_search_jump_requested(
        self,
        path_ids: tuple[str, ...],
        path_names: tuple[str, ...],
        select_item_id: str | None,
    ) -> None:
        """Jump the file view to the folder and optionally select the item."""
        already_there = [entry.item_id for entry in self._breadcrumb] == list(path_ids)
        if already_there and select_item_id is None:
            return
        if not already_there:
            self._breadcrumb = [
                BreadcrumbEntry(item_id=item_id, name=name)
                for item_id, name in zip(path_ids, path_names, strict=True)
            ]
        if select_item_id is not None:
            # 刷新落地后在 GUI 线程选中目标行（_after_refresh 契约：不读旧列表）；
            # 已在目标目录时同样等待刷新，避免选中跑在列表更新前。
            self.refresh_current_directory(
                after=lambda items, still_current: (
                    self._select_item_row(select_item_id) if still_current else None
                )
            )
        else:
            self.refresh_current_directory()

    def _select_item_row(self, item_id: str) -> None:
        """Select and scroll to the row whose item id matches."""
        for row, item in enumerate(self._items):
            if item.item_id == item_id:
                table = self.file_interface.file_table
                table.selectRow(row)
                table.scrollToItem(
                    table.item(row, 0),
                    QAbstractItemView.ScrollHint.PositionAtCenter,
                )
                self.update_operation_controls()
                return
        self._set_status("目标对象不在当前列表中（可能已被移动或删除）")

    def open_tree_path(self, path_ids: tuple[str, ...], path_names: tuple[str, ...]) -> None:
        """Navigate to the folder identified by a folder-tree node path."""
        if not path_ids or not path_names:
            return
        if [entry.item_id for entry in self._breadcrumb] == list(path_ids):
            return
        self._breadcrumb = [
            BreadcrumbEntry(item_id=item_id, name=name)
            for item_id, name in zip(path_ids, path_names, strict=True)
        ]
        self.refresh_current_directory()

    def _on_directory_refresh_failed(self, message: str) -> None:
        self._items = []
        self._after_refresh = None
        LOGGER.warning(
            "main_window.refresh.failed parent_id=%s error=%s",
            self._directory_parent_id,
            message,
        )
        self._set_status(f"加载失败：{message}")
        InfoBar.error(title="加载失败", content=message, parent=self)
        self._render_items()

    def _on_directory_refresh_login_required(self, message: str) -> None:
        self._items = []
        self._after_refresh = None
        LOGGER.info(
            "main_window.refresh.login_required parent_id=%s",
            self._directory_parent_id,
        )
        self._set_status(message)
        self.login_required.emit(message)
        self._render_items()

    def _clear_directory_refresh(self) -> None:
        self._delete_finished_thread()
        self._directory_thread = None
        self._directory_worker = None
        self._directory_parent_id = None
        if self._directory_refresh_pending:
            self._directory_refresh_pending = False
            self.refresh_current_directory()

    def request_search(self) -> None:
        """Run a global search with the keyword from the top search bar."""
        keyword = self.file_interface.search_bar.text().strip()
        if not keyword:
            return
        if self._file_browser is None:
            self._set_status("请先登录")
            return
        self._show_search_window(keyword)

    def _show_search_window(self, keyword: str) -> None:
        """Open (or raise) the standalone search window and run the search."""
        file_browser = self._file_browser
        if file_browser is None:
            self._set_status("请先登录")
            return
        if self._search_window is None:
            self._search_window = SearchResultsWindow(
                self._search_callable,
                self._resolve_directory_path_callable,
                parent=self,
            )
            self._search_window.jump_requested.connect(self._on_search_jump_requested)
            self._search_window.download_requested.connect(self.download_search_item)
        self._search_window.show()
        self._search_window.raise_()
        self._search_window.activateWindow()
        self._search_window.start_search(keyword)

    def _search_callable(self, keyword: str, page_no: int, page_size: int) -> list[WopanItem]:
        file_browser = self._file_browser
        if file_browser is None:
            raise FileBrowserError("请先登录")
        return file_browser.search_files(keyword, page_no, page_size)

    def _resolve_directory_path_callable(self, directory_id: str) -> list[tuple[str, str]]:
        file_browser = self._file_browser
        if file_browser is None:
            raise FileBrowserError("请先登录")
        return file_browser.resolve_directory_path(directory_id)

    def download_search_item(self, item: WopanItem) -> None:
        """Download one file handed over from the search results window."""
        if self._file_browser is None:
            self._set_status("请先登录")
            return
        if item.kind is not WopanItemKind.FILE or not item.download_id:
            self._set_status("只能下载文件")
            InfoBar.warning(title="下载", content="只能下载文件", parent=self)
            return
        if self._settings.ask_download_location:
            path_text, _selected_filter = QFileDialog.getSaveFileName(self, "保存文件", item.name)
            if not path_text:
                return
            paths = [(item, Path(path_text))]
            self._submit_download_items(paths, automatic=False)
            return
        folder = self._settings.default_download_path
        paths = [(item, folder / _safe_local_file_name(item.name))]
        self._submit_download_items(paths, automatic=True)

    def create_folder_with_name(self, name: str) -> None:
        """Create a folder in the current directory."""
        requested_name = name.strip()
        if not requested_name:
            self._set_status("文件夹名称不能为空")
            InfoBar.warning(title="新建文件夹", content="文件夹名称不能为空", parent=self)
            return
        if self._file_browser is None:
            self._set_status("请先登录")
            return

        if self._create_thread is not None:
            LOGGER.debug("main_window.create_folder.skipped_busy")
            return
        parent_id = self.current_directory_id()
        folder_name = _next_available_name(
            requested_name,
            existing_names={item.name for item in self._items},
        )
        LOGGER.info(
            "main_window.create_folder.start parent_id=%s name_length=%s renamed=%s",
            parent_id,
            len(folder_name),
            folder_name != requested_name,
        )
        file_browser = self._file_browser
        thread = QThread(self)
        worker = BrowserOperationWorker(lambda: file_browser.create_folder(parent_id, folder_name))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_create_folder_succeeded)
        worker.failed.connect(self._on_create_folder_failed)
        worker.login_required.connect(self._on_create_folder_login_required)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.login_required.connect(thread.quit)
        thread.finished.connect(self._clear_create_folder)
        self._create_thread = thread
        self._create_worker = worker
        self._create_parent_id = parent_id
        self._create_folder_name = folder_name
        thread.start()

    def _on_create_folder_succeeded(self, result: object) -> None:
        created_item = cast(WopanItem, result)
        folder_name = self._create_folder_name or created_item.name
        LOGGER.info("main_window.create_folder.success item_id=%s", created_item.item_id)
        self.refresh_current_directory(
            after=lambda items, still_current: self._report_create_visibility(
                created_item, folder_name, still_current
            )
        )

    def _report_create_visibility(
        self, created_item: WopanItem, folder_name: str, still_current: bool
    ) -> None:
        if still_current and not any(item.item_id == created_item.item_id for item in self._items):
            LOGGER.warning(
                "main_window.create_folder.not_visible_after_refresh item_id=%s parent_id=%s",
                created_item.item_id,
                created_item.parent_id,
            )
            self._set_status(f"已创建「{folder_name}」，但刷新后未在当前目录看到，请稍后再刷新")
        else:
            InfoBar.success(title="创建成功", content=f"已创建「{folder_name}」", parent=self)

    def _on_create_folder_failed(self, message: str) -> None:
        LOGGER.warning("main_window.create_folder.failed error=%s", message)
        self._set_status(f"新建文件夹失败：{message}")
        InfoBar.error(title="新建文件夹失败", content=message, parent=self)

    def _on_create_folder_login_required(self, message: str) -> None:
        self._show_login_required_error(message)

    def _clear_create_folder(self) -> None:
        self._delete_finished_thread()
        self._create_thread = None
        self._create_worker = None
        self._create_parent_id = None
        self._create_folder_name = None

    def rename_displayed_item(self, row: int, new_name: str) -> None:
        """Rename a displayed file or folder row."""
        item = self._item_at_row(row)
        if item is None:
            return
        item_name = new_name.strip()
        if not item_name:
            self._set_status("名称不能为空")
            InfoBar.warning(title="重命名", content="名称不能为空", parent=self)
            return
        if self._file_browser is None:
            self._set_status("请先登录")
            return

        if self._rename_thread is not None:
            LOGGER.debug("main_window.rename.skipped_busy")
            return
        file_browser = self._file_browser
        thread = QThread(self)
        worker = BrowserOperationWorker(lambda: file_browser.rename_item(item, item_name))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_rename_succeeded)
        worker.failed.connect(self._on_rename_failed)
        worker.login_required.connect(self._on_rename_login_required)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.login_required.connect(thread.quit)
        thread.finished.connect(self._clear_rename)
        self._rename_thread = thread
        self._rename_worker = worker
        thread.start()

    def _on_rename_succeeded(self, result: object) -> None:
        self.refresh_current_directory()

    def _on_rename_failed(self, message: str) -> None:
        self._set_status(f"重命名失败：{message}")
        InfoBar.error(title="重命名失败", content=message, parent=self)

    def _on_rename_login_required(self, message: str) -> None:
        self._show_login_required_error(message)

    def _clear_rename(self) -> None:
        self._delete_finished_thread()
        self._rename_thread = None
        self._rename_worker = None

    def delete_displayed_item(self, row: int) -> None:
        """Delete a displayed file or folder row."""
        self.delete_displayed_items([row])

    def delete_displayed_items(self, rows: Sequence[int]) -> None:
        """Delete one or more displayed rows in a single batch request."""
        items = [item for row in rows if (item := self._item_at_row(row)) is not None]
        if not items:
            return
        if self._file_browser is None:
            self._set_status("请先登录")
            return

        if self._delete_thread is not None:
            LOGGER.debug("main_window.delete.skipped_busy")
            InfoBar.warning(title="删除", content="已有删除任务进行中，请稍候", parent=self)
            return
        file_browser = self._file_browser
        batch = tuple(items)
        self.file_interface.set_operation_busy(True)
        thread = QThread(self)
        worker = BrowserOperationWorker(lambda: file_browser.delete_items(batch))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_delete_succeeded)
        worker.failed.connect(self._on_delete_failed)
        worker.login_required.connect(self._on_delete_login_required)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.login_required.connect(thread.quit)
        thread.finished.connect(self._clear_delete)
        self._delete_thread = thread
        self._delete_worker = worker
        thread.start()

    def _on_delete_succeeded(self, result: object) -> None:
        self.refresh_current_directory()

    def _on_delete_failed(self, message: str) -> None:
        self._set_status(f"删除失败：{message}")
        InfoBar.error(title="删除失败", content=message, parent=self)

    def _on_delete_login_required(self, message: str) -> None:
        self._show_login_required_error(message)

    def _clear_delete(self) -> None:
        self.file_interface.set_operation_busy(False)
        self._delete_finished_thread()
        self._delete_thread = None
        self._delete_worker = None

    def move_displayed_item(self, row: int, target_parent_id: str) -> None:
        """Move a displayed file or folder row to another directory."""
        self.move_displayed_items([row], target_parent_id)

    def move_displayed_items(self, rows: Sequence[int], target_parent_id: str) -> None:
        """Move one or more displayed rows in a single batch request."""
        items = [item for row in rows if (item := self._item_at_row(row)) is not None]
        if not items:
            return
        target_id = target_parent_id.strip()
        if not target_id:
            self._set_status("目标文件夹不能为空")
            InfoBar.warning(title="移动", content="目标文件夹不能为空", parent=self)
            return
        self._start_move_batch(tuple(items), target_id)

    def copy_displayed_items(self, rows: Sequence[int], target_parent_id: str) -> None:
        """Copy one or more displayed rows in a single batch request."""
        items = [item for row in rows if (item := self._item_at_row(row)) is not None]
        if not items:
            return
        target_id = target_parent_id.strip()
        if not target_id:
            self._set_status("目标文件夹不能为空")
            InfoBar.warning(title="复制", content="目标文件夹不能为空", parent=self)
            return
        self._start_copy_batch(tuple(items), target_id)

    def _start_move_batch(self, items: tuple[WopanItem, ...], target_id: str) -> None:
        file_browser = self._file_browser
        if file_browser is None:
            self._set_status("请先登录")
            return

        if self._move_thread is not None:
            LOGGER.debug("main_window.move.skipped_busy")
            InfoBar.warning(title="移动", content="已有移动任务进行中，请稍候", parent=self)
            return
        self._set_status(f"正在移动 {len(items)} 个项目…")
        self.file_interface.set_operation_busy(True)
        thread = QThread(self)
        worker = BrowserOperationWorker(lambda: file_browser.move_items(items, target_id))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_move_succeeded)
        worker.failed.connect(self._on_move_failed)
        worker.login_required.connect(self._on_move_login_required)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.login_required.connect(thread.quit)
        thread.finished.connect(self._clear_move)
        self._move_thread = thread
        self._move_worker = worker
        thread.start()

    def _start_copy_batch(self, items: tuple[WopanItem, ...], target_id: str) -> None:
        file_browser = self._file_browser
        if file_browser is None:
            self._set_status("请先登录")
            return

        if self._copy_thread is not None:
            LOGGER.debug("main_window.copy.skipped_busy")
            InfoBar.warning(title="复制", content="已有复制任务进行中，请稍候", parent=self)
            return
        self._set_status(f"正在复制 {len(items)} 个项目…")
        self.file_interface.set_operation_busy(True)
        thread = QThread(self)
        worker = BrowserOperationWorker(lambda: file_browser.copy_items(items, target_id))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_copy_succeeded)
        worker.failed.connect(self._on_copy_failed)
        worker.login_required.connect(self._on_copy_login_required)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.login_required.connect(thread.quit)
        thread.finished.connect(self._clear_copy)
        self._copy_thread = thread
        self._copy_worker = worker
        thread.start()

    def _on_move_succeeded(self, result: object) -> None:
        self.refresh_current_directory()

    def _on_move_failed(self, message: str) -> None:
        self._set_status(f"移动失败：{message}")
        InfoBar.error(title="移动失败", content=message, parent=self)

    def _on_move_login_required(self, message: str) -> None:
        self._show_login_required_error(message)

    def _clear_move(self) -> None:
        self.file_interface.set_operation_busy(False)
        self._delete_finished_thread()
        self._move_thread = None
        self._move_worker = None

    def _on_copy_succeeded(self, result: object) -> None:
        self.refresh_current_directory()
        InfoBar.success(title="复制", content="复制完成", parent=self)

    def _on_copy_failed(self, message: str) -> None:
        self._set_status(f"复制失败：{message}")
        InfoBar.error(title="复制失败", content=message, parent=self)

    def _on_copy_login_required(self, message: str) -> None:
        self._show_login_required_error(message)

    def _clear_copy(self) -> None:
        self.file_interface.set_operation_busy(False)
        self._delete_finished_thread()
        self._copy_thread = None
        self._copy_worker = None

    def refresh_recycle_items(self) -> None:
        """Load recycle-bin entries on a worker thread."""
        if self._file_browser is None:
            self._set_status("请先登录")
            return
        if self._recycle_list_thread is not None:
            LOGGER.debug("main_window.recycle_list.skipped_busy")
            return
        file_browser = self._file_browser
        thread = QThread(self)
        worker = BrowserOperationWorker(lambda: file_browser.list_recycle_items())
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_recycle_list_succeeded)
        worker.failed.connect(self._on_recycle_list_failed)
        worker.login_required.connect(self._on_recycle_login_required)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.login_required.connect(thread.quit)
        thread.finished.connect(self._clear_recycle_list)
        self._recycle_list_thread = thread
        self._recycle_list_worker = worker
        thread.start()

    def _on_recycle_list_succeeded(self, result: object) -> None:
        items = cast("list[WopanRecycleItem]", result)
        LOGGER.info("main_window.recycle_list.success item_count=%s", len(items))
        self.recycle_interface.render_items(items)

    def _on_recycle_list_failed(self, message: str) -> None:
        self._set_status(f"回收站加载失败：{message}")
        InfoBar.error(title="回收站加载失败", content=message, parent=self)

    def _on_recycle_login_required(self, message: str) -> None:
        self._show_login_required_error(message)

    def _clear_recycle_list(self) -> None:
        self._delete_finished_thread()
        self._recycle_list_thread = None
        self._recycle_list_worker = None

    def _restore_recycle_items(self, delete_nos: Sequence[str]) -> None:
        """Restore recycle-bin entries back to their original folders."""
        file_browser = self._file_browser
        if file_browser is None:
            self._set_status("请先登录")
            return
        batch = tuple(delete_nos)
        if not batch:
            return
        self._start_recycle_action(
            lambda: file_browser.restore_recycle_items(batch),
            self._on_recycle_restore_succeeded,
            self._on_recycle_restore_failed,
        )

    def _purge_recycle_items(self, delete_nos: Sequence[str]) -> None:
        """Permanently delete recycle-bin entries after UI confirmation."""
        file_browser = self._file_browser
        if file_browser is None:
            self._set_status("请先登录")
            return
        batch = tuple(delete_nos)
        if not batch:
            return
        self._start_recycle_action(
            lambda: file_browser.purge_recycle_items(batch),
            self._on_recycle_purge_succeeded,
            self._on_recycle_purge_failed,
        )

    def _empty_recycle_bin(self) -> None:
        """Permanently delete every recycle-bin entry after UI confirmation."""
        file_browser = self._file_browser
        if file_browser is None:
            self._set_status("请先登录")
            return
        self._start_recycle_action(
            file_browser.empty_recycle_bin,
            self._on_recycle_empty_succeeded,
            self._on_recycle_empty_failed,
        )

    def _start_recycle_action(
        self,
        operation: Callable[[], object],
        on_succeeded: Callable[[object], None],
        on_failed: Callable[[str], None],
    ) -> None:
        """Run one recycle-bin mutation on a worker thread."""
        if self._recycle_action_thread is not None:
            LOGGER.debug("main_window.recycle_action.skipped_busy")
            return
        thread = QThread(self)
        worker = BrowserOperationWorker(operation)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(on_succeeded)
        worker.failed.connect(on_failed)
        worker.login_required.connect(self._on_recycle_login_required)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.login_required.connect(thread.quit)
        thread.finished.connect(self._clear_recycle_action)
        self._recycle_action_thread = thread
        self._recycle_action_worker = worker
        thread.start()

    def _on_recycle_restore_succeeded(self, result: object) -> None:
        self._recycle_dirty = True
        self._set_status("恢复成功，已回到原位置")
        self.refresh_recycle_items()

    def _on_recycle_restore_failed(self, message: str) -> None:
        self._set_status(f"恢复失败：{message}")
        InfoBar.error(title="恢复失败", content=message, parent=self)

    def _on_recycle_purge_succeeded(self, result: object) -> None:
        self._recycle_dirty = True
        self._set_status("已彻底删除，无法恢复")
        self.refresh_recycle_items()

    def _on_recycle_purge_failed(self, message: str) -> None:
        self._set_status(f"彻底删除失败：{message}")
        InfoBar.error(title="彻底删除失败", content=message, parent=self)

    def _on_recycle_empty_succeeded(self, result: object) -> None:
        self._recycle_dirty = True
        self._set_status("回收站已清空")
        self.refresh_recycle_items()

    def _on_recycle_empty_failed(self, message: str) -> None:
        self._set_status(f"清空回收站失败：{message}")
        InfoBar.error(title="清空回收站失败", content=message, parent=self)

    def _clear_recycle_action(self) -> None:
        self._delete_finished_thread()
        self._recycle_action_thread = None
        self._recycle_action_worker = None

    def download_displayed_item(
        self, row: int, local_path: Path, *, run_in_background: bool = True
    ) -> None:
        """Submit one displayed file through the same scheduler path as batches."""
        item = self._item_at_row(row)
        if item is None:
            return
        self._submit_download_items([(item, local_path)], run_in_background=run_in_background)

    def _submit_download_items(
        self,
        items: list[tuple[WopanItem, Path]],
        *,
        run_in_background: bool = True,
        automatic: bool = False,
    ) -> None:
        if self._file_browser is None:
            self._set_status("请先登录")
            return
        valid = []
        for item, path in items:
            if item.kind is not WopanItemKind.FILE:
                self._set_status("只能下载文件")
                return
            if not path.name:
                self._set_status("保存路径不能为空")
                return
            valid.append((item, path))
        if not valid:
            return
        if run_in_background:
            if self._download_target_thread is not None:
                self._download_target_pending.append((valid, automatic, run_in_background))
                return
            self._start_download_target_scan(valid, automatic, run_in_background)
            return
        try:
            names = self._scan_download_target_names(valid, automatic)
        except OSError as exc:
            self._on_download_target_scan_failed(str(exc))
            return
        self._on_download_targets_scanned((valid, automatic, run_in_background, names))

    @staticmethod
    def _scan_download_target_names(
        items: list[tuple[WopanItem, Path]],
        automatic: bool,
    ) -> dict[Path, set[str]]:
        names: dict[Path, set[str]] = {}
        for _item, path in items:
            parent = path.parent
            if parent in names:
                continue
            if automatic:
                parent.mkdir(parents=True, exist_ok=True)
            if not parent.is_dir():
                raise OSError("下载目录不可用")
            names[parent] = {entry.name for entry in parent.iterdir()}
        return names

    def _start_download_target_scan(
        self,
        items: list[tuple[WopanItem, Path]],
        automatic: bool,
        run_in_background: bool,
    ) -> None:
        thread = QThread(self)
        worker = BrowserOperationWorker(
            lambda: (
                items,
                automatic,
                run_in_background,
                self._scan_download_target_names(items, automatic),
            )
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_download_targets_scanned)
        worker.failed.connect(self._on_download_target_scan_failed)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._clear_download_target_scan)
        self._download_target_thread = thread
        self._download_target_worker = worker
        self._set_status("正在检查下载目标...")
        thread.start()

    def _clear_download_target_scan(self) -> None:
        self._delete_finished_thread()
        self._download_target_thread = None
        self._download_target_worker = None
        if self._download_target_pending and not self._closing:
            items, automatic, run_in_background = self._download_target_pending.pop(0)
            self._start_download_target_scan(items, automatic, run_in_background)

    def _on_download_target_scan_failed(self, message: str) -> None:
        if self._closing:
            return
        self._set_status(f"下载目录不可用：{message}")
        InfoBar.error(title="下载目录不可用", content=message, parent=self)

    def _on_download_targets_scanned(self, result: object) -> None:
        if self._closing:
            return
        valid, automatic, run_in_background, names = cast(
            tuple[list[tuple[WopanItem, Path]], bool, bool, dict[Path, set[str]]], result
        )
        resolved = self._resolve_download_targets(valid, names, automatic=automatic)
        if resolved is None:
            return
        skipped_count = len(valid) - len(resolved)
        if not resolved:
            self._set_status(f"已跳过 {skipped_count} 个冲突项目")
            return
        if skipped_count:
            self._set_status(f"已跳过 {skipped_count} 个冲突项目")
        if callable(getattr(self._file_browser, "submit_download", None)):
            self._download_reserved_targets.update(path for _, path in resolved)
        self._submit_resolved_download_items(resolved, run_in_background=run_in_background)

    def _submit_resolved_download_items(
        self,
        valid: list[tuple[WopanItem, Path]],
        *,
        run_in_background: bool,
    ) -> None:
        submit = getattr(self._file_browser, "submit_download", None)
        if not callable(submit):
            # Keep test doubles and older backends on the original worker boundary.
            for item, path in valid:
                task_id = self._create_download_record(item, path)
                if run_in_background:
                    self._start_download_task(item, path, task_id)
                else:
                    try:
                        self._download_with_callbacks(item, path, task_id)
                    except FileBrowserLoginRequiredError as exc:
                        self._mark_transfer_failed("download", task_id, str(exc))
                        self._show_login_required_error(str(exc))
                    except FileBrowserError as exc:
                        self._on_download_failed(str(exc), task_id=task_id)
                    else:
                        self._on_download_succeeded(item.name, str(path), task_id=task_id)
            return

        def submit_all() -> list[tuple[WopanItem, Path, str | None, str]]:
            results: list[tuple[WopanItem, Path, str | None, str]] = []
            for item, path in valid:
                try:
                    task_id = submit(item, path)
                except FileBrowserError as exc:
                    results.append((item, path, None, str(exc)))
                else:
                    results.append((item, path, task_id, ""))
            return results

        thread = QThread(self)
        worker = BrowserOperationWorker(submit_all)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_download_submissions_succeeded)
        worker.failed.connect(self._on_download_submissions_failed)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._clear_download_submission)
        self._download_submit_threads.add(thread)
        self._download_submit_workers[thread] = worker
        self._download_submit_paths[thread] = {path for _, path in valid}
        self._download_submit_thread = thread
        self._download_submit_worker = worker
        self._set_status("正在加入下载队列...")
        thread.start()

    def _on_download_submissions_succeeded(self, result: object) -> None:
        if self._closing:
            return
        submissions = cast(list[tuple[WopanItem, Path, str | None, str]], result)
        succeeded = 0
        failed = 0
        for item, path, task_id, error in submissions:
            if task_id is None:
                failed += 1
                self._create_download_record(item, path, status="失败", error=error)
                if error == "登录已过期，请重新登录":
                    self._show_login_required_error(error)
                continue
            succeeded += 1
            self._removed_download_task_ids.discard(task_id)
            self._create_download_record(item, path, task_id=task_id)
            event = self._pending_download_events.pop(task_id, None)
            if event is not None:
                self._on_download_event(event)
        self._set_status(f"已添加 {succeeded} 个下载任务，失败 {failed} 个")
        self.update_operation_controls()

    def _on_download_submissions_failed(self, message: str) -> None:
        if self._closing:
            return
        self._set_status(f"下载失败：{message}")
        InfoBar.error(title="下载失败", content=message, parent=self)

    def _clear_download_submission(self) -> None:
        self._delete_finished_thread()
        thread = self.sender()
        if isinstance(thread, QThread):
            self._download_submit_threads.discard(thread)
            self._download_submit_workers.pop(thread, None)
            self._download_reserved_targets.difference_update(
                self._download_submit_paths.pop(thread, set())
            )
            if self._download_submit_thread is not thread:
                return
        self._download_submit_thread = None
        self._download_submit_worker = None

    def _resolve_download_targets(
        self,
        items: list[tuple[WopanItem, Path]],
        existing_names: dict[Path, set[str]],
        *,
        automatic: bool = False,
    ) -> list[tuple[WopanItem, Path]] | None:
        """Resolve scanned local targets without doing filesystem work on the GUI thread."""
        targets: list[tuple[WopanItem, Path]] = []
        occupied = {parent: names.copy() for parent, names in existing_names.items()}
        for record in self.transfer_interface.download_records:
            path = record.target_path
            if record.status in ACTIVE_DOWNLOAD_STATUSES and path is not None:
                if path.parent in occupied:
                    occupied[path.parent].add(path.name)
        for path in self._download_reserved_targets:
            if path.parent in occupied:
                occupied[path.parent].add(path.name)
        if automatic:
            used_names = occupied
            for item, path in items:
                names = used_names[path.parent]
                copy_name = _next_available_file_name(path.name, names)
                targets.append((item, path.with_name(copy_name)))
                names.add(copy_name)
            return targets

        seen_names = {parent: names.copy() for parent, names in occupied.items()}
        conflicts: list[Path] = []
        for _item, path in items:
            names = seen_names[path.parent]
            if path.name in names:
                conflicts.append(path)
            names.add(path.name)
        if not conflicts:
            return items

        dialog = DownloadConflictDialog(tuple(conflicts), self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if self._closing or not accepted:
            self._set_status("已取消下载")
            return None
        resolution = dialog.resolution()
        if resolution is None:
            self._set_status("已取消下载")
            return None

        used_names = occupied
        for item, path in items:
            names = used_names[path.parent]
            if path.name not in names:
                targets.append((item, path))
                names.add(path.name)
                continue
            if resolution == "skip":
                continue
            copy_name = _next_available_file_name(path.name, names)
            targets.append((item, path.with_name(copy_name)))
            names.add(copy_name)
        return targets

    def _receive_download_event(self, event: DownloadTaskEvent) -> None:
        self._download_event_signal.emit(event)

    def _on_download_event(self, event: object) -> None:
        if self._closing or not isinstance(event, DownloadTaskEvent):
            return
        if event.task_id in self._removed_download_task_ids:
            return
        record = self.transfer_interface._find_record("download", event.task_id)
        if record is None:
            self._pending_download_events[event.task_id] = event
            return

        terminal = event.status in TERMINAL_TRANSFER_STATUSES
        status = event.status if terminal or event.status != record.status else None
        total_bytes = (
            event.total_bytes
            if event.total_bytes is not None and event.total_bytes != record.total_bytes
            else None
        )
        max_connections = (
            event.max_connections if event.max_connections != record.max_connections else None
        )
        can_resume = event.status in {"已暂停", "失败"}
        can_resume_value = can_resume if can_resume != record.can_resume else None
        error = event.error if event.error != record.error else None
        self.transfer_interface.update_record(
            "download",
            event.task_id,
            status=status,
            bytes_done=event.bytes_done,
            total_bytes=total_bytes,
            active_connections=event.active_connections,
            max_connections=max_connections,
            can_resume=can_resume_value,
            error=error,
        )
        if event.status == "已完成":
            self._set_status("下载完成")
        elif event.status == "失败":
            self._set_status(f"下载失败：{event.error}")

    def handle_upload_drop(self, paths: object) -> None:
        """Start a background summary scan for local paths dropped on the file list."""
        if self._file_browser is None:
            self._set_status("请先登录")
            return
        if not isinstance(paths, tuple) or not all(isinstance(path, Path) for path in paths):
            return
        unique_paths = tuple(dict.fromkeys(paths))
        if not unique_paths:
            return
        parent_id = self.current_directory_id()
        if self._scan_thread is not None:
            self._upload_scan_pending.append((unique_paths, parent_id))
            self._set_status("已排队待扫描")
            return
        self._start_upload_scan(unique_paths, parent_id)

    def _start_upload_scan(self, paths: tuple[Path, ...], parent_id: str) -> None:
        self._upload_scan_parent_id = parent_id
        thread = QThread(self)
        worker = UploadScanWorker(paths)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_upload_scan_succeeded)
        worker.failed.connect(self._on_upload_scan_failed)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._clear_upload_scan)
        self._scan_thread = thread
        self._scan_worker = worker
        self._set_status("正在扫描待上传内容...")
        LOGGER.info("main_window.upload_scan.start top_count=%s", len(paths))
        thread.start()

    def _on_upload_scan_succeeded(self, result: object) -> None:
        if self._closing:
            return
        if not isinstance(result, UploadBatchSummary):
            self._on_upload_scan_failed("扫描结果无效")
            return
        LOGGER.info(
            "main_window.upload_scan.success top_count=%s file_count=%s folder_count=%s",
            len(result.top_paths),
            result.file_count,
            result.folder_count,
        )
        parent_id = self._upload_scan_parent_id
        if parent_id is None or self.current_directory_id() != parent_id:
            self._set_status("目录已变化，请重新提交上传任务")
            return
        self._submit_upload_paths(result.top_paths, parent_id=parent_id, summary=result)

    def _submit_upload_paths(
        self,
        paths: tuple[Path, ...],
        *,
        parent_id: str | None = None,
        run_in_background: bool = True,
        summary: UploadBatchSummary | None = None,
    ) -> None:
        """Queue a background target-directory check before creating upload tasks."""
        if self._closing:
            return
        if self._file_browser is None:
            self._set_status("请先登录")
            return
        unique_paths = tuple(dict.fromkeys(paths))
        if not unique_paths:
            return
        requested_parent_id = parent_id if parent_id is not None else self.current_directory_id()
        self._upload_conflict_pending.append(
            (unique_paths, requested_parent_id, run_in_background, summary)
        )
        self._start_next_upload_conflict_check()

    def _start_next_upload_conflict_check(self) -> None:
        if (
            self._closing
            or self._upload_conflict_thread is not None
            or self._upload_conflict_dialog_open
            or not self._upload_conflict_pending
        ):
            return
        request = self._upload_conflict_pending.pop(0)
        self._upload_conflict_current = request
        paths, parent_id, _run_in_background, _summary = request
        file_browser = self._file_browser
        if file_browser is None:
            self._upload_conflict_current = None
            return
        thread = QThread(self)
        worker = BrowserOperationWorker(lambda: file_browser.list_directory(parent_id))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_upload_conflict_check_succeeded)
        worker.failed.connect(self._on_upload_conflict_check_failed)
        worker.login_required.connect(self._on_upload_conflict_check_login_required)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.login_required.connect(thread.quit)
        thread.finished.connect(self._clear_upload_conflict_check)
        self._upload_conflict_thread = thread
        self._upload_conflict_worker = worker
        self._set_status("正在检查上传名称...")
        LOGGER.info("main_window.upload_conflict_check.start top_count=%s", len(paths))
        thread.start()

    def _on_upload_conflict_check_succeeded(self, result: object) -> None:
        if self._closing:
            return
        request = self._upload_conflict_current
        if request is None or not isinstance(result, list):
            self._on_upload_conflict_check_failed("上传目录检查结果无效")
            return
        if not all(isinstance(item, WopanItem) for item in result):
            self._on_upload_conflict_check_failed("上传目录检查结果无效")
            return
        paths, parent_id, run_in_background, summary = request
        if self.current_directory_id() != parent_id:
            self._set_status("目录已变化，请重新提交上传任务")
            return
        existing_names = {item.name for item in result}
        LOGGER.info(
            "main_window.upload_conflict_check.success top_count=%s cloud_count=%s",
            len(paths),
            len(result),
        )
        self._upload_conflict_dialog_open = True
        try:
            resolution: UploadConflictResolution | None = None
            if summary is not None:
                conflicts = find_upload_conflicts(paths, existing_names)
                dialog = UploadSummaryDialog(
                    summary, self.breadcrumb_names()[-1], self, conflicts=conflicts
                )
                accepted = dialog.exec() == QDialog.DialogCode.Accepted
                if self._closing:
                    return
                if not accepted:
                    self._set_status("已取消上传")
                    return
                resolution = dialog.resolution()
            self._resolve_upload_paths(
                paths,
                existing_names,
                parent_id=parent_id,
                run_in_background=run_in_background,
                resolution=resolution,
            )
        finally:
            self._upload_conflict_dialog_open = False
            self._start_next_upload_conflict_check()

    def _on_upload_conflict_check_failed(self, message: str) -> None:
        if self._closing:
            return
        LOGGER.warning("main_window.upload_conflict_check.failed error_length=%s", len(message))
        self._set_status(f"检查上传名称失败：{message}")
        InfoBar.error(title="检查上传名称失败", content=message, parent=self)

    def _on_upload_conflict_check_login_required(self, message: str) -> None:
        if self._closing:
            return
        self._show_login_required_error(message)

    def _clear_upload_conflict_check(self) -> None:
        self._delete_finished_thread()
        self._upload_conflict_thread = None
        self._upload_conflict_worker = None
        self._upload_conflict_current = None
        self._start_next_upload_conflict_check()

    def _resolve_upload_paths(
        self,
        paths: tuple[Path, ...],
        existing_names: set[str],
        *,
        parent_id: str,
        run_in_background: bool,
        resolution: UploadConflictResolution | None = None,
    ) -> None:
        conflicts = find_upload_conflicts(paths, existing_names)
        if resolution is None:
            resolution = "copy"
            if conflicts:
                dialog = UploadConflictDialog(conflicts, self)
                dialog.exec()
                if self._closing:
                    return
                selected_resolution = dialog.resolution()
                if selected_resolution is None:
                    self._set_status("已取消上传")
                    return
                resolution = selected_resolution
        if self._closing:
            return
        if self.current_directory_id() != parent_id:
            self._set_status("目录已变化，请重新提交上传任务")
            return
        targets = resolve_upload_targets(paths, existing_names, resolution)
        skipped_count = len(paths) - len(targets)
        if skipped_count:
            self._set_status(f"已跳过 {skipped_count} 个冲突项目，已添加 {len(targets)} 个上传任务")
        else:
            self._set_status(f"已添加 {len(targets)} 个上传任务")
        for target in targets:
            if target.local_path.is_dir():
                self.upload_folder_to_current_directory(
                    target.local_path,
                    root_name=(
                        target.upload_name
                        if target.upload_name is not None
                        else target.local_path.name
                    ),
                    _parent_id=parent_id,
                    _conflict_checked=True,
                )
            else:
                self.upload_file_to_current_directory(
                    target.local_path,
                    run_in_background=run_in_background,
                    upload_name=(
                        target.upload_name
                        if target.upload_name is not None
                        else target.local_path.name
                    ),
                    _parent_id=parent_id,
                    _conflict_checked=True,
                    _show_enqueue_status=len(targets) == 1,
                )

    def _on_upload_scan_failed(self, message: str) -> None:
        if self._closing:
            return
        LOGGER.warning("main_window.upload_scan.failed error_length=%s", len(message))
        self._set_status(f"扫描上传内容失败：{message}")
        InfoBar.error(title="扫描上传内容失败", content=message, parent=self)

    def _clear_upload_scan(self) -> None:
        self._delete_finished_thread()
        self._scan_thread = None
        self._scan_worker = None
        self._upload_scan_parent_id = None
        if self._upload_scan_pending:
            paths, parent_id = self._upload_scan_pending.pop(0)
            self._start_upload_scan(paths, parent_id)

    def upload_file_to_current_directory(
        self,
        local_path: Path,
        *,
        run_in_background: bool = True,
        upload_name: str | None = None,
        _conflict_checked: bool = False,
        _parent_id: str | None = None,
        _show_enqueue_status: bool = True,
    ) -> None:
        """Upload one local file to the current directory."""
        if self._file_browser is None:
            self._set_status("请先登录")
            return
        if not local_path.name:
            self._set_status("上传文件不能为空")
            InfoBar.warning(title="上传", content="上传文件不能为空", parent=self)
            return
        if upload_name is None and not _conflict_checked:
            self._submit_upload_paths((local_path,), run_in_background=run_in_background)
            return

        parent_id = _parent_id if _parent_id is not None else self.current_directory_id()
        LOGGER.info(
            "main_window.upload.start parent_id=%s file_name_length=%s",
            parent_id,
            len(local_path.name),
        )
        task_id = self._create_upload_record(
            local_path,
            name=upload_name if upload_name is not None else local_path.name,
            parent_id=parent_id,
            upload_name=upload_name,
            retryable=True,
        )
        if run_in_background:
            self._start_upload_task(
                parent_id,
                local_path,
                task_id,
                upload_name=upload_name,
                show_enqueue_status=_show_enqueue_status,
            )
            return
        self._set_status(f"正在上传「{local_path.name}」...")
        try:
            if upload_name is None:
                uploaded_item = self._file_browser.upload_file(parent_id, local_path)
            else:
                uploaded_item = self._file_browser.upload_file(
                    parent_id, local_path, upload_name=upload_name
                )
        except FileBrowserLoginRequiredError as exc:
            self._mark_transfer_failed("upload", task_id, str(exc))
            self._show_login_required_error(str(exc))
        except FileBrowserError as exc:
            self._on_upload_failed(str(exc), task_id=task_id)
        else:
            self._on_upload_succeeded(uploaded_item, task_id=task_id)

    def upload_folder_to_current_directory(
        self,
        local_root: Path,
        *,
        root_name: str | None = None,
        _conflict_checked: bool = False,
        _parent_id: str | None = None,
        _record_id: str | None = None,
    ) -> None:
        """Upload one local folder tree to the current directory (two phases)."""
        if self._file_browser is None:
            self._set_status("请先登录")
            return
        if not local_root.name:
            self._set_status("上传文件夹不能为空")
            InfoBar.warning(title="上传", content="上传文件夹不能为空", parent=self)
            return
        if root_name is None and not _conflict_checked:
            self._submit_upload_paths((local_root,))
            return
        if (
            self._folder_prepare_thread is not None
            or self._folder_upload_active is not None
            or self._folder_upload_queue
        ):
            record_id = self._create_upload_record(
                local_root,
                name=root_name if root_name is not None else local_root.name,
                parent_id=(_parent_id if _parent_id is not None else self.current_directory_id()),
            )
            self._folder_prepare_pending.append(
                PendingFolderUpload(
                    local_path=local_root,
                    parent_id=(
                        _parent_id if _parent_id is not None else self.current_directory_id()
                    ),
                    root_name=root_name,
                    record_id=record_id,
                )
            )
            self._set_status(f"已添加「{local_root.name}」上传任务")
            return

        parent_id = _parent_id if _parent_id is not None else self.current_directory_id()
        LOGGER.info(
            "main_window.folder_upload.prepare.start parent_id=%s root_name_length=%s",
            parent_id,
            len(local_root.name),
        )
        if _record_id is None:
            record_id = self._create_upload_record(
                local_root,
                name=root_name if root_name is not None else local_root.name,
                parent_id=parent_id,
            )
        else:
            record_id = _record_id
        self._folder_upload_record_id = record_id
        self.transfer_interface.update_record("upload", record_id, status="创建目录中")
        file_browser = self._file_browser
        thread = QThread(self)
        prepare_cancel = threading.Event()
        if root_name is None:

            def operation() -> FolderUploadJob:
                return file_browser.prepare_folder_upload(
                    parent_id, local_root, cancel_requested=prepare_cancel.is_set
                )
        else:
            resolved_root_name = root_name

            def operation() -> FolderUploadJob:
                return file_browser.prepare_folder_upload(
                    parent_id,
                    local_root,
                    root_name=resolved_root_name,
                    cancel_requested=prepare_cancel.is_set,
                )

        worker = BrowserOperationWorker(operation)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_folder_upload_prepared)
        worker.failed.connect(self._on_folder_upload_prepare_failed)
        worker.login_required.connect(self._on_folder_upload_prepare_login_required)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.login_required.connect(thread.quit)
        thread.finished.connect(self._clear_folder_prepare)

        self._folder_prepare_thread = thread
        self._folder_prepare_worker = worker
        self._folder_prepare_cancel = prepare_cancel
        self._folder_upload_record_id = record_id
        self._folder_upload_target_dir_id = parent_id
        self._set_status(f"正在创建目录「{local_root.name}」...")
        thread.start()

    def _on_folder_upload_prepared(self, result: object) -> None:
        if self._closing:
            return
        if self._folder_upload_record_id in self._upload_removal_requested:
            self._finish_folder_upload()
            return
        job = cast(FolderUploadJob, result)
        record_id = self._folder_upload_record_id
        if record_id is not None:
            self.transfer_interface.update_record("upload", record_id, status="上传中")
        LOGGER.info(
            "main_window.folder_upload.prepare.success root_item_id=%s file_count=%s",
            job.root_item_id,
            len(job.files),
        )
        queue: list[QueuedUploadFile] = []
        for planned in job.files:
            task_id = self._create_upload_record(
                planned.local_path,
                name=planned.name,
                size=planned.size,
                parent_id=planned.target_dir_id,
                upload_name=planned.name,
                retryable=True,
            )
            queue.append(
                QueuedUploadFile(
                    task_id=task_id,
                    local_path=planned.local_path,
                    target_dir_id=planned.target_dir_id,
                    upload_name=planned.name,
                )
            )
        self._folder_upload_queue = queue
        self._folder_upload_child_ids = {item.task_id for item in queue}
        self._set_status(f"已添加 {len(queue)} 个上传任务")
        self._start_next_folder_upload_file()

    def _on_folder_upload_prepare_failed(self, message: str) -> None:
        if self._closing:
            return
        if self._folder_upload_record_id in self._upload_removal_requested:
            self._finish_folder_upload()
            return
        LOGGER.warning("main_window.folder_upload.prepare.failed error_length=%s", len(message))
        self._mark_transfer_failed("upload", self._folder_upload_record_id, message)
        self._set_status(f"上传文件夹失败：{message}")
        InfoBar.error(title="上传文件夹失败", content=message, parent=self)

    def _on_folder_upload_prepare_login_required(self, message: str) -> None:
        if self._closing:
            return
        if self._folder_upload_record_id in self._upload_removal_requested:
            self._finish_folder_upload()
            return
        self._mark_transfer_failed("upload", self._folder_upload_record_id, message)
        self._show_login_required_error(message)

    def _clear_folder_prepare(self) -> None:
        self._delete_finished_thread()
        self._folder_prepare_thread = None
        self._folder_prepare_worker = None
        self._folder_prepare_cancel = None
        if self._folder_upload_active is None and not self._folder_upload_queue:
            self._folder_upload_record_id = None
        self._start_next_pending_folder()

    def _start_next_pending_folder(self) -> None:
        if (
            self._closing
            or self._upload_delete_batch
            or self._folder_prepare_thread is not None
            or self._folder_upload_active is not None
            or self._folder_upload_queue
            or not self._folder_prepare_pending
        ):
            return
        pending = self._take_pending_folder()
        self.upload_folder_to_current_directory(
            pending.local_path,
            root_name=pending.root_name,
            _conflict_checked=True,
            _parent_id=pending.parent_id,
            _record_id=pending.record_id,
        )

    def _take_pending_folder(self) -> PendingFolderUpload:
        return self._folder_prepare_pending.pop(0)

    def _start_next_folder_upload_file(self) -> None:
        """Dequeue and start the next folder-upload file when the slot is free."""
        if self._upload_delete_batch:
            return
        if not self._folder_upload_queue:
            self._finish_folder_upload()
            return
        queued = next(
            (
                item
                for item in self._folder_upload_queue
                if item.task_id not in self._paused_uploads
            ),
            None,
        )
        if queued is None:
            return
        self._folder_upload_queue.remove(queued)
        self._folder_upload_active = queued
        self._start_upload_task(
            queued.target_dir_id,
            queued.local_path,
            queued.task_id,
            upload_name=queued.upload_name,
            show_enqueue_status=False,
        )

    def _continue_folder_upload_queue(self) -> None:
        """Advance the folder-upload queue after the upload slot cleared."""
        if self._closing:
            return
        active = self._folder_upload_active
        if active is not None and (
            active.task_id in self._upload_threads
            or active.task_id in self._paused_uploads
            or any(pending.task_id == active.task_id for pending in self._upload_pending)
        ):
            return
        self._folder_upload_active = None
        if self._folder_upload_queue:
            self._start_next_folder_upload_file()
            return
        if active is not None:
            self._finish_folder_upload()

    def _finish_folder_upload(self) -> None:
        success_count = self._folder_upload_success_count
        failure_count = self._folder_upload_failure_count
        cancel_count = self._folder_upload_cancel_count
        target_dir_id = self._folder_upload_target_dir_id
        root_id = self._folder_upload_record_id
        if (
            any(child_id in self._upload_workers for child_id in self._folder_upload_child_ids)
            or any(
                pending.task_id in self._folder_upload_child_ids for pending in self._upload_pending
            )
            or any(task_id in self._paused_uploads for task_id in self._folder_upload_child_ids)
        ):
            return
        self._folder_upload_active = None
        self._folder_upload_queue = []
        self._folder_upload_child_ids.clear()
        self._folder_upload_failed_ids.clear()
        self._folder_upload_success_count = 0
        self._folder_upload_failure_count = 0
        self._folder_upload_cancel_count = 0
        self._folder_upload_target_dir_id = None
        self._folder_upload_record_id = None
        if root_id in self._upload_removal_requested:
            self._upload_removal_requested.discard(root_id)
            self.transfer_interface.remove_records("upload", {root_id})
            if target_dir_id is not None and self.current_directory_id() == target_dir_id:
                self.refresh_current_directory()
            self._start_next_pending_folder()
            return
        LOGGER.info(
            "main_window.folder_upload.finished success=%s failed=%s",
            success_count,
            failure_count,
        )
        content = f"成功 {success_count} 个，失败 {failure_count} 个"
        if cancel_count:
            content += f"，取消 {cancel_count} 个"
        if root_id is not None:
            if cancel_count:
                self.transfer_interface.update_record("upload", root_id, status="已取消")
            elif failure_count:
                self._mark_transfer_failed("upload", root_id, content)
            else:
                self.transfer_interface.update_record("upload", root_id, status="已完成")
        if failure_count == 0:
            InfoBar.info(title="上传完成", content=content, parent=self)
        else:
            InfoBar.warning(title="上传完成", content=content, parent=self)
        self._set_status(f"文件夹上传完成：{content}")
        if target_dir_id is not None and self.current_directory_id() == target_dir_id:
            self.refresh_current_directory()
        self._start_next_pending_folder()

    def prompt_create_folder(self) -> None:
        """Prompt for a folder name and create it."""
        dialog = NameInputDialog(
            title="新建文件夹",
            hint="请输入文件夹名称",
            default_text="新建文件夹",
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.create_folder_with_name(dialog.name_text())
        dialog.deleteLater()

    def prompt_rename_item(self, row: int) -> None:
        """Prompt for a new name and rename a row."""
        item = self._item_at_row(row)
        if item is None:
            return
        dialog = NameInputDialog(
            title="重命名",
            hint="请输入新的名称",
            default_text=item.name,
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.rename_displayed_item(row, dialog.name_text())
        dialog.deleteLater()

    def prompt_delete_item(self, row: int) -> None:
        """Confirm and delete a row, batching the full selection when included."""
        rows = self.selected_rows()
        if row not in rows:
            rows = [row]
        self.prompt_delete_items(rows)

    def prompt_delete_items(self, rows: Sequence[int]) -> None:
        """Confirm and delete one or more displayed rows."""
        items = [item for row in rows if (item := self._item_at_row(row)) is not None]
        if not items:
            return
        summary = _format_items_summary([item.name for item in items])
        message = MessageBox(
            "确认删除",
            f"确定要删除{summary}吗？删除后将移入回收站，可在回收站中恢复。",
            self,
        )
        accepted = message.exec()
        message.deleteLater()
        if accepted:
            self.delete_displayed_items(rows)

    def prompt_move_item(self, row: int) -> None:
        """Prompt for a target directory and move a row (or the full selection)."""
        self._prompt_transfer_target(row, "move")

    def prompt_copy_item(self, row: int) -> None:
        """Prompt for a target directory and copy a row (or the full selection)."""
        self._prompt_transfer_target(row, "copy")

    def _prompt_transfer_target(self, row: int, mode: TransferMode) -> None:
        """Open the browsing target dialog for a move/copy batch."""
        if self._file_browser is None:
            self._set_status("请先登录")
            return
        rows = self.selected_rows()
        if row not in rows:
            rows = [row]
        items = [item for moved_row in rows if (item := self._item_at_row(moved_row)) is not None]
        if not items:
            return

        selected_ids = frozenset(item.item_id for item in items)
        # Readable root label: the breadcrumb root name is "/", which the
        # dialog's " / ".join path bar would render as "/ / 子目录".
        dialog = TargetFolderDialog(
            mode,
            TargetEntry(item_id=self._breadcrumb[0].item_id, name="根目录"),
            [],
            excluded_ids=selected_ids,
            parent=self,
        )
        dialog.directory_requested.connect(self._on_target_directory_requested)
        dialog.create_folder_requested.connect(self._on_target_create_folder_requested)
        self._target_dialog = dialog
        dialog.start_browse()
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        target = dialog.current_target() if accepted else None
        dialog.deleteLater()
        self._target_dialog = None
        if target is None:
            return
        self._check_transfer_conflicts(tuple(items), target.item_id, mode)

    def _on_target_directory_requested(self, parent_id: str) -> None:
        """Fetch one directory level for the open target dialog."""
        if self._target_dialog is None or self._target_load_thread is not None:
            LOGGER.debug("main_window.target_load.skipped")
            return
        file_browser = self._file_browser
        if file_browser is None:
            return
        thread = QThread(self)
        worker = BrowserOperationWorker(lambda: file_browser.list_directory(parent_id))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_target_directory_loaded)
        worker.failed.connect(self._on_target_directory_load_failed)
        worker.login_required.connect(self._on_target_directory_load_login_required)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.login_required.connect(thread.quit)
        thread.finished.connect(self._clear_target_directory_load)
        self._target_load_thread = thread
        self._target_load_worker = worker
        thread.start()

    def _on_target_directory_loaded(self, result: object) -> None:
        if self._closing:
            return
        dialog = self._target_dialog
        if dialog is None or not isinstance(result, list):
            return
        entries = [
            TargetEntry(item_id=item.item_id, name=item.name)
            for item in result
            if isinstance(item, WopanItem) and item.kind is WopanItemKind.FOLDER
        ]
        dialog.show_entries(entries)

    def _on_target_directory_load_failed(self, message: str) -> None:
        if self._closing:
            return
        if self._target_dialog is not None:
            self._target_dialog.show_load_error(message)

    def _on_target_directory_load_login_required(self, message: str) -> None:
        if self._target_dialog is not None:
            self._target_dialog.reject()
        self._show_login_required_error(message)

    def _clear_target_directory_load(self) -> None:
        self._delete_finished_thread()
        self._target_load_thread = None
        self._target_load_worker = None

    def _on_target_create_folder_requested(self, parent_id: str, name: str) -> None:
        """Create a folder for the open target dialog on a worker thread."""
        dialog = self._target_dialog
        if dialog is None:
            return
        if self._target_create_thread is not None:
            LOGGER.debug("main_window.target_create.skipped_busy")
            dialog.show_create_error("已有创建任务进行中，请稍候")
            return
        file_browser = self._file_browser
        if file_browser is None:
            dialog.show_create_error("请先登录")
            return
        dialog.begin_create()
        thread = QThread(self)
        worker = BrowserOperationWorker(lambda: file_browser.create_folder(parent_id, name))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_target_create_succeeded)
        worker.failed.connect(self._on_target_create_failed)
        worker.login_required.connect(self._on_target_create_login_required)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.login_required.connect(thread.quit)
        thread.finished.connect(self._clear_target_create)
        self._target_create_thread = thread
        self._target_create_worker = worker
        LOGGER.info(
            "main_window.target_create.start parent_id=%s name_length=%s", parent_id, len(name)
        )
        thread.start()

    def _on_target_create_succeeded(self, result: object) -> None:
        if self._closing:
            return
        dialog = self._target_dialog
        if dialog is None or not isinstance(result, WopanItem):
            return
        LOGGER.info("main_window.target_create.success item_id=%s", result.item_id)
        dialog.show_created_entry(TargetEntry(item_id=result.item_id, name=result.name))

    def _on_target_create_failed(self, message: str) -> None:
        if self._closing:
            return
        if self._target_dialog is not None:
            self._target_dialog.show_create_error(message)

    def _on_target_create_login_required(self, message: str) -> None:
        if self._target_dialog is not None:
            self._target_dialog.reject()
        self._show_login_required_error(message)

    def _clear_target_create(self) -> None:
        self._delete_finished_thread()
        self._target_create_thread = None
        self._target_create_worker = None

    def _check_transfer_conflicts(
        self, items: tuple[WopanItem, ...], target_id: str, mode: TransferMode
    ) -> None:
        """List the target directory once, then resolve or execute the batch."""
        if self._transfer_check_thread is not None:
            LOGGER.debug("main_window.transfer_check.skipped_busy")
            return
        file_browser = self._file_browser
        if file_browser is None:
            self._set_status("请先登录")
            return
        self._transfer_pending = (items, target_id, mode)
        self._set_status("正在检查目标文件夹…")
        thread = QThread(self)
        worker = BrowserOperationWorker(lambda: file_browser.list_directory(target_id))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_transfer_check_succeeded)
        worker.failed.connect(self._on_transfer_check_failed)
        worker.login_required.connect(self._on_transfer_check_login_required)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.login_required.connect(thread.quit)
        thread.finished.connect(self._clear_transfer_check)
        self._transfer_check_thread = thread
        self._transfer_check_worker = worker
        LOGGER.info("main_window.transfer_check.start count=%s mode=%s", len(items), mode)
        thread.start()

    def _on_transfer_check_succeeded(self, result: object) -> None:
        if self._closing:
            return
        request = self._transfer_pending
        self._transfer_pending = None
        if request is None or not isinstance(result, list):
            self._on_transfer_check_failed("目标文件夹检查结果无效")
            return
        if not all(isinstance(item, WopanItem) for item in result):
            self._on_transfer_check_failed("目标文件夹检查结果无效")
            return
        items, target_id, mode = request
        verb = mode_label(mode)
        plan = plan_transfer_batch(items, result)
        LOGGER.info(
            "main_window.transfer_check.success transferable=%s conflicts=%s noops=%s",
            len(plan.transfer_items),
            len(plan.conflict_names),
            len(plan.noop_ids),
        )
        if plan.conflict_names:
            dialog = TransferConflictDialog(plan.conflict_names, mode, parent=self)
            dialog.exec()
            resolution = dialog.resolution()
            dialog.deleteLater()
            if resolution != "skip":
                self._set_status(f"已取消{verb}")
                return
        if not plan.transfer_items:
            self._set_status(f"没有可{verb}的项目")
            InfoBar.info(title=verb, content="没有可执行的项目", parent=self)
            return
        self._execute_transfer(plan.transfer_items, target_id, mode)

    def _on_transfer_check_failed(self, message: str) -> None:
        if self._closing:
            return
        LOGGER.warning("main_window.transfer_check.failed error_length=%s", len(message))
        self._set_status(f"检查目标文件夹失败：{message}")
        InfoBar.error(title="检查目标文件夹失败", content=message, parent=self)

    def _on_transfer_check_login_required(self, message: str) -> None:
        self._show_login_required_error(message)

    def _clear_transfer_check(self) -> None:
        self._delete_finished_thread()
        self._transfer_check_thread = None
        self._transfer_check_worker = None
        self._transfer_pending = None

    def _execute_transfer(
        self, items: tuple[WopanItem, ...], target_id: str, mode: TransferMode
    ) -> None:
        if mode == "move":
            self._start_move_batch(items, target_id)
        else:
            self._start_copy_batch(items, target_id)

    def prompt_download_item(self, row: int) -> None:
        """Prompt for one save path or a directory for the selected files."""
        if self._file_browser is None:
            self._set_status("请先登录")
            return
        rows = self.selected_download_rows()
        if row not in rows:
            rows = [row]
        items = [self._item_at_row(index) for index in rows]
        if not items or all(item is None for item in items):
            self._set_status("请先登录")
            return
        files = [
            (index, item)
            for index, item in zip(rows, items, strict=True)
            if item is not None and item.kind is WopanItemKind.FILE and item.download_id
        ]
        if not files:
            self._set_status("只能下载文件")
            InfoBar.warning(title="下载", content="只能下载文件", parent=self)
            return
        if len(files) == 1 and self._settings.ask_download_location:
            index, item = files[0]
            assert item is not None
            path_text, _selected_filter = QFileDialog.getSaveFileName(self, "保存文件", item.name)
            if path_text:
                self.download_displayed_item(index, Path(path_text))
            return
        if self._settings.ask_download_location:
            folder_text = QFileDialog.getExistingDirectory(self, "选择下载目录")
            if not folder_text:
                return
            folder = Path(folder_text)
        else:
            folder = self._settings.default_download_path
        paths = [(item, folder / _safe_local_file_name(item.name)) for _, item in files]
        self._submit_download_items(paths, automatic=not self._settings.ask_download_location)

    def _resolve_automatic_download_path(self, remote_name: str) -> Path | None:
        folder = self._settings.default_download_path
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            LOGGER.warning("main_window.download.default_path_unavailable error=%s", exc)
            self._set_status(f"下载目录不可用：{exc}")
            InfoBar.error(title="下载目录不可用", content=str(exc), parent=self)
            return None
        if not folder.is_dir():
            self._set_status("下载目录不可用")
            InfoBar.error(title="下载目录不可用", content=str(folder), parent=self)
            return None
        file_name = _safe_local_file_name(remote_name)
        used_names = {path.name for path in folder.iterdir()}
        if self._download_item is not None:
            used_names.add(self._download_item.name)
        return folder / _next_available_file_name(file_name, used_names)

    def prompt_upload_file(self) -> None:
        """Prompt for one local file and upload it to the current directory."""
        if self._file_browser is None:
            self._set_status("请先登录")
            return

        path_text, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "上传文件",
        )
        if not path_text:
            return
        self._submit_upload_paths((Path(path_text),))

    def prompt_upload_folder(self) -> None:
        """Prompt for one local folder and upload it to the current directory."""
        if self._file_browser is None:
            self._set_status("请先登录")
            return

        path_text = QFileDialog.getExistingDirectory(
            self,
            "上传文件夹",
        )
        if not path_text:
            return
        self._submit_upload_paths((Path(path_text),))

    def enter_displayed_folder(self, row: int) -> None:
        """Enter a displayed folder row."""
        if row < 0 or row >= len(self._items):
            return
        item = self._items[row]
        if item.kind is not WopanItemKind.FOLDER:
            return
        self._breadcrumb.append(BreadcrumbEntry(item_id=item.item_id, name=item.name))
        self.refresh_current_directory()

    def open_breadcrumb_index(self, index: int) -> None:
        """Open a breadcrumb entry by index."""
        if index < 0 or index >= len(self._breadcrumb):
            return
        self._breadcrumb = self._breadcrumb[: index + 1]
        self.refresh_current_directory()

    def open_file_context_menu(self, position: QPoint) -> None:
        """Open file context menu for the file table."""
        table = self.file_interface.file_table
        row = table.rowAt(position.y())
        item = self._item_at_row(row)
        menu = QMenu(self)
        if item is None:
            menu.addAction("刷新", self.refresh_current_directory)
            menu.addAction("新建文件夹", self.prompt_create_folder)
            menu.addAction("上传文件", self.prompt_upload_file)
            menu.addAction("上传文件夹", self.prompt_upload_folder)
        else:
            if item.kind is WopanItemKind.FOLDER:
                menu.addAction("打开", lambda: self.enter_displayed_folder(row))
            else:
                download_action = QAction("下载", self)
                download_action.triggered.connect(lambda: self.prompt_download_item(row))
                menu.addAction(download_action)
            menu.addAction("重命名", lambda: self.prompt_rename_item(row))
            menu.addAction("移动", lambda: self.prompt_move_item(row))
            menu.addAction("复制", lambda: self.prompt_copy_item(row))
            menu.addAction("删除", lambda: self.prompt_delete_item(row))
        viewport = table.viewport()
        if viewport is None:  # pragma: no cover - docs/testing-exemptions.md
            return
        menu.exec(viewport.mapToGlobal(position))

    def current_directory_id(self) -> str:
        """Return the current directory id."""
        return self._breadcrumb[-1].item_id

    def breadcrumb_names(self) -> tuple[str, ...]:
        """Return current breadcrumb names for orchestration and tests."""
        return tuple(entry.name for entry in self._breadcrumb)

    def displayed_items(self) -> tuple[WopanItem, ...]:
        """Return the current displayed file items."""
        return tuple(self._items)

    def selected_rows(self) -> list[int]:
        """Return currently selected file rows in display order."""
        selection_model = self.file_interface.file_table.selectionModel()
        if selection_model is None:  # pragma: no cover - docs/testing-exemptions.md
            return []
        return sorted(index.row() for index in selection_model.selectedRows())

    def selected_download_rows(self) -> list[int]:
        """Return selected file rows that have a downloadable identifier."""
        rows: list[int] = []
        for row in self.selected_rows():
            item = self._item_at_row(row)
            if item is not None and item.kind is WopanItemKind.FILE and item.download_id:
                rows.append(row)
        return rows

    def selected_download_row(self) -> int | None:
        """Return the single selected file row if it can be downloaded."""
        rows = self.selected_download_rows()
        return rows[0] if len(rows) == 1 else None

    def update_operation_controls(self) -> None:
        """Update selection-sensitive operation controls."""
        can_download = self._file_browser is not None and bool(self.selected_download_rows())
        self.file_interface.download_button.setEnabled(can_download)
        can_delete = self._file_browser is not None and bool(self.selected_rows())
        self.file_interface.delete_button.setEnabled(can_delete)
        can_upload = self._file_browser is not None
        self.file_interface.upload_button_group.setEnabled(can_upload)
        self.file_interface.upload_file_action.setEnabled(can_upload)
        self.file_interface.upload_folder_action.setEnabled(can_upload)

    def status_message(self) -> str:
        """Return the current non-sensitive status message."""
        return self._status_message

    def _start_download_task(self, item: WopanItem, local_path: Path, task_id: str) -> None:
        if self._file_browser is None:
            self._set_status("请先登录")
            return
        if self._download_thread is not None:
            self._set_status("已有下载任务正在进行")
            InfoBar.warning(title="下载", content="已有下载任务正在进行", parent=self)
            return

        thread = QThread(self)
        control = DownloadTaskControl()
        worker = DownloadWorker(self._file_browser, item, local_path, task_id, control)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progress.connect(self._on_download_progress)
        worker.status_changed.connect(self._on_download_status_changed)
        worker.connections_changed.connect(self._on_download_connections_changed)
        worker.succeeded.connect(self._on_download_succeeded)
        worker.stopped.connect(self._on_download_stopped)
        worker.failed.connect(self._on_download_failed)
        worker.login_required.connect(self._on_download_login_required)
        worker.succeeded.connect(thread.quit)
        worker.stopped.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.login_required.connect(thread.quit)
        thread.finished.connect(self._clear_download_task)

        self._download_thread = thread
        self._download_worker = worker
        self._download_item = item
        self._download_task_id = task_id
        self._download_controls[task_id] = control
        self._download_items_by_task[task_id] = item
        self.transfer_interface.update_record("download", task_id, status="下载中")
        self._set_status(f"正在下载「{item.name}」...")
        self.update_operation_controls()
        thread.start()

    def _start_next_upload_task(self) -> None:
        while self._upload_pending:
            active_count = len(self._upload_threads)
            if active_count >= max(1, self._settings.max_concurrent_uploads):
                return
            if self._upload_pending[0].task_id in self._upload_threads:
                return
            pending = self._upload_pending.pop(0)
            self._launch_upload_task(
                pending.parent_id,
                pending.local_path,
                pending.task_id,
                upload_name=pending.upload_name,
                show_enqueue_status=pending.show_enqueue_status,
            )

    def _download_with_callbacks(self, item: WopanItem, local_path: Path, task_id: str) -> None:
        if self._file_browser is None:
            return

        def progress_callback(bytes_read: int, total_bytes: object) -> None:
            self._on_download_progress(bytes_read, total_bytes, task_id=task_id)

        try:
            self._file_browser.download_file(
                item,
                local_path,
                progress_callback,
                status_callback=lambda status: self._on_download_status_changed(
                    status,
                    task_id=task_id,
                ),
                connection_callback=lambda active, maximum: self._on_download_connections_changed(
                    active,
                    maximum,
                    task_id=task_id,
                ),
                control=self._download_controls.setdefault(task_id, DownloadTaskControl()),
                task_id=task_id,
            )
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise
            self._file_browser.download_file(item, local_path, progress_callback)

    def _start_upload_task(
        self,
        parent_id: str,
        local_path: Path,
        task_id: str,
        *,
        upload_name: str | None = None,
        show_enqueue_status: bool = True,
    ) -> None:
        if self._file_browser is None:
            self._set_status("请先登录")
            return
        active_count = len(self._upload_threads)
        if active_count == 0 and self._upload_thread is not None:
            active_count = 1
        if (
            self._upload_pending
            or task_id in self._upload_threads
            or active_count >= max(1, self._settings.max_concurrent_uploads)
        ):
            self._upload_pending.append(
                PendingUploadTask(
                    parent_id=parent_id,
                    local_path=local_path,
                    task_id=task_id,
                    upload_name=upload_name,
                    show_enqueue_status=show_enqueue_status,
                )
            )
            if show_enqueue_status:
                display_name = upload_name if upload_name is not None else local_path.name
                self._set_status(f"已添加「{display_name}」上传任务")
            if active_count < max(1, self._settings.max_concurrent_uploads):
                self._start_next_upload_task()
            return
        self._launch_upload_task(
            parent_id,
            local_path,
            task_id,
            upload_name=upload_name,
            show_enqueue_status=show_enqueue_status,
        )

    def _launch_upload_task(
        self,
        parent_id: str,
        local_path: Path,
        task_id: str,
        *,
        upload_name: str | None,
        show_enqueue_status: bool,
    ) -> None:
        if self._file_browser is None:
            self._set_status("请先登录")
            return
        thread = QThread(self)
        worker = UploadWorker(self._file_browser, parent_id, local_path, task_id, upload_name)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progress.connect(self._on_upload_progress)
        worker.succeeded.connect(self._on_upload_succeeded)
        worker.failed.connect(self._on_upload_failed)
        worker.login_required.connect(self._on_upload_login_required)
        worker.cancelled.connect(self._on_upload_cancelled)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.login_required.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        thread.finished.connect(self._clear_upload_task)

        self._upload_threads[task_id] = thread
        self._upload_workers[task_id] = worker
        self._upload_thread = thread
        self._upload_worker = worker
        self._upload_path = local_path
        self._upload_task_id = task_id
        self.transfer_interface.update_record("upload", task_id, status="上传中")
        if show_enqueue_status:
            display_name = upload_name if upload_name is not None else local_path.name
            self._set_status(f"已添加「{display_name}」上传任务")
        self.update_operation_controls()
        thread.start()

    def _on_download_progress(
        self,
        bytes_read: int,
        total_bytes: object,
        task_id: str | None = None,
    ) -> None:
        total = total_bytes if isinstance(total_bytes, int) and total_bytes > 0 else None
        record_id = task_id or self._download_task_id
        if record_id is not None:
            self.transfer_interface.update_record(
                "download",
                record_id,
                bytes_done=bytes_read,
                total_bytes=total,
            )
        if total is None:
            self._set_status(f"正在下载：{_format_bytes(bytes_read)}")
            return
        self._set_status(f"正在下载：{_format_bytes(bytes_read)} / {_format_bytes(total)}")

    def _on_download_status_changed(self, status: str, task_id: str | None = None) -> None:
        record_id = task_id or self._download_task_id
        if record_id is None:
            return
        self.transfer_interface.update_record(
            "download",
            record_id,
            status=status,
            can_resume=status == "已暂停",
        )
        if status in {"校验中", "合并中", "已暂停", "已取消"}:
            self._set_status(f"下载状态：{status}")

    def _on_download_connections_changed(
        self,
        active_connections: int,
        max_connections: int,
        task_id: str | None = None,
    ) -> None:
        record_id = task_id or self._download_task_id
        if record_id is None:
            return
        self.transfer_interface.update_record(
            "download",
            record_id,
            active_connections=active_connections,
            max_connections=max_connections,
        )

    def _on_download_succeeded(
        self,
        item_name: str,
        local_path: str,
        task_id: str | None = None,
    ) -> None:
        path = Path(local_path)
        LOGGER.info(
            "main_window.download.success name_length=%s path_name_length=%s",
            len(item_name),
            len(path.name),
        )
        record_id = task_id or self._download_task_id
        if record_id is not None:
            record = self.transfer_interface._find_record("download", record_id)
            total = record.total_bytes or record.size if record is not None else None
            self.transfer_interface.update_record(
                "download",
                record_id,
                status="已完成",
                bytes_done=total or 0,
                total_bytes=total,
            )
        self._set_status(f"下载完成：{path.name}")
        InfoBar.success(title="下载完成", content=path.name, parent=self)

    def _on_download_failed(self, message: str, task_id: str | None = None) -> None:
        LOGGER.warning("main_window.download.failed error=%s", message)
        self._mark_transfer_failed("download", task_id or self._download_task_id, message)
        self._set_status(f"下载失败：{message}")
        InfoBar.error(title="下载失败", content=message, parent=self)

    def _on_download_stopped(self, status: str, task_id: str | None = None) -> None:
        record_id = task_id or self._download_task_id
        if record_id is not None:
            self.transfer_interface.update_record(
                "download",
                record_id,
                status=status,
                active_connections=0,
                can_resume=status == "已暂停",
            )
        self._set_status(f"下载状态：{status}")

    def _on_download_login_required(self, message: str, task_id: str | None = None) -> None:
        self._mark_transfer_failed("download", task_id or self._download_task_id, message)
        self._show_login_required_error(message)

    def _clear_download_task(self) -> None:
        self._delete_finished_thread()
        if self._download_task_id is not None:
            self._download_controls.pop(self._download_task_id, None)
        self._download_thread = None
        self._download_worker = None
        self._download_item = None
        self._download_task_id = None
        self.update_operation_controls()

    def _on_upload_progress(
        self,
        bytes_done: object,
        total_bytes: object,
        task_id: str,
    ) -> None:
        if not isinstance(bytes_done, int) or not isinstance(total_bytes, int):
            return
        if task_id in self._upload_removal_requested:
            return
        self.transfer_interface.update_record(
            "upload",
            task_id,
            bytes_done=bytes_done,
            total_bytes=total_bytes,
        )

    def _on_upload_cancelled(self, task_id: str) -> None:
        folder_child = task_id in self._folder_upload_child_ids or (
            self._folder_upload_active is not None and self._folder_upload_active.task_id == task_id
        )
        if folder_child:
            self._folder_upload_cancel_count += 1
        self.transfer_interface.update_record("upload", task_id, status="已取消")
        if task_id in self._upload_removal_requested:
            self._upload_removal_requested.discard(task_id)
            self.transfer_interface.remove_records("upload", {task_id})
        if not self._closing and not folder_child:
            self.refresh_current_directory()

    def _on_upload_succeeded(self, item: object, task_id: str | None = None) -> None:
        if task_id in self._upload_removal_requested:
            self._on_upload_cancelled(task_id)
            return
        if not isinstance(item, WopanItem):
            LOGGER.warning("main_window.upload.invalid_success_payload")
            self._on_upload_failed("上传结果无效", task_id=task_id)
            return
        LOGGER.info(
            "main_window.upload.success item_id=%s name_length=%s",
            item.item_id,
            len(item.name),
        )
        record_id = task_id or self._upload_task_id
        if record_id is not None:
            total = item.size if item.size is not None else None
            self.transfer_interface.update_record(
                "upload",
                record_id,
                status="已完成",
                bytes_done=total or 0,
                total_bytes=total,
            )
        if (
            record_id is not None
            and self._folder_upload_record_id is not None
            and (
                record_id in self._folder_upload_child_ids
                or (
                    self._folder_upload_active is not None
                    and record_id == self._folder_upload_active.task_id
                )
            )
        ):
            if record_id in self._folder_upload_failed_ids:
                self._folder_upload_failed_ids.remove(record_id)
                self._folder_upload_failure_count -= 1
            self._folder_upload_success_count += 1
            return
        self.refresh_current_directory(
            after=lambda items, still_current: self._report_upload_visibility(item, still_current)
        )

    def _report_upload_visibility(self, item: WopanItem, still_current: bool) -> None:
        visible = not still_current or any(
            displayed_item.item_id == item.item_id
            or (item.download_id is not None and displayed_item.download_id == item.download_id)
            or displayed_item.name == item.name
            for displayed_item in self._items
        )
        if visible:
            self._set_status(f"上传完成：{item.name}")
            InfoBar.success(title="上传完成", content=item.name, parent=self)
            return
        self._set_status(f"已上传「{item.name}」，但刷新后未在当前目录看到，请稍后再刷新")

    def _on_upload_failed(self, message: str, task_id: str | None = None) -> None:
        if task_id in self._upload_removal_requested:
            self._on_upload_cancelled(task_id)
            return
        LOGGER.warning("main_window.upload.failed error=%s", message)
        record_id = task_id or self._upload_task_id
        if record_id is not None and (
            record_id in self._folder_upload_child_ids
            or (
                self._folder_upload_active is not None
                and record_id == self._folder_upload_active.task_id
            )
        ):
            if record_id not in self._folder_upload_failed_ids:
                self._folder_upload_failed_ids.add(record_id)
                self._folder_upload_failure_count += 1
        self._mark_transfer_failed("upload", record_id, message)
        self._set_status(f"上传失败：{message}")
        InfoBar.error(title="上传失败", content=message, parent=self)

    def _on_upload_login_required(self, message: str, task_id: str | None = None) -> None:
        if task_id in self._upload_removal_requested:
            self._on_upload_cancelled(task_id)
            self._show_login_required_error(message)
            return
        failed_task_id = task_id or self._upload_task_id
        self._mark_transfer_failed("upload", failed_task_id, message)
        self._show_login_required_error(message)
        folder_task_ids = {queued.task_id for queued in self._folder_upload_queue}
        if self._folder_upload_active is not None:
            folder_task_ids.add(self._folder_upload_active.task_id)
        if failed_task_id not in folder_task_ids:
            return
        self._mark_transfer_failed("upload", self._folder_upload_record_id, message)
        self._folder_upload_record_id = None
        self._folder_upload_child_ids.clear()
        self._folder_upload_failed_ids.clear()
        LOGGER.info(
            "main_window.folder_upload.login_stopped pending=%s",
            len(self._folder_upload_queue),
        )
        for pending_task_id in folder_task_ids:
            if pending_task_id != failed_task_id:
                self._mark_transfer_failed("upload", pending_task_id, "登录已过期，请重新登录")
        self._folder_upload_active = None
        self._folder_upload_queue = []
        self._folder_upload_success_count = 0
        self._folder_upload_failure_count = 0
        self._folder_upload_target_dir_id = None

    def _clear_upload_task(self) -> None:
        self._delete_finished_thread()
        thread = self.sender()
        task_id = next(
            (
                candidate
                for candidate, candidate_thread in self._upload_threads.items()
                if candidate_thread is thread
            ),
            self._upload_task_id,
        )
        if task_id is not None:
            self._upload_threads.pop(task_id, None)
            self._upload_workers.pop(task_id, None)
        if self._upload_thread is thread or self._upload_thread is None:
            next_task_id = next(iter(self._upload_threads), None)
            self._upload_thread = (
                self._upload_threads[next_task_id] if next_task_id is not None else None
            )
            self._upload_worker = (
                self._upload_workers[next_task_id] if next_task_id is not None else None
            )
            self._upload_task_id = next_task_id
            self._upload_path = None
        self._continue_folder_upload_queue()
        if (
            self._folder_upload_record_id is not None
            and self._folder_upload_active is None
            and not self._folder_upload_queue
        ):
            self._finish_folder_upload()
        self._start_next_upload_task()
        self.update_operation_controls()
        self._start_next_pending_folder()

    def _create_download_record(
        self,
        item: WopanItem,
        local_path: Path,
        *,
        task_id: str | None = None,
        status: str = "等待中",
        error: str = "",
    ) -> str:
        task_id = task_id or self._next_transfer_task_id("download")
        record = TransferRecord(
            task_id=task_id,
            direction="download",
            name=item.name,
            size=item.size,
            target_path=local_path,
            status=status,
            error=error,
        )
        self.transfer_interface.add_download_record(record)
        self._download_items_by_task[task_id] = item
        return task_id

    def _create_upload_record(
        self,
        local_path: Path,
        *,
        name: str | None = None,
        size: int | None = None,
        parent_id: str | None = None,
        upload_name: str | None = None,
        retryable: bool = False,
    ) -> str:
        task_id = self._next_transfer_task_id("upload")
        if size is None:
            size = (
                local_path.stat().st_size if local_path.exists() and local_path.is_file() else None
            )
        record = TransferRecord(
            task_id=task_id,
            direction="upload",
            name=name if name is not None else local_path.name,
            size=size,
            target_path=local_path,
            upload_parent_id=parent_id,
            upload_name=upload_name,
            upload_retryable=retryable,
        )
        self.transfer_interface.add_upload_record(record)
        return task_id

    def _next_transfer_task_id(self, direction: str) -> str:
        self._transfer_sequence += 1
        return f"{direction}-{self._transfer_sequence}"

    def _mark_transfer_failed(
        self,
        direction: str,
        task_id: str | None,
        message: str,
    ) -> None:
        if task_id is None:
            return
        self.transfer_interface.update_record(
            direction,
            task_id,
            status="失败",
            error=message,
        )

    def _pause_selected_downloads(self, task_ids: object) -> None:
        if not isinstance(task_ids, set):
            return
        for task_id in sorted(task_ids):
            self._pause_download_task(task_id)

    def _resume_selected_downloads(self, task_ids: object) -> None:
        if not isinstance(task_ids, set):
            return
        for task_id in sorted(task_ids):
            self._resume_download_task(task_id)

    def _pause_selected_uploads(self, task_ids: object) -> None:
        if not isinstance(task_ids, set):
            return
        for task_id in sorted(task_ids):
            self._pause_upload_task(task_id)

    def _resume_selected_uploads(self, task_ids: object) -> None:
        if not isinstance(task_ids, set):
            return
        for task_id in sorted(task_ids):
            self._resume_upload_task(task_id)

    def _pause_upload_task(self, task_id: str) -> None:
        record = self.transfer_interface._find_record("upload", task_id)
        if record is None or not record.upload_retryable:
            return
        worker = self._upload_workers.get(task_id)
        if worker is not None:
            worker.request_pause()
            self.transfer_interface.update_record(
                "upload", task_id, status="已暂停", can_resume=True
            )
            active = self._folder_upload_active
            if active is not None and active.task_id == task_id:
                self._paused_uploads[task_id] = PendingUploadTask(
                    parent_id=active.target_dir_id,
                    local_path=active.local_path,
                    task_id=active.task_id,
                    upload_name=active.upload_name,
                    show_enqueue_status=False,
                )
                self._folder_upload_active = None
                self._continue_folder_upload_queue()
                self._start_next_upload_task()
            return
        pending = next((item for item in self._upload_pending if item.task_id == task_id), None)
        if pending is None:
            queued = next(
                (item for item in self._folder_upload_queue if item.task_id == task_id), None
            )
            if queued is not None:
                pending = PendingUploadTask(
                    parent_id=queued.target_dir_id,
                    local_path=queued.local_path,
                    task_id=queued.task_id,
                    upload_name=queued.upload_name,
                    show_enqueue_status=False,
                )
        else:
            self._upload_pending.remove(pending)
        if pending is None:
            return
        if task_id in self._paused_uploads or record.status not in ACTIVE_UPLOAD_STATUSES:
            return
        self._paused_uploads[task_id] = pending
        self.transfer_interface.update_record("upload", task_id, status="已暂停", can_resume=True)
        self._continue_folder_upload_queue()
        self._start_next_upload_task()

    def _resume_upload_task(self, task_id: str) -> None:
        record = self.transfer_interface._find_record("upload", task_id)
        if record is None or record.status != "已暂停":
            return
        worker = self._upload_workers.get(task_id)
        if worker is not None:
            worker.request_resume()
            self._paused_uploads.pop(task_id, None)
            self.transfer_interface.update_record(
                "upload", task_id, status="上传中", can_resume=False
            )
            return
        pending = self._paused_uploads.pop(task_id, None)
        if pending is None:
            return
        if any(item.task_id == task_id for item in self._folder_upload_queue):
            self.transfer_interface.update_record(
                "upload", task_id, status="等待中", can_resume=False
            )
            if self._folder_upload_active is None:
                self._start_next_folder_upload_file()
            return
        self.transfer_interface.update_record("upload", task_id, status="等待中", can_resume=False)
        self._start_upload_task(
            pending.parent_id,
            pending.local_path,
            pending.task_id,
            upload_name=pending.upload_name,
            show_enqueue_status=False,
        )

    def _retry_selected_uploads(self, task_ids: object) -> None:
        """Retry selected failed uploads in their original targets."""
        if not isinstance(task_ids, set):
            return
        records = {
            record.task_id: record
            for record in self.transfer_interface.upload_records
            if record.task_id in task_ids
        }
        for task_id in sorted(task_ids):
            record = records.get(task_id)
            if record is None or not record.upload_retryable or record.status != "失败":
                continue
            self._retry_upload_task(record)

    def _retry_upload_task(self, record: TransferRecord) -> None:
        if self._file_browser is None:
            self._set_status("请先登录")
            return
        if record.target_path is None or not record.upload_parent_id:
            message = "缺少上传任务信息，请重新选择文件上传"
            self._set_status(f"重试上传失败：{message}")
            InfoBar.warning(title="重试上传", content=message, parent=self)
            return
        self.transfer_interface.update_record(
            "upload",
            record.task_id,
            status="等待中",
            bytes_done=0,
            can_resume=False,
            error="",
        )
        self._start_upload_task(
            record.upload_parent_id,
            record.target_path,
            record.task_id,
            upload_name=record.upload_name if record.upload_name is not None else record.name,
            show_enqueue_status=True,
        )

    def _remove_transfer_records(self, direction: str, task_ids: object) -> None:
        if not isinstance(task_ids, set):
            return
        normalized_ids = {task_id for task_id in task_ids if isinstance(task_id, str)}
        if direction == "download" and self._file_browser is not None:
            remove_record = getattr(self._file_browser, "remove_download_record", None)
            if callable(remove_record):
                for task_id in normalized_ids:
                    self._queue_download_operation(
                        "remove", task_id, partial(remove_record, task_id)
                    )
                return
        if direction == "upload":
            self._upload_delete_batch = True
            try:
                for task_id in sorted(
                    normalized_ids,
                    key=lambda value: (
                        value == self._folder_upload_record_id,
                        value in self._upload_workers,
                    ),
                ):
                    self._remove_upload_task(task_id)
            finally:
                self._upload_delete_batch = False
            self._continue_folder_upload_queue()
            self._start_next_upload_task()
            self._start_next_pending_folder()
            return
        self.transfer_interface.remove_records(direction, normalized_ids)
        for task_id in normalized_ids:
            self._download_items_by_task.pop(task_id, None)
            self._download_controls.pop(task_id, None)

    def _remove_upload_task(self, task_id: str) -> None:
        record = self.transfer_interface._find_record("upload", task_id)
        if record is None or task_id in self._upload_removal_requested:
            return
        if record.status in TERMINAL_TRANSFER_STATUSES:
            self.transfer_interface.remove_records("upload", {task_id})
            return
        if task_id == self._folder_upload_record_id:
            self._cancel_folder_upload()
            return
        paused = self._paused_uploads.pop(task_id, None)
        if paused is not None:
            worker = self._upload_workers.get(task_id)
            if worker is not None:
                self._upload_removal_requested.add(task_id)
                worker.request_cancel()
                self._set_status("正在停止上传；已发出的请求可能仍会在云端完成")
                return
            queued = next(
                (item for item in self._folder_upload_queue if item.task_id == task_id), None
            )
            if queued is not None:
                self._folder_upload_queue.remove(queued)
            if task_id in self._folder_upload_child_ids:
                self._folder_upload_cancel_count += 1
            if (
                self._folder_upload_active is not None
                and self._folder_upload_active.task_id == task_id
            ):
                self._folder_upload_active = None
            self.transfer_interface.remove_records("upload", {task_id})
            self._continue_folder_upload_queue()
            if self._folder_upload_record_id is not None and not self._folder_upload_queue:
                self._finish_folder_upload()
            return
        pending = next((item for item in self._upload_pending if item.task_id == task_id), None)
        if pending is not None:
            self._upload_pending.remove(pending)
        folder_pending = next(
            (item for item in self._folder_prepare_pending if item.record_id == task_id), None
        )
        if folder_pending is not None:
            self._folder_prepare_pending.remove(folder_pending)
        queued = next((item for item in self._folder_upload_queue if item.task_id == task_id), None)
        if queued is not None:
            self._folder_upload_queue.remove(queued)
            self._folder_upload_cancel_count += 1
        active = self._folder_upload_active
        if active is not None and active.task_id == task_id and task_id not in self._upload_workers:
            self._folder_upload_cancel_count += 1
            self._folder_upload_active = None
            self.transfer_interface.remove_records("upload", {task_id})
            if self._folder_upload_queue:
                self._start_next_folder_upload_file()
            else:
                self._finish_folder_upload()
            return
        if pending is not None or folder_pending is not None or queued is not None:
            self.transfer_interface.remove_records("upload", {task_id})
            self._start_next_pending_folder()
            return
        worker = self._upload_workers.get(task_id)
        if worker is not None:
            self._upload_removal_requested.add(task_id)
            worker.request_cancel()
            self._set_status("正在停止上传；已发出的请求可能仍会在云端完成")
            return
        InfoBar.warning(title="删除上传任务", content="任务尚未退出，请稍后重试", parent=self)

    def _cancel_folder_upload(self) -> None:
        root_id = self._folder_upload_record_id
        if root_id is None:
            return
        self._upload_removal_requested.add(root_id)
        if self._folder_prepare_cancel is not None:
            self._folder_prepare_cancel.set()
        pending_ids = {queued.task_id for queued in self._folder_upload_queue}
        self._folder_upload_queue.clear()
        for child_id in self._folder_upload_child_ids:
            paused = self._paused_uploads.pop(child_id, None)
            worker = self._upload_workers.get(child_id)
            if worker is not None:
                self._upload_removal_requested.add(child_id)
                worker.request_cancel()
            elif paused is not None:
                pending_ids.add(child_id)
            pending_ids.update(
                pending.task_id for pending in self._upload_pending if pending.task_id == child_id
            )
        active = self._folder_upload_active
        if active is not None:
            pending_ids.update(
                pending.task_id
                for pending in self._upload_pending
                if pending.task_id == active.task_id
            )
        self._upload_pending = [
            pending for pending in self._upload_pending if pending.task_id not in pending_ids
        ]
        if pending_ids:
            self.transfer_interface.remove_records("upload", pending_ids)
        if active is not None:
            worker = self._upload_workers.get(active.task_id)
            if worker is not None:
                if active.task_id not in self._folder_upload_child_ids:
                    self._upload_removal_requested.add(active.task_id)
                    worker.request_cancel()
            else:
                self._folder_upload_active = None
        self._set_status("正在停止文件夹上传；已创建的云端内容不会自动删除")
        if self._folder_prepare_thread is None and self._folder_upload_active is None:
            self._finish_folder_upload()

    def _queue_download_operation(
        self, action: str, task_id: str, operation: Callable[[], object]
    ) -> None:
        self._download_operations.append((action, task_id, operation))
        if self._download_operation_thread is None:
            self._start_next_download_operation()

    def _start_next_download_operation(self) -> None:
        if self._closing or not self._download_operations:
            return
        action, task_id, operation = self._download_operations.pop(0)
        thread = QThread(self)
        worker = BrowserOperationWorker(lambda: (action, task_id, operation()))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_download_operation_succeeded)
        worker.failed.connect(self._on_download_operation_failed)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._clear_download_operation)
        self._download_operation_thread = thread
        self._download_operation_worker = worker
        thread.start()

    def _on_download_operation_succeeded(self, result: object) -> None:
        if self._closing:
            return
        action, task_id, _outcome = cast(tuple[str, str, object], result)
        if action == "remove":
            self._removed_download_task_ids.add(task_id)
            self.transfer_interface.remove_records("download", {task_id})
            self._download_items_by_task.pop(task_id, None)
            self._download_controls.pop(task_id, None)
            self._pending_download_events.pop(task_id, None)

    def _on_download_operation_failed(self, message: str) -> None:
        if self._closing:
            return
        self._set_status(f"下载任务操作失败：{message}")
        InfoBar.error(title="下载任务操作失败", content=message, parent=self)

    def _clear_download_operation(self) -> None:
        self._delete_finished_thread()
        self._download_operation_thread = None
        self._download_operation_worker = None
        self._start_next_download_operation()

    def _pause_download_task(self, task_id: str) -> None:
        control = self._download_controls.get(task_id)
        if control is not None:
            control.request_pause()
            self.transfer_interface.update_record(
                "download", task_id, status="已暂停", can_resume=True
            )
            return
        pause_download = getattr(self._file_browser, "pause_download", None)
        if callable(pause_download):
            self._queue_download_operation("pause", task_id, partial(pause_download, task_id))

    def _cancel_download_task(self, task_id: str) -> None:
        control = self._download_controls.get(task_id)
        if control is not None:
            control.request_cancel(cleanup=True)
            self.transfer_interface.update_record(
                "download", task_id, status="已取消", active_connections=0, can_resume=False
            )
            return
        cancel_download = getattr(self._file_browser, "cancel_download", None)
        if callable(cancel_download):
            self._queue_download_operation(
                "cancel", task_id, partial(cancel_download, task_id, cleanup=True)
            )

    def _resume_download_task(self, task_id: str) -> None:
        resume_download = getattr(self._file_browser, "resume_download", None)
        if callable(resume_download):
            self._queue_download_operation("resume", task_id, partial(resume_download, task_id))
            return
        record = self.transfer_interface._find_record("download", task_id)
        if record is None or record.target_path is None:
            return
        item = self._download_items_by_task.get(task_id) or self._find_displayed_item_for_download(
            record
        )
        if item is None:
            self._set_status("无法继续下载，请从文件列表重新创建任务")
            InfoBar.warning(
                title="继续下载",
                content="无法继续下载，请从文件列表重新创建任务",
                parent=self,
            )
            return
        self.transfer_interface.update_record("download", task_id, status="等待中")
        self._start_download_task(item, record.target_path, task_id)

    def _find_displayed_item_for_download(self, record: TransferRecord) -> WopanItem | None:
        for item in self._items:
            if item.kind is WopanItemKind.FILE and item.name == record.name and item.download_id:
                return item
        return None

    def _recover_downloads(self) -> None:
        if self._file_browser is None:
            return
        recover = getattr(self._file_browser, "recover_downloads", None)
        if not callable(recover):
            self._load_persisted_download_records()
            return
        thread = QThread(self)
        worker = BrowserOperationWorker(recover)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_download_recovery_succeeded)
        worker.failed.connect(self._on_download_recovery_failed)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._clear_download_recovery)
        self._download_recovery_thread = thread
        self._download_recovery_worker = worker
        thread.start()

    def _on_download_recovery_succeeded(self, result: object) -> None:
        if not isinstance(result, tuple) or self._closing:
            return
        for persisted in result:
            self._add_persisted_download_record(persisted, render=False)
        if result:
            self.transfer_interface.flush_progress_render()
            self.transfer_interface._render_download_table()

    def _on_download_recovery_failed(self, message: str) -> None:
        LOGGER.warning("main_window.download.recovery.failed error=%s", message)
        self._set_status(f"恢复下载任务失败：{message}")

    def _clear_download_recovery(self) -> None:
        self._delete_finished_thread()
        self._download_recovery_thread = None
        self._download_recovery_worker = None

    def _recover_uploads(self) -> None:
        if self._file_browser is None:
            return
        recover = getattr(self._file_browser, "recover_uploads", None)
        if not callable(recover):
            return
        thread = QThread(self)
        worker = BrowserOperationWorker(recover)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_upload_recovery_succeeded)
        worker.failed.connect(self._on_upload_recovery_failed)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._clear_upload_recovery)
        self._upload_recovery_thread = thread
        self._upload_recovery_worker = worker
        thread.start()

    def _on_upload_recovery_succeeded(self, result: object) -> None:
        if not isinstance(result, tuple) or self._closing:
            return
        for persisted in result:
            self._add_persisted_upload_record(persisted)
        if result:
            self.transfer_interface.flush_progress_render()
            self.transfer_interface._render_upload_table()

    def _on_upload_recovery_failed(self, message: str) -> None:
        LOGGER.warning("main_window.upload.recovery.failed error=%s", message)
        self._set_status(f"恢复上传任务失败：{message}")

    def _clear_upload_recovery(self) -> None:
        self._delete_finished_thread()
        self._upload_recovery_thread = None
        self._upload_recovery_worker = None

    def _add_persisted_upload_record(self, persisted: object) -> None:
        required = ("task_id", "name", "local_path", "target_parent_id", "status")
        if not all(hasattr(persisted, attribute) for attribute in required):
            return
        persisted_record = cast(UploadTaskRecord, persisted)
        record = TransferRecord(
            task_id=persisted_record.task_id,
            direction="upload",
            name=persisted_record.name,
            size=getattr(persisted_record, "file_size", None),
            target_path=persisted_record.local_path,
            status=persisted_record.status,
            error=str(getattr(persisted_record, "error", "") or ""),
            upload_parent_id=persisted_record.target_parent_id,
            upload_name=getattr(persisted_record, "upload_name", None),
            upload_retryable=bool(getattr(persisted_record, "resumable", False)),
        )
        self.transfer_interface.add_upload_record(record)

    def _load_persisted_download_records(self) -> None:
        if self._file_browser is None:
            return
        download_records = getattr(self._file_browser, "download_records", None)
        if not callable(download_records):
            return
        persisted_records = download_records()
        for persisted in persisted_records:
            self._add_persisted_download_record(persisted, render=False)
        if persisted_records:
            self.transfer_interface.flush_progress_render()
            self.transfer_interface._render_download_table()

    def _add_persisted_download_record(self, persisted: object, *, render: bool = True) -> None:
        required = ("task_id", "name", "target_path", "status")
        if not all(hasattr(persisted, attribute) for attribute in required):
            return
        persisted_record = cast(DownloadTaskRecord, persisted)
        record = TransferRecord(
            task_id=persisted_record.task_id,
            direction="download",
            name=persisted_record.name,
            size=persisted_record.total_bytes,
            target_path=persisted_record.target_path,
            status=persisted_record.status,
            bytes_done=persisted_record.bytes_done,
            total_bytes=persisted_record.total_bytes,
            active_connections=persisted_record.active_connections,
            max_connections=persisted_record.max_connections,
            can_resume=persisted_record.supports_resume,
            error=str(getattr(persisted, "error", "") or ""),
        )
        self.transfer_interface.add_download_record(record, render=render)

    def _open_transfer_download_folder(self, folder: object) -> None:
        if not isinstance(folder, Path):
            folder = self._settings.default_download_path
        if not folder.exists():
            self._set_status(f"下载文件夹不存在：{folder}")
            InfoBar.error(title="打开失败", content=f"下载文件夹不存在：{folder}", parent=self)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _item_at_row(self, row: int) -> WopanItem | None:
        if row < 0 or row >= len(self._items):
            return None
        return self._items[row]

    def _show_login_required_error(self, message: str) -> None:
        self._set_status(message)
        self.login_required.emit(message)

    def _on_settings_changed(self, settings: object) -> None:
        if isinstance(settings, AppSettings):
            self._settings = settings
            update_settings = getattr(self._file_browser, "update_settings", None)
            if callable(update_settings):
                update_settings(settings)

    def _set_status(self, message: str) -> None:
        self._status_message = message
        if isinstance(self, QMainWindow):  # pragma: no cover - docs/testing-exemptions.md
            self.statusBar().showMessage(message)
        self.file_interface.status_label.setText(message)

    def _render_items(self) -> None:
        self.file_interface.set_operations_enabled(self._file_browser is not None)
        self.file_interface.render_state(tuple(self._items), tuple(self._breadcrumb))
        self.update_operation_controls()
        if self._items:
            path = " > ".join(self.breadcrumb_names())
            self._set_status(f"{len(self._items)} 项 | 当前路径：{path}")
        elif self._file_browser is None:
            self._set_status("请先登录")
        elif self._status_message == "正在加载...":
            self._set_status("当前文件夹为空")


def _format_speed(speed_bps: float) -> str:
    if speed_bps <= 0:
        return "--"
    return f"{_format_bytes(round(speed_bps))}/s"


def _format_usage_value(usage: WopanCloudUsage) -> str:
    return f"{_format_bytes(usage.used_bytes)} / {_format_bytes(usage.total_bytes)}"


def _usage_percent(usage: WopanCloudUsage) -> int:
    return max(0, min(100, round(usage.used_bytes / usage.total_bytes * 100)))


def _mask_account_id(account_id: str) -> str:
    if len(account_id) == 11 and account_id.isdigit():
        return f"{account_id[:3]}****{account_id[7:]}"
    if len(account_id) <= 4:
        return account_id
    return f"{account_id[:2]}***{account_id[-2:]}"


def _next_available_name(requested_name: str, existing_names: set[str]) -> str:
    if requested_name not in existing_names:
        return requested_name
    suffix = 1
    while True:
        candidate = f"{requested_name} ({suffix})"
        if candidate not in existing_names:
            return candidate
        suffix += 1


def _safe_local_file_name(name: str) -> str:
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().strip(".")
    return safe_name or "download"


def _next_available_file_name(requested_name: str, existing_names: set[str]) -> str:
    if requested_name not in existing_names:
        return requested_name
    path = Path(requested_name)
    suffix = 1
    while True:
        candidate = f"{path.stem} ({suffix}){path.suffix}"
        if candidate not in existing_names:
            return candidate
        suffix += 1
