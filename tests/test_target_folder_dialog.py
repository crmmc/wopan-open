"""Target folder browsing and transfer conflict dialog tests (signal-driven)."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QListWidgetItem

from openwopan.ui.target_folder_dialog import (
    TargetEntry,
    TargetFolderDialog,
    TransferConflictDialog,
    TransferMode,
    mode_label,
)

ROOT = TargetEntry(item_id="root", name="全部文件")
FOLDER_A = TargetEntry(item_id="folder-a", name="相册")
FOLDER_B = TargetEntry(item_id="folder-b", name="文档")
FOLDER_C = TargetEntry(item_id="folder-c", name="备份")


@pytest.fixture
def qapp():
    import sys

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("move", "移动"), ("copy", "复制")],
)
def test_mode_label_translates_mode(mode: TransferMode, expected: str) -> None:
    assert mode_label(mode) == expected


def test_dialog_defaults_to_current_location_as_target(qapp) -> None:
    dialog = TargetFolderDialog("move", ROOT, [FOLDER_A, FOLDER_B])

    assert dialog.windowTitle() == "移动"
    assert dialog.current_target() == ROOT
    assert dialog._ok_button.text() == "移动到此（全部文件）"
    assert dialog._folder_list.count() == 2
    assert dialog._up_button.isEnabled() is False


def test_single_click_selects_child_as_target(qapp) -> None:
    dialog = TargetFolderDialog("copy", ROOT, [FOLDER_A, FOLDER_B])

    item = dialog._folder_list.item(0)
    dialog._folder_list.itemClicked.emit(item)

    assert dialog.current_target() == FOLDER_A
    assert dialog._ok_button.text() == "复制到「相册」"


def test_double_click_requests_directory_and_path_appends_on_entries(qapp) -> None:
    dialog = TargetFolderDialog("move", ROOT, [FOLDER_A])
    requested: list[str] = []
    dialog.directory_requested.connect(requested.append)

    dialog._folder_list.itemDoubleClicked.emit(dialog._folder_list.item(0))

    assert requested == ["folder-a"]
    assert dialog._load_in_flight is True
    assert dialog._folder_list.isEnabled() is False

    dialog.show_entries([FOLDER_B])

    assert [entry.item_id for entry in dialog._path] == ["root", "folder-a"]
    assert dialog.current_target() == FOLDER_A
    assert dialog._path_label.text() == "全部文件 / 相册"
    assert dialog._load_in_flight is False
    assert dialog._folder_list.isEnabled() is True
    assert dialog._folder_list.count() == 1


def test_load_in_flight_ignores_navigation(qapp) -> None:
    dialog = TargetFolderDialog("move", ROOT, [FOLDER_A, FOLDER_B])
    requested: list[str] = []
    dialog.directory_requested.connect(requested.append)

    dialog._folder_list.itemDoubleClicked.emit(dialog._folder_list.item(0))
    dialog._folder_list.itemDoubleClicked.emit(dialog._folder_list.item(1))
    dialog._up_button.click()

    assert requested == ["folder-a"]


def test_load_error_releases_navigation_and_keeps_path(qapp) -> None:
    dialog = TargetFolderDialog("move", ROOT, [FOLDER_A])
    requested: list[str] = []
    dialog.directory_requested.connect(requested.append)
    dialog._folder_list.itemDoubleClicked.emit(dialog._folder_list.item(0))

    dialog.show_load_error("网络超时")

    assert [entry.item_id for entry in dialog._path] == ["root"]
    assert dialog.current_target() == ROOT
    assert dialog._status_label.text() == "加载失败：网络超时"
    assert dialog._load_in_flight is False

    dialog._folder_list.itemDoubleClicked.emit(dialog._folder_list.item(0))
    assert requested == ["folder-a", "folder-a"]


def test_show_entries_drops_excluded_folders(qapp) -> None:
    dialog = TargetFolderDialog("move", ROOT, [FOLDER_A], excluded_ids=frozenset({"folder-b"}))

    dialog.show_entries([FOLDER_A, FOLDER_B, FOLDER_C])

    shown = [
        dialog._folder_list.item(i).data(Qt.ItemDataRole.UserRole)
        for i in range(dialog._folder_list.count())
    ]
    assert shown == [FOLDER_A, FOLDER_C]


def test_go_up_pops_path_and_reloads_current_level(qapp) -> None:
    dialog = TargetFolderDialog("move", ROOT, [FOLDER_A])
    requested: list[str] = []
    dialog.directory_requested.connect(requested.append)
    dialog._folder_list.itemDoubleClicked.emit(dialog._folder_list.item(0))
    dialog.show_entries([FOLDER_B])
    requested.clear()

    dialog._up_button.click()

    assert requested == ["root"]
    assert dialog._load_in_flight is True
    dialog.show_entries([FOLDER_A, FOLDER_C])

    assert [entry.item_id for entry in dialog._path] == ["root"]
    assert dialog.current_target() == ROOT
    assert dialog._folder_list.count() == 2


def test_go_up_during_load_and_at_root_is_ignored(qapp) -> None:
    dialog = TargetFolderDialog("move", ROOT, [FOLDER_A])
    requested: list[str] = []
    dialog.directory_requested.connect(requested.append)

    dialog._go_up()
    assert requested == []

    dialog._folder_list.itemDoubleClicked.emit(dialog._folder_list.item(0))
    dialog._reload_current()
    assert requested == ["folder-a"]

    dialog.show_entries([])
    requested.clear()
    dialog._go_up()
    assert requested == ["root"]


def test_conflict_dialog_skip_returns_resolution(qapp) -> None:
    dialog = TransferConflictDialog(["a.txt", "b.txt"], "move")

    assert dialog.windowTitle() == "处理同名项目（移动）"
    assert dialog._preview.count() == 2

    dialog._skip_button.click()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.resolution() == "skip"


def test_conflict_dialog_cancel_returns_none(qapp) -> None:
    dialog = TransferConflictDialog(["a.txt"], "copy")
    dialog.reject()

    assert dialog.resolution() is None


def test_conflict_dialog_previews_at_most_twenty_names(qapp) -> None:
    names = [f"file-{index}.txt" for index in range(25)]

    dialog = TransferConflictDialog(names, "copy")

    assert dialog._preview.count() == 21
    assert dialog._preview.item(20).text() == "另有 5 项"


def test_dialog_ignores_items_without_target_entry_data(qapp) -> None:
    dialog = TargetFolderDialog("move", ROOT, [FOLDER_A])
    requested: list[str] = []
    dialog.directory_requested.connect(requested.append)

    stray = QListWidgetItem("stray")
    dialog._folder_list.addItem(stray)

    dialog._folder_list.itemClicked.emit(stray)
    dialog._folder_list.itemDoubleClicked.emit(stray)

    assert requested == []
    assert dialog.current_target() == ROOT
