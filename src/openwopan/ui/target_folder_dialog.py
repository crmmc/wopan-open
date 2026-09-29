from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, FluentIcon, PrimaryPushButton, PushButton, ToolButton

TransferMode = Literal["move", "copy"]


@dataclass(frozen=True)
class TargetEntry:
    """One browsable folder candidate for the transfer target dialog."""

    item_id: str
    name: str


def mode_label(mode: TransferMode) -> str:
    """Return the Chinese verb shown in titles, buttons, and toasts."""
    return "移动" if mode == "move" else "复制"


class TargetFolderDialog(QDialog):
    """Windows-Explorer-style dialog for browsing to a transfer target folder.

    The dialog owns no threads: it emits ``directory_requested`` and the
    main window feeds results back through ``show_entries`` /
    ``show_load_error``. The current browsed location is always a valid
    target; single-clicking a listed folder selects it instead.
    """

    directory_requested = Signal(str)

    def __init__(
        self,
        mode: TransferMode,
        initial_entry: TargetEntry,
        initial_folders: Sequence[TargetEntry],
        *,
        excluded_ids: frozenset[str] = frozenset(),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._mode = mode
        self._excluded_ids = excluded_ids
        self._path: list[TargetEntry] = [initial_entry]
        self._selected_target = initial_entry
        self._load_in_flight = False
        self._pending_entry: TargetEntry | None = None
        self._pending_appends = False
        self.setWindowTitle(mode_label(mode))
        self.resize(420, 480)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        self._path_label = BodyLabel(self._format_path(), self)
        layout.addWidget(self._path_label)

        nav_layout = QHBoxLayout()
        self._up_button = ToolButton(FluentIcon.UP.icon(), self)
        self._up_button.setToolTip("上级文件夹")
        self._up_button.clicked.connect(self._go_up)
        nav_layout.addWidget(self._up_button)
        self._hint_label = BodyLabel("双击进入文件夹，单击选中为目标", self)
        nav_layout.addWidget(self._hint_label, 1)
        layout.addLayout(nav_layout)

        self._folder_list = QListWidget(self)
        self._folder_list.itemClicked.connect(self._on_item_clicked)
        self._folder_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self._folder_list, 1)

        self._status_label = BodyLabel("", self)
        layout.addWidget(self._status_label)

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        cancel_button = PushButton("取消", self)
        cancel_button.setMinimumWidth(96)
        cancel_button.clicked.connect(self.reject)
        self._ok_button = PrimaryPushButton(self._format_ok_text(), self)
        self._ok_button.setMinimumWidth(96)
        self._ok_button.clicked.connect(self.accept)
        button_layout.addWidget(cancel_button)
        button_layout.addWidget(self._ok_button)
        layout.addLayout(button_layout)

        self._refresh_controls()
        self._show_entries(initial_folders)

    def current_target(self) -> TargetEntry:
        """Return the folder new items would land in."""
        return self._selected_target

    def start_browse(self) -> None:
        """Load the listing for the current location (used right after open)."""
        self._reload_current()

    def show_entries(self, entries: Sequence[TargetEntry]) -> None:
        """Display one browsed level, dropping excluded folders."""
        usable = [entry for entry in entries if entry.item_id not in self._excluded_ids]
        self._show_entries(usable)
        if self._pending_appends and self._pending_entry is not None:
            self._path.append(self._pending_entry)
        self._pending_entry = None
        self._pending_appends = False
        self._selected_target = self._path[-1]
        self._path_label.setText(self._format_path())
        self._ok_button.setText(self._format_ok_text())
        self._load_in_flight = False
        self._status_label.setText("")
        self._refresh_controls()

    def show_load_error(self, message: str) -> None:
        """Surface a failed directory load and release navigation."""
        self._pending_entry = None
        self._pending_appends = False
        self._load_in_flight = False
        self._status_label.setText(f"加载失败：{message}")
        self._refresh_controls()

    def _show_entries(self, entries: Sequence[TargetEntry]) -> None:
        self._folder_list.clear()
        for entry in entries:
            list_item = QListWidgetItem(FluentIcon.FOLDER.icon(), entry.name)
            list_item.setData(Qt.ItemDataRole.UserRole, entry)
            self._folder_list.addItem(list_item)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        entry = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry, TargetEntry):
            return
        self._selected_target = entry
        self._ok_button.setText(f"{mode_label(self._mode)}到「{entry.name}」")

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        entry = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry, TargetEntry):
            return
        self._enter(entry)

    def _enter(self, entry: TargetEntry) -> None:
        if self._load_in_flight:
            return
        self._load_in_flight = True
        self._pending_entry = entry
        self._pending_appends = True
        self._status_label.setText("正在加载…")
        self._refresh_controls()
        self.directory_requested.emit(entry.item_id)

    def _reload_current(self) -> None:
        if self._load_in_flight:
            return
        self._load_in_flight = True
        self._pending_entry = None
        self._pending_appends = False
        self._status_label.setText("正在加载…")
        self._refresh_controls()
        self.directory_requested.emit(self._path[-1].item_id)

    def _go_up(self) -> None:
        if self._load_in_flight or len(self._path) <= 1:
            return
        self._path.pop()
        self._selected_target = self._path[-1]
        self._path_label.setText(self._format_path())
        self._ok_button.setText(self._format_ok_text())
        self._reload_current()

    def _refresh_controls(self) -> None:
        self._up_button.setEnabled(len(self._path) > 1 and not self._load_in_flight)
        self._folder_list.setEnabled(not self._load_in_flight)

    def _format_path(self) -> str:
        return " / ".join(entry.name for entry in self._path)

    def _format_ok_text(self) -> str:
        location = self._path[-1]
        return f"{mode_label(self._mode)}到此（{location.name}）"


class TransferConflictDialog(QDialog):
    """Ask how to handle items whose names already exist in the target."""

    def __init__(
        self,
        conflict_names: Sequence[str],
        mode: TransferMode,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._resolution: str | None = None
        self.setWindowTitle(f"处理同名项目（{mode_label(mode)}）")
        layout = QVBoxLayout(self)
        layout.addWidget(
            BodyLabel(
                f"目标文件夹已存在 {len(conflict_names)} 个同名项目，请选择处理方式。",
                self,
            )
        )
        preview = QListWidget(self)
        for name in conflict_names[:20]:
            preview.addItem(name)
        if len(conflict_names) > 20:
            preview.addItem(f"另有 {len(conflict_names) - 20} 项")
        layout.addWidget(preview)
        self._preview = preview

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_button = PushButton("取消", self)
        cancel_button.setMinimumWidth(96)
        cancel_button.clicked.connect(self.reject)
        skip_button = PrimaryPushButton("跳过同名项目", self)
        skip_button.setMinimumWidth(96)
        skip_button.clicked.connect(self._skip)
        buttons.addWidget(cancel_button)
        buttons.addWidget(skip_button)
        layout.addLayout(buttons)
        self._skip_button = skip_button

    def resolution(self) -> str | None:
        """Return "skip" when the user chose to skip, None when cancelled."""
        return self._resolution

    def _skip(self) -> None:
        self._resolution = "skip"
        self.accept()
