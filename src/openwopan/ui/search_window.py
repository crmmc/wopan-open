from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QPoint, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QLabel,
    QMenu,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, TableWidget

from openwopan.ui.formatting import format_size as _format_item_size
from openwopan.wopan.client import SEARCH_DEFAULT_PAGE_SIZE
from openwopan.wopan.models import WopanItem, WopanItemKind

SEARCH_PAGE_COLUMNS = ("名称", "大小", "位置")
COL_SEARCH_NAME = 0
COL_SEARCH_SIZE = 1
COL_SEARCH_LOCATION = 2
UNRESOLVED_LOCATION = "…"


class _PageWorker(QObject):
    """Run one search page request on a worker thread."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        search_callable: Callable[[str, int, int], list[WopanItem]],
        keyword: str,
        page_no: int,
    ) -> None:
        super().__init__()
        self._search_callable = search_callable
        self._keyword = keyword
        self._page_no = page_no

    def run(self) -> None:
        try:
            result = self._search_callable(self._keyword, self._page_no, SEARCH_DEFAULT_PAGE_SIZE)
        except Exception as exc:  # worker boundary: report, never propagate
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(result)


class _ResolveWorker(QObject):
    """Resolve a batch of directory ids to root-relative paths."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        resolve_callable: Callable[[str], list[tuple[str, str]]],
        directory_ids: list[str],
    ) -> None:
        super().__init__()
        self._resolve_callable = resolve_callable
        self._directory_ids = directory_ids

    def run(self) -> None:
        resolved: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        try:
            for directory_id in self._directory_ids:
                resolved.append((directory_id, tuple(self._resolve_callable(directory_id))))
        except Exception as exc:  # worker boundary: report, never propagate
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(resolved)


class SearchResultsWindow(QDialog):
    """独立搜索结果窗口。

    结果表（名称/大小/位置）+ 滚动分页；双击结果跳转到所在文件夹（主窗口
    文件视图），窗口保持打开以便连续跳转。跳转路径由注入的
    ``resolve_callable`` 按目录 id 解析（服务层带会话缓存）。

    继承 QDialog：以独立浮层显示在父窗口之上（QWidget 子对象会被嵌入
    父窗口内部绘制，无法作为弹出窗口使用）。
    """

    jump_requested = Signal(object, object, object)  # path_ids, path_names, select_item_id
    download_requested = Signal(object)  # WopanItem

    def __init__(
        self,
        search_callable: Callable[[str, int, int], list[WopanItem]],
        resolve_callable: Callable[[str], list[tuple[str, str]]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._search_callable = search_callable
        self._resolve_callable = resolve_callable
        self._keyword: str | None = None
        self._page_no = 1
        self._has_more = False
        self._items: list[WopanItem] = []
        self._resolved_paths: dict[str, tuple[tuple[str, str], ...]] = {}
        self._search_thread: QThread | None = None
        self._search_worker: _PageWorker | None = None
        self._resolve_thread: QThread | None = None
        self._resolve_worker: _ResolveWorker | None = None
        self.setWindowTitle("搜索结果")
        # 非模态浮层：双击结果跳转主窗口时本窗口保持可用、不被遮挡锁定
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setModal(False)
        self.resize(760, 480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        self.keyword_label = BodyLabel("", self)
        layout.addWidget(self.keyword_label)

        self.result_table = TableWidget(self)
        self.result_table.setColumnCount(len(SEARCH_PAGE_COLUMNS))
        self.result_table.setHorizontalHeaderLabels(SEARCH_PAGE_COLUMNS)
        self.result_table.setBorderVisible(True)
        vertical_header = self.result_table.verticalHeader()
        if vertical_header is not None:
            vertical_header.hide()
        header = self.result_table.horizontalHeader()
        if header is not None:  # pragma: no cover - Qt 表格恒持有表头
            header.setSectionResizeMode(COL_SEARCH_NAME, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(COL_SEARCH_SIZE, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(COL_SEARCH_LOCATION, QHeaderView.ResizeMode.Stretch)
        self.result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.result_table.setWordWrap(False)
        self.result_table.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.result_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.result_table.customContextMenuRequested.connect(self._open_result_context_menu)
        layout.addWidget(self.result_table, 1)

        self.status_label = QLabel("", self)
        layout.addWidget(self.status_label)
        self.result_table.verticalScrollBar().valueChanged.connect(self._on_table_scrolled)

    def start_search(self, keyword: str) -> None:
        """Run a fresh search for ``keyword`` and reset the result table."""
        if self._search_thread is not None:
            return
        self._keyword = keyword
        self._page_no = 1
        self._has_more = False
        self._items = []
        self.keyword_label.setText(f"搜索：「{keyword}」")
        self.result_table.setRowCount(0)
        self._set_status("正在搜索...")
        self._request_page(1)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _request_page(self, page_no: int) -> None:
        keyword = self._keyword
        if keyword is None:
            return
        thread = QThread(self)
        worker = _PageWorker(self._search_callable, keyword, page_no)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_page_succeeded)
        worker.failed.connect(self._on_page_failed)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._clear_page_thread)
        self._page_no = page_no
        self._search_thread = thread
        self._search_worker = worker
        thread.start()

    def _on_page_succeeded(self, result: object) -> None:
        if not isinstance(result, list):
            self._on_page_failed("搜索结果无效")
            return
        items = [item for item in result if isinstance(item, WopanItem)]
        previous = self.result_table.rowCount()
        self.result_table.setRowCount(previous + len(items))
        for offset, item in enumerate(items):
            row = previous + offset
            self._items.append(item)
            self.result_table.setItem(row, COL_SEARCH_NAME, QTableWidgetItem(item.name))
            self.result_table.setItem(
                row,
                COL_SEARCH_SIZE,
                QTableWidgetItem(_format_item_size(item.size, item.kind)),
            )
            location_item = QTableWidgetItem(UNRESOLVED_LOCATION)
            location_item.setData(Qt.ItemDataRole.UserRole, item.parent_id)
            self.result_table.setItem(row, COL_SEARCH_LOCATION, location_item)
        self._has_more = len(items) >= SEARCH_DEFAULT_PAGE_SIZE
        if not self._items:
            self._set_status("未找到匹配文件")
            return
        self._set_status(f"已显示 {len(self._items)} 项")
        self._resolve_locations(rows=list(range(previous, previous + len(items))))

    def _on_page_failed(self, message: str) -> None:
        self._set_status(f"搜索失败：{message}")

    def _clear_page_thread(self) -> None:
        self._search_thread = None
        self._search_worker = None

    def _on_table_scrolled(self, value: int) -> None:
        if self._keyword is None or not self._has_more:
            return
        if self._search_thread is not None:
            return
        bar = self.result_table.verticalScrollBar()
        if value >= bar.maximum() - 4:
            self._request_page(self._page_no + 1)

    def _resolve_locations(self, rows: list[int] | None = None) -> None:
        """Resolve locations for the given rows (default: all pending rows).

        GetDirectoryPath answers one directory in a single request and the
        service caches chains, so resolving a page costs one call per unique
        folder (typically a handful); no viewport lazy-loading is needed.
        """
        if self._resolve_thread is not None:
            return
        pending: list[str] = []
        for row in rows if rows is not None else range(self.result_table.rowCount()):
            location_item = self.result_table.item(row, COL_SEARCH_LOCATION)
            if location_item is None:  # pragma: no cover - defensive
                continue
            directory_id = location_item.data(Qt.ItemDataRole.UserRole)
            if directory_id and directory_id not in self._resolved_paths:
                pending.append(directory_id)
        unique = list(dict.fromkeys(pending))
        if not unique:
            return
        thread = QThread(self)
        worker = _ResolveWorker(self._resolve_callable, unique)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_locations_resolved)
        worker.failed.connect(self._on_resolve_failed)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._clear_resolve_thread)
        self._resolve_thread = thread
        self._resolve_worker = worker
        thread.start()

    def _on_locations_resolved(self, result: object) -> None:
        if not isinstance(result, list):
            return
        for entry in result:
            if not isinstance(entry, tuple) or len(entry) != 2:
                continue
            directory_id, path = entry
            # 空链视为未解析（目录可能已被删除），双击时允许重试。
            if isinstance(path, tuple) and path:
                self._resolved_paths[directory_id] = path
        self._refresh_location_cells()

    def _on_resolve_failed(self, message: str) -> None:
        # 位置列保持"…"，面包屑跳转仍可点击后重试解析
        self._set_status(f"位置解析失败：{message}")

    def _clear_resolve_thread(self) -> None:
        self._resolve_thread = None
        self._resolve_worker = None

    def _refresh_location_cells(self) -> None:
        for row in range(self.result_table.rowCount()):
            location_item = self.result_table.item(row, COL_SEARCH_LOCATION)
            if location_item is None:  # pragma: no cover - defensive
                continue
            directory_id = location_item.data(Qt.ItemDataRole.UserRole)
            path = self._resolved_paths.get(directory_id)
            if path is not None:
                location_item.setText(_format_path(path))

    def _result_item_at_row(self, row: int) -> WopanItem | None:
        return self._items[row] if 0 <= row < len(self._items) else None

    def _on_item_double_clicked(self, item: QTableWidgetItem) -> None:
        row = item.row()
        result_item = self._result_item_at_row(row)
        if result_item is None:
            return
        path = self._resolved_paths.get(result_item.parent_id or "")
        if not path:
            self._set_status("正在解析所在文件夹，请稍后重试...")
            self._resolve_locations()
            return
        if result_item.kind is WopanItemKind.FOLDER:
            # 文件夹结果：跳进该文件夹本身，无需选中行
            path = (*path, (result_item.item_id, result_item.name))
            select_item_id: str | None = None
        else:
            # 文件结果：跳到所在文件夹并选中该文件行
            select_item_id = result_item.item_id
        path_ids = tuple(directory_id for directory_id, _name in path)
        path_names = tuple(name for _id, name in path)
        self.jump_requested.emit(path_ids, path_names, select_item_id)

    def _open_result_context_menu(self, position: QPoint) -> None:
        row = self.result_table.rowAt(position.y())
        result_item = self._result_item_at_row(row)
        if result_item is None or result_item.kind is not WopanItemKind.FILE:
            return
        menu = QMenu(self)
        menu.addAction("下载", lambda: self.download_requested.emit(result_item))
        viewport = self.result_table.viewport()
        if viewport is not None:
            menu.exec(viewport.mapToGlobal(position))


def _format_path(path: tuple[tuple[str, str], ...]) -> str:
    return " / ".join(name for _id, name in path)
