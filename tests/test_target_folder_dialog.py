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


def _install_name_dialog_stub(
    monkeypatch: pytest.MonkeyPatch, *, accepted: bool = True, name: str = "新目录"
) -> list[dict[str, object]]:
    """Replace NameInputDialog inside main_window with a recording stub."""
    from openwopan.ui import main_window as main_window_module

    created: list[dict[str, object]] = []

    class _StubNameDialog:
        def __init__(
            self,
            *,
            title: str,
            hint: str,
            default_text: str,
            parent: object = None,
        ) -> None:
            created.append(
                {"title": title, "hint": hint, "default_text": default_text, "deleted": False}
            )

        exec = staticmethod(
            lambda: QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected
        )

        def name_text(self) -> str:
            return name

        def deleteLater(self) -> None:
            created[-1]["deleted"] = True

    monkeypatch.setattr(main_window_module, "NameInputDialog", _StubNameDialog)
    return created


def test_create_folder_button_confirms_name_and_emits_request(
    qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    stubs = _install_name_dialog_stub(monkeypatch, accepted=True, name="新目录")
    dialog = TargetFolderDialog("move", ROOT, [FOLDER_A])
    requests: list[tuple[str, str]] = []
    dialog.create_folder_requested.connect(
        lambda parent_id, name: requests.append((parent_id, name))
    )

    dialog._create_folder_button.click()

    assert len(stubs) == 1
    assert stubs[0]["title"] == "新建文件夹"
    assert stubs[0]["default_text"] == "新建文件夹"
    assert "全部文件" in stubs[0]["hint"]
    assert stubs[0]["deleted"] is True
    assert requests == [("root", "新目录")]


def test_create_folder_cancelled_emits_nothing(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    stubs = _install_name_dialog_stub(monkeypatch, accepted=False)
    dialog = TargetFolderDialog("move", ROOT, [FOLDER_A])
    requests: list[tuple[str, str]] = []
    dialog.create_folder_requested.connect(
        lambda parent_id, name: requests.append((parent_id, name))
    )

    dialog._create_folder_button.click()

    assert len(stubs) == 1
    assert requests == []


@pytest.mark.parametrize("busy_attribute", ["_load_in_flight", "_create_in_flight"])
def test_create_folder_click_ignored_while_busy(
    qapp, monkeypatch: pytest.MonkeyPatch, busy_attribute: str
) -> None:
    stubs = _install_name_dialog_stub(monkeypatch)
    dialog = TargetFolderDialog("move", ROOT, [FOLDER_A])
    requests: list[tuple[str, str]] = []
    dialog.create_folder_requested.connect(
        lambda parent_id, name: requests.append((parent_id, name))
    )
    setattr(dialog, busy_attribute, True)

    dialog._create_folder_button.click()

    assert stubs == []
    assert requests == []


def test_begin_create_disables_controls_until_error_resolves(qapp) -> None:
    dialog = TargetFolderDialog("move", ROOT, [FOLDER_A])

    dialog.begin_create()

    assert dialog._create_in_flight is True
    assert dialog._status_label.text() == "正在创建文件夹…"
    assert not dialog._create_folder_button.isEnabled()
    assert not dialog._folder_list.isEnabled()
    assert not dialog._ok_button.isEnabled()

    dialog.show_create_error("目录已存在")

    assert dialog._create_in_flight is False
    assert dialog._status_label.text() == "创建失败：目录已存在"
    assert dialog._create_folder_button.isEnabled()
    assert dialog._folder_list.isEnabled()
    assert dialog._ok_button.isEnabled()


def test_show_created_entry_reloads_and_selects_new_folder(qapp) -> None:
    dialog = TargetFolderDialog("move", ROOT, [FOLDER_A])
    requested: list[str] = []
    dialog.directory_requested.connect(requested.append)

    dialog.show_created_entry(FOLDER_B)

    assert requested == ["root"]
    assert dialog._create_in_flight is False
    assert dialog._load_in_flight is True
    assert dialog._folder_list.isEnabled() is False

    dialog.show_entries([FOLDER_A, FOLDER_B])

    assert dialog.current_target() == FOLDER_B
    assert dialog._ok_button.text() == "移动到「文档」"
    assert dialog._pending_selection is None
    assert dialog._folder_list.currentItem().data(Qt.ItemDataRole.UserRole) == FOLDER_B
    assert dialog._folder_list.isEnabled() is True


def test_show_created_entry_falls_back_when_folder_not_listed(qapp) -> None:
    dialog = TargetFolderDialog("move", ROOT, [FOLDER_A])

    dialog.show_created_entry(FOLDER_B)
    dialog.show_entries([FOLDER_A])

    assert dialog.current_target() == ROOT
    assert dialog._pending_selection is None


def test_show_load_error_clears_pending_selection(qapp) -> None:
    dialog = TargetFolderDialog("move", ROOT, [FOLDER_A])

    dialog.show_created_entry(FOLDER_B)
    dialog.show_load_error("网络超时")

    assert dialog._pending_selection is None
    dialog.show_entries([FOLDER_A])
    assert dialog.current_target() == ROOT
