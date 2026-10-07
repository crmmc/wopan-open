from __future__ import annotations

import logging
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QAbstractButton, QApplication, QMessageBox, QWidget

from openwopan.ui import crash_dialog
from openwopan.ui.crash_dialog import ISSUE_URL, PROJECT_URL


def _button_texts(message_box: QMessageBox) -> set[str]:
    return {button.text() for button in message_box.buttons()}


def test_build_crash_message_box_sets_content_and_buttons(qapp: QApplication) -> None:
    text = (
        f"{PROJECT_URL}\n{ISSUE_URL}\n"
        "openwopan.log（运行日志）\nopenwopan-crash.log（原生崩溃记录）"
    )

    message_box, _ = crash_dialog._build_crash_message_box(
        title="程序遇到错误",
        text=text,
        icon=QMessageBox.Icon.Critical,
    )

    assert message_box.windowTitle() == "程序遇到错误"
    assert message_box.icon() == QMessageBox.Icon.Critical
    assert PROJECT_URL in message_box.text()
    assert ISSUE_URL in message_box.text()
    assert "openwopan.log" in message_box.text()
    assert "openwopan-crash.log" in message_box.text()
    assert _button_texts(message_box) == {"打开日志文件夹", "关闭"}


def test_show_crash_dialog_uses_expected_content(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[QMessageBox] = []
    original_build = crash_dialog._build_crash_message_box

    def _capture_build(**kwargs: object):
        message_box, button = original_build(**kwargs)  # type: ignore[arg-type]
        built.append(message_box)
        return message_box, button

    monkeypatch.setattr(crash_dialog, "_build_crash_message_box", _capture_build)
    monkeypatch.setattr(QMessageBox, "exec", lambda _self: 0)

    assert crash_dialog.show_crash_dialog(tmp_path) is True

    assert len(built) == 1
    message_box = built[0]
    assert message_box.windowTitle() == "程序遇到错误"
    assert message_box.icon() == QMessageBox.Icon.Critical
    assert PROJECT_URL in message_box.text()
    assert ISSUE_URL in message_box.text()
    assert "openwopan.log" in message_box.text()
    assert "openwopan-crash.log" in message_box.text()


def test_show_unclean_shutdown_notice_is_non_blocking(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非干净退出提示必须非模态：挂在主窗口上浮出、show() 不阻塞，
    关闭只关闭提示本身；「打开日志文件夹」按钮在非模态下同样可用。"""
    built: list[QMessageBox] = []
    original_build = crash_dialog._build_crash_message_box

    def _capture_build(**kwargs: object):
        message_box, button = original_build(**kwargs)  # type: ignore[arg-type]
        built.append(message_box)
        return message_box, button

    shown: list[QMessageBox] = []

    def _record_show(self: QMessageBox) -> None:
        shown.append(self)

    monkeypatch.setattr(crash_dialog, "_build_crash_message_box", _capture_build)
    monkeypatch.setattr(QMessageBox, "show", _record_show)
    monkeypatch.setattr(
        QMessageBox, "exec", lambda _self: pytest.fail("提示必须用 show() 非阻塞显示")
    )
    parent = QWidget()

    assert crash_dialog.show_unclean_shutdown_notice(tmp_path, parent) is True

    (message_box,) = built
    assert message_box.windowTitle() == "检测到上次程序异常退出"
    assert message_box.icon() == QMessageBox.Icon.Information
    assert message_box.parent() is parent  # 挂在主窗口上浮出
    assert message_box.isModal() is False  # 非模态：不挡主窗口
    assert shown == [message_box]  # show() 而非 exec()


def test_show_unclean_shutdown_notice_open_log_folder_button_works(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非模态提示点「打开日志文件夹」：按钮信号直连，无需阻塞等返回值。"""
    opened_urls: list[str] = []
    original_build = crash_dialog._build_crash_message_box
    built: list[tuple[QMessageBox, QAbstractButton]] = []

    def _capture_build(**kwargs: object):
        message_box, open_button = original_build(**kwargs)  # type: ignore[arg-type]
        built.append((message_box, open_button))
        return message_box, open_button

    monkeypatch.setattr(crash_dialog, "_build_crash_message_box", _capture_build)
    monkeypatch.setattr(
        crash_dialog.QDesktopServices,
        "openUrl",
        lambda url: opened_urls.append(url.toString()) or True,
    )
    monkeypatch.setattr(QMessageBox, "show", lambda _self: None)

    assert crash_dialog.show_unclean_shutdown_notice(tmp_path) is True

    _message_box, open_button = built[0]
    open_button.click()  # 模拟用户点击：信号已连接，直接触发

    assert opened_urls == [tmp_path.as_uri()]


def test_show_crash_dialog_opens_log_folder(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_urls: list[str] = []
    original_build = crash_dialog._build_crash_message_box

    def _build_and_click(**kwargs: object):
        message_box, open_button = original_build(**kwargs)  # type: ignore[arg-type]
        QTimer.singleShot(0, open_button.click)
        return message_box, open_button

    monkeypatch.setattr(crash_dialog, "_build_crash_message_box", _build_and_click)
    monkeypatch.setattr(
        crash_dialog.QDesktopServices,
        "openUrl",
        lambda url: opened_urls.append(url.toString()) or True,
    )

    assert crash_dialog.show_crash_dialog(tmp_path) is True

    assert opened_urls == [tmp_path.as_uri()]


def test_show_crash_dialog_logs_open_failure(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    original_build = crash_dialog._build_crash_message_box

    def _build_and_click(**kwargs: object):
        message_box, open_button = original_build(**kwargs)  # type: ignore[arg-type]
        QTimer.singleShot(0, open_button.click)
        return message_box, open_button

    def _raise_os_error(_url: object) -> bool:
        raise OSError("unavailable")

    monkeypatch.setattr(crash_dialog, "_build_crash_message_box", _build_and_click)
    monkeypatch.setattr(crash_dialog.QDesktopServices, "openUrl", _raise_os_error)

    with caplog.at_level(logging.WARNING, logger=crash_dialog.__name__):
        assert crash_dialog.show_crash_dialog(tmp_path) is True

    assert "app.crash.dialog.open_log_folder_failed" in caplog.text


def test_show_crash_dialog_logs_rejected_open_request(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    original_build = crash_dialog._build_crash_message_box

    def _build_and_click(**kwargs: object):
        message_box, open_button = original_build(**kwargs)  # type: ignore[arg-type]
        QTimer.singleShot(0, open_button.click)
        return message_box, open_button

    monkeypatch.setattr(crash_dialog, "_build_crash_message_box", _build_and_click)
    monkeypatch.setattr(crash_dialog.QDesktopServices, "openUrl", lambda _url: False)

    with caplog.at_level(logging.WARNING, logger=crash_dialog.__name__):
        assert crash_dialog.show_crash_dialog(tmp_path) is True

    assert "app.crash.dialog.open_log_folder_failed" in caplog.text


def test_show_crash_dialog_without_application_returns_false(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _NoApplication:
        @staticmethod
        def instance() -> None:
            return None

    monkeypatch.setattr(crash_dialog, "QApplication", _NoApplication)

    with caplog.at_level(logging.WARNING, logger=crash_dialog.__name__):
        assert crash_dialog.show_crash_dialog(tmp_path) is False

    assert "app.crash.dialog_unavailable" in caplog.text
