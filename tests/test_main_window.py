from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import QAbstractItemView, QApplication, QDialog, QFrame
from qfluentwidgets import TableWidget

import openwopan.ui.main_window as main_window_module
from openwopan.app.file_browser import FileBrowserError, FileBrowserLoginRequiredError
from openwopan.auth.session import AuthSession
from openwopan.storage.settings import AppSettings
from openwopan.ui.main_window import (
    DOWNLOAD_STATUS_FILTERS,
    FILE_COL_KIND,
    FILE_COL_NAME,
    FILE_COL_SIZE,
    FILE_SPLITTER_STRETCH_FACTORS,
    FILE_TYPE_FILTER_ALL,
    FILE_TYPE_FILTER_FILES,
    FILE_TYPE_FILTER_FOLDERS,
    ROOT_DISPLAY_NAME,
    TRANSFER_COL_ACTION,
    TRANSFER_COL_NAME,
    TRANSFER_COL_PROGRESS,
    TRANSFER_COL_SIZE,
    TRANSFER_COL_SPEED,
    TRANSFER_COL_STATUS,
    TRANSFER_TABLE_HEADERS,
    UPLOAD_STATUS_FILTERS,
    MainWindow,
    TransferInterface,
    TransferRecord,
)
from openwopan.ui.table_view import NO_MATCH_TEXT
from openwopan.ui.target_folder_dialog import TargetEntry, TargetFolderDialog
from openwopan.wopan.client import ROOT_DIRECTORY_ID
from openwopan.wopan.models import WopanCloudUsage, WopanItem, WopanItemKind, WopanRecycleItem


@pytest.fixture(autouse=True)
def _sync_main_window_threads(sync_threads: None) -> None:
    """Keep legacy synchronous MainWindow assertions deterministic."""


class FakeFileBrowser:
    def __init__(self) -> None:
        self.requested_parent_ids: list[str] = []
        self.created_folders: list[tuple[str, str]] = []
        self.renamed_items: list[tuple[str, str]] = []
        self.deleted_items: list[str] = []
        self.moved_items: list[tuple[str, str]] = []
        self.copied_items: list[tuple[str, str]] = []
        self.downloaded_items: list[tuple[str, Path]] = []
        self.uploaded_files: list[tuple[str, Path]] = []
        self.usage_account_ids: list[str] = []
        self.listed_recycle_bin = False
        self.recycle_items: list[WopanRecycleItem] = []
        self.restored_delete_nos: list[tuple[str, ...]] = []
        self.purged_delete_nos: list[tuple[str, ...]] = []
        self.emptied_recycle_bin = False
        self.items_by_parent = {
            ROOT_DIRECTORY_ID: [
                WopanItem(
                    item_id="folder-1",
                    name="Folder",
                    kind=WopanItemKind.FOLDER,
                    parent_id=ROOT_DIRECTORY_ID,
                    updated_at=datetime(2026, 6, 26, 1, 2, 3),
                ),
                WopanItem(
                    item_id="file-1",
                    name="report.txt",
                    kind=WopanItemKind.FILE,
                    parent_id=ROOT_DIRECTORY_ID,
                    file_type="4",
                    download_id="fid-1",
                    size=2048,
                    updated_at=datetime(2026, 6, 26, 11, 22, 33),
                ),
            ],
            "folder-1": [
                WopanItem(
                    item_id="child-file",
                    name="child.txt",
                    kind=WopanItemKind.FILE,
                    parent_id="folder-1",
                    download_id="child-fid",
                    size=1,
                )
            ],
        }

    def list_directory(self, parent_id: str = ROOT_DIRECTORY_ID) -> list[WopanItem]:
        self.requested_parent_ids.append(parent_id)
        return list(self.items_by_parent[parent_id])

    def create_folder(self, parent_id: str, name: str) -> WopanItem:
        self.created_folders.append((parent_id, name))
        created = WopanItem(
            item_id="created-folder",
            name=name,
            kind=WopanItemKind.FOLDER,
            parent_id=parent_id,
        )
        self.items_by_parent[parent_id] = [*self.items_by_parent[parent_id], created]
        return created

    def rename_item(self, item: WopanItem, new_name: str) -> None:
        self.renamed_items.append((item.item_id, new_name))
        self.items_by_parent[item.parent_id or ROOT_DIRECTORY_ID] = [
            WopanItem(
                item_id=existing.item_id,
                name=new_name if existing.item_id == item.item_id else existing.name,
                kind=existing.kind,
                parent_id=existing.parent_id,
                file_type=existing.file_type,
                download_id=existing.download_id,
                size=existing.size,
                updated_at=existing.updated_at,
            )
            for existing in self.items_by_parent[item.parent_id or ROOT_DIRECTORY_ID]
        ]

    def delete_item(self, item: WopanItem) -> None:
        self.deleted_items.append(item.item_id)
        self.items_by_parent[item.parent_id or ROOT_DIRECTORY_ID] = [
            existing
            for existing in self.items_by_parent[item.parent_id or ROOT_DIRECTORY_ID]
            if existing.item_id != item.item_id
        ]

    def delete_items(self, items: Sequence[WopanItem]) -> None:
        for item in items:
            self.delete_item(item)

    def move_item(self, item: WopanItem, target_parent_id: str) -> None:
        self.moved_items.append((item.item_id, target_parent_id))
        self.delete_item(item)

    def move_items(self, items: Sequence[WopanItem], target_parent_id: str) -> None:
        for item in items:
            self.move_item(item, target_parent_id)

    def copy_item(self, item: WopanItem, target_parent_id: str) -> None:
        self.copied_items.append((item.item_id, target_parent_id))

    def copy_items(self, items: Sequence[WopanItem], target_parent_id: str) -> None:
        for item in items:
            self.copy_item(item, target_parent_id)

    def download_file(
        self,
        item: WopanItem,
        local_path: Path,
        progress_callback: object | None = None,
    ) -> None:
        self.downloaded_items.append((item.item_id, local_path))

    def upload_file(
        self,
        parent_id: str,
        local_path: Path,
        *,
        upload_name: str | None = None,
    ) -> WopanItem:
        self.uploaded_files.append((parent_id, local_path))
        effective_name = upload_name if upload_name is not None else local_path.name
        uploaded = WopanItem(
            item_id="uploaded-file",
            name=effective_name,
            kind=WopanItemKind.FILE,
            parent_id=parent_id,
            download_id="uploaded-fid",
            size=local_path.stat().st_size,
        )
        self.items_by_parent[parent_id] = [*self.items_by_parent[parent_id], uploaded]
        return uploaded

    def get_cloud_usage(self, account_id: str) -> WopanCloudUsage:
        self.usage_account_ids.append(account_id)
        return WopanCloudUsage(used_bytes=1024, total_bytes=2048)

    def list_recycle_items(self) -> list[WopanRecycleItem]:
        self.listed_recycle_bin = True
        return list(self.recycle_items)

    def restore_recycle_items(self, delete_nos: Sequence[str]) -> None:
        self.restored_delete_nos.append(tuple(delete_nos))

    def purge_recycle_items(self, delete_nos: Sequence[str]) -> None:
        self.purged_delete_nos.append(tuple(delete_nos))

    def empty_recycle_bin(self) -> None:
        self.emptied_recycle_bin = True


class LoginExpiredFileBrowser:
    def list_directory(self, parent_id: str = ROOT_DIRECTORY_ID) -> list[WopanItem]:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def create_folder(self, parent_id: str, name: str) -> WopanItem:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def rename_item(self, item: WopanItem, new_name: str) -> None:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def delete_item(self, item: WopanItem) -> None:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def delete_items(self, items: Sequence[WopanItem]) -> None:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def move_item(self, item: WopanItem, target_parent_id: str) -> None:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def move_items(self, items: Sequence[WopanItem], target_parent_id: str) -> None:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def copy_item(self, item: WopanItem, target_parent_id: str) -> None:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def copy_items(self, items: Sequence[WopanItem], target_parent_id: str) -> None:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def download_file(
        self,
        item: WopanItem,
        local_path: Path,
        progress_callback: object | None = None,
    ) -> None:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def upload_file(
        self,
        parent_id: str,
        local_path: Path,
        *,
        upload_name: str | None = None,
    ) -> WopanItem:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def get_cloud_usage(self, account_id: str) -> WopanCloudUsage:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def list_recycle_items(self) -> list[WopanRecycleItem]:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def restore_recycle_items(self, delete_nos: Sequence[str]) -> None:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def purge_recycle_items(self, delete_nos: Sequence[str]) -> None:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def empty_recycle_bin(self) -> None:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")


class FailingOperationFileBrowser(FakeFileBrowser):
    def rename_item(self, item: WopanItem, new_name: str) -> None:
        raise FileBrowserError("name exists")


class DelayedCreatedFolderBrowser(FakeFileBrowser):
    def create_folder(self, parent_id: str, name: str) -> WopanItem:
        self.created_folders.append((parent_id, name))
        return WopanItem(
            item_id="delayed-folder",
            name=name,
            kind=WopanItemKind.FOLDER,
            parent_id=parent_id,
        )


class DelayedUploadedFileBrowser(FakeFileBrowser):
    def upload_file(
        self,
        parent_id: str,
        local_path: Path,
        *,
        upload_name: str | None = None,
    ) -> WopanItem:
        self.uploaded_files.append((parent_id, local_path))
        return WopanItem(
            item_id="delayed-upload",
            name=upload_name if upload_name is not None else local_path.name,
            kind=WopanItemKind.FILE,
            parent_id=parent_id,
            download_id="delayed-fid",
            size=local_path.stat().st_size,
        )


class ProgressFileBrowser(FakeFileBrowser):
    def download_file(
        self,
        item: WopanItem,
        local_path: Path,
        progress_callback: object | None = None,
    ) -> None:
        self.downloaded_items.append((item.item_id, local_path))
        if callable(progress_callback):
            progress_callback(1024, 2048)
            progress_callback(2048, 2048)


class FailingDownloadFileBrowser(FakeFileBrowser):
    def download_file(
        self,
        item: WopanItem,
        local_path: Path,
        progress_callback: object | None = None,
    ) -> None:
        self.downloaded_items.append((item.item_id, local_path))
        raise FileBrowserError("network down")


def test_main_window_without_browser_shows_login_state(qapp: QApplication) -> None:
    window = MainWindow()

    assert window.current_directory_id() == ROOT_DIRECTORY_ID
    assert window.breadcrumb_names() == (ROOT_DISPLAY_NAME,)
    assert window.displayed_items() == ()
    assert window.status_message() == "请先登录"


def test_main_window_matches_sibling_file_layout_invariants(qapp: QApplication) -> None:
    window = MainWindow()
    file_interface = window.file_interface

    assert window.size().width() == 900
    assert window.size().height() == 600
    assert isinstance(file_interface.top_bar_frame, QFrame)
    assert file_interface.top_bar_frame.objectName() == "frame"
    assert isinstance(file_interface.breadcrumb_frame, QFrame)
    assert file_interface.breadcrumb_frame.objectName() == "frame"
    assert isinstance(file_interface.tree_frame, QFrame)
    assert file_interface.tree_frame.objectName() == "frame"
    assert isinstance(file_interface.list_frame, QFrame)
    assert file_interface.list_frame.objectName() == "listFrame"
    assert "border-radius: 5px" in file_interface.top_bar_frame.styleSheet()
    assert file_interface.tree_frame.minimumWidth() == 200
    assert FILE_SPLITTER_STRETCH_FACTORS == (1, 6)

    table = file_interface.file_table
    assert table.columnCount() == 3
    assert [table.horizontalHeaderItem(index).text() for index in range(3)] == [
        "名称",
        "类型",
        "大小",
    ]
    assert table.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection

    assert file_interface.storage_card is not None
    assert not hasattr(file_interface, "storage_label")
    storage_top_layout = file_interface.storage_card.layout().itemAt(0).layout()
    assert storage_top_layout is not None
    assert storage_top_layout.itemAt(0).widget() is file_interface.storage_icon
    assert storage_top_layout.itemAt(1).widget() is file_interface.storage_value_label
    assert file_interface.storage_value_label.text() == "-- / --"
    assert file_interface.storage_progress_bar.value() == 0
    assert file_interface.search_bar.width() == 200
    assert not file_interface.upload_button_group.isEnabled()
    assert not file_interface.upload_file_action.isEnabled()
    assert not file_interface.upload_folder_action.isEnabled()
    assert not file_interface.download_button.isEnabled()
    assert window.account_interface.account_group.titleLabel.text() == "账户信息"
    assert window.setting_interface.startup_group.titleLabel.text() == "启动"
    assert window.setting_interface.transfer_group.titleLabel.text() == "传输设置"
    assert window.setting_interface.about_group.titleLabel.text() == "关于"
    assert window.setting_interface.viewportMargins().top() == 0
    assert "background: transparent" in window.setting_interface.styleSheet()


def test_transfer_interface_matches_sibling_layout_invariants(qapp: QApplication) -> None:
    window = MainWindow()
    transfer = window.transfer_interface

    assert transfer.top_bar_frame.objectName() == "frame"
    assert transfer.title_label.text() == "传输管理"
    assert transfer._active_direction == "download"
    assert tuple(
        transfer.upload_filter_combo.itemText(index)
        for index in range(len(UPLOAD_STATUS_FILTERS))
    ) == UPLOAD_STATUS_FILTERS
    assert tuple(
        transfer.download_filter_combo.itemText(index)
        for index in range(len(DOWNLOAD_STATUS_FILTERS))
    ) == DOWNLOAD_STATUS_FILTERS
    assert transfer.upload_frame.isHidden()
    assert not transfer.download_frame.isHidden()
    assert not transfer.open_download_folder_button.isHidden()

    assert transfer.upload_table.columnCount() == len(TRANSFER_TABLE_HEADERS)
    assert [
        transfer.upload_table.horizontalHeaderItem(index).text()
        for index in range(len(TRANSFER_TABLE_HEADERS))
    ] == list(TRANSFER_TABLE_HEADERS)
    assert (
        transfer.upload_table.selectionMode()
        == QAbstractItemView.SelectionMode.ExtendedSelection
    )
    assert transfer.upload_batch_buttons["count"].text() == "已选 0 项"
    assert transfer.upload_batch_buttons["speed"].text() == "总速度: --"

    transfer._on_segment_changed("upload")
    assert transfer._active_direction == "upload"
    assert not transfer.upload_frame.isHidden()
    assert transfer.download_frame.isHidden()
    assert transfer.open_download_folder_button.isHidden()

    transfer._on_segment_changed("download")
    assert transfer._active_direction == "download"
    assert not transfer.download_frame.isHidden()
    assert not transfer.open_download_folder_button.isHidden()
    assert transfer.upload_frame.isHidden()


def test_main_window_loads_root_and_enters_child_folder(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)

    window.refresh_current_directory()
    assert browser.requested_parent_ids == [ROOT_DIRECTORY_ID]
    assert [item.name for item in window.displayed_items()] == ["Folder", "report.txt"]
    assert "2 项" in window.status_message()

    window.enter_displayed_folder(0)

    # B24：进入 folder-1 后树同步的祖先层（根目录）直接命中已访问缓存，
    # 不再对根目录重复发列表请求
    assert browser.requested_parent_ids == [ROOT_DIRECTORY_ID, "folder-1"]
    assert window.current_directory_id() == "folder-1"
    assert window.breadcrumb_names() == (ROOT_DISPLAY_NAME, "Folder")
    assert [item.name for item in window.displayed_items()] == ["child.txt"]


def test_main_window_renders_account_and_cloud_usage(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)

    window.set_auth_session(AuthSession(account_id="13800138000", display_name="User One"))
    window.refresh_cloud_usage()

    assert browser.usage_account_ids == ["13800138000"]
    assert window.account_interface.account_value_label.text() == "User One / 138****8000"
    assert window.account_interface.usage_value_label.text() == "1.0 KB / 2.0 KB"
    assert window.file_interface.storage_value_label.text() == "1.0 KB / 2.0 KB"
    assert window.file_interface.storage_progress_bar.value() == 50


def test_main_window_refreshes_all_account_information(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)

    window.set_auth_session(AuthSession(account_id="13800138000", display_name="User One"))
    window.refresh_all_information()

    assert browser.usage_account_ids == ["13800138000"]
    assert browser.requested_parent_ids == [ROOT_DIRECTORY_ID]
    assert window.account_interface.usage_value_label.text() == "1.0 KB / 2.0 KB"
    assert [item.name for item in window.displayed_items()] == ["Folder", "report.txt"]


def test_recycle_success_marks_dirty_flag(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    window.set_auth_session(AuthSession(account_id="13800138000", display_name="User One"))

    for slot in (
        window._on_recycle_restore_succeeded,
        window._on_recycle_purge_succeeded,
        window._on_recycle_empty_succeeded,
    ):
        window._recycle_dirty = False
        slot(None)
        assert window._recycle_dirty is True


def test_recycle_restore_clears_visited_directory_cache(qapp: QApplication) -> None:
    # 恢复的目标目录可能不是当前目录；脏标记消费只强制刷新当前目录，
    # 缓存若不整体失效，导航到恢复目标会看到缺少恢复文件的旧列表。
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    window.set_auth_session(AuthSession(account_id="13800138000", display_name="User One"))

    window.refresh_current_directory()
    window.enter_displayed_folder(0)
    assert window._directory_cache  # 已访问目录进入缓存

    window._on_recycle_restore_succeeded(None)

    assert window._recycle_dirty is True
    assert window._directory_cache == {}


def test_switch_back_to_file_page_consumes_dirty_flag(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    window.set_auth_session(AuthSession(account_id="13800138000", display_name="User One"))

    window._stacked_widget.setCurrentWidget(window.recycle_interface)
    window._recycle_dirty = True
    usage_calls = len(browser.usage_account_ids)
    list_calls = len(browser.requested_parent_ids)

    window._stacked_widget.setCurrentWidget(window.file_interface)

    assert window._recycle_dirty is False
    assert len(browser.usage_account_ids) == usage_calls + 1
    assert len(browser.requested_parent_ids) == list_calls + 1


def test_switch_back_to_file_page_without_dirty_flag_skips_refresh(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    window.set_auth_session(AuthSession(account_id="13800138000", display_name="User One"))

    window._stacked_widget.setCurrentWidget(window.recycle_interface)
    window._recycle_dirty = False
    usage_calls = len(browser.usage_account_ids)
    list_calls = len(browser.requested_parent_ids)

    window._stacked_widget.setCurrentWidget(window.file_interface)

    assert len(browser.usage_account_ids) == usage_calls
    assert len(browser.requested_parent_ids) == list_calls


def test_file_refresh_button_refreshes_cloud_usage_too(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    window.set_auth_session(AuthSession(account_id="13800138000", display_name="User One"))
    usage_calls = len(browser.usage_account_ids)
    list_calls = len(browser.requested_parent_ids)

    window.file_interface.refresh_button.click()

    assert len(browser.usage_account_ids) == usage_calls + 1
    assert len(browser.requested_parent_ids) == list_calls + 1


def test_clear_auth_session_resets_dirty_flag(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    window._recycle_dirty = True

    window.clear_auth_session()

    assert window._recycle_dirty is False


def test_settings_interface_persists_non_transfer_settings(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "settings.json"
    log_path = tmp_path / "openwopan.log"
    window = MainWindow(
        settings=AppSettings(log_level="INFO", stay_logged_in=True),
        settings_path=settings_path,
        log_path=log_path,
    )

    window.setting_interface.stay_logged_in_card.setChecked(False)
    window.setting_interface.log_level_combo_box.setCurrentIndex(
        window.setting_interface._LOG_LEVELS.index("ERROR")
    )

    assert window.setting_interface.settings().stay_logged_in is False
    assert window.setting_interface.settings().log_level == "ERROR"


def test_settings_interface_persists_transfer_settings(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "settings.json"
    window = MainWindow(
        settings=AppSettings(default_download_path=tmp_path / "downloads"),
        settings_path=settings_path,
    )

    window.setting_interface.ask_download_location_card.setChecked(False)
    window.setting_interface.download_threads_spin_box.setValue(4)
    window.setting_interface.upload_threads_spin_box.setValue(6)
    window.setting_interface.concurrent_downloads_spin_box.setValue(2)
    window.setting_interface.concurrent_uploads_spin_box.setValue(1)
    window.setting_interface.retry_attempts_combo_box.setCurrentIndex(5)
    window.setting_interface.download_part_size_spin_box.setValue(12)
    window.setting_interface.download_part_mode_combo_box.setCurrentIndex(1)
    window.setting_interface.upload_part_size_spin_box.setValue(8)

    settings = window.setting_interface.settings()

    assert settings.ask_download_location is False
    assert settings.max_download_threads == 4
    assert settings.max_upload_threads == 6
    assert settings.max_concurrent_downloads == 2
    assert settings.max_concurrent_uploads == 1
    assert settings.retry_max_attempts == 5
    assert settings.download_part_size_mb == 12
    assert settings.download_part_mode == "fixed"
    assert settings.upload_part_size_mb == 8


def test_main_window_does_not_enter_file_rows(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)

    window.refresh_current_directory()
    window.enter_displayed_folder(1)

    assert browser.requested_parent_ids == [ROOT_DIRECTORY_ID]
    assert window.current_directory_id() == ROOT_DIRECTORY_ID


@pytest.mark.parametrize(
    "trigger_login_expired",
    [
        lambda window: window.refresh_current_directory(),
        lambda window: window.create_folder_with_name("Reports"),
    ],
)
def test_main_window_maps_login_required_status(
    qapp: QApplication, trigger_login_expired
) -> None:
    messages: list[str] = []
    window = MainWindow(LoginExpiredFileBrowser())
    window.login_required.connect(messages.append)

    trigger_login_expired(window)

    assert window.displayed_items() == ()
    assert window.status_message() == "登录已过期，请重新登录"
    assert messages == ["登录已过期，请重新登录"]


def test_main_window_basic_operations_refresh_current_directory(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)

    window.refresh_current_directory()
    window.create_folder_with_name(" Reports ")
    window.rename_displayed_item(1, " renamed.txt ")
    window.delete_displayed_item(0)
    window.move_displayed_item(0, "folder-2")

    assert browser.created_folders == [(ROOT_DIRECTORY_ID, "Reports")]
    assert browser.renamed_items == [("file-1", "renamed.txt")]
    assert browser.deleted_items == ["folder-1", "file-1"]
    assert browser.moved_items == [("file-1", "folder-2")]
    assert browser.requested_parent_ids == [
        ROOT_DIRECTORY_ID,
        ROOT_DIRECTORY_ID,
        ROOT_DIRECTORY_ID,
        ROOT_DIRECTORY_ID,
        ROOT_DIRECTORY_ID,
    ]


def test_main_window_batch_operations_delete_and_move_multiple_rows(
    qapp: QApplication,
) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()

    window.move_displayed_items([1], "folder-2")
    window.delete_displayed_items([0])

    assert browser.moved_items == [("file-1", "folder-2")]
    # FakeFileBrowser.move_item 内部复用 delete_item 记录，因此 file-1 也会出现
    assert browser.deleted_items == ["file-1", "folder-1"]


def test_main_window_batch_copies_multiple_rows(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()

    window.copy_displayed_items([0, 1], "folder-2")

    assert browser.copied_items == [
        ("folder-1", "folder-2"),
        ("file-1", "folder-2"),
    ]


def test_main_window_enables_download_for_single_file_selection(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)

    window.refresh_current_directory()
    table = window.file_interface.file_table

    table.selectRow(0)
    window.update_operation_controls()
    assert window.selected_download_row() is None
    assert not window.file_interface.download_button.isEnabled()

    table.clearSelection()
    table.selectRow(1)
    window.update_operation_controls()

    assert window.selected_download_row() == 1
    assert window.file_interface.download_button.isEnabled()


def test_directory_refresh_clears_stale_row_selection(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    browser.items_by_parent[ROOT_DIRECTORY_ID] = [
        *browser.items_by_parent[ROOT_DIRECTORY_ID],
        WopanItem(
            item_id="file-2",
            name="notes.txt",
            kind=WopanItemKind.FILE,
            parent_id=ROOT_DIRECTORY_ID,
            download_id="fid-2",
            size=128,
        ),
    ]
    window = MainWindow(browser)

    window.refresh_current_directory()
    table = window.file_interface.file_table
    table.selectRow(1)
    assert window.selected_rows() == [1]
    assert window.file_interface.delete_button.isEnabled()
    assert window.file_interface.download_button.isEnabled()

    browser.items_by_parent[ROOT_DIRECTORY_ID] = browser.items_by_parent[ROOT_DIRECTORY_ID][:2]
    window.refresh_current_directory()

    assert window.selected_rows() == []
    assert not window.file_interface.delete_button.isEnabled()
    assert not window.file_interface.download_button.isEnabled()


def test_main_window_direct_download_delegates_to_browser(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    local_path = tmp_path / "report.txt"

    window.refresh_current_directory()
    window.download_displayed_item(1, local_path, run_in_background=False)

    assert browser.downloaded_items == [("file-1", local_path)]
    assert window.status_message() == "下载完成：report.txt"


def test_transfer_center_records_direct_download_progress_and_completion(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    browser = ProgressFileBrowser()
    window = MainWindow(browser)
    local_path = tmp_path / "report.txt"

    window.refresh_current_directory()
    window.download_displayed_item(1, local_path, run_in_background=False)

    records = window.transfer_interface.download_records
    assert len(records) == 1
    assert records[0].name == "report.txt"
    assert records[0].status == "已完成"
    assert records[0].progress_percent == 100
    assert window.transfer_interface.download_table.item(0, 4).text() == "已完成"


def test_transfer_center_records_direct_download_failure(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    browser = FailingDownloadFileBrowser()
    window = MainWindow(browser)

    window.refresh_current_directory()
    window.download_displayed_item(1, tmp_path / "report.txt", run_in_background=False)

    records = window.transfer_interface.download_records
    assert len(records) == 1
    assert records[0].status == "失败"
    assert records[0].error == "network down"
    assert window.transfer_interface.download_table.item(0, 4).text() == "失败"
    assert window.status_message() == "下载失败：network down"


def test_main_window_auto_download_uses_default_path_without_prompt(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    browser = FakeFileBrowser()
    download_dir = tmp_path / "downloads"
    window = MainWindow(
        browser,
        settings=AppSettings(
            default_download_path=download_dir,
            ask_download_location=False,
        ),
    )

    window.refresh_current_directory()
    local_path = window._resolve_automatic_download_path("report.txt")
    assert local_path is not None
    window.download_displayed_item(1, local_path, run_in_background=False)

    assert browser.downloaded_items == [("file-1", download_dir / "report.txt")]
    assert download_dir.exists()


def test_main_window_auto_download_avoids_existing_file_name(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    browser = FakeFileBrowser()
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    (download_dir / "report.txt").write_text("existing")
    window = MainWindow(
        browser,
        settings=AppSettings(
            default_download_path=download_dir,
            ask_download_location=False,
        ),
    )

    window.refresh_current_directory()
    local_path = window._resolve_automatic_download_path("report.txt")
    assert local_path is not None
    window.download_displayed_item(1, local_path, run_in_background=False)

    assert browser.downloaded_items == [("file-1", download_dir / "report (1).txt")]


def test_main_window_direct_upload_delegates_to_browser_and_refreshes(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    local_path = tmp_path / "upload.txt"
    local_path.write_bytes(b"upload-content")

    window.refresh_current_directory()
    window.upload_file_to_current_directory(local_path, run_in_background=False)

    assert browser.uploaded_files == [(ROOT_DIRECTORY_ID, local_path)]
    assert browser.requested_parent_ids == [ROOT_DIRECTORY_ID, ROOT_DIRECTORY_ID, ROOT_DIRECTORY_ID]
    assert [item.name for item in window.displayed_items()] == [
        "Folder",
        "report.txt",
        "upload.txt",
    ]
    assert window.status_message() == "上传完成：upload.txt"
    assert len(window.transfer_interface.upload_records) == 1
    assert window.transfer_interface.upload_records[0].status == "已完成"
    assert window.transfer_interface.upload_table.item(0, 0).text() == "upload.txt"
    assert window.transfer_interface.upload_table.item(0, 4).text() == "已完成"


def test_transfer_center_deletes_terminal_records(qapp: QApplication, tmp_path: Path) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    local_path = tmp_path / "upload.txt"
    local_path.write_text("content")

    window.refresh_current_directory()
    window.upload_file_to_current_directory(local_path, run_in_background=False)
    window.transfer_interface.upload_table.selectRow(0)
    window.transfer_interface._request_delete_selected("upload")

    assert window.transfer_interface.upload_records == []
    assert window.transfer_interface.upload_table.rowCount() == 0


def test_transfer_record_removal_clears_stale_selection(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)

    window.refresh_current_directory()
    for index in range(3):
        local_path = tmp_path / f"upload-{index}.txt"
        local_path.write_text("content")
        window.upload_file_to_current_directory(local_path, run_in_background=False)

    interface = window.transfer_interface
    table = interface.upload_table
    assert len(interface.upload_records) == 3

    table.selectRow(1)
    assert interface.upload_batch_buttons["count"].text() == "已选 1 项"

    # Removing the first record shifts rows up; a stale row-number selection
    # would keep pointing at row 1, which now holds the never-selected third
    # record (contract 16, transfer page).
    removed_id = interface.upload_records[0].task_id
    interface.remove_records("upload", {removed_id})

    assert [record.name for record in interface.upload_records] == [
        "upload-1.txt",
        "upload-2.txt",
    ]
    assert table.selectionModel().selectedRows() == []
    assert interface.upload_batch_buttons["count"].text() == "已选 0 项"
    for key in ("pause", "resume", "retry"):
        assert not interface.upload_batch_buttons[key].isEnabled()


def test_transfer_progress_render_preserves_selection(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()

    interface = window.transfer_interface
    table = interface.upload_table
    record = TransferRecord(
        task_id="task-progress",
        direction="upload",
        name="upload.txt",
        size=100,
        status="上传中",
    )
    interface.add_upload_record(record)

    table.selectRow(0)
    assert interface.upload_batch_buttons["count"].text() == "已选 1 项"

    # Progress-only update leaves the task-id sequence unchanged, so the
    # coalesced render must keep the user's selection (no unconditional clear).
    interface.update_record("upload", record.task_id, bytes_done=42)
    interface.flush_progress_render()

    assert interface.upload_records[0].bytes_done == 42
    assert table.item(0, TRANSFER_COL_PROGRESS).text() == "42% (42 B / 100 B)"
    assert [index.row() for index in table.selectionModel().selectedRows()] == [0]
    assert interface.upload_batch_buttons["count"].text() == "已选 1 项"


def test_transfer_filter_change_clears_stale_selection(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()

    interface = window.transfer_interface
    table = interface.upload_table
    interface.add_upload_record(
        TransferRecord(
            task_id="task-done",
            direction="upload",
            name="done.txt",
            size=10,
            status="已完成",
        )
    )
    interface.add_upload_record(
        TransferRecord(
            task_id="task-active",
            direction="upload",
            name="active.txt",
            size=10,
            status="上传中",
        )
    )

    table.selectRow(0)
    assert interface.upload_batch_buttons["count"].text() == "已选 1 项"

    # Filtering to another status shrinks the visible set; the selected row
    # number stays in range and would otherwise silently repoint at the
    # never-selected "上传中" record.
    interface._on_upload_filter_changed("上传中")

    assert [record.name for record in interface._filtered_upload_records()] == ["active.txt"]
    assert table.selectionModel().selectedRows() == []
    assert interface.upload_batch_buttons["count"].text() == "已选 0 项"


def test_main_window_enables_upload_after_browser_attached(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)

    window.refresh_current_directory()

    assert window.file_interface.upload_button_group.isEnabled()
    assert window.file_interface.upload_file_action.isEnabled()
    assert window.file_interface.upload_folder_action.isEnabled()


def test_main_window_rejects_folder_download(qapp: QApplication, tmp_path: Path) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)

    window.refresh_current_directory()
    window.download_displayed_item(0, tmp_path / "Folder", run_in_background=False)

    assert browser.downloaded_items == []
    assert window.status_message() == "只能下载文件"


def test_main_window_create_folder_uses_explorer_style_suffix_for_duplicate_name(
    qapp: QApplication,
) -> None:
    browser = FakeFileBrowser()
    browser.items_by_parent[ROOT_DIRECTORY_ID].append(
        WopanItem(
            item_id="folder-2",
            name="新建文件夹",
            kind=WopanItemKind.FOLDER,
            parent_id=ROOT_DIRECTORY_ID,
        )
    )
    window = MainWindow(browser)

    window.refresh_current_directory()
    window.create_folder_with_name("新建文件夹")

    assert browser.created_folders == [(ROOT_DIRECTORY_ID, "新建文件夹 (1)")]
    assert [item.name for item in window.displayed_items()] == [
        "Folder",
        "report.txt",
        "新建文件夹",
        "新建文件夹 (1)",
    ]


def test_main_window_create_folder_increments_duplicate_suffix(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    browser.items_by_parent[ROOT_DIRECTORY_ID].extend(
        [
            WopanItem(
                item_id="folder-2",
                name="Reports",
                kind=WopanItemKind.FOLDER,
                parent_id=ROOT_DIRECTORY_ID,
            ),
            WopanItem(
                item_id="folder-3",
                name="Reports (1)",
                kind=WopanItemKind.FOLDER,
                parent_id=ROOT_DIRECTORY_ID,
            ),
        ]
    )
    window = MainWindow(browser)

    window.refresh_current_directory()
    window.create_folder_with_name(" Reports ")

    assert browser.created_folders == [(ROOT_DIRECTORY_ID, "Reports (2)")]


def test_main_window_rejects_empty_operation_inputs(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)

    window.refresh_current_directory()
    window.create_folder_with_name(" ")
    assert window.status_message() == "文件夹名称不能为空"

    window.rename_displayed_item(0, " ")
    assert window.status_message() == "名称不能为空"

    window.move_displayed_item(0, " ")
    assert window.status_message() == "目标文件夹不能为空"


def test_main_window_operation_failure_keeps_items_and_shows_error(qapp: QApplication) -> None:
    browser = FailingOperationFileBrowser()
    window = MainWindow(browser)

    window.refresh_current_directory()
    before = window.displayed_items()
    window.rename_displayed_item(0, "renamed")

    assert window.displayed_items() == before
    assert window.status_message() == "重命名失败：name exists"


def test_main_window_upload_login_required_emits_signal(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    messages: list[str] = []
    local_path = tmp_path / "upload.txt"
    local_path.write_text("content")
    window = MainWindow(LoginExpiredFileBrowser())
    window.login_required.connect(messages.append)

    window.upload_file_to_current_directory(local_path, run_in_background=False)

    assert window.status_message() == "登录已过期，请重新登录"
    assert messages == ["登录已过期，请重新登录"]


def test_main_window_create_folder_reports_when_refresh_does_not_show_item(
    qapp: QApplication,
) -> None:
    browser = DelayedCreatedFolderBrowser()
    window = MainWindow(browser)

    window.refresh_current_directory()
    window.create_folder_with_name("New Folder")

    assert browser.created_folders == [(ROOT_DIRECTORY_ID, "New Folder")]
    assert "刷新后未在当前目录看到" in window.status_message()


def test_main_window_upload_reports_when_refresh_does_not_show_item(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    browser = DelayedUploadedFileBrowser()
    window = MainWindow(browser)
    local_path = tmp_path / "upload.txt"
    local_path.write_text("content")

    window.refresh_current_directory()
    window.upload_file_to_current_directory(local_path, run_in_background=False)

    assert browser.uploaded_files == [(ROOT_DIRECTORY_ID, local_path)]
    assert "刷新后未在当前目录看到" in window.status_message()


def test_main_window_move_prompt_opens_dialog_without_subfolders(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = FakeFileBrowser()
    browser.items_by_parent[ROOT_DIRECTORY_ID] = [
        WopanItem(
            item_id="file-1",
            name="report.txt",
            kind=WopanItemKind.FILE,
            parent_id=ROOT_DIRECTORY_ID,
        )
    ]
    instances: list[object] = []

    class _StubDialog:
        def __init__(self, mode, initial_entry, initial_folders, **_kwargs) -> None:
            self.mode = mode
            self.initial_entry = initial_entry
            self.initial_folders = initial_folders
            self.directory_requested = _DisconnectedSignal()
            self.create_folder_requested = _DisconnectedSignal()
            instances.append(self)

        exec = staticmethod(lambda: QDialog.DialogCode.Rejected)

        def start_browse(self) -> None:
            return None

        def current_target(self) -> None:
            return None

        def deleteLater(self) -> None:
            return None

    class _DisconnectedSignal:
        def connect(self, _callback: object) -> None:
            return None

    monkeypatch.setattr(main_window_module, "TargetFolderDialog", _StubDialog)
    window = MainWindow(browser)

    window.refresh_current_directory()
    window.prompt_move_item(0)

    assert len(instances) == 1
    assert instances[0].mode == "move"
    assert instances[0].initial_folders == []


@pytest.mark.parametrize(
    ("prompt_method", "expected_verb"),
    [("prompt_move_item", "移动"), ("prompt_copy_item", "复制")],
)
def test_main_window_transfer_prompt_shows_readable_root_path(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    prompt_method: str,
    expected_verb: str,
) -> None:
    """The dialog root segment reads 根目录, never a "/ /" double separator."""
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    opened: list[TargetFolderDialog] = []

    def _capture_exec(dialog: TargetFolderDialog) -> int:
        opened.append(dialog)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(TargetFolderDialog, "exec", _capture_exec)
    getattr(window, prompt_method)(1)

    dialog = opened[0]
    assert dialog._path_label.text() == "根目录"
    assert dialog._ok_button.text() == f"{expected_verb}到此（根目录）"
    assert "/ /" not in dialog._path_label.text()
    dialog.deleteLater()


class QueuedFileBrowser(FakeFileBrowser):
    def __init__(self) -> None:
        super().__init__()
        self.submitted_downloads: list[tuple[str, Path]] = []
        self._download_callback = None

    def set_download_event_callback(self, callback: object) -> None:
        self._download_callback = callback

    def recover_downloads(self) -> tuple[object, ...]:
        return ()

    def submit_download(self, item: WopanItem, local_path: Path) -> str:
        self.submitted_downloads.append((item.item_id, local_path))
        return f"queued-{len(self.submitted_downloads)}"


def test_main_window_submits_multiple_selected_files_to_scheduler(
    qapp: QApplication, tmp_path: Path, sync_threads: None
) -> None:
    browser = QueuedFileBrowser()
    window = MainWindow(browser)
    browser.set_download_event_callback(window._receive_download_event)
    window.refresh_current_directory()
    table = window.file_interface.file_table
    table.selectRow(1)
    table.selectRow(0)
    window._submit_download_items(
        [
            (window.displayed_items()[1], tmp_path / "report.txt"),
            (
                WopanItem(
                    item_id="file-2",
                    name="other.txt",
                    kind=WopanItemKind.FILE,
                    download_id="fid-2",
                ),
                tmp_path / "other.txt",
            ),
        ]
    )

    assert browser.submitted_downloads == [
        ("file-1", tmp_path / "report.txt"),
        ("file-2", tmp_path / "other.txt"),
    ]
    assert [record.task_id for record in window.transfer_interface.download_records] == [
        "queued-1",
        "queued-2",
    ]


def _make_target_dialog(window: MainWindow) -> TargetFolderDialog:
    return TargetFolderDialog(
        "move",
        TargetEntry(item_id=ROOT_DIRECTORY_ID, name="/"),
        [],
        parent=window,
    )


def test_target_dialog_create_folder_runs_on_worker_and_selects_new_folder(
    qapp: QApplication,
) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    dialog = _make_target_dialog(window)
    dialog.directory_requested.connect(window._on_target_directory_requested)
    window._target_dialog = dialog

    window._on_target_create_folder_requested(ROOT_DIRECTORY_ID, "新目录")

    assert browser.created_folders == [(ROOT_DIRECTORY_ID, "新目录")]
    assert dialog.current_target() == TargetEntry(item_id="created-folder", name="新目录")
    assert dialog._ok_button.text() == "移动到「新目录」"
    assert dialog._create_in_flight is False
    assert dialog._load_in_flight is False
    assert dialog._create_folder_button.isEnabled()
    assert window._target_create_thread is None
    assert window._target_create_worker is None
    window._target_dialog = None
    dialog.deleteLater()


def test_target_dialog_create_folder_failure_shows_error_and_reenables(
    qapp: QApplication,
) -> None:
    class _FailingTargetCreateBrowser(FakeFileBrowser):
        def create_folder(self, parent_id: str, name: str) -> WopanItem:
            raise FileBrowserError("目录已存在")

    window = MainWindow(_FailingTargetCreateBrowser())
    dialog = _make_target_dialog(window)
    window._target_dialog = dialog

    window._on_target_create_folder_requested(ROOT_DIRECTORY_ID, "新目录")

    assert dialog._status_label.text() == "创建失败：目录已存在"
    assert dialog._create_in_flight is False
    assert dialog._create_folder_button.isEnabled()
    assert dialog.current_target() == TargetEntry(item_id=ROOT_DIRECTORY_ID, name="/")
    assert window._target_create_thread is None
    window._target_dialog = None
    dialog.deleteLater()


def test_target_dialog_create_folder_login_required_rejects_dialog(
    qapp: QApplication,
) -> None:
    window = MainWindow(LoginExpiredFileBrowser())
    messages: list[str] = []
    window.login_required.connect(messages.append)
    dialog = _make_target_dialog(window)
    window._target_dialog = dialog

    window._on_target_create_folder_requested(ROOT_DIRECTORY_ID, "新目录")

    assert messages == ["登录已过期，请重新登录"]
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert window._target_create_thread is None
    window._target_dialog = None
    dialog.deleteLater()


def test_target_dialog_create_folder_skips_while_previous_create_in_flight(
    qapp: QApplication,
) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    dialog = _make_target_dialog(window)
    window._target_dialog = dialog
    window._target_create_thread = QThread(window)

    window._on_target_create_folder_requested(ROOT_DIRECTORY_ID, "新目录")

    assert browser.created_folders == []
    assert dialog._status_label.text() == "创建失败：已有创建任务进行中，请稍候"
    window._target_create_thread = None
    window._target_dialog = None
    dialog.deleteLater()


def test_target_dialog_create_folder_without_browser_shows_error(qapp: QApplication) -> None:
    window = MainWindow()
    dialog = _make_target_dialog(window)
    window._target_dialog = dialog

    window._on_target_create_folder_requested(ROOT_DIRECTORY_ID, "新目录")

    assert dialog._status_label.text() == "创建失败：请先登录"
    window._target_dialog = None
    dialog.deleteLater()


def test_target_dialog_create_folder_without_dialog_is_ignored(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)

    window._on_target_create_folder_requested(ROOT_DIRECTORY_ID, "新目录")

    assert browser.created_folders == []
    assert window._target_create_thread is None


def test_target_dialog_create_terminal_results_guarded(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    dialog = _make_target_dialog(window)
    window._target_dialog = dialog

    window._on_target_create_succeeded("bogus")
    assert dialog._pending_selection is None

    window._target_dialog = None
    window._on_target_create_succeeded(None)
    window._on_target_create_failed("late")
    window._on_target_create_login_required("late")

    window._closing = True
    window._target_dialog = dialog
    window._on_target_create_succeeded(None)
    window._on_target_create_failed("closing")
    window._on_target_create_login_required("closing")


class _RecordingInfoBar:
    calls: list[tuple[str, str]] = []

    @classmethod
    def warning(cls, *, title: str, content: str, parent: object = None) -> None:
        cls.calls.append((title, content))

    @classmethod
    def error(cls, *, title: str, content: str, parent: object = None) -> None:
        cls.calls.append((title, content))

    @classmethod
    def success(cls, *, title: str, content: str, parent: object = None) -> None:
        cls.calls.append((title, content))

    @classmethod
    def info(cls, *, title: str, content: str, parent: object = None) -> None:
        cls.calls.append((title, content))


def test_move_copy_delete_while_busy_show_visible_warning(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    monkeypatch.setattr(main_window_module, "InfoBar", _RecordingInfoBar)
    _RecordingInfoBar.calls = []
    window._move_thread = QThread(window)
    window._copy_thread = QThread(window)
    window._delete_thread = QThread(window)

    window.move_displayed_items([1], "folder-2")
    window.copy_displayed_items([1], "folder-2")
    window.delete_displayed_items([0])

    assert _RecordingInfoBar.calls == [
        ("移动", "已有移动任务进行中，请稍候"),
        ("复制", "已有复制任务进行中，请稍候"),
        ("删除", "已有删除任务进行中，请稍候"),
    ]
    assert browser.moved_items == []
    assert browser.copied_items == []
    assert browser.deleted_items == []


def test_operation_busy_bar_reference_counting(qapp: QApplication) -> None:
    window = MainWindow(FakeFileBrowser())
    bar = window.file_interface.operation_busy_bar

    assert bar.isHidden()

    window.file_interface.set_operation_busy(True)
    assert not bar.isHidden()
    window.file_interface.set_operation_busy(True)
    assert not bar.isHidden()
    window.file_interface.set_operation_busy(False)
    assert not bar.isHidden()
    window.file_interface.set_operation_busy(False)
    assert bar.isHidden()


class _BusyObservingBrowser(FakeFileBrowser):
    def __init__(self) -> None:
        super().__init__()
        self.busy_snapshots: dict[str, bool] = {}
        self.window: MainWindow | None = None

    def _busy_visible(self) -> bool:
        assert self.window is not None
        return not self.window.file_interface.operation_busy_bar.isHidden()

    def move_items(self, items: Sequence[WopanItem], target_parent_id: str) -> None:
        self.busy_snapshots["move"] = self._busy_visible()
        super().move_items(items, target_parent_id)

    def copy_items(self, items: Sequence[WopanItem], target_parent_id: str) -> None:
        self.busy_snapshots["copy"] = self._busy_visible()
        super().copy_items(items, target_parent_id)

    def delete_items(self, items: Sequence[WopanItem]) -> None:
        self.busy_snapshots["delete"] = self._busy_visible()
        super().delete_items(items)


def test_move_copy_delete_show_busy_indicator_until_terminal(qapp: QApplication) -> None:
    browser = _BusyObservingBrowser()
    window = MainWindow(browser)
    browser.window = window
    window.refresh_current_directory()

    window.move_displayed_items([1], "folder-2")
    window.copy_displayed_items([0], "folder-3")
    window.delete_displayed_items([0])

    assert browser.busy_snapshots == {"move": True, "copy": True, "delete": True}
    assert window.file_interface.operation_busy_bar.isHidden()


# ---------------------------------------------------------------------------
# File-page header-click sorting and type filtering (10-01-table-sort-filter)
# ---------------------------------------------------------------------------


def _rich_root_items() -> list[WopanItem]:
    return [
        WopanItem(
            item_id="folder-b", name="Zed", kind=WopanItemKind.FOLDER, parent_id=ROOT_DIRECTORY_ID
        ),
        WopanItem(
            item_id="file-a",
            name="b.txt",
            kind=WopanItemKind.FILE,
            parent_id=ROOT_DIRECTORY_ID,
            size=999,
        ),
        WopanItem(
            item_id="file-b",
            name="A.txt",
            kind=WopanItemKind.FILE,
            parent_id=ROOT_DIRECTORY_ID,
            size=1_000_000,
        ),
        WopanItem(
            item_id="file-c",
            name="报告.pdf",
            kind=WopanItemKind.FILE,
            parent_id=ROOT_DIRECTORY_ID,
            size=2048,
        ),
        WopanItem(
            item_id="folder-a",
            name="alpha",
            kind=WopanItemKind.FOLDER,
            parent_id=ROOT_DIRECTORY_ID,
        ),
    ]


def _file_row_names(window: MainWindow) -> list[str]:
    table = window.file_interface.file_table
    return [table.item(row, FILE_COL_NAME).text() for row in range(table.rowCount())]


def test_file_page_sort_by_name_casefold(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    browser.items_by_parent[ROOT_DIRECTORY_ID] = _rich_root_items()
    window = MainWindow(browser)
    window.refresh_current_directory()

    window.file_interface._on_table_header_clicked(FILE_COL_NAME)

    assert _file_row_names(window) == ["A.txt", "alpha", "b.txt", "Zed", "报告.pdf"]
    header = window.file_interface.file_table.horizontalHeader()
    assert header.sortIndicatorSection() == FILE_COL_NAME
    assert header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder


def test_file_page_sort_by_name_descending(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    browser.items_by_parent[ROOT_DIRECTORY_ID] = _rich_root_items()
    window = MainWindow(browser)
    window.refresh_current_directory()
    window.file_interface._on_table_header_clicked(FILE_COL_NAME)

    window.file_interface._on_table_header_clicked(FILE_COL_NAME)

    assert _file_row_names(window) == ["报告.pdf", "Zed", "b.txt", "alpha", "A.txt"]


def test_file_page_sort_by_size_is_numeric_not_lexicographic(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    browser.items_by_parent[ROOT_DIRECTORY_ID] = _rich_root_items()
    window = MainWindow(browser)
    window.refresh_current_directory()

    window.file_interface._on_table_header_clicked(FILE_COL_SIZE)

    # Folders carry size None (keyed as 0) and stay ahead of the files;
    # "1.0 MB" must not lexicographically precede "999 B".
    assert _file_row_names(window) == ["Zed", "alpha", "b.txt", "报告.pdf", "A.txt"]


def test_file_page_sort_by_kind_puts_folders_first(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    browser.items_by_parent[ROOT_DIRECTORY_ID] = _rich_root_items()
    window = MainWindow(browser)
    window.refresh_current_directory()

    window.file_interface._on_table_header_clicked(FILE_COL_KIND)

    assert _file_row_names(window)[:2] == ["Zed", "alpha"]
    assert sorted(_file_row_names(window)[2:]) == ["A.txt", "b.txt", "报告.pdf"]


def test_file_page_sort_cycle_returns_to_server_order(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    browser.items_by_parent[ROOT_DIRECTORY_ID] = _rich_root_items()
    window = MainWindow(browser)
    window.refresh_current_directory()

    for _click in range(3):
        window.file_interface._on_table_header_clicked(FILE_COL_NAME)

    assert _file_row_names(window) == ["Zed", "b.txt", "A.txt", "报告.pdf", "alpha"]
    header = window.file_interface.file_table.horizontalHeader()
    assert header.sortIndicatorSection() == -1


def test_file_page_sort_keeps_backing_order_untouched(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    browser.items_by_parent[ROOT_DIRECTORY_ID] = _rich_root_items()
    window = MainWindow(browser)
    window.refresh_current_directory()

    window.file_interface._on_table_header_clicked(FILE_COL_NAME)

    assert [item.name for item in window.displayed_items()] == [
        "Zed",
        "b.txt",
        "A.txt",
        "报告.pdf",
        "alpha",
    ]


def test_file_page_type_filter_combo(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    browser.items_by_parent[ROOT_DIRECTORY_ID] = _rich_root_items()
    window = MainWindow(browser)
    window.refresh_current_directory()
    combo = window.file_interface.type_filter_combo

    combo.setCurrentText(FILE_TYPE_FILTER_FOLDERS)
    assert _file_row_names(window) == ["Zed", "alpha"]
    assert window.status_message().startswith("2 项")

    combo.setCurrentText(FILE_TYPE_FILTER_FILES)
    assert _file_row_names(window) == ["b.txt", "A.txt", "报告.pdf"]

    combo.setCurrentText(FILE_TYPE_FILTER_ALL)
    assert len(_file_row_names(window)) == 5


def test_file_page_type_filter_no_match_shows_no_match_status(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    window.enter_displayed_folder(0)
    assert [item.name for item in window.displayed_items()] == ["child.txt"]

    window.file_interface.type_filter_combo.setCurrentText(FILE_TYPE_FILTER_FOLDERS)

    assert _file_row_names(window) == []
    assert window.status_message() == NO_MATCH_TEXT

    window.file_interface.type_filter_combo.setCurrentText(FILE_TYPE_FILTER_ALL)
    assert _file_row_names(window) == ["child.txt"]
    assert window.status_message().startswith("1 项")


def test_file_page_type_filter_combo_follows_operations_enabled(qapp: QApplication) -> None:
    logged_out = MainWindow()
    assert not logged_out.file_interface.type_filter_combo.isEnabled()

    browser = FakeFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()

    assert window.file_interface.type_filter_combo.isEnabled()


def test_file_page_enter_folder_from_filtered_view(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()
    window.file_interface.type_filter_combo.setCurrentText(FILE_TYPE_FILTER_FOLDERS)
    assert _file_row_names(window) == ["Folder"]

    # Row 0 of the filtered view is the folder itself; entering it must work
    # even though the backing list also holds report.txt at row 0.
    window.enter_displayed_folder(0)

    assert window.current_directory_id() == "folder-1"
    assert [item.name for item in window.displayed_items()] == ["child.txt"]


def test_file_page_delete_targets_visible_rows_under_sort(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    browser.items_by_parent[ROOT_DIRECTORY_ID] = _rich_root_items()
    window = MainWindow(browser)
    window.refresh_current_directory()
    window.file_interface._on_table_header_clicked(FILE_COL_NAME)
    window.file_interface._on_table_header_clicked(FILE_COL_NAME)  # descending
    assert _file_row_names(window)[0] == "报告.pdf"

    # Row 0 of the sorted view is file-c (报告.pdf), not the backing list's
    # own row 0 — the deletion must remove exactly the displayed entry.
    window.delete_displayed_items([0])

    assert browser.deleted_items == ["file-c"]


def test_file_page_select_item_row_uses_visible_order(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    browser.items_by_parent[ROOT_DIRECTORY_ID] = _rich_root_items()
    window = MainWindow(browser)
    window.refresh_current_directory()
    window.file_interface._on_table_header_clicked(FILE_COL_NAME)
    window.file_interface._on_table_header_clicked(FILE_COL_NAME)  # descending
    # Descending rows: Zed, 报告.pdf, b.txt, alpha, A.txt → file-a lands at row 2.

    window._select_item_row("file-a")

    assert window.selected_rows() == [2]


def test_file_page_sort_persists_across_navigation(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    browser.items_by_parent[ROOT_DIRECTORY_ID] = _rich_root_items()
    browser.items_by_parent["folder-a"] = [
        WopanItem(
            item_id="nested-file",
            name="nested.txt",
            kind=WopanItemKind.FILE,
            parent_id="folder-a",
            size=1,
        )
    ]
    window = MainWindow(browser)
    window.refresh_current_directory()
    window.file_interface._on_table_header_clicked(FILE_COL_NAME)
    # Ascending rows: A.txt, alpha, b.txt, Zed, 报告.pdf → alpha is row 1.
    assert _file_row_names(window)[1] == "alpha"

    window.enter_displayed_folder(1)
    assert _file_row_names(window) == ["nested.txt"]
    window.go_up_one_level()

    assert _file_row_names(window) == ["A.txt", "alpha", "b.txt", "Zed", "报告.pdf"]
    assert window.current_directory_id() == ROOT_DIRECTORY_ID


def test_file_page_b24_cache_hit_applies_active_filter(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()

    window.enter_displayed_folder(0)
    window.file_interface.type_filter_combo.setCurrentText(FILE_TYPE_FILTER_FILES)
    assert _file_row_names(window) == ["child.txt"]
    listing_calls_after_enter = len(browser.requested_parent_ids)

    window.go_up_one_level()

    # Returning to the cached root issues no extra listing (B24) and the
    # filter still shapes the rendered rows.
    assert len(browser.requested_parent_ids) == listing_calls_after_enter
    assert _file_row_names(window) == ["report.txt"]


# ---------------------------------------------------------------------------
# Transfer-page header-click sorting (10-01-table-sort-filter)
# ---------------------------------------------------------------------------


def _upload_record(task_id: str, name: str, **overrides: object) -> TransferRecord:
    values: dict[str, object] = {
        "task_id": task_id,
        "direction": "upload",
        "name": name,
        "size": 100,
        "status": "已完成",
    }
    values.update(overrides)
    return TransferRecord(**values)  # type: ignore[arg-type]


def _transfer_row_names(transfer: TransferInterface, table: TableWidget) -> list[str]:
    return [table.item(row, TRANSFER_COL_NAME).text() for row in range(table.rowCount())]


def test_transfer_sort_by_name_casefold(qapp: QApplication) -> None:
    transfer = TransferInterface()
    transfer.add_upload_record(_upload_record("t-1", "c.txt"))
    transfer.add_upload_record(_upload_record("t-2", "B.txt"))
    transfer.add_upload_record(_upload_record("t-3", "报告.txt"))

    transfer._on_table_header_clicked("upload", TRANSFER_COL_NAME)

    assert _transfer_row_names(transfer, transfer.upload_table) == ["B.txt", "c.txt", "报告.txt"]
    header = transfer.upload_table.horizontalHeader()
    assert header.sortIndicatorSection() == TRANSFER_COL_NAME
    assert header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
    # The backing list keeps insertion order.
    assert [record.name for record in transfer.upload_records] == ["c.txt", "B.txt", "报告.txt"]


def test_transfer_sort_by_size_numeric_with_none(qapp: QApplication) -> None:
    transfer = TransferInterface()
    transfer.add_upload_record(_upload_record("t-1", "mb.txt", size=1_000_000))
    transfer.add_upload_record(_upload_record("t-2", "unknown.txt", size=None))
    transfer.add_upload_record(_upload_record("t-3", "kb.txt", size=2048))

    transfer._on_table_header_clicked("upload", TRANSFER_COL_SIZE)

    assert _transfer_row_names(transfer, transfer.upload_table) == [
        "unknown.txt",
        "kb.txt",
        "mb.txt",
    ]


def test_transfer_sort_by_status_rank_unknown_last(qapp: QApplication) -> None:
    transfer = TransferInterface()
    transfer.add_upload_record(_upload_record("t-1", "done.txt", status="已完成"))
    transfer.add_upload_record(_upload_record("t-2", "active.txt", status="上传中"))
    transfer.add_upload_record(_upload_record("t-3", "mystery.txt", status="神秘状态"))
    transfer.add_upload_record(_upload_record("t-4", "cancelled.txt", status="已取消"))
    transfer.add_upload_record(_upload_record("t-5", "failed.txt", status="失败"))
    transfer.add_upload_record(_upload_record("t-6", "waiting.txt", status="等待中"))

    transfer._on_table_header_clicked("upload", TRANSFER_COL_STATUS)

    assert _transfer_row_names(transfer, transfer.upload_table) == [
        "active.txt",
        "waiting.txt",
        "failed.txt",
        "done.txt",
        "cancelled.txt",
        "mystery.txt",
    ]


@pytest.mark.parametrize("column", [TRANSFER_COL_PROGRESS, TRANSFER_COL_SPEED, TRANSFER_COL_ACTION])
def test_transfer_dynamic_columns_ignore_header_clicks(qapp: QApplication, column: int) -> None:
    transfer = TransferInterface()
    transfer.add_upload_record(_upload_record("t-2", "b.txt"))
    transfer.add_upload_record(_upload_record("t-1", "a.txt"))

    transfer._on_table_header_clicked("upload", column)

    assert transfer._upload_sort_state.column is None
    assert _transfer_row_names(transfer, transfer.upload_table) == ["b.txt", "a.txt"]
    assert transfer.upload_table.horizontalHeader().sortIndicatorSection() == -1


def test_transfer_batch_updates_renders_once_per_direction(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batch mutations render each table once on exit, not once per mutation.

    Regression guard for the folder-upload add freeze: every render inside
    one slot rebuilds all rows' action widgets, whose deferred deletes can
    only run after the slot returns, so N renders cost O(n²) live widgets
    (~60s freeze and a multi-GB RSS spike on a 157-file folder).
    """
    transfer = TransferInterface()
    counts = {"upload": 0, "download": 0}
    for direction in ("upload", "download"):
        original = getattr(transfer, f"_render_{direction}_table")

        def make_counted(direction: str, original: object) -> Callable[[], None]:
            def counted() -> None:
                counts[direction] += 1
                cast(Callable[[], None], original)()

            return counted

        monkeypatch.setattr(
            transfer, f"_render_{direction}_table", make_counted(direction, original)
        )

    with transfer.batch_updates():
        for index in range(20):
            transfer.add_upload_record(_upload_record(f"u-{index}", f"u{index}.txt"))
            transfer.add_download_record(
                _upload_record(f"d-{index}", f"d{index}.txt", direction="download")
            )
            transfer.update_record("upload", f"u-{index}", status="已暂停", can_resume=True)
            transfer.remove_records("download", {f"d-{index}"})
        assert counts == {"upload": 0, "download": 0}

    assert counts == {"upload": 1, "download": 1}

    # Outside a batch a status change still renders immediately.
    transfer.update_record("upload", "u-0", status="上传中")
    assert counts["upload"] == 2

    # Nested batches coalesce into the outermost exit.
    with transfer.batch_updates():
        transfer.update_record("upload", "u-1", status="已暂停", can_resume=True)
        with transfer.batch_updates():
            transfer.update_record("upload", "u-2", status="已暂停", can_resume=True)
    assert counts["upload"] == 3


def test_file_page_unknown_column_click_is_ignored(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)
    window.refresh_current_directory()

    # Defensive guard: a column without a sort key (future columns) never
    # activates sorting or crashes the click handler.
    window.file_interface._on_table_header_clicked(99)

    assert window.file_interface._sort_state.column is None
    assert _file_row_names(window) == ["Folder", "report.txt"]


def test_transfer_sort_reorder_clears_stale_selection(qapp: QApplication) -> None:
    transfer = TransferInterface()
    transfer.add_upload_record(_upload_record("t-2", "b.txt"))
    transfer.add_upload_record(_upload_record("t-1", "a.txt"))
    transfer.add_upload_record(_upload_record("t-3", "c.txt"))
    transfer.upload_table.selectRow(0)
    assert transfer.upload_batch_buttons["count"].text() == "已选 1 项"

    # Sorting reorders the task-id sequence, which contract 16 treats like
    # any other row shift: the stale row selection must be cleared.
    transfer._on_table_header_clicked("upload", TRANSFER_COL_NAME)

    assert _transfer_row_names(transfer, transfer.upload_table) == ["a.txt", "b.txt", "c.txt"]
    assert transfer.upload_table.selectionModel().selectedRows() == []
    assert transfer.upload_batch_buttons["count"].text() == "已选 0 项"


def test_transfer_progress_render_keeps_sorted_order_and_selection(
    qapp: QApplication,
) -> None:
    transfer = TransferInterface()
    transfer.add_upload_record(_upload_record("t-2", "b.txt", status="上传中"))
    transfer.add_upload_record(_upload_record("t-1", "a.txt", status="上传中"))
    transfer._on_table_header_clicked("upload", TRANSFER_COL_NAME)
    transfer.upload_table.selectRow(0)

    # Progress-only updates never touch the sort keys, so the coalesced
    # render must neither reshuffle rows nor wipe the selection.
    transfer.update_record("upload", "t-2", bytes_done=50)
    transfer.flush_progress_render()

    assert _transfer_row_names(transfer, transfer.upload_table) == ["a.txt", "b.txt"]
    assert [index.row() for index in transfer.upload_table.selectionModel().selectedRows()] == [0]
    assert transfer.upload_table.item(1, TRANSFER_COL_PROGRESS).text().startswith("50%")


def test_transfer_status_filter_and_sort_stacked(qapp: QApplication) -> None:
    transfer = TransferInterface()
    transfer.add_upload_record(
        _upload_record("t-1", "small-done.txt", size=10, status="已完成")
    )
    transfer.add_upload_record(_upload_record("t-2", "active.txt", size=5000, status="上传中"))
    transfer.add_upload_record(
        _upload_record("t-3", "big-done.txt", size=1_000_000, status="已完成")
    )

    transfer.upload_filter_combo.setCurrentText("已完成")
    transfer._on_table_header_clicked("upload", TRANSFER_COL_SIZE)
    transfer._on_table_header_clicked("upload", TRANSFER_COL_SIZE)  # descending

    assert _transfer_row_names(transfer, transfer.upload_table) == [
        "big-done.txt",
        "small-done.txt",
    ]


def test_transfer_batch_delete_targets_visible_rows_under_sort(qapp: QApplication) -> None:
    transfer = TransferInterface()
    transfer.add_upload_record(_upload_record("t-2", "b.txt", status="失败"))
    transfer.add_upload_record(_upload_record("t-1", "a.txt", status="已完成"))
    emitted: list[tuple[str, set[str]]] = []
    transfer.remove_records_requested.connect(
        lambda direction, task_ids: emitted.append((direction, set(task_ids)))
    )
    transfer._on_table_header_clicked("upload", TRANSFER_COL_NAME)

    # Ascending order puts a.txt at row 0; the batch delete must hit the
    # displayed row, not the backing list's row 0 (b.txt).
    transfer.upload_table.selectRow(0)
    transfer._request_delete_selected("upload")

    assert emitted == [("upload", {"t-1"})]


def test_transfer_download_header_click_is_independent(qapp: QApplication) -> None:
    transfer = TransferInterface()
    transfer.add_upload_record(_upload_record("t-2", "b.txt"))
    transfer.add_upload_record(_upload_record("t-1", "a.txt"))
    transfer.add_download_record(
        TransferRecord(
            task_id="d-2",
            direction="download",
            name="y.txt",
            size=10,
            status="下载中",
        )
    )
    transfer.add_download_record(
        TransferRecord(
            task_id="d-1",
            direction="download",
            name="x.txt",
            size=10,
            status="下载中",
        )
    )

    transfer._on_table_header_clicked("download", TRANSFER_COL_NAME)

    assert _transfer_row_names(transfer, transfer.download_table) == ["x.txt", "y.txt"]
    assert transfer._upload_sort_state.column is None
    assert _transfer_row_names(transfer, transfer.upload_table) == ["b.txt", "a.txt"]


def test_transfer_sort_persists_across_segment_switch(qapp: QApplication) -> None:
    transfer = TransferInterface()
    transfer.add_upload_record(_upload_record("t-2", "b.txt"))
    transfer.add_upload_record(_upload_record("t-1", "a.txt"))
    transfer._on_table_header_clicked("upload", TRANSFER_COL_NAME)

    transfer._on_segment_changed("download")
    transfer._on_segment_changed("upload")

    assert _transfer_row_names(transfer, transfer.upload_table) == ["a.txt", "b.txt"]
    assert transfer.upload_table.horizontalHeader().sortIndicatorSection() == TRANSFER_COL_NAME
