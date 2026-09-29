"""Recycle-bin page with signal-decoupled restore/purge/empty actions."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, MessageBox, PushButton, TableWidget
from qfluentwidgets import FluentIcon as FIF

from openwopan.ui.formatting import format_items_summary, format_kind, format_size
from openwopan.wopan.models import WopanRecycleItem

RECYCLE_TABLE_HEADERS = ("名称", "类型", "大小", "删除时间", "剩余天数")
COL_NAME = 0
COL_KIND = 1
COL_SIZE = 2
COL_DELETED_AT = 3
COL_KEEP_DAYS = 4
EMPTY_STATE_TEXT = "回收站是空的"
UNKNOWN_VALUE = "--"
DELETE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class RecycleInterface(QWidget):
    """Recycle-bin page: list deleted entries and restore/purge/empty them.

    Signal-decoupled like ``SearchResultsWindow``: the page only emits
    signals and never holds a ``MainWindow`` reference. Destructive actions
    confirm through ``MessageBox`` before emitting their signal.
    """

    refresh_requested = Signal()
    restore_requested = Signal(object)  # tuple[str, ...] deleteNos
    purge_requested = Signal(object)  # tuple[str, ...] deleteNos
    empty_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("RecycleInterface")
        self._items: list[WopanRecycleItem] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(12)

        action_layout = QHBoxLayout()
        action_layout.setSpacing(8)
        self.restore_button = PushButton(FIF.RETURN.icon(), "恢复", self)
        self.restore_button.setEnabled(False)
        self.purge_button = PushButton(FIF.DELETE.icon(), "彻底删除", self)
        self.purge_button.setEnabled(False)
        self.empty_button = PushButton(FIF.BROOM.icon(), "清空回收站", self)
        self.empty_button.setEnabled(False)
        self.refresh_button = PushButton(FIF.UPDATE.icon(), "刷新", self)
        action_layout.addWidget(self.restore_button)
        action_layout.addWidget(self.purge_button)
        action_layout.addWidget(self.empty_button)
        action_layout.addStretch(1)
        action_layout.addWidget(self.refresh_button)
        layout.addLayout(action_layout)

        self.item_table = TableWidget(self)
        self.item_table.setColumnCount(len(RECYCLE_TABLE_HEADERS))
        self.item_table.setHorizontalHeaderLabels(list(RECYCLE_TABLE_HEADERS))
        self.item_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.item_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.item_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.item_table.setAlternatingRowColors(True)
        self.item_table.setBorderRadius(8)
        self.item_table.setBorderVisible(True)
        vertical_header = self.item_table.verticalHeader()
        if vertical_header is not None:  # pragma: no cover - docs/testing-exemptions.md
            vertical_header.hide()
        header = self.item_table.horizontalHeader()
        if header is not None:  # pragma: no cover - docs/testing-exemptions.md
            header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
            for column in (COL_KIND, COL_SIZE, COL_DELETED_AT, COL_KEEP_DAYS):
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.item_table, 1)

        self.empty_label = BodyLabel(EMPTY_STATE_TEXT, self)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_label, 1)

        self.restore_button.clicked.connect(self._on_restore_clicked)
        self.purge_button.clicked.connect(self._on_purge_clicked)
        self.empty_button.clicked.connect(self._on_empty_clicked)
        self.refresh_button.clicked.connect(lambda: self.refresh_requested.emit())
        self.item_table.itemSelectionChanged.connect(self._update_action_buttons)

    def render_items(self, items: Sequence[WopanRecycleItem]) -> None:
        """Render recycle-bin rows and refresh the empty/action state."""
        self._items = list(items)
        self.item_table.setRowCount(len(self._items))
        for row, item in enumerate(self._items):
            values = (
                item.name,
                format_kind(item.kind),
                format_size(item.size, item.kind),
                _format_deleted_at(item.deleted_at),
                _format_keep_days(item.keep_days),
            )
            for column, value in enumerate(values):
                table_item = QTableWidgetItem(value)
                table_item.setData(Qt.ItemDataRole.UserRole, item.delete_no)
                if column in (COL_KIND, COL_SIZE, COL_DELETED_AT, COL_KEEP_DAYS):
                    table_item.setTextAlignment(
                        Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight
                    )
                self.item_table.setItem(row, column, table_item)
        has_items = bool(self._items)
        self.item_table.setVisible(has_items)
        self.empty_label.setVisible(not has_items)
        self.empty_button.setEnabled(has_items)
        self._update_action_buttons()

    def selected_items(self) -> tuple[WopanRecycleItem, ...]:
        """Return the selected recycle entries in display order."""
        selection_model = self.item_table.selectionModel()
        if selection_model is None:  # pragma: no cover - docs/testing-exemptions.md
            return ()
        rows = sorted(index.row() for index in selection_model.selectedRows())
        return tuple(self._items[row] for row in rows)

    def _update_action_buttons(self) -> None:
        has_selection = bool(self.selected_items())
        self.restore_button.setEnabled(has_selection)
        self.purge_button.setEnabled(has_selection)

    def _on_restore_clicked(self) -> None:
        items = self.selected_items()
        if not items:
            return
        self.restore_requested.emit(tuple(item.delete_no for item in items))

    def _on_purge_clicked(self) -> None:
        items = self.selected_items()
        if not items:
            return
        summary = format_items_summary([item.name for item in items])
        message = MessageBox(
            "确认彻底删除",
            f"确定要彻底删除{summary}吗？彻底删除后无法恢复。",
            self,
        )
        accepted = message.exec()
        message.deleteLater()
        if accepted:
            self.purge_requested.emit(tuple(item.delete_no for item in items))

    def _on_empty_clicked(self) -> None:
        if not self._items:
            return
        message = MessageBox("清空回收站", "确定要清空回收站吗？清空后所有条目无法恢复。", self)
        accepted = message.exec()
        message.deleteLater()
        if accepted:
            self.empty_requested.emit()


def _format_deleted_at(deleted_at: datetime | None) -> str:
    if deleted_at is None:
        return UNKNOWN_VALUE
    return deleted_at.strftime(DELETE_TIME_FORMAT)


def _format_keep_days(keep_days: int | None) -> str:
    if keep_days is None:
        return UNKNOWN_VALUE
    return f"{keep_days} 天"
