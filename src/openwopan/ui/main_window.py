from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QItemSelectionModel, QObject, QPoint, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QMainWindow,
    QMenu,
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
)
from openwopan.app.logging_config import app_log_path, set_logging_level
from openwopan.auth.session import AuthSession
from openwopan.storage.settings import AppSettings, app_settings_path, save_app_settings
from openwopan.tasks.download import DownloadTaskControl
from openwopan.wopan.client import ROOT_DIRECTORY_ID
from openwopan.wopan.models import WopanCloudUsage, WopanItem, WopanItemKind

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
UPLOAD_STATUS_FILTERS = ("全部", "等待中", "上传中", "已完成", "失败")
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
FRAME_STYLE = (
    "QFrame#frame, QFrame#listFrame {"
    "border: 1px solid rgba(0, 0, 0, 15);"
    "border-radius: 5px;"
    "background: transparent;"
    "}"
)
LOGGER = logging.getLogger(__name__)
FIF = FluentIcon


def _is_offscreen_platform() -> bool:
    return os.environ.get("QT_QPA_PLATFORM") == "offscreen"


if TYPE_CHECKING:

    class _MainWindowBase(QMainWindow):
        """Type-checking base; runtime may use FluentWindow."""

elif _is_offscreen_platform():

    class _MainWindowBase(QMainWindow):
        """QMainWindow fallback avoids qframelesswindow offscreen crashes."""

else:

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


class MoveTargetDialog(QDialog):
    """Fluent-style dialog for choosing a known move target."""

    def __init__(
        self,
        entries: list[BreadcrumbEntry],
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._entries = entries
        self._selected_index: int | None = None
        self.setWindowTitle("移动到")
        self.resize(420, 420)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        title_label = BodyLabel("移动到", self)
        layout.addWidget(title_label, alignment=Qt.AlignmentFlag.AlignCenter)
        hint_label = BodyLabel("选择目标文件夹", self)
        layout.addWidget(hint_label, alignment=Qt.AlignmentFlag.AlignCenter)

        self._target_tree = TreeWidget(self)
        self._target_tree.setHeaderHidden(True)
        for index, entry in enumerate(entries):
            tree_item = QTreeWidgetItem([entry.name])
            tree_item.setIcon(0, FIF.FOLDER.icon())
            tree_item.setData(0, Qt.ItemDataRole.UserRole, index)
            self._target_tree.addTopLevelItem(tree_item)
        self._target_tree.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self._target_tree, 1)

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        cancel_button = PushButton("取消", self)
        cancel_button.setMinimumWidth(96)
        cancel_button.clicked.connect(self.reject)
        self._ok_button = PrimaryPushButton("移动到此", self)
        self._ok_button.setMinimumWidth(96)
        self._ok_button.setEnabled(False)
        self._ok_button.clicked.connect(self.accept)
        button_layout.addWidget(cancel_button)
        button_layout.addWidget(self._ok_button)
        layout.addLayout(button_layout)

    def selected_entry(self) -> BreadcrumbEntry | None:
        """Return the selected target entry."""
        if self._selected_index is None:
            return None
        return self._entries[self._selected_index]

    def _on_item_clicked(self, item: QTreeWidgetItem) -> None:
        index = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(index, int):
            return
        self._selected_index = index
        self._ok_button.setEnabled(True)
        self._ok_button.setText(f"移动到「{self._entries[index].name}」")


class DownloadWorker(QObject):
    """Background worker for one ordinary file download."""

    progress = Signal(int, object)
    status_changed = Signal(str)
    connections_changed = Signal(int, int)
    succeeded = Signal(str, str)
    stopped = Signal(str)
    failed = Signal(str)
    login_required = Signal(str)

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
                    self.progress.emit,
                    status_callback=self.status_changed.emit,
                    connection_callback=self.connections_changed.emit,
                    control=self._control,
                    task_id=self._task_id,
                )
            except TypeError as exc:
                if "unexpected keyword argument" not in str(exc):
                    raise
                result = self._file_browser.download_file(
                    self._item,
                    self._local_path,
                    self.progress.emit,
                )
        except FileBrowserLoginRequiredError as exc:
            self.login_required.emit(str(exc))
        except FileBrowserError as exc:
            self.failed.emit(str(exc))
        else:
            status = getattr(result, "status", "已完成")
            if status == "已完成":
                self.succeeded.emit(self._item.name, str(self._local_path))
                return
            self.stopped.emit(str(status))


class UploadWorker(QObject):
    """Background worker for one ordinary file upload."""

    succeeded = Signal(object)
    failed = Signal(str)
    login_required = Signal(str)

    def __init__(
        self,
        file_browser: FileBrowserBackend,
        parent_id: str,
        local_path: Path,
    ) -> None:
        super().__init__()
        self._file_browser = file_browser
        self._parent_id = parent_id
        self._local_path = local_path

    def run(self) -> None:
        """Run the blocking upload in a worker thread."""
        try:
            item = self._file_browser.upload_file(self._parent_id, self._local_path)
        except FileBrowserLoginRequiredError as exc:
            self.login_required.emit(str(exc))
        except FileBrowserError as exc:
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(item)


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
    created_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        now = time.monotonic()
        if self.created_at <= 0:
            self.created_at = now
        if self.updated_at <= 0:
            self.updated_at = self.created_at

    @property
    def progress_percent(self) -> int:
        if self.status == "已完成":
            return 100
        total = self.total_bytes or self.size
        if not total or total <= 0:
            return 0
        return max(0, min(100, int(self.bytes_done * 100 / total)))


class TransferInterface(QWidget):
    """Transfer center aligned with the sibling Fluent client."""

    remove_records_requested = Signal(str, object)
    open_download_folder_requested = Signal(object)
    pause_download_requested = Signal(str)
    resume_download_requested = Signal(str)
    cancel_download_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TransferInterface")
        self.upload_records: list[TransferRecord] = []
        self.download_records: list[TransferRecord] = []
        self.upload_status_filter = "全部"
        self.download_status_filter = "全部"
        self._active_direction = "upload"
        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(24, 20, 24, 24)
        self._main_layout.setSpacing(12)

        self._build_top_bar()
        self._build_content()
        self._connect_signals()
        self._render_all()
        self._on_segment_changed("upload")

    def add_upload_record(self, record: TransferRecord) -> None:
        """Add or replace an upload task row."""
        self._upsert_record(self.upload_records, record)
        self._render_upload_table()

    def add_download_record(self, record: TransferRecord) -> None:
        """Add or replace a download task row."""
        self._upsert_record(self.download_records, record)
        self._render_download_table()

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
        previous_bytes = record.bytes_done
        previous_time = record.updated_at
        now = time.monotonic()
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
            record.bytes_done = max(0, bytes_done)
            elapsed = max(now - previous_time, 0.001)
            delta = record.bytes_done - previous_bytes
            record.speed_bps = max(0.0, delta / elapsed)
        if error is not None:
            record.error = error
        if record.status in TERMINAL_TRANSFER_STATUSES:
            record.speed_bps = 0.0
            record.active_connections = 0
        record.updated_at = now
        if direction == "upload":
            self._render_upload_table()
        else:
            self._render_download_table()

    def remove_records(self, direction: str, task_ids: set[str]) -> None:
        """Remove task rows by id."""
        if direction == "upload":
            self.upload_records = [
                record for record in self.upload_records if record.task_id not in task_ids
            ]
            self._render_upload_table()
            return
        self.download_records = [
            record for record in self.download_records if record.task_id not in task_ids
        ]
        self._render_download_table()

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
        self.segmented_widget.setCurrentItem("upload")

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
        if vertical_header is not None:
            vertical_header.hide()
        header = table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(TRANSFER_COL_NAME, QHeaderView.ResizeMode.Stretch)
            for column in range(1, len(TRANSFER_TABLE_HEADERS)):
                if column == TRANSFER_COL_ACTION:
                    header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
                    header.resizeSection(column, TRANSFER_ACTION_COLUMN_WIDTH)
                else:
                    header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

        layout.addWidget(batch_bar)
        layout.addWidget(table)
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
        delete_button = PushButton(FIF.DELETE.icon(), "删除", frame)
        for button in (select_all_button, invert_button, delete_button):
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
            "delete": delete_button,
            "count": count_label,
            "speed": speed_label,
        }

    def _connect_signals(self) -> None:
        self.segmented_widget.currentItemChanged.connect(self._on_segment_changed)
        self.upload_filter_combo.currentTextChanged.connect(self._on_upload_filter_changed)
        self.download_filter_combo.currentTextChanged.connect(self._on_download_filter_changed)
        self.open_download_folder_button.clicked.connect(self._request_open_download_folder)
        self.upload_table.itemSelectionChanged.connect(
            lambda: self._update_batch_bar("upload")
        )
        self.download_table.itemSelectionChanged.connect(
            lambda: self._update_batch_bar("download")
        )
        self.upload_batch_buttons["select_all"].clicked.connect(
            lambda: self._select_all(self.upload_table)
        )
        self.upload_batch_buttons["invert"].clicked.connect(
            lambda: self._invert_selection(self.upload_table, len(self._filtered_upload_records()))
        )
        self.upload_batch_buttons["delete"].clicked.connect(
            lambda: self._request_delete_selected("upload")
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
        self._render_upload_table()

    def _on_download_filter_changed(self, status: str) -> None:
        self.download_status_filter = status
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
            table.setCellWidget(
                row,
                TRANSFER_COL_ACTION,
                self._build_row_action_widget(record, direction, table),
            )
        self._update_total_speed(direction)

    def _build_row_action_widget(
        self,
        record: TransferRecord,
        direction: str,
        parent: QWidget,
    ) -> QWidget:
        widget = QWidget(parent)
        widget.setMinimumWidth(TRANSFER_ACTION_COLUMN_WIDTH)
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)
        layout.addStretch(1)
        if direction == "download":
            self._add_download_action_buttons(layout, record, widget)
        delete_button = self._build_action_button(FIF.DELETE, "删除", widget)
        delete_button.setEnabled(record.status in TERMINAL_TRANSFER_STATUSES)
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
            and visible[row].status in TERMINAL_TRANSFER_STATUSES
        }
        self._request_delete_ids(direction, task_ids)

    def _request_delete_ids(self, direction: str, task_ids: set[str]) -> None:
        if task_ids:
            self.remove_records_requested.emit(direction, task_ids)

    @staticmethod
    def _select_all(table: TableWidget) -> None:
        table.selectAll()

    @staticmethod
    def _invert_selection(table: TableWidget, row_count: int) -> None:
        selection_model = table.selectionModel()
        if selection_model is None:
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
                QItemSelectionModel.SelectionFlag.Select
                | QItemSelectionModel.SelectionFlag.Rows,
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
        if isinstance(count_label, BodyLabel):
            count_label.setText(f"已选 {count} 项")

    def _update_total_speed(self, direction: str) -> None:
        records = self.upload_records if direction == "upload" else self.download_records
        buttons = (
            self.upload_batch_buttons
            if direction == "upload"
            else self.download_batch_buttons
        )
        total_speed = sum(record.speed_bps for record in records if record.speed_bps > 0)
        speed_label = buttons["speed"]
        if isinstance(speed_label, BodyLabel):
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
        self.download_part_size_spin_box.valueChanged.connect(
            self._on_download_part_size_changed
        )
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

        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(24, 20, 24, 24)
        self._main_layout.setSpacing(12)

        self._build_top_bar()
        self._build_content()
        self._connect_signals()

    def render_state(
        self,
        items: tuple[WopanItem, ...],
        breadcrumb: tuple[BreadcrumbEntry, ...],
    ) -> None:
        """Render file rows, tree, and breadcrumb from current window state."""
        self._render_breadcrumb(breadcrumb)
        self._render_tree(items)
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
        self.storage_label = BodyLabel("云盘空间", self.storage_card)
        self.storage_value_label = BodyLabel("-- / --", self.storage_card)
        self.storage_value_label.setStyleSheet("font-size: 12px; color: gray;")
        storage_top_layout.addWidget(self.storage_icon)
        storage_top_layout.addWidget(self.storage_label)
        storage_top_layout.addStretch(1)
        storage_top_layout.addWidget(self.storage_value_label)
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
        self.file_table = TableWidget(right_panel)
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
        if vertical_header is not None:
            vertical_header.hide()
        header = self.file_table.horizontalHeader()
        if header is not None:
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
        self.refresh_button.clicked.connect(self._window.refresh_current_directory)
        self.back_button.clicked.connect(self._window.go_up_one_level)
        self.download_button.clicked.connect(self._download_selected_row)
        self.delete_button.clicked.connect(self._delete_selected_row)
        self.folder_tree.itemClicked.connect(self._on_tree_item_clicked)
        self.file_table.itemDoubleClicked.connect(
            lambda item: self._window.enter_displayed_folder(item.row())
        )
        self.file_table.itemSelectionChanged.connect(self._window.update_operation_controls)
        self.file_table.customContextMenuRequested.connect(self._window.open_file_context_menu)

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

    def _render_tree(self, items: tuple[WopanItem, ...]) -> None:
        self.folder_tree.clear()
        root_item = QTreeWidgetItem([ROOT_DISPLAY_NAME])
        root_item.setIcon(0, FIF.FOLDER.icon())
        root_item.setData(0, Qt.ItemDataRole.UserRole, ROOT_DIRECTORY_ID)
        self.folder_tree.addTopLevelItem(root_item)
        for item in items:
            if item.kind is not WopanItemKind.FOLDER:
                continue
            tree_item = QTreeWidgetItem([item.name])
            tree_item.setIcon(0, FIF.FOLDER.icon())
            tree_item.setData(0, Qt.ItemDataRole.UserRole, item.item_id)
            root_item.addChild(tree_item)
        root_item.setExpanded(True)

    def _render_table(self, items: tuple[WopanItem, ...]) -> None:
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
        row = self.file_table.currentRow()
        if row >= 0:
            self._window.prompt_delete_item(row)

    def _download_selected_row(self) -> None:
        row = self._window.selected_download_row()
        if row is not None:
            self._window.prompt_download_item(row)

    def _on_tree_item_clicked(self, item: QTreeWidgetItem) -> None:
        item_id = item.data(0, Qt.ItemDataRole.UserRole)
        if item_id == ROOT_DIRECTORY_ID:
            self._window.refresh_root()
            return
        for row, displayed_item in enumerate(self._window.displayed_items()):
            if displayed_item.item_id == item_id:
                self._window.enter_displayed_folder(row)
                return


class MainWindow(_MainWindowBase):
    """Main OpenWoPan window aligned with the sibling Fluent desktop client."""

    login_required = Signal(str)
    logout_requested = Signal()

    def __init__(
        self,
        file_browser: FileBrowserBackend | None = None,
        *,
        settings: AppSettings | None = None,
        settings_path: Path | None = None,
        log_path: Path | None = None,
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
        self._download_thread: QThread | None = None
        self._download_worker: DownloadWorker | None = None
        self._download_item: WopanItem | None = None
        self._download_task_id: str | None = None
        self._download_controls: dict[str, DownloadTaskControl] = {}
        self._download_items_by_task: dict[str, WopanItem] = {}
        self._upload_thread: QThread | None = None
        self._upload_worker: UploadWorker | None = None
        self._upload_path: Path | None = None
        self._upload_task_id: str | None = None
        self._transfer_sequence = 0

        self.setWindowTitle("OpenWoPan")
        self.resize(*MAIN_WINDOW_DEFAULT_SIZE)
        self.setMinimumSize(*MAIN_WINDOW_MINIMUM_SIZE)

        self.file_interface = FileInterface(self)
        self.transfer_interface = TransferInterface(self)
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

        self._stacked_widget: QStackedWidget | None = None
        self._navigation_interface: NavigationInterface | None = None
        self._init_navigation_shell()

        self._render_items()

    def _add_sub_interface(
        self,
        widget: QWidget,
        route_key: str,
        icon: FluentIcon,
        text: str,
        *,
        position: NavigationItemPosition = NavigationItemPosition.TOP,
    ) -> None:
        if isinstance(self, FluentWindow):
            widget.setObjectName(route_key)
            self.addSubInterface(widget, icon, text, position=position)
            return

        if self._stacked_widget is None or self._navigation_interface is None:
            raise RuntimeError("fallback navigation shell is not initialized")
        self._stacked_widget.addWidget(widget)
        self._navigation_interface.addItem(
            routeKey=route_key,
            icon=icon,
            text=text,
            onClick=lambda target=widget, key=route_key: self._switch_to_interface(target, key),
            position=position,
        )

    def _init_navigation_shell(self) -> None:
        if isinstance(self, FluentWindow):
            nav = self.navigationInterface
            nav.setExpandWidth(120)
            nav.setMinimumExpandWidth(0)
            nav.setCollapsible(False)
            nav.setMenuButtonVisible(False)
            self._add_sub_interface(self.file_interface, "files", FIF.FOLDER, "文件")
            self._add_sub_interface(self.transfer_interface, "transfers", FIF.SYNC, "传输")
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
        self._load_persisted_download_records()
        self.refresh_root()
        self.refresh_cloud_usage()

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
        LOGGER.info("main_window.cloud_usage.refresh.start")
        try:
            usage = self._file_browser.get_cloud_usage(self._auth_session.account_id)
        except FileBrowserLoginRequiredError as exc:
            LOGGER.info("main_window.cloud_usage.login_required")
            self._show_login_required_error(str(exc))
        except FileBrowserError as exc:
            LOGGER.warning("main_window.cloud_usage.refresh.failed error=%s", exc)
            self._set_status(f"空间信息刷新失败：{exc}")
            InfoBar.warning(title="空间信息刷新失败", content=str(exc), parent=self)
        else:
            self._cloud_usage = usage
            self.account_interface.set_usage(usage)
            self.file_interface.set_storage_usage(usage)
            self._set_status("空间信息已刷新")
            LOGGER.info(
                "main_window.cloud_usage.refresh.success used_bytes=%s total_bytes=%s",
                usage.used_bytes,
                usage.total_bytes,
            )

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

    def refresh_current_directory(self) -> None:
        """Load the current directory from the application file browser service."""
        if self._file_browser is None:
            self._items = []
            self._set_status("请先登录")
            self._render_items()
            return

        parent_id = self.current_directory_id()
        LOGGER.info("main_window.refresh.start parent_id=%s", parent_id)
        self._set_status("正在加载...")
        try:
            self._items = self._file_browser.list_directory(parent_id)
        except FileBrowserLoginRequiredError as exc:
            self._items = []
            message = str(exc)
            LOGGER.info("main_window.refresh.login_required parent_id=%s", parent_id)
            self._set_status(message)
            self.login_required.emit(message)
        except FileBrowserError as exc:
            self._items = []
            LOGGER.warning("main_window.refresh.failed parent_id=%s error=%s", parent_id, exc)
            self._set_status(f"加载失败：{exc}")
            InfoBar.error(title="加载失败", content=str(exc), parent=self)
        else:
            LOGGER.info(
                "main_window.refresh.success parent_id=%s item_count=%s",
                parent_id,
                len(self._items),
            )
        self._render_items()

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

        try:
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
            created_item = self._file_browser.create_folder(parent_id, folder_name)
        except FileBrowserLoginRequiredError as exc:
            self._show_login_required_error(str(exc))
        except FileBrowserError as exc:
            LOGGER.warning("main_window.create_folder.failed error=%s", exc)
            self._set_status(f"新建文件夹失败：{exc}")
            InfoBar.error(title="新建文件夹失败", content=str(exc), parent=self)
        else:
            LOGGER.info("main_window.create_folder.success item_id=%s", created_item.item_id)
            self.refresh_current_directory()
            if not any(item.item_id == created_item.item_id for item in self._items):
                LOGGER.warning(
                    "main_window.create_folder.not_visible_after_refresh item_id=%s parent_id=%s",
                    created_item.item_id,
                    parent_id,
                )
                self._set_status(f"已创建「{folder_name}」，但刷新后未在当前目录看到，请稍后再刷新")
            else:
                InfoBar.success(title="创建成功", content=f"已创建「{folder_name}」", parent=self)

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

        try:
            self._file_browser.rename_item(item, item_name)
        except FileBrowserLoginRequiredError as exc:
            self._show_login_required_error(str(exc))
        except FileBrowserError as exc:
            self._set_status(f"重命名失败：{exc}")
            InfoBar.error(title="重命名失败", content=str(exc), parent=self)
        else:
            self.refresh_current_directory()

    def delete_displayed_item(self, row: int) -> None:
        """Delete a displayed file or folder row."""
        item = self._item_at_row(row)
        if item is None:
            return
        if self._file_browser is None:
            self._set_status("请先登录")
            return

        try:
            self._file_browser.delete_item(item)
        except FileBrowserLoginRequiredError as exc:
            self._show_login_required_error(str(exc))
        except FileBrowserError as exc:
            self._set_status(f"删除失败：{exc}")
            InfoBar.error(title="删除失败", content=str(exc), parent=self)
        else:
            self.refresh_current_directory()

    def move_displayed_item(self, row: int, target_parent_id: str) -> None:
        """Move a displayed file or folder row to another directory."""
        item = self._item_at_row(row)
        if item is None:
            return
        target_id = target_parent_id.strip()
        if not target_id:
            self._set_status("目标文件夹不能为空")
            InfoBar.warning(title="移动", content="目标文件夹不能为空", parent=self)
            return
        if self._file_browser is None:
            self._set_status("请先登录")
            return

        try:
            self._file_browser.move_item(item, target_id)
        except FileBrowserLoginRequiredError as exc:
            self._show_login_required_error(str(exc))
        except FileBrowserError as exc:
            self._set_status(f"移动失败：{exc}")
            InfoBar.error(title="移动失败", content=str(exc), parent=self)
        else:
            self.refresh_current_directory()

    def download_displayed_item(
        self,
        row: int,
        local_path: Path,
        *,
        run_in_background: bool = True,
    ) -> None:
        """Download a displayed file row to a local path."""
        item = self._item_at_row(row)
        if item is None:
            return
        if item.kind is not WopanItemKind.FILE:
            self._set_status("只能下载文件")
            InfoBar.warning(title="下载", content="只能下载文件", parent=self)
            return
        if not local_path.name:
            self._set_status("保存路径不能为空")
            InfoBar.warning(title="下载", content="保存路径不能为空", parent=self)
            return
        if self._file_browser is None:
            self._set_status("请先登录")
            return

        LOGGER.info(
            "main_window.download.start item_id=%s name_length=%s",
            item.item_id,
            len(item.name),
        )
        task_id = self._create_download_record(item, local_path)
        if run_in_background:
            self._start_download_task(item, local_path, task_id)
            return
        self._set_status(f"正在下载「{item.name}」...")
        try:
            self._download_with_callbacks(item, local_path, task_id)
        except FileBrowserLoginRequiredError as exc:
            self._mark_transfer_failed("download", task_id, str(exc))
            self._show_login_required_error(str(exc))
        except FileBrowserError as exc:
            self._on_download_failed(str(exc), task_id=task_id)
        else:
            self._on_download_succeeded(item.name, str(local_path), task_id=task_id)

    def upload_file_to_current_directory(
        self,
        local_path: Path,
        *,
        run_in_background: bool = True,
    ) -> None:
        """Upload one local file to the current directory."""
        if self._file_browser is None:
            self._set_status("请先登录")
            return
        if not local_path.name:
            self._set_status("上传文件不能为空")
            InfoBar.warning(title="上传", content="上传文件不能为空", parent=self)
            return

        parent_id = self.current_directory_id()
        LOGGER.info(
            "main_window.upload.start parent_id=%s file_name_length=%s",
            parent_id,
            len(local_path.name),
        )
        task_id = self._create_upload_record(local_path)
        if run_in_background:
            self._start_upload_task(parent_id, local_path, task_id)
            return
        self._set_status(f"正在上传「{local_path.name}」...")
        try:
            uploaded_item = self._file_browser.upload_file(parent_id, local_path)
        except FileBrowserLoginRequiredError as exc:
            self._mark_transfer_failed("upload", task_id, str(exc))
            self._show_login_required_error(str(exc))
        except FileBrowserError as exc:
            self._on_upload_failed(str(exc), task_id=task_id)
        else:
            self._on_upload_succeeded(uploaded_item, task_id=task_id)

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
        """Confirm and delete a row."""
        item = self._item_at_row(row)
        if item is None:
            return
        message = MessageBox("确认删除", f"确定要删除「{item.name}」吗？此操作不可恢复。", self)
        accepted = message.exec()
        message.deleteLater()
        if accepted:
            self.delete_displayed_item(row)

    def prompt_move_item(self, row: int) -> None:
        """Prompt for a target directory and move a row."""
        item = self._item_at_row(row)
        if item is None:
            return

        target_entries = [*self._breadcrumb[:-1]]
        target_entries.extend(
            BreadcrumbEntry(item_id=folder.item_id, name=folder.name)
            for folder in self._items
            if folder.kind is WopanItemKind.FOLDER and folder.item_id != item.item_id
        )
        if not target_entries:
            self._set_status("没有可用的目标文件夹")
            InfoBar.warning(title="移动", content="没有可用的目标文件夹", parent=self)
            return

        dialog = MoveTargetDialog(target_entries, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected_entry = dialog.selected_entry()
            if selected_entry is not None:
                self.move_displayed_item(row, selected_entry.item_id)
        dialog.deleteLater()

    def prompt_download_item(self, row: int) -> None:
        """Download a row, prompting for a path only when configured."""
        item = self._item_at_row(row)
        if item is None:
            return
        if item.kind is not WopanItemKind.FILE:
            self._set_status("只能下载文件")
            InfoBar.warning(title="下载", content="只能下载文件", parent=self)
            return

        if not self._settings.ask_download_location:
            local_path = self._resolve_automatic_download_path(item.name)
            if local_path is None:
                return
            self.download_displayed_item(row, local_path)
            return

        path_text, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "保存文件",
            item.name,
        )
        if not path_text:
            return
        self.download_displayed_item(row, Path(path_text))

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
        self.upload_file_to_current_directory(Path(path_text))

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
        else:
            if item.kind is WopanItemKind.FOLDER:
                menu.addAction("打开", lambda: self.enter_displayed_folder(row))
            else:
                download_action = QAction("下载", self)
                download_action.triggered.connect(lambda: self.prompt_download_item(row))
                menu.addAction(download_action)
            menu.addAction("重命名", lambda: self.prompt_rename_item(row))
            menu.addAction("移动", lambda: self.prompt_move_item(row))
            menu.addAction("删除", lambda: self.prompt_delete_item(row))
        viewport = table.viewport()
        if viewport is None:
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

    def selected_download_row(self) -> int | None:
        """Return the single selected file row if it can be downloaded."""
        table = self.file_interface.file_table
        selection_model = table.selectionModel()
        if selection_model is None:
            return None
        rows = sorted({index.row() for index in selection_model.selectedRows()})
        if len(rows) != 1:
            return None
        item = self._item_at_row(rows[0])
        if item is None or item.kind is not WopanItemKind.FILE or not item.download_id:
            return None
        return rows[0]

    def update_operation_controls(self) -> None:
        """Update selection-sensitive operation controls."""
        can_download = (
            self._file_browser is not None
            and self._download_thread is None
            and self.selected_download_row() is not None
        )
        self.file_interface.download_button.setEnabled(can_download)
        can_upload = self._file_browser is not None and self._upload_thread is None
        self.file_interface.upload_button_group.setEnabled(can_upload)
        self.file_interface.upload_file_action.setEnabled(can_upload)
        self.file_interface.upload_folder_action.setEnabled(False)

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
        worker.status_changed.connect(
            lambda status, record_id=task_id: self._on_download_status_changed(
                status,
                task_id=record_id,
            )
        )
        worker.connections_changed.connect(
            lambda active, maximum, record_id=task_id: self._on_download_connections_changed(
                active,
                maximum,
                task_id=record_id,
            )
        )
        worker.succeeded.connect(
            lambda item_name, path, record_id=task_id: self._on_download_succeeded(
                item_name,
                path,
                task_id=record_id,
            )
        )
        worker.stopped.connect(
            lambda status, record_id=task_id: self._on_download_stopped(
                status,
                task_id=record_id,
            )
        )
        worker.failed.connect(
            lambda message, record_id=task_id: self._on_download_failed(
                message,
                task_id=record_id,
            )
        )
        worker.login_required.connect(
            lambda message, record_id=task_id: self._on_download_login_required(
                message,
                task_id=record_id,
            )
        )
        worker.succeeded.connect(thread.quit)
        worker.stopped.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.login_required.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
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

    def _start_upload_task(self, parent_id: str, local_path: Path, task_id: str) -> None:
        if self._file_browser is None:
            self._set_status("请先登录")
            return
        if self._upload_thread is not None:
            self._set_status("已有上传任务正在进行")
            InfoBar.warning(title="上传", content="已有上传任务正在进行", parent=self)
            return

        thread = QThread(self)
        worker = UploadWorker(self._file_browser, parent_id, local_path)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.succeeded.connect(
            lambda item, record_id=task_id: self._on_upload_succeeded(
                item,
                task_id=record_id,
            )
        )
        worker.failed.connect(
            lambda message, record_id=task_id: self._on_upload_failed(
                message,
                task_id=record_id,
            )
        )
        worker.login_required.connect(
            lambda message, record_id=task_id: self._on_upload_login_required(
                message,
                task_id=record_id,
            )
        )
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.login_required.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_upload_task)

        self._upload_thread = thread
        self._upload_worker = worker
        self._upload_path = local_path
        self._upload_task_id = task_id
        self.transfer_interface.update_record("upload", task_id, status="上传中")
        self._set_status(f"正在上传「{local_path.name}」...")
        self.update_operation_controls()
        thread.start()

    def _on_download_progress(
        self,
        bytes_read: int,
        total_bytes: object,
        *,
        task_id: str | None = None,
    ) -> None:
        total = total_bytes if isinstance(total_bytes, int) and total_bytes > 0 else None
        record_id = task_id or self._download_task_id
        if record_id is not None:
            self.transfer_interface.update_record(
                "download",
                record_id,
                status="下载中",
                bytes_done=bytes_read,
                total_bytes=total,
            )
        if total is None:
            self._set_status(f"正在下载：{_format_bytes(bytes_read)}")
            return
        self._set_status(f"正在下载：{_format_bytes(bytes_read)} / {_format_bytes(total)}")

    def _on_download_status_changed(self, status: str, *, task_id: str | None = None) -> None:
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
        *,
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
        *,
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

    def _on_download_failed(self, message: str, *, task_id: str | None = None) -> None:
        LOGGER.warning("main_window.download.failed error=%s", message)
        self._mark_transfer_failed("download", task_id or self._download_task_id, message)
        self._set_status(f"下载失败：{message}")
        InfoBar.error(title="下载失败", content=message, parent=self)

    def _on_download_stopped(self, status: str, *, task_id: str | None = None) -> None:
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

    def _on_download_login_required(self, message: str, *, task_id: str | None = None) -> None:
        self._mark_transfer_failed("download", task_id or self._download_task_id, message)
        self._show_login_required_error(message)

    def _clear_download_task(self) -> None:
        if self._download_task_id is not None:
            self._download_controls.pop(self._download_task_id, None)
        self._download_thread = None
        self._download_worker = None
        self._download_item = None
        self._download_task_id = None
        self.update_operation_controls()

    def _on_upload_succeeded(self, item: object, *, task_id: str | None = None) -> None:
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
        self.refresh_current_directory()
        visible = any(
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

    def _on_upload_failed(self, message: str, *, task_id: str | None = None) -> None:
        LOGGER.warning("main_window.upload.failed error=%s", message)
        self._mark_transfer_failed("upload", task_id or self._upload_task_id, message)
        self._set_status(f"上传失败：{message}")
        InfoBar.error(title="上传失败", content=message, parent=self)

    def _on_upload_login_required(self, message: str, *, task_id: str | None = None) -> None:
        self._mark_transfer_failed("upload", task_id or self._upload_task_id, message)
        self._show_login_required_error(message)

    def _clear_upload_task(self) -> None:
        self._upload_thread = None
        self._upload_worker = None
        self._upload_path = None
        self._upload_task_id = None
        self.update_operation_controls()

    def _create_download_record(self, item: WopanItem, local_path: Path) -> str:
        task_id = self._next_transfer_task_id("download")
        record = TransferRecord(
            task_id=task_id,
            direction="download",
            name=item.name,
            size=item.size,
            target_path=local_path,
        )
        self.transfer_interface.add_download_record(record)
        self._download_items_by_task[task_id] = item
        return task_id

    def _create_upload_record(self, local_path: Path) -> str:
        task_id = self._next_transfer_task_id("upload")
        size = local_path.stat().st_size if local_path.exists() and local_path.is_file() else None
        record = TransferRecord(
            task_id=task_id,
            direction="upload",
            name=local_path.name,
            size=size,
            target_path=local_path,
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

    def _remove_transfer_records(self, direction: str, task_ids: object) -> None:
        if not isinstance(task_ids, set):
            return
        normalized_ids = {task_id for task_id in task_ids if isinstance(task_id, str)}
        if direction == "download" and self._file_browser is not None:
            remove_download_record = getattr(self._file_browser, "remove_download_record", None)
            if callable(remove_download_record):
                for task_id in normalized_ids:
                    remove_download_record(task_id)
        self.transfer_interface.remove_records(direction, normalized_ids)

    def _pause_download_task(self, task_id: str) -> None:
        control = self._download_controls.get(task_id)
        if control is None:
            return
        control.request_pause()
        self.transfer_interface.update_record("download", task_id, status="已暂停", can_resume=True)

    def _cancel_download_task(self, task_id: str) -> None:
        control = self._download_controls.get(task_id)
        if control is None:
            return
        control.request_cancel(cleanup=True)
        self.transfer_interface.update_record(
            "download",
            task_id,
            status="已取消",
            active_connections=0,
            can_resume=False,
        )

    def _resume_download_task(self, task_id: str) -> None:
        record = self.transfer_interface._find_record("download", task_id)
        if record is None or record.target_path is None:
            return
        if self._download_thread is not None:
            self._set_status("已有下载任务正在进行")
            InfoBar.warning(title="下载", content="已有下载任务正在进行", parent=self)
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

    def _load_persisted_download_records(self) -> None:
        if self._file_browser is None:
            return
        download_records = getattr(self._file_browser, "download_records", None)
        if not callable(download_records):
            return
        for persisted in download_records():
            if not all(
                hasattr(persisted, attribute)
                for attribute in ("task_id", "name", "target_path", "status")
            ):
                continue
            record = TransferRecord(
                task_id=str(persisted.task_id),
                direction="download",
                name=str(persisted.name),
                size=getattr(persisted, "total_bytes", None),
                target_path=Path(persisted.target_path),
                status=str(persisted.status),
                bytes_done=int(getattr(persisted, "bytes_done", 0) or 0),
                total_bytes=getattr(persisted, "total_bytes", None),
                active_connections=int(getattr(persisted, "active_connections", 0) or 0),
                max_connections=int(getattr(persisted, "max_connections", 1) or 1),
                can_resume=bool(getattr(persisted, "supports_resume", False)),
                error=str(getattr(persisted, "error", "") or ""),
            )
            self.transfer_interface.add_download_record(record)

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
        if isinstance(self, QMainWindow):
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


def _format_kind(kind: WopanItemKind) -> str:
    if kind is WopanItemKind.FOLDER:
        return "文件夹"
    return "文件"


def _format_size(size: int | None, kind: WopanItemKind) -> str:
    if kind is WopanItemKind.FOLDER:
        return "-"
    if size is None:
        return "-"
    return _format_bytes(size)


def _format_optional_bytes(size: int | None) -> str:
    if size is None:
        return "-"
    return _format_bytes(size)


def _format_bytes(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    value = float(size)
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    if unit == "B":
        return f"{int(value)} {unit}"
    return f"{value:.1f} {unit}"


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
