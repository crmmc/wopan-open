"""Recycle-bin page and main-window wiring tests (sync-threaded)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import openwopan.ui.recycle_interface as recycle_module
from openwopan.app.file_browser import FileBrowserError, FileBrowserLoginRequiredError
from openwopan.ui.main_window import MainWindow
from openwopan.ui.recycle_interface import (
    COL_DELETED_AT,
    COL_KEEP_DAYS,
    COL_KIND,
    COL_NAME,
    COL_SIZE,
    EMPTY_STATE_TEXT,
    RECYCLE_TABLE_HEADERS,
    UNKNOWN_VALUE,
    RecycleInterface,
)
from openwopan.ui.table_view import NO_MATCH_TEXT
from openwopan.wopan.models import WopanItemKind, WopanRecycleItem

RESTORE_STATUS = "恢复成功，已回到原位置"
PURGE_STATUS = "已彻底删除，无法恢复"
EMPTY_STATUS = "回收站已清空"


@pytest.fixture(autouse=True)
def _sync_recycle_threads(sync_threads: None) -> None:
    """Run main-window workers synchronously via the shared sync fixture."""


def _accept_result_exec() -> Callable[..., object]:
    """Qt exec() stub returning the class-level ``accept_result`` attribute."""

    def _run(self, *args: object, **kwargs: object) -> object:
        return type(self).accept_result

    return _run


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


@pytest.fixture
def stub_message_box(monkeypatch: pytest.MonkeyPatch):
    _StubMessageBox.instances = []
    _StubMessageBox.accept_result = 1
    monkeypatch.setattr(recycle_module, "MessageBox", _StubMessageBox)
    return _StubMessageBox


def _recycle_item(
    delete_no: str,
    name: str,
    *,
    kind: WopanItemKind = WopanItemKind.FILE,
    size: int | None = 2048,
    deleted_at: datetime | None = datetime(2026, 9, 28, 12, 30, 5),
    keep_days: int | None = 27,
) -> WopanRecycleItem:
    return WopanRecycleItem(
        delete_no=delete_no,
        item_id=f"item-{delete_no}",
        name=name,
        kind=kind,
        size=size,
        deleted_at=deleted_at,
        keep_days=keep_days,
    )


class _RecycleBrowser:
    """File-browser double for recycle-bin main-window wiring tests."""

    def __init__(self, failures: dict[str, Exception] | None = None) -> None:
        self.failures = failures or {}
        self.list_calls = 0
        self.recycle_items: list[WopanRecycleItem] = []
        self.restored_delete_nos: list[tuple[str, ...]] = []
        self.purged_delete_nos: list[tuple[str, ...]] = []
        self.emptied_recycle_bin = 0

    def list_recycle_items(self) -> list[WopanRecycleItem]:
        self.list_calls += 1
        if "list" in self.failures:
            raise self.failures["list"]
        return list(self.recycle_items)

    def restore_recycle_items(self, delete_nos: Sequence[str]) -> None:
        if "restore" in self.failures:
            raise self.failures["restore"]
        self.restored_delete_nos.append(tuple(delete_nos))

    def purge_recycle_items(self, delete_nos: Sequence[str]) -> None:
        if "purge" in self.failures:
            raise self.failures["purge"]
        self.purged_delete_nos.append(tuple(delete_nos))

    def empty_recycle_bin(self) -> None:
        if "empty" in self.failures:
            raise self.failures["empty"]
        self.emptied_recycle_bin += 1


def _action_trigger(window: MainWindow, action: str) -> Callable[[], None]:
    triggers: dict[str, Callable[[], None]] = {
        "list": window.refresh_recycle_items,
        "restore": lambda: window._restore_recycle_items(("d-1",)),
        "purge": lambda: window._purge_recycle_items(("d-1",)),
        "empty": window._empty_recycle_bin,
    }
    return triggers[action]


# ---------------------------------------------------------------------------
# RecycleInterface rendering and signals
# ---------------------------------------------------------------------------


def test_render_items_fills_table_columns(qapp: QApplication) -> None:
    interface = RecycleInterface()
    interface.render_items(
        [
            _recycle_item("d-1", "报告.txt"),
            _recycle_item(
                "d-2",
                "Folder",
                kind=WopanItemKind.FOLDER,
                size=None,
                deleted_at=None,
                keep_days=None,
            ),
        ]
    )

    assert [
        interface.item_table.horizontalHeaderItem(column).text()
        for column in range(len(RECYCLE_TABLE_HEADERS))
    ] == list(RECYCLE_TABLE_HEADERS)
    assert interface.item_table.rowCount() == 2
    assert interface.item_table.item(0, COL_NAME).text() == "报告.txt"
    assert interface.item_table.item(0, COL_KIND).text() == "文件"
    assert interface.item_table.item(0, COL_SIZE).text() == "2.0 KB"
    assert interface.item_table.item(0, COL_DELETED_AT).text() == "2026-09-28 12:30:05"
    assert interface.item_table.item(0, COL_KEEP_DAYS).text() == "27 天"
    assert interface.item_table.item(1, COL_KIND).text() == "文件夹"
    assert interface.item_table.item(1, COL_SIZE).text() == "-"
    assert interface.item_table.item(1, COL_DELETED_AT).text() == UNKNOWN_VALUE
    assert interface.item_table.item(1, COL_KEEP_DAYS).text() == UNKNOWN_VALUE


def test_render_empty_shows_empty_state_and_disables_actions(qapp: QApplication) -> None:
    interface = RecycleInterface()

    interface.render_items([])

    assert interface.item_table.isHidden()
    assert not interface.empty_label.isHidden()
    assert interface.empty_label.text() == EMPTY_STATE_TEXT
    assert not interface.restore_button.isEnabled()
    assert not interface.purge_button.isEnabled()
    assert not interface.empty_button.isEnabled()


def test_selection_updates_restore_and_purge_buttons(qapp: QApplication) -> None:
    interface = RecycleInterface()
    interface.render_items([_recycle_item("d-1", "a.txt"), _recycle_item("d-2", "b.txt")])
    assert not interface.restore_button.isEnabled()
    assert not interface.purge_button.isEnabled()

    interface.item_table.selectRow(0)

    assert interface.restore_button.isEnabled()
    assert interface.purge_button.isEnabled()

    interface.item_table.clearSelection()

    assert not interface.restore_button.isEnabled()
    assert not interface.purge_button.isEnabled()


def test_render_items_clears_stale_row_selection(qapp: QApplication) -> None:
    interface = RecycleInterface()
    interface.render_items(
        [
            _recycle_item("d-1", "a.txt"),
            _recycle_item("d-2", "b.txt"),
            _recycle_item("d-3", "c.txt"),
        ]
    )
    interface.item_table.selectRow(1)
    assert interface.restore_button.isEnabled()

    interface.render_items([_recycle_item("d-1", "a.txt"), _recycle_item("d-3", "c.txt")])

    assert interface.selected_items() == ()
    assert not interface.restore_button.isEnabled()
    assert not interface.purge_button.isEnabled()


def test_restore_button_emits_selected_delete_nos_without_prompt(
    qapp: QApplication, stub_message_box
) -> None:
    interface = RecycleInterface()
    interface.render_items([_recycle_item("d-1", "a.txt"), _recycle_item("d-2", "b.txt")])
    emitted: list[object] = []
    interface.restore_requested.connect(lambda value: emitted.append(value))
    interface.item_table.selectAll()

    interface.restore_button.click()

    assert emitted == [("d-1", "d-2")]
    assert stub_message_box.instances == []


def test_purge_confirmed_emits_delete_nos_with_summary(
    qapp: QApplication, stub_message_box
) -> None:
    interface = RecycleInterface()
    interface.render_items([_recycle_item("d-1", "a.txt"), _recycle_item("d-2", "b.txt")])
    emitted: list[object] = []
    interface.purge_requested.connect(lambda value: emitted.append(value))
    interface.item_table.selectAll()
    stub_message_box.accept_result = 1

    interface.purge_button.click()

    assert stub_message_box.instances[0].title == "确认彻底删除"
    assert stub_message_box.instances[0].content == (
        "确定要彻底删除 2 个对象（a.txt、b.txt）吗？彻底删除后无法恢复。"
    )
    assert stub_message_box.instances[0].deleted
    assert emitted == [("d-1", "d-2")]


def test_purge_cancelled_emits_nothing(qapp: QApplication, stub_message_box) -> None:
    interface = RecycleInterface()
    interface.render_items([_recycle_item("d-1", "a.txt")])
    emitted: list[object] = []
    interface.purge_requested.connect(lambda value: emitted.append(value))
    interface.item_table.selectAll()
    stub_message_box.accept_result = 0

    interface.purge_button.click()

    assert stub_message_box.instances[0].content == (
        "确定要彻底删除「a.txt」吗？彻底删除后无法恢复。"
    )
    assert emitted == []


def test_purge_summary_previews_first_three_names(qapp: QApplication, stub_message_box) -> None:
    interface = RecycleInterface()
    names = ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]
    interface.render_items([_recycle_item(f"d-{index}", name) for index, name in enumerate(names)])
    interface.item_table.selectAll()
    stub_message_box.accept_result = 0

    interface.purge_button.click()

    assert stub_message_box.instances[0].content == (
        "确定要彻底删除 5 个对象（a.txt、b.txt、c.txt 等）吗？彻底删除后无法恢复。"
    )


def test_empty_confirmed_emits_signal(qapp: QApplication, stub_message_box) -> None:
    interface = RecycleInterface()
    interface.render_items([_recycle_item("d-1", "a.txt")])
    emitted: list[bool] = []
    interface.empty_requested.connect(lambda: emitted.append(True))
    stub_message_box.accept_result = 1

    interface.empty_button.click()

    assert stub_message_box.instances[0].title == "清空回收站"
    assert stub_message_box.instances[0].content == (
        "确定要清空回收站吗？清空后所有条目无法恢复。"
    )
    assert stub_message_box.instances[0].deleted
    assert emitted == [True]


def test_empty_cancelled_emits_nothing(qapp: QApplication, stub_message_box) -> None:
    interface = RecycleInterface()
    interface.render_items([_recycle_item("d-1", "a.txt")])
    emitted: list[bool] = []
    interface.empty_requested.connect(lambda: emitted.append(True))
    stub_message_box.accept_result = 0

    interface.empty_button.click()

    assert emitted == []


def test_refresh_button_emits_refresh_requested(qapp: QApplication) -> None:
    interface = RecycleInterface()
    emitted: list[bool] = []
    interface.refresh_requested.connect(lambda: emitted.append(True))

    interface.refresh_button.click()

    assert emitted == [True]


def test_action_handlers_ignore_empty_selection(qapp: QApplication, stub_message_box) -> None:
    interface = RecycleInterface()
    interface.render_items([_recycle_item("d-1", "a.txt")])
    emitted: list[object] = []
    interface.restore_requested.connect(lambda value: emitted.append(value))
    interface.purge_requested.connect(lambda value: emitted.append(value))

    interface._on_restore_clicked()
    interface._on_purge_clicked()

    assert emitted == []
    assert stub_message_box.instances == []


def test_empty_handler_ignores_empty_bin(qapp: QApplication, stub_message_box) -> None:
    interface = RecycleInterface()
    interface.render_items([])

    interface._on_empty_clicked()

    assert stub_message_box.instances == []


# ---------------------------------------------------------------------------
# MainWindow wiring
# ---------------------------------------------------------------------------


def test_switching_to_recycle_page_loads_items(qapp: QApplication) -> None:
    browser = _RecycleBrowser()
    browser.recycle_items = [_recycle_item("d-1", "report.txt")]
    window = MainWindow(browser)

    window._switch_to_interface(window.recycle_interface, "recycle")

    assert browser.list_calls == 1
    assert window.recycle_interface.item_table.rowCount() == 1
    assert window.recycle_interface.item_table.item(0, COL_NAME).text() == "report.txt"

    window._switch_to_interface(window.file_interface, "files")

    assert browser.list_calls == 1


def test_recycle_restore_wiring_reloads_list(qapp: QApplication) -> None:
    browser = _RecycleBrowser()
    browser.recycle_items = [_recycle_item("d-1", "a.txt"), _recycle_item("d-2", "b.txt")]
    window = MainWindow(browser)
    window.refresh_recycle_items()
    window.recycle_interface.item_table.selectRow(0)

    window.recycle_interface.restore_button.click()

    assert browser.restored_delete_nos == [("d-1",)]
    assert browser.list_calls == 2
    assert window.status_message() == RESTORE_STATUS


def test_recycle_purge_wiring_confirmed_reloads_list(
    qapp: QApplication, stub_message_box
) -> None:
    browser = _RecycleBrowser()
    browser.recycle_items = [_recycle_item("d-1", "a.txt"), _recycle_item("d-2", "b.txt")]
    window = MainWindow(browser)
    window.refresh_recycle_items()
    window.recycle_interface.item_table.selectRow(0)
    stub_message_box.accept_result = 1

    window.recycle_interface.purge_button.click()

    assert browser.purged_delete_nos == [("d-1",)]
    assert browser.list_calls == 2
    assert window.status_message() == PURGE_STATUS


def test_recycle_purge_cancelled_skips_backend(qapp: QApplication, stub_message_box) -> None:
    browser = _RecycleBrowser()
    browser.recycle_items = [_recycle_item("d-1", "a.txt")]
    window = MainWindow(browser)
    window.refresh_recycle_items()
    window.recycle_interface.item_table.selectAll()
    stub_message_box.accept_result = 0

    window.recycle_interface.purge_button.click()

    assert browser.purged_delete_nos == []
    assert browser.list_calls == 1


def test_recycle_empty_wiring_confirmed_reloads_list(qapp: QApplication, stub_message_box) -> None:
    browser = _RecycleBrowser()
    browser.recycle_items = [_recycle_item("d-1", "a.txt")]
    window = MainWindow(browser)
    window.refresh_recycle_items()
    stub_message_box.accept_result = 1

    window.recycle_interface.empty_button.click()

    assert browser.emptied_recycle_bin == 1
    assert browser.list_calls == 2
    assert window.status_message() == EMPTY_STATUS


def test_recycle_list_failure_reports_error(qapp: QApplication) -> None:
    browser = _RecycleBrowser(failures={"list": FileBrowserError("网络错误")})
    window = MainWindow(browser)

    window.refresh_recycle_items()

    assert window.status_message() == "回收站加载失败：网络错误"


@pytest.mark.parametrize(
    ("action", "expected_status"),
    [
        ("restore", "恢复失败：网络错误"),
        ("purge", "彻底删除失败：网络错误"),
        ("empty", "清空回收站失败：网络错误"),
    ],
)
def test_recycle_action_failure_reports_error(
    qapp: QApplication, action: str, expected_status: str
) -> None:
    browser = _RecycleBrowser(failures={action: FileBrowserError("网络错误")})
    window = MainWindow(browser)

    _action_trigger(window, action)()

    assert window.status_message() == expected_status


@pytest.mark.parametrize("action", ["list", "restore", "purge", "empty"])
def test_recycle_login_required_maps_to_login_signal(qapp: QApplication, action: str) -> None:
    messages: list[str] = []
    browser = _RecycleBrowser(
        failures={action: FileBrowserLoginRequiredError("登录已过期，请重新登录")}
    )
    window = MainWindow(browser)
    window.login_required.connect(messages.append)

    _action_trigger(window, action)()

    assert messages == ["登录已过期，请重新登录"]
    assert window.status_message() == "登录已过期，请重新登录"


@pytest.mark.parametrize("action", ["list", "restore", "purge", "empty"])
def test_recycle_operations_without_login_ask_for_login(qapp: QApplication, action: str) -> None:
    window = MainWindow()

    _action_trigger(window, action)()

    assert window.status_message() == "请先登录"


def test_recycle_actions_ignore_empty_batch(qapp: QApplication) -> None:
    browser = _RecycleBrowser()
    window = MainWindow(browser)

    window._restore_recycle_items(())
    window._purge_recycle_items(())

    assert browser.restored_delete_nos == []
    assert browser.purged_delete_nos == []


def test_recycle_list_skips_while_busy(qapp: QApplication) -> None:
    browser = _RecycleBrowser()
    window = MainWindow(browser)
    window._recycle_list_thread = object()  # type: ignore[assignment]

    window.refresh_recycle_items()

    assert browser.list_calls == 0
    window._recycle_list_thread = None


def test_recycle_action_skips_while_busy(qapp: QApplication) -> None:
    browser = _RecycleBrowser()
    window = MainWindow(browser)
    window._recycle_action_thread = object()  # type: ignore[assignment]

    window._restore_recycle_items(("d-1",))

    assert browser.restored_delete_nos == []
    window._recycle_action_thread = None


# ---------------------------------------------------------------------------
# Header-click sorting and name filtering (10-01-table-sort-filter)
# ---------------------------------------------------------------------------


def _row_texts(interface: RecycleInterface, column: int) -> list[str]:
    return [
        interface.item_table.item(row, column).text()
        for row in range(interface.item_table.rowCount())
    ]


def test_header_click_cycles_sort_indicator_and_order(qapp: QApplication) -> None:
    interface = RecycleInterface()
    interface.render_items([_recycle_item("d-1", "b.txt"), _recycle_item("d-2", "a.txt")])
    header = interface.item_table.horizontalHeader()

    interface._on_header_section_clicked(COL_NAME)
    assert _row_texts(interface, COL_NAME) == ["a.txt", "b.txt"]
    assert header.sortIndicatorSection() == COL_NAME
    assert header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder

    interface._on_header_section_clicked(COL_NAME)
    assert _row_texts(interface, COL_NAME) == ["b.txt", "a.txt"]
    assert header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder

    # Third click returns to the source order and clears the indicator.
    interface._on_header_section_clicked(COL_NAME)
    assert _row_texts(interface, COL_NAME) == ["b.txt", "a.txt"]
    assert header.sortIndicatorSection() == -1


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        (Qt.SortOrder.AscendingOrder, ["A.txt", "apple.txt", "b.txt", "报告.txt"]),
        (Qt.SortOrder.DescendingOrder, ["报告.txt", "b.txt", "apple.txt", "A.txt"]),
    ],
)
def test_sort_by_name_casefold_with_chinese(
    qapp: QApplication, order: Qt.SortOrder, expected: list[str]
) -> None:
    interface = RecycleInterface()
    interface.render_items(
        [
            _recycle_item("d-1", "b.txt"),
            _recycle_item("d-2", "A.txt"),
            _recycle_item("d-3", "报告.txt"),
            _recycle_item("d-4", "apple.txt"),
        ]
    )

    interface._on_header_section_clicked(COL_NAME)
    if order is Qt.SortOrder.DescendingOrder:
        interface._on_header_section_clicked(COL_NAME)

    assert _row_texts(interface, COL_NAME) == expected


def test_sort_by_size_is_numeric_not_lexicographic(qapp: QApplication) -> None:
    interface = RecycleInterface()
    interface.render_items(
        [
            _recycle_item("d-1", "mb.txt", size=1_000_000),
            _recycle_item("d-2", "kb.txt", size=2048),
            _recycle_item("d-3", "bytes.txt", size=999),
        ]
    )

    interface._on_header_section_clicked(COL_SIZE)

    # Formatted text would sort "1.0 MB" < "2.0 KB" < "999 B"; the data-level
    # key must order by the numeric value instead.
    assert _row_texts(interface, COL_NAME) == ["bytes.txt", "kb.txt", "mb.txt"]


def test_sort_by_kind_puts_folders_first(qapp: QApplication) -> None:
    interface = RecycleInterface()
    interface.render_items(
        [
            _recycle_item("d-1", "a.txt"),
            _recycle_item("d-2", "Zed", kind=WopanItemKind.FOLDER),
        ]
    )

    interface._on_header_section_clicked(COL_KIND)

    assert _row_texts(interface, COL_NAME) == ["Zed", "a.txt"]


def test_sort_by_deleted_at_orders_timestamps_with_none_as_min(
    qapp: QApplication,
) -> None:
    interface = RecycleInterface()
    interface.render_items(
        [
            _recycle_item("d-1", "known-late.txt", deleted_at=datetime(2026, 9, 30, 8, 0, 0)),
            _recycle_item("d-2", "unknown.txt", deleted_at=None),
            _recycle_item("d-3", "known-early.txt", deleted_at=datetime(2026, 9, 1, 8, 0, 0)),
        ]
    )

    interface._on_header_section_clicked(COL_DELETED_AT)

    assert _row_texts(interface, COL_NAME) == ["unknown.txt", "known-early.txt", "known-late.txt"]


def test_sort_by_keep_days_treats_none_as_zero(qapp: QApplication) -> None:
    interface = RecycleInterface()
    interface.render_items(
        [
            _recycle_item("d-1", "five.txt", keep_days=5),
            _recycle_item("d-2", "unknown.txt", keep_days=None),
            _recycle_item("d-3", "one.txt", keep_days=1),
        ]
    )

    interface._on_header_section_clicked(COL_KEEP_DAYS)

    assert _row_texts(interface, COL_NAME) == ["unknown.txt", "one.txt", "five.txt"]


def test_sort_keeps_backing_list_order_untouched(qapp: QApplication) -> None:
    interface = RecycleInterface()
    interface.render_items([_recycle_item("d-1", "b.txt"), _recycle_item("d-2", "a.txt")])

    interface._on_header_section_clicked(COL_NAME)

    assert [item.name for item in interface._items] == ["b.txt", "a.txt"]


def test_sort_state_survives_re_render_with_new_data(qapp: QApplication) -> None:
    interface = RecycleInterface()
    interface.render_items([_recycle_item("d-1", "b.txt"), _recycle_item("d-2", "a.txt")])
    interface._on_header_section_clicked(COL_NAME)

    interface.render_items([_recycle_item("d-3", "c.txt"), _recycle_item("d-4", "A.txt")])

    assert _row_texts(interface, COL_NAME) == ["A.txt", "c.txt"]


def test_name_filter_is_casefold_substring(qapp: QApplication) -> None:
    interface = RecycleInterface()
    interface.render_items(
        [
            _recycle_item("d-1", "Report.TXT"),
            _recycle_item("d-2", "照片.jpg"),
            _recycle_item("d-3", "notes.txt"),
        ]
    )

    interface.name_filter_bar.setText("TXT")

    assert _row_texts(interface, COL_NAME) == ["Report.TXT", "notes.txt"]


def test_name_filter_with_chinese_substring(qapp: QApplication) -> None:
    interface = RecycleInterface()
    interface.render_items([_recycle_item("d-1", "旅行照片.jpg"), _recycle_item("d-2", "报告.pdf")])

    interface.name_filter_bar.setText("照片")

    assert _row_texts(interface, COL_NAME) == ["旅行照片.jpg"]


def test_name_filter_empty_string_is_noop(qapp: QApplication) -> None:
    interface = RecycleInterface()
    interface.render_items([_recycle_item("d-1", "a.txt"), _recycle_item("d-2", "b.txt")])
    interface.name_filter_bar.setText("zzz")

    interface.name_filter_bar.setText("")

    assert _row_texts(interface, COL_NAME) == ["a.txt", "b.txt"]


def test_name_filter_no_match_shows_no_match_state(qapp: QApplication) -> None:
    interface = RecycleInterface()
    interface.render_items([_recycle_item("d-1", "a.txt")])

    interface.name_filter_bar.setText("zzz")

    assert interface.item_table.isHidden()
    assert not interface.empty_label.isHidden()
    assert interface.empty_label.text() == NO_MATCH_TEXT
    # The bin itself is not empty: clearing stays available, row actions are not.
    assert interface.empty_button.isEnabled()
    assert not interface.restore_button.isEnabled()
    assert not interface.purge_button.isEnabled()


def test_filter_and_sort_stacked(qapp: QApplication) -> None:
    interface = RecycleInterface()
    interface.render_items(
        [
            _recycle_item("d-1", "big.txt", size=4096),
            _recycle_item("d-2", "tiny.txt", size=10),
            _recycle_item("d-3", "big-report.pdf", size=2048),
        ]
    )
    interface._on_header_section_clicked(COL_SIZE)

    interface.name_filter_bar.setText("big")

    # Filter first, then the active size sort applies to the filtered subset.
    assert _row_texts(interface, COL_NAME) == ["big-report.pdf", "big.txt"]


def test_restore_and_purge_use_displayed_delete_nos_with_filter_and_sort(
    qapp: QApplication, stub_message_box
) -> None:
    interface = RecycleInterface()
    interface.render_items(
        [
            _recycle_item("d-1", "a.txt"),
            _recycle_item("d-2", "B.txt"),
            _recycle_item("d-3", "ba.txt"),
        ]
    )
    interface._on_header_section_clicked(COL_NAME)
    interface._on_header_section_clicked(COL_NAME)  # descending
    interface.name_filter_bar.setText("b")
    restored: list[object] = []
    purged: list[object] = []
    interface.restore_requested.connect(lambda value: restored.append(value))
    interface.purge_requested.connect(lambda value: purged.append(value))
    # Descending casefold order over the "b" subset: ba.txt, B.txt.
    assert _row_texts(interface, COL_NAME) == ["ba.txt", "B.txt"]
    assert [
        interface.item_table.item(row, COL_NAME).data(Qt.ItemDataRole.UserRole)
        for row in range(interface.item_table.rowCount())
    ] == ["d-3", "d-2"]

    interface.item_table.selectAll()
    interface.restore_button.click()
    interface.purge_button.click()

    # Operations ride the UserRole delete_no, never the backing row number.
    assert restored == [("d-3", "d-2")]
    assert purged == [("d-3", "d-2")]


def test_selection_maps_through_visible_rows_under_sort(qapp: QApplication) -> None:
    interface = RecycleInterface()
    interface.render_items([_recycle_item("d-1", "b.txt"), _recycle_item("d-2", "a.txt")])
    interface._on_header_section_clicked(COL_NAME)

    interface.item_table.selectRow(0)

    assert [item.delete_no for item in interface.selected_items()] == ["d-2"]


def test_unknown_column_click_is_ignored(qapp: QApplication) -> None:
    interface = RecycleInterface()
    interface.render_items([_recycle_item("d-1", "b.txt"), _recycle_item("d-2", "a.txt")])

    # Defensive guard: a column without a sort key (future columns) never
    # activates sorting or crashes the click handler.
    interface._on_header_section_clicked(99)

    assert interface._sort_state.column is None
    assert _row_texts(interface, COL_NAME) == ["b.txt", "a.txt"]
