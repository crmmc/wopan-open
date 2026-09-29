"""Recycle-bin page and main-window wiring tests (sync-threaded)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

import pytest
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
