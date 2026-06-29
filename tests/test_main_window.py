from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import QAbstractItemView, QApplication, QFrame

from openwopan.app.file_browser import FileBrowserError, FileBrowserLoginRequiredError
from openwopan.auth.session import AuthSession
from openwopan.storage.settings import AppSettings
from openwopan.ui.main_window import (
    DOWNLOAD_STATUS_FILTERS,
    FILE_SPLITTER_STRETCH_FACTORS,
    ROOT_DISPLAY_NAME,
    TRANSFER_TABLE_HEADERS,
    UPLOAD_STATUS_FILTERS,
    MainWindow,
)
from openwopan.wopan.client import ROOT_DIRECTORY_ID
from openwopan.wopan.models import WopanCloudUsage, WopanItem, WopanItemKind


class FakeFileBrowser:
    def __init__(self) -> None:
        self.requested_parent_ids: list[str] = []
        self.created_folders: list[tuple[str, str]] = []
        self.renamed_items: list[tuple[str, str]] = []
        self.deleted_items: list[str] = []
        self.moved_items: list[tuple[str, str]] = []
        self.downloaded_items: list[tuple[str, Path]] = []
        self.uploaded_files: list[tuple[str, Path]] = []
        self.usage_account_ids: list[str] = []
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

    def move_item(self, item: WopanItem, target_parent_id: str) -> None:
        self.moved_items.append((item.item_id, target_parent_id))
        self.delete_item(item)

    def download_file(
        self,
        item: WopanItem,
        local_path: Path,
        progress_callback: object | None = None,
    ) -> None:
        self.downloaded_items.append((item.item_id, local_path))

    def upload_file(self, parent_id: str, local_path: Path) -> WopanItem:
        self.uploaded_files.append((parent_id, local_path))
        uploaded = WopanItem(
            item_id="uploaded-file",
            name=local_path.name,
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


class LoginExpiredFileBrowser:
    def list_directory(self, parent_id: str = ROOT_DIRECTORY_ID) -> list[WopanItem]:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def create_folder(self, parent_id: str, name: str) -> WopanItem:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def rename_item(self, item: WopanItem, new_name: str) -> None:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def delete_item(self, item: WopanItem) -> None:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def move_item(self, item: WopanItem, target_parent_id: str) -> None:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def download_file(
        self,
        item: WopanItem,
        local_path: Path,
        progress_callback: object | None = None,
    ) -> None:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def upload_file(self, parent_id: str, local_path: Path) -> WopanItem:
        raise FileBrowserLoginRequiredError("登录已过期，请重新登录")

    def get_cloud_usage(self, account_id: str) -> WopanCloudUsage:
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
    def upload_file(self, parent_id: str, local_path: Path) -> WopanItem:
        self.uploaded_files.append((parent_id, local_path))
        return WopanItem(
            item_id="delayed-upload",
            name=local_path.name,
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
    assert file_interface.storage_label.text() == "云盘空间"
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
    assert transfer._active_direction == "upload"
    assert tuple(
        transfer.upload_filter_combo.itemText(index)
        for index in range(len(UPLOAD_STATUS_FILTERS))
    ) == UPLOAD_STATUS_FILTERS
    assert tuple(
        transfer.download_filter_combo.itemText(index)
        for index in range(len(DOWNLOAD_STATUS_FILTERS))
    ) == DOWNLOAD_STATUS_FILTERS
    assert transfer.download_frame.isHidden()
    assert transfer.open_download_folder_button.isHidden()

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


def test_main_window_maps_login_required_status(qapp: QApplication) -> None:
    messages: list[str] = []
    window = MainWindow(LoginExpiredFileBrowser())
    window.login_required.connect(messages.append)

    window.refresh_current_directory()

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
    assert browser.requested_parent_ids == [ROOT_DIRECTORY_ID, ROOT_DIRECTORY_ID]
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


def test_main_window_enables_upload_after_browser_attached(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    window = MainWindow(browser)

    window.refresh_current_directory()

    assert window.file_interface.upload_button_group.isEnabled()
    assert window.file_interface.upload_file_action.isEnabled()
    assert not window.file_interface.upload_folder_action.isEnabled()


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


def test_main_window_operation_login_required_emits_signal(qapp: QApplication) -> None:
    messages: list[str] = []
    window = MainWindow(LoginExpiredFileBrowser())
    window.login_required.connect(messages.append)

    window.create_folder_with_name("Reports")

    assert window.status_message() == "登录已过期，请重新登录"
    assert messages == ["登录已过期，请重新登录"]


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


def test_main_window_move_prompt_reports_when_no_target_folder(qapp: QApplication) -> None:
    browser = FakeFileBrowser()
    browser.items_by_parent[ROOT_DIRECTORY_ID] = [
        WopanItem(
            item_id="file-1",
            name="report.txt",
            kind=WopanItemKind.FILE,
            parent_id=ROOT_DIRECTORY_ID,
        )
    ]
    window = MainWindow(browser)

    window.refresh_current_directory()
    window.prompt_move_item(0)

    assert window.status_message() == "没有可用的目标文件夹"
