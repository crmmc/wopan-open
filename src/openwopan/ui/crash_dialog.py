from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QAbstractButton, QApplication, QMessageBox

PROJECT_URL = "https://github.com/crmmc/wopan-open"
ISSUE_URL = f"{PROJECT_URL}/issues/new"

LOGGER = logging.getLogger(__name__)


def _build_crash_message_box(
    *,
    title: str,
    text: str,
    icon: QMessageBox.Icon,
) -> tuple[QMessageBox, QAbstractButton]:
    message_box = QMessageBox()
    message_box.setIcon(icon)
    message_box.setText(text)
    open_button = message_box.addButton("打开日志文件夹", QMessageBox.ButtonRole.AcceptRole)
    message_box.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
    message_box.setProperty("windowTitle", title)
    return message_box, open_button


def _show_dialog(
    log_dir: Path,
    *,
    title: str,
    text: str,
    icon: QMessageBox.Icon,
) -> bool:
    if QApplication.instance() is None:
        LOGGER.warning("app.crash.dialog_unavailable")
        return False

    message_box, open_button = _build_crash_message_box(title=title, text=text, icon=icon)
    message_box.exec()
    if message_box.clickedButton() is open_button:
        try:
            opened = QDesktopServices.openUrl(QUrl(log_dir.as_uri()))
        except OSError:
            LOGGER.warning("app.crash.dialog.open_log_folder_failed")
        else:
            if opened:
                LOGGER.info("app.crash.dialog.open_log_folder")
            else:
                LOGGER.warning("app.crash.dialog.open_log_folder_failed")
    return True


def show_crash_dialog(log_dir: Path) -> bool:
    return _show_dialog(
        log_dir,
        title="程序遇到错误",
        text=(
            "程序遇到未处理的错误，需要重启。\n\n"
            "崩溃详情已写入日志文件夹：\n"
            "- openwopan.log（运行日志）\n"
            "- openwopan-crash.log（原生崩溃记录）\n\n"
            f"项目地址：{PROJECT_URL}\n"
            f"请携带上述日志前往 {ISSUE_URL} 提交 issue，帮助我们定位并修复问题。"
        ),
        icon=QMessageBox.Icon.Critical,
    )


def show_unclean_shutdown_notice(log_dir: Path) -> bool:
    return _show_dialog(
        log_dir,
        title="检测到上次程序异常退出",
        text=(
            "检测到上次程序可能异常退出。如问题仍然存在，请重启程序。\n\n"
            "崩溃详情已写入日志文件夹：\n"
            "- openwopan.log（运行日志）\n"
            "- openwopan-crash.log（原生崩溃记录）\n\n"
            f"项目地址：{PROJECT_URL}\n"
            f"请携带上述日志前往 {ISSUE_URL} 提交 issue，帮助我们定位并修复问题。"
        ),
        icon=QMessageBox.Icon.Information,
    )
