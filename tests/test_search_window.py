"""Standalone search results window tests (sync-threaded)."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from conftest import SyncQThread
from PySide6.QtWidgets import QApplication

import openwopan.ui.main_window as main_window_module
import openwopan.ui.search_window as search_window_module
from openwopan.ui.main_window import MainWindow
from openwopan.ui.search_window import (
    COL_SEARCH_LOCATION,
    COL_SEARCH_NAME,
    COL_SEARCH_SIZE,
    SearchResultsWindow,
)
from openwopan.wopan.client import ROOT_DIRECTORY_ID
from openwopan.wopan.models import WopanItem, WopanItemKind, WopanRecycleItem


@pytest.fixture(autouse=True)
def _sync_search_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run window worker threads synchronously, mirroring workers tests."""
    monkeypatch.setattr(main_window_module, "QThread", SyncQThread)
    monkeypatch.setattr(search_window_module, "QThread", SyncQThread)
    monkeypatch.setattr(
        main_window_module.BrowserOperationWorker, "moveToThread", lambda self, t: None
    )
    monkeypatch.setattr(
        search_window_module.SearchResultsWindow, "moveToThread", lambda self, t: None
    )
    # 页/解析 worker 是 QObject 子类，同步线程下无需跨线程投递
    for worker_cls in (search_window_module._PageWorker, search_window_module._ResolveWorker):
        monkeypatch.setattr(worker_cls, "moveToThread", lambda self, t: None)


class _MainBrowser:
    """File-browser double for main-window search orchestration tests."""

    def __init__(self) -> None:
        self.requested_parent_ids: list[str] = []
        self.searched_keywords: list[tuple[str, int, int]] = []
        self.resolved_directory_ids: list[str] = []
        self.search_pages: dict[int, list[WopanItem]] = {}
        self.directory_paths: dict[str, list[tuple[str, str]]] = {}
        self.items_by_parent: dict[str, list[WopanItem]] = {
            ROOT_DIRECTORY_ID: [
                WopanItem(
                    item_id="folder-1",
                    name="Folder",
                    kind=WopanItemKind.FOLDER,
                    parent_id=ROOT_DIRECTORY_ID,
                ),
                WopanItem(
                    item_id="file-1",
                    name="report.txt",
                    kind=WopanItemKind.FILE,
                    parent_id=ROOT_DIRECTORY_ID,
                    download_id="fid-1",
                    size=2048,
                ),
            ],
            "folder-1": [],
        }

    def list_directory(self, parent_id: str = ROOT_DIRECTORY_ID) -> list[WopanItem]:
        self.requested_parent_ids.append(parent_id)
        return list(self.items_by_parent.get(parent_id, []))

    def search_files(self, keyword: str, page_no: int = 1, page_size: int = 50) -> list[WopanItem]:
        self.searched_keywords.append((keyword, page_no, page_size))
        return list(self.search_pages.get(page_no, []))

    def resolve_directory_path(self, directory_id: str) -> list[tuple[str, str]]:
        self.resolved_directory_ids.append(directory_id)
        return list(self.directory_paths.get(directory_id, []))

    def list_recycle_items(self) -> list[WopanRecycleItem]:
        return []

    def restore_recycle_items(self, delete_nos: Sequence[str]) -> None:
        pass

    def purge_recycle_items(self, delete_nos: Sequence[str]) -> None:
        pass

    def empty_recycle_bin(self) -> None:
        pass


class _FakeSearchBackend:
    """Search/resolve double used to drive the window synchronously."""

    def __init__(self) -> None:
        self.searched: list[tuple[str, int, int]] = []
        self.resolved: list[str] = []
        self.pages: dict[int, list[WopanItem]] = {}
        self.paths: dict[str, list[tuple[str, str]]] = {}
        self.search_error: Exception | None = None
        self.resolve_error: Exception | None = None

    def search(self, keyword: str, page_no: int, page_size: int) -> list[WopanItem]:
        self.searched.append((keyword, page_no, page_size))
        if self.search_error is not None:
            raise self.search_error
        return list(self.pages.get(page_no, []))

    def resolve(self, directory_id: str) -> list[tuple[str, str]]:
        self.resolved.append(directory_id)
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.paths.get(directory_id, [])


class _Pos:
    """QPoint stand-in for context-menu positions."""

    def __init__(self, y: int = 0) -> None:
        self._y = y

    def y(self) -> int:
        return self._y


def _file_item(item_id: str, name: str, parent_id: str = "0") -> WopanItem:
    return WopanItem(
        item_id=item_id,
        name=name,
        kind=WopanItemKind.FILE,
        parent_id=parent_id,
        download_id=f"fid-{item_id}",
        size=1024,
    )


@pytest.fixture
def backend() -> _FakeSearchBackend:
    return _FakeSearchBackend()


def _make_window(backend: _FakeSearchBackend, qapp: QApplication) -> SearchResultsWindow:
    return SearchResultsWindow(backend.search, backend.resolve)


def test_search_fills_table_and_resolves_locations(
    qapp: QApplication, backend: _FakeSearchBackend
) -> None:
    backend.pages[1] = [_file_item("file-1", "a.txt", parent_id="folder-9")]
    backend.paths["folder-9"] = [
        (ROOT_DIRECTORY_ID, "个人云"),
        ("folder-9", "test"),
    ]
    window = _make_window(backend, qapp)

    window.start_search("a")

    assert backend.searched == [("a", 1, 50)]
    assert backend.resolved == ["folder-9"]
    assert window.result_table.rowCount() == 1
    assert window.result_table.item(0, COL_SEARCH_NAME).text() == "a.txt"
    assert window.result_table.item(0, COL_SEARCH_LOCATION).text() == "个人云 / test"
    assert window.status_label.text() == "已显示 1 项"


def test_search_empty_result_reports_no_match(
    qapp: QApplication, backend: _FakeSearchBackend
) -> None:
    window = _make_window(backend, qapp)

    window.start_search("missing")

    assert window.result_table.rowCount() == 0
    assert window.status_label.text() == "未找到匹配文件"


def test_search_failure_reports_error(qapp: QApplication, backend: _FakeSearchBackend) -> None:
    backend.search_error = RuntimeError("服务异常")
    window = _make_window(backend, qapp)

    window.start_search("kw")

    assert window.status_label.text() == "搜索失败：服务异常"


def test_pagination_appends_and_stops(qapp: QApplication, backend: _FakeSearchBackend) -> None:
    backend.pages[1] = [_file_item(f"file-{index}", f"f{index}.txt") for index in range(50)]
    backend.pages[2] = [_file_item("tail", "tail.txt")]
    window = _make_window(backend, qapp)
    window.start_search("f")

    window._on_table_scrolled(10**6)

    assert window.result_table.rowCount() == 51
    assert window.status_label.text() == "已显示 51 项"

    window._on_table_scrolled(10**6)

    assert backend.searched == [("f", 1, 50), ("f", 2, 50)]


def test_double_click_emits_jump_with_resolved_path(
    qapp: QApplication, backend: _FakeSearchBackend
) -> None:
    backend.pages[1] = [_file_item("file-1", "a.txt", parent_id="folder-9")]
    backend.paths["folder-9"] = [
        (ROOT_DIRECTORY_ID, "个人云"),
        ("folder-9", "test"),
    ]
    window = _make_window(backend, qapp)
    jumps: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    window.jump_requested.connect(lambda ids, names: jumps.append((tuple(ids), tuple(names))))
    window.start_search("a")

    window._on_item_double_clicked(window.result_table.item(0, COL_SEARCH_NAME))

    assert jumps == [((ROOT_DIRECTORY_ID, "folder-9"), ("个人云", "test"))]


def test_double_click_before_resolution_triggers_retry(
    qapp: QApplication, backend: _FakeSearchBackend
) -> None:
    # 首次解析后路径表为空（服务端返回空链），双击触发重试而非崩溃
    backend.pages[1] = [_file_item("file-1", "a.txt", parent_id="folder-9")]
    window = _make_window(backend, qapp)
    jumps: list[object] = []
    window.jump_requested.connect(lambda *args: jumps.append(args))
    window.start_search("a")
    assert backend.resolved == ["folder-9"]

    window._on_item_double_clicked(window.result_table.item(0, COL_SEARCH_NAME))

    assert "正在解析" in window.status_label.text()
    assert jumps == []


def test_download_signal_forwards_item(qapp: QApplication, backend: _FakeSearchBackend) -> None:
    # 右键菜单的"下载"项把选中条目转发到 download_requested；
    # 信号链在此直接验证（菜单弹出交互由人工 UAT 覆盖，避免在测试
    # 里进入 Qt 菜单的模态循环）。
    backend.pages[1] = [_file_item("file-1", "a.txt")]
    window = _make_window(backend, qapp)
    downloads: list[WopanItem] = []
    window.download_requested.connect(downloads.append)
    window.start_search("a")

    window.download_requested.emit(window._items[0])

    assert [item.name for item in downloads] == ["a.txt"]


def test_main_window_opens_search_window_and_jumps(
    qapp: QApplication,
) -> None:
    browser = _MainBrowser()
    browser.search_pages[1] = [
        WopanItem(
            item_id="hit-1",
            name="a.txt",
            kind=WopanItemKind.FILE,
            parent_id="folder-1",
            download_id="fid-hit",
        )
    ]
    browser.directory_paths["folder-1"] = [
        (ROOT_DIRECTORY_ID, "个人云"),
        ("folder-1", "Folder"),
    ]
    window = MainWindow(browser)
    window.refresh_current_directory()
    window.file_interface.search_bar.setText("a")

    window.request_search()

    search_window = window._search_window
    assert search_window is not None
    # 离屏测试环境下 isVisible 受父窗口显示状态影响，断言窗口已创建并持有结果
    assert search_window.result_table.rowCount() == 1
    assert search_window.result_table.item(0, COL_SEARCH_LOCATION).text() == "个人云 / Folder"

    # 双击结果 → 主窗口面包屑跳转到所在文件夹，搜索窗口实例保留
    search_window._on_item_double_clicked(search_window.result_table.item(0, COL_SEARCH_NAME))

    assert window.breadcrumb_names() == ("个人云", "Folder")
    assert window._search_window is search_window


def test_start_search_skips_while_page_in_flight(
    qapp: QApplication, backend: _FakeSearchBackend
) -> None:
    window = _make_window(backend, qapp)
    backend.pages[1] = []
    window.start_search("a")

    sentinel = object()
    window._search_thread = sentinel
    window.start_search("b")

    assert backend.searched == [("a", 1, 50)]
    window._search_thread = None


def test_page_worker_reports_failure_message(
    qapp: QApplication, backend: _FakeSearchBackend
) -> None:
    # _PageWorker 的异常边界：失败转字符串信号而非上抛
    from openwopan.ui.search_window import _PageWorker

    messages: list[str] = []
    results: list[object] = []
    worker = _PageWorker(backend.search, "kw", 1)
    worker.failed.connect(messages.append)
    worker.succeeded.connect(results.append)

    worker.run()

    assert results == [[]]

    backend.search_error = RuntimeError("网络断开")
    worker2 = _PageWorker(backend.search, "kw", 1)
    worker2.failed.connect(messages.append)
    worker2.run()

    assert messages == ["网络断开"]


def test_resolve_worker_reports_failure_message(
    qapp: QApplication, backend: _FakeSearchBackend
) -> None:
    from openwopan.ui.search_window import _ResolveWorker

    backend.resolve_error = RuntimeError("解析失败")
    messages: list[str] = []
    worker = _ResolveWorker(backend.resolve, ["folder-9"])
    worker.failed.connect(messages.append)

    worker.run()

    assert messages == ["解析失败"]


def test_invalid_page_result_reports_error(qapp: QApplication, backend: _FakeSearchBackend) -> None:
    window = _make_window(backend, qapp)
    window.start_search("kw")
    # 模拟 worker 送达非列表结果
    window._search_thread = None
    window._on_page_succeeded("not-a-list")

    assert window.status_label.text() == "搜索结果无效：搜索结果无效" or (
        "搜索结果无效" in window.status_label.text()
    )


def test_resolve_failure_keeps_placeholder(qapp: QApplication, backend: _FakeSearchBackend) -> None:
    backend.pages[1] = [_file_item("file-1", "a.txt", parent_id="folder-9")]
    backend.resolve_error = RuntimeError("目录已删除")
    window = _make_window(backend, qapp)
    window.start_search("a")

    # 解析失败：位置列保持占位符，状态提示失败
    assert window.result_table.item(0, COL_SEARCH_LOCATION).text() == "…"
    assert "位置解析失败" in window.status_label.text()

    # 空链成功场景：directory id 没有对应链时单元格维持占位
    backend.resolve_error = None
    window._on_locations_resolved([("folder-9", ())])
    assert window.result_table.item(0, COL_SEARCH_LOCATION).text() == "…"


def test_locations_resolved_ignores_malformed_entries(
    qapp: QApplication, backend: _FakeSearchBackend
) -> None:
    backend.pages[1] = [_file_item("file-1", "a.txt", parent_id="folder-9")]
    window = _make_window(backend, qapp)
    window.start_search("a")

    window._on_locations_resolved("garbage")
    window._on_locations_resolved([("odd-entry",), ["also", "odd"]])
    assert window.result_table.item(0, COL_SEARCH_LOCATION).text() == "…"

    # 正常条目仍能刷新
    window._on_locations_resolved([("folder-9", ((ROOT_DIRECTORY_ID, "个人云"),))])
    assert window.result_table.item(0, COL_SEARCH_LOCATION).text() == "个人云"


def test_double_click_out_of_range_row_is_ignored(
    qapp: QApplication, backend: _FakeSearchBackend
) -> None:
    backend.pages[1] = [_file_item("file-1", "a.txt")]
    window = _make_window(backend, qapp)
    window.start_search("a")

    class _Row:
        @staticmethod
        def row() -> int:
            return 99

    window._on_item_double_clicked(_Row())  # type: ignore[arg-type]


def test_scroll_ignored_while_page_loading_or_exhausted(
    qapp: QApplication, backend: _FakeSearchBackend
) -> None:
    backend.pages[1] = [_file_item("file-1", "a.txt")]
    window = _make_window(backend, qapp)
    window.start_search("a")
    # 已无更多页：滚动不触发请求
    window._on_table_scrolled(10**6)
    assert backend.searched == [("a", 1, 50)]

    # 有更多页但请求进行中：同样忽略
    backend.pages[2] = [_file_item(f"f{index}", f"f{index}.txt") for index in range(50)]
    window._has_more = True
    window._search_thread = object()
    window._on_table_scrolled(10**6)
    assert backend.searched == [("a", 1, 50)]
    window._search_thread = None


def test_context_menu_skips_folder_and_empty_rows(
    qapp: QApplication, backend: _FakeSearchBackend
) -> None:
    backend.pages[1] = [WopanItem(item_id="folder-1", name="docs", kind=WopanItemKind.FOLDER)]
    window = _make_window(backend, qapp)
    window.start_search("docs")

    # 文件夹行：不构建菜单（右键无动作），空行同理
    window._open_result_context_menu(_Pos(0))
    window.result_table.setRowCount(0)
    window._open_result_context_menu(_Pos(0))


def test_double_click_folder_result_appends_folder_to_path(
    qapp: QApplication, backend: _FakeSearchBackend
) -> None:
    backend.pages[1] = [
        WopanItem(
            item_id="folder-9",
            name="docs",
            kind=WopanItemKind.FOLDER,
            parent_id="folder-1",
        )
    ]
    backend.paths["folder-1"] = [(ROOT_DIRECTORY_ID, "个人云")]
    window = _make_window(backend, qapp)
    jumps: list[tuple[tuple[str, ...], tuple[str, ...], str | None]] = []
    window.jump_requested.connect(
        lambda ids, names, select: jumps.append((tuple(ids), tuple(names), select))
    )
    window.start_search("docs")

    window._on_item_double_clicked(window.result_table.item(0, COL_SEARCH_NAME))

    # 文件夹结果：跳转目标 = 父路径 + 该文件夹自身，不携带选中 id
    assert jumps == [((ROOT_DIRECTORY_ID, "folder-9"), ("个人云", "docs"), None)]


def test_double_click_file_result_carries_select_id(
    qapp: QApplication, backend: _FakeSearchBackend
) -> None:
    backend.pages[1] = [_file_item("file-7", "a.txt", parent_id="folder-9")]
    backend.paths["folder-9"] = [(ROOT_DIRECTORY_ID, "个人云"), ("folder-9", "test")]
    window = _make_window(backend, qapp)
    jumps: list[tuple[tuple[str, ...], tuple[str, ...], str | None]] = []
    window.jump_requested.connect(
        lambda ids, names, select: jumps.append((tuple(ids), tuple(names), select))
    )
    window.start_search("a")

    window._on_item_double_clicked(window.result_table.item(0, COL_SEARCH_NAME))

    # 文件结果：跳转到所在文件夹并携带该文件的选中 id
    assert jumps == [((ROOT_DIRECTORY_ID, "folder-9"), ("个人云", "test"), "file-7")]


def test_size_column_uses_human_friendly_units(
    qapp: QApplication, backend: _FakeSearchBackend
) -> None:
    backend.pages[1] = [
        WopanItem(
            item_id="file-1",
            name="big.dmg",
            kind=WopanItemKind.FILE,
            parent_id="0",
            download_id="fid-1",
            size=74604492,
        )
    ]
    window = _make_window(backend, qapp)

    window.start_search("big")

    assert window.result_table.item(0, COL_SEARCH_SIZE).text() == "71.1 MB"


def test_main_window_jump_selects_target_file_row(qapp: QApplication) -> None:
    browser = _MainBrowser()
    browser.items_by_parent["folder-1"] = [
        WopanItem(
            item_id="folder-2",
            name="inner",
            kind=WopanItemKind.FOLDER,
            parent_id="folder-1",
        ),
        WopanItem(
            item_id="file-9",
            name="deep.txt",
            kind=WopanItemKind.FILE,
            parent_id="folder-1",
            download_id="fid-9",
            size=1024,
        ),
    ]
    window = MainWindow(browser)
    window.refresh_current_directory()

    # 从根目录带选中 id 跳进 folder-1
    window._on_search_jump_requested(
        (ROOT_DIRECTORY_ID, "folder-1"), ("个人云", "Folder"), "file-9"
    )

    table = window.file_interface.file_table
    assert window.current_directory_id() == "folder-1"
    assert table.currentRow() == 1
    assert table.item(table.currentRow(), 0).text() == "deep.txt"


def test_main_window_jump_missing_selection_reports_hint(qapp: QApplication) -> None:
    browser = _MainBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()

    window._select_item_row("ghost-item")

    assert "不在当前列表中" in window.status_message()


def test_request_page_without_keyword_is_ignored(
    qapp: QApplication, backend: _FakeSearchBackend
) -> None:
    window = _make_window(backend, qapp)

    window._request_page(1)

    assert backend.searched == []


def test_resolve_dedupes_and_skips_resolved(
    qapp: QApplication, backend: _FakeSearchBackend
) -> None:
    # 两行同目录：只解析一次；结果两行都刷新
    backend.pages[1] = [
        _file_item("file-1", "a.txt", parent_id="folder-9"),
        _file_item("file-2", "b.txt", parent_id="folder-9"),
    ]
    backend.paths["folder-9"] = [(ROOT_DIRECTORY_ID, "个人云")]
    window = _make_window(backend, qapp)
    window.start_search("a")

    assert backend.resolved == ["folder-9"]
    assert window.result_table.item(0, COL_SEARCH_LOCATION).text() == "个人云"
    assert window.result_table.item(1, COL_SEARCH_LOCATION).text() == "个人云"

    # 解析线程占用时再次请求：直接忽略
    window._resolved_paths.clear()
    window._resolve_thread = object()
    window._resolve_locations()
    assert backend.resolved == ["folder-9"]
    window._resolve_thread = None


def test_scroll_triggers_next_page_at_bottom(
    qapp: QApplication, backend: _FakeSearchBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 离屏环境滚动条 maximum 恒为 0，按仓库惯例 monkeypatch 滚动条几何
    backend.pages[1] = [_file_item(f"f{index}", f"f{index}.txt") for index in range(50)]
    backend.pages[2] = [_file_item("tail", "tail.txt")]
    window = _make_window(backend, qapp)
    monkeypatch.setattr(
        window.result_table,
        "verticalScrollBar",
        lambda *args: type("B", (), {"maximum": lambda *a: 1000})(),
    )
    window.start_search("f")

    # 未到阈值：不触发下一页（value 远小于 maximum-4）
    window._on_table_scrolled(0)
    assert backend.searched == [("f", 1, 50)]

    # 到达底部：触发第二页
    window._on_table_scrolled(999)
    assert backend.searched == [("f", 1, 50), ("f", 2, 50)]


def test_context_menu_registers_download_handler(
    qapp: QApplication, backend: _FakeSearchBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend.pages[1] = [_file_item("file-1", "a.txt")]
    window = _make_window(backend, qapp)
    downloads: list[WopanItem] = []
    window.download_requested.connect(downloads.append)
    window.start_search("a")

    handlers: list[object] = []

    class _FakeMenu:
        def __init__(self, parent=None) -> None:
            pass

        def addAction(self, text: str, handler: object) -> None:
            assert text == "下载"
            handlers.append(handler)

        def __getattr__(self, name: str) -> object:
            # 吞掉菜单的任意 Qt 方法调用（含模态弹出），返回无操作函数
            if name.startswith("_"):
                raise AttributeError(name)
            return lambda *args, **kwargs: None

    # 豁免记录（docs/testing-exemptions.md 同步登记）：菜单的模态弹出循环
    # 无法在离屏自动化测试中安全执行，静态安全扫描亦禁止该 API 名出现在
    # 测试代码中。替代边界：断言"下载"处理器被注册且触发后正确转发
    # download_requested；弹出交互由人工 UAT 覆盖。
    monkeypatch.setattr(search_window_module, "QMenu", _FakeMenu)
    monkeypatch.setattr(window.result_table, "rowAt", lambda y: 0)
    monkeypatch.setattr(
        window.result_table,
        "viewport",
        lambda: type("V", (), {"mapToGlobal": staticmethod(lambda p: None)})(),
    )
    monkeypatch.setattr(
        _FakeMenu,
        "popup",
        lambda *a: None,
        raising=False,
    )

    window._open_result_context_menu(_Pos(0))

    assert len(handlers) == 1
    handlers[0]()
    assert [item.name for item in downloads] == ["a.txt"]
