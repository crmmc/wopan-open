from __future__ import annotations

import logging
import os
import platform
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication
from qfluentwidgets import FluentTranslator, Theme, setTheme

from openwopan.app.bootstrap import AppDependencies, build_dependencies
from openwopan.app.controller import ApplicationController, LoginWindowBoundary
from openwopan.app.logging_config import configure_logging, install_crash_reporting
from openwopan.storage.settings import ensure_app_settings_file, load_app_settings
from openwopan.ui.crash_dialog import show_crash_dialog, show_unclean_shutdown_notice
from openwopan.ui.login_window import LoginWindow
from openwopan.ui.main_window import MainWindow

SMOKE_TEST_ENV = "OPENWOPAN_SMOKE_TEST"
LOGGER = logging.getLogger(__name__)


def _build_login_window() -> LoginWindowBoundary:
    return cast(LoginWindowBoundary, LoginWindow())


def _application_args(argv: Sequence[str] | None) -> list[str]:
    args = list(argv) if argv is not None else list(sys.argv)
    if not args:
        return ["openwopan"]
    return args


def session_marker_path(log_path: Path) -> Path:
    return log_path.parent / "openwopan.running"


def write_session_marker(path: Path) -> None:
    path.write_text("", encoding="utf-8")


def clear_session_marker(path: Path) -> None:
    path.unlink(missing_ok=True)


def detect_and_clear_stale_marker(path: Path) -> bool:
    if not path.exists():
        return False
    clear_session_marker(path)
    return True


def _show_crash_dialog_for_log(log_path: Path) -> None:
    show_crash_dialog(log_path.parent)


def main(argv: Sequence[str] | None = None) -> int:
    """Start the OpenWoPan desktop application."""
    settings = load_app_settings()
    settings_path = ensure_app_settings_file(settings)
    log_path = configure_logging(settings)
    marker_path = session_marker_path(log_path)
    unclean_previous_session = detect_and_clear_stale_marker(marker_path)
    if unclean_previous_session:
        LOGGER.warning("app.start.unclean_previous_session")
    crash_log_path = install_crash_reporting(
        log_path,
        on_crash=_show_crash_dialog_for_log,
    )
    write_session_marker(marker_path)
    LOGGER.info(
        "app.start log_level=%s settings_path=%s log_path=%s crash_log_path=%s",
        settings.log_level,
        settings_path,
        log_path,
        crash_log_path,
    )

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(_application_args(argv))
    app.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)
    if platform.system() == "Darwin":
        font = QFont("PingFang SC")
        font.insertSubstitution("Segoe UI", "PingFang SC")
        font.insertSubstitution("Segoe UI Semibold", "PingFang SC")
        app.setFont(font)
    app.installTranslator(FluentTranslator())
    setTheme(Theme.LIGHT)

    is_smoke_test = os.environ.get(SMOKE_TEST_ENV) == "1"
    if unclean_previous_session and not is_smoke_test:
        show_unclean_shutdown_notice(log_path.parent)

    dependencies = build_dependencies()
    dependencies = AppDependencies(
        credential_store=dependencies.credential_store,
        web_login_coordinator=dependencies.web_login_coordinator,
        file_browser_factory=dependencies.file_browser_factory,
        settings=settings,
    )
    window = MainWindow(settings=settings, settings_path=settings_path, log_path=log_path)
    controller = ApplicationController(dependencies, window, _build_login_window, app.quit)

    if is_smoke_test:
        QTimer.singleShot(0, app.quit)
    else:
        QTimer.singleShot(0, controller.start)

    try:
        return int(app.exec())
    finally:
        clear_session_marker(marker_path)


if __name__ == "__main__":
    raise SystemExit(main())
