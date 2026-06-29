from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from openwopan.app.bootstrap import AppDependencies
from openwopan.auth.web_login import WebLoginError, WebLoginResult
from openwopan.ui.main_window import MainWindow

LOGGER = logging.getLogger(__name__)


class CookieHeaderSignal(Protocol):
    """Minimal Qt signal surface used by the application controller."""

    def connect(self, slot: Callable[[str], None]) -> object:
        """Connect a Cookie header callback."""


class DialogFinishedSignal(Protocol):
    """Minimal Qt dialog-finished signal surface used by orchestration."""

    def connect(self, slot: Callable[[int], None]) -> object:
        """Connect a dialog-finished callback."""


class LoginWindowBoundary(Protocol):
    """Login-window methods used by app orchestration."""

    cookie_header_captured: CookieHeaderSignal
    finished: DialogFinishedSignal

    def show_error(self, message: str) -> None:
        """Show a safe login error."""

    def clear_error(self) -> None:
        """Clear the login error."""

    def show(self) -> None:
        """Show the login window."""

    def close(self) -> bool:
        """Close the login window."""

    def raise_(self) -> None:
        """Raise the login window."""

    def activateWindow(self) -> None:
        """Activate the login window."""


def _noop_quit() -> None:
    """Default quit hook for controller tests."""


class ApplicationController:
    """Orchestrates login completion and file browser attachment."""

    def __init__(
        self,
        dependencies: AppDependencies,
        main_window: MainWindow,
        login_window_factory: Callable[[], LoginWindowBoundary],
        quit_application: Callable[[], None] = _noop_quit,
    ) -> None:
        self._dependencies = dependencies
        self._main_window = main_window
        self._login_window_factory = login_window_factory
        self._quit_application = quit_application
        self._login_window: LoginWindowBoundary | None = None
        self._main_window.login_required.connect(self.prompt_login)
        self._main_window.logout_requested.connect(self.logout)

    def start(self) -> None:
        """Start the app by restoring a persisted login or opening the login window."""
        LOGGER.info("controller.start")
        if not self._dependencies.settings.stay_logged_in:
            LOGGER.info("controller.restore.disabled_by_settings")
            self.prompt_login()
            return
        try:
            restored_login = self._dependencies.web_login_coordinator.restore_last_session()
        except Exception:
            LOGGER.info("controller.restore.failed")
            self.prompt_login("登录已过期，请重新登录")
            return

        if restored_login is None:
            LOGGER.info("controller.restore.unavailable")
            self.prompt_login()
            return

        try:
            file_browser = self._dependencies.file_browser_factory(
                restored_login.cookie_header,
                self._dependencies.settings,
            )
        except Exception:
            LOGGER.info("controller.restore.file_browser_failed")
            self.prompt_login("登录已过期，请重新登录")
            return

        self._main_window.set_auth_session(restored_login.session)
        self._main_window.set_file_browser(file_browser)
        self._main_window.show()
        LOGGER.info("controller.restore.success")

    def prompt_login(self, message: str = "") -> None:
        """Open or focus the official login window."""
        LOGGER.info("controller.prompt_login has_message=%s", bool(message))
        self._main_window.hide()
        login_window = self._login_window
        if login_window is None:
            login_window = self._login_window_factory()
            login_window.cookie_header_captured.connect(self.complete_login)
            login_window.finished.connect(self._on_login_window_finished)
            self._login_window = login_window

        if message:
            login_window.show_error(message)
        else:
            login_window.clear_error()
        login_window.show()
        login_window.raise_()
        login_window.activateWindow()

    def complete_login(self, cookie_header: str) -> None:
        """Validate a captured Cookie header and attach file browsing on success."""
        LOGGER.info("controller.complete_login.start")
        login_window = self._login_window
        try:
            login_result = WebLoginResult.from_cookie_header(cookie_header)
            session = self._dependencies.web_login_coordinator.complete(login_result)
            file_browser = self._dependencies.file_browser_factory(
                cookie_header,
                self._dependencies.settings,
            )
        except WebLoginError:
            LOGGER.info("controller.complete_login.web_login_error")
            if login_window is not None:
                login_window.show_error("登录失败，请重试")
            return
        except Exception:
            LOGGER.info("controller.complete_login.failed")
            if login_window is not None:
                login_window.show_error("登录失败，请重试")
            return

        self._main_window.set_auth_session(session)
        self._main_window.set_file_browser(file_browser)
        self._main_window.show()
        if login_window is not None:
            login_window.close()
        self._login_window = None
        LOGGER.info("controller.complete_login.success")

    def logout(self) -> None:
        """Clear the current persisted session and return to login."""
        session = self._main_window.auth_session()
        LOGGER.info("controller.logout.start has_session=%s", session is not None)
        if session is not None:
            try:
                self._dependencies.credential_store.delete_session_cookie(session.account_id)
                self._dependencies.credential_store.delete_last_account_id()
            except Exception:
                LOGGER.warning("controller.logout.credential_cleanup_failed")
        self._main_window.clear_auth_session()
        self.prompt_login()
        LOGGER.info("controller.logout.complete")

    def _on_login_window_finished(self, _result: int) -> None:
        LOGGER.info("controller.login_window.finished")
        login_window = self._login_window
        if login_window is None:
            return
        self._login_window = None
        if not self._main_window.isVisible():
            LOGGER.info("controller.login_window.closed_before_login")
            self._quit_application()
