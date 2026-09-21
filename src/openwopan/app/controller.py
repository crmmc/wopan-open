from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol, cast

from PySide6.QtCore import QObject, QThread, Signal

from openwopan.app.bootstrap import AppDependencies
from openwopan.app.file_browser import FileBrowserBackend
from openwopan.auth.session import AuthSession
from openwopan.auth.web_login import RestoredWebLogin, WebLoginError, WebLoginResult
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


class ControllerOperationWorker(QObject):
    """Run one blocking controller operation outside the GUI thread."""

    succeeded = Signal(object)
    web_login_error = Signal()
    failed = Signal()

    def __init__(self, operation: Callable[[], object]) -> None:
        super().__init__()
        self._operation = operation

    def run(self) -> None:
        """Run the operation and emit exactly one terminal signal."""
        try:
            result = self._operation()
        except WebLoginError:
            self.web_login_error.emit()
        except Exception:
            LOGGER.exception("controller.operation.unexpected_error")
            self.failed.emit()
        else:
            self.succeeded.emit(result)


def _noop_quit() -> None:
    """Default quit hook for controller tests."""


class ApplicationController(QObject):
    """Orchestrates login completion and file browser attachment."""

    def __init__(
        self,
        dependencies: AppDependencies,
        main_window: MainWindow,
        login_window_factory: Callable[[], LoginWindowBoundary],
        quit_application: Callable[[], None] = _noop_quit,
    ) -> None:
        super().__init__()
        self._dependencies = dependencies
        self._main_window = main_window
        self._login_window_factory = login_window_factory
        self._quit_application = quit_application
        self._login_window: LoginWindowBoundary | None = None
        self._restore_thread: QThread | None = None
        self._restore_worker: ControllerOperationWorker | None = None
        self._login_thread: QThread | None = None
        self._login_worker: ControllerOperationWorker | None = None
        self._main_window.login_required.connect(self.prompt_login)
        self._main_window.logout_requested.connect(self.logout)

    def start(self) -> None:
        """Start the app by restoring a persisted login or opening the login window."""
        LOGGER.info("controller.start")
        if not self._dependencies.settings.stay_logged_in:
            LOGGER.info("controller.restore.disabled_by_settings")
            self.prompt_login()
            return
        if self._restore_thread is not None:
            LOGGER.debug("controller.restore.already_running")
            return

        worker = ControllerOperationWorker(self._restore_session)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_restore_succeeded)
        worker.web_login_error.connect(self._on_restore_failed)
        worker.failed.connect(self._on_restore_failed)
        worker.succeeded.connect(thread.quit)
        worker.web_login_error.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_restore_finished)
        self._restore_worker = worker
        self._restore_thread = thread
        thread.start()

    def _restore_session(self) -> object:
        try:
            restored_login = self._dependencies.web_login_coordinator.restore_last_session()
        except Exception:
            return "failed", None, None
        if restored_login is None:
            return "unavailable", None, None
        try:
            file_browser = self._dependencies.file_browser_factory(
                restored_login.cookie_header,
                self._dependencies.settings,
            )
        except Exception:
            return "file_browser_failed", None, None
        return "success", restored_login, file_browser

    def _on_restore_succeeded(self, result: object) -> None:
        outcome, restored_login, file_browser = cast(
            tuple[str, RestoredWebLogin | None, FileBrowserBackend | None], result
        )
        if outcome == "unavailable":
            LOGGER.info("controller.restore.unavailable")
            self.prompt_login()
            return
        if outcome == "file_browser_failed":
            LOGGER.info("controller.restore.file_browser_failed")
            self.prompt_login("登录已过期，请重新登录")
            return
        if outcome == "failed":
            self._on_restore_failed()
            return
        assert restored_login is not None
        assert file_browser is not None
        self._main_window.set_auth_session(restored_login.session)
        self._main_window.set_file_browser(file_browser)
        self._main_window.show()
        LOGGER.info("controller.restore.success")

    def _on_restore_failed(self) -> None:
        LOGGER.info("controller.restore.failed")
        self.prompt_login("登录已过期，请重新登录")

    def _on_restore_finished(self) -> None:
        self._restore_worker = None
        self._restore_thread = None

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
        if self._login_thread is not None:
            LOGGER.debug("controller.complete_login.already_running")
            return

        def complete() -> object:
            login_result = WebLoginResult.from_cookie_header(cookie_header)
            session = self._dependencies.web_login_coordinator.complete(login_result)
            file_browser = self._dependencies.file_browser_factory(
                cookie_header,
                self._dependencies.settings,
            )
            return session, file_browser

        worker = ControllerOperationWorker(complete)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_login_succeeded)
        worker.web_login_error.connect(self._on_login_web_login_error)
        worker.failed.connect(self._on_login_failed)
        worker.succeeded.connect(thread.quit)
        worker.web_login_error.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_login_finished)
        self._login_worker = worker
        self._login_thread = thread
        thread.start()

    def _on_login_succeeded(self, result: object) -> None:
        session, file_browser = cast(tuple[AuthSession, FileBrowserBackend], result)
        login_window = self._login_window
        self._main_window.set_auth_session(session)
        self._main_window.set_file_browser(file_browser)
        self._main_window.show()
        if login_window is not None:
            login_window.close()
        self._login_window = None
        LOGGER.info("controller.complete_login.success")

    def _on_login_web_login_error(self) -> None:
        LOGGER.info("controller.complete_login.web_login_error")
        if self._login_window is not None:
            self._login_window.show_error("登录失败，请重试")

    def _on_login_failed(self) -> None:
        LOGGER.info("controller.complete_login.failed")
        if self._login_window is not None:
            self._login_window.show_error("登录失败，请重试")

    def _on_login_finished(self) -> None:
        self._login_worker = None
        self._login_thread = None

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
