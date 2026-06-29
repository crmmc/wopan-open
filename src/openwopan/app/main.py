from __future__ import annotations

import logging
import os
import platform
import sys
from collections.abc import Sequence
from typing import cast

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication
from qfluentwidgets import FluentTranslator, Theme, setTheme

from openwopan.app.bootstrap import AppDependencies, build_dependencies
from openwopan.app.controller import ApplicationController, LoginWindowBoundary
from openwopan.app.logging_config import configure_logging
from openwopan.storage.settings import ensure_app_settings_file, load_app_settings
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


def main(argv: Sequence[str] | None = None) -> int:
    """Start the OpenWoPan desktop application."""
    settings = load_app_settings()
    settings_path = ensure_app_settings_file(settings)
    log_path = configure_logging(settings)
    LOGGER.info(
        "app.start log_level=%s settings_path=%s log_path=%s",
        settings.log_level,
        settings_path,
        log_path,
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

    dependencies = build_dependencies()
    dependencies = AppDependencies(
        credential_store=dependencies.credential_store,
        web_login_coordinator=dependencies.web_login_coordinator,
        file_browser_factory=dependencies.file_browser_factory,
        settings=settings,
    )
    window = MainWindow(settings=settings, settings_path=settings_path, log_path=log_path)
    controller = ApplicationController(dependencies, window, _build_login_window, app.quit)

    if os.environ.get(SMOKE_TEST_ENV) == "1":
        QTimer.singleShot(0, app.quit)
    else:
        QTimer.singleShot(0, controller.start)

    return int(app.exec())
