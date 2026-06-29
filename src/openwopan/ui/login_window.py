from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QUrl, Signal
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

DEFAULT_LOGIN_URL = "https://pan.wo.cn/"
LOGIN_WINDOW_TITLE = "登录联通云盘"
LOGIN_WINDOW_DEFAULT_SIZE = (960, 720)
LOGIN_WINDOW_MINIMUM_SIZE = (800, 600)
TOKEN_COOKIE_NAME = "WoCloud-Web-Token"
STANDARD_CHROME_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class CapturedCookie:
    """Browser cookie captured from the official login page."""

    name: str
    value: str


@dataclass(frozen=True, slots=True)
class LoginErrorState:
    """Safe UI state for the login error bar."""

    message: str = ""

    @classmethod
    def from_message(cls, message: str) -> LoginErrorState:
        """Create a normalized login error state from a safe message."""
        return cls(message=message.strip())

    @property
    def is_visible(self) -> bool:
        """Return whether the error bar should be visible."""
        return bool(self.message)


def build_cookie_header(cookies: Iterable[CapturedCookie]) -> str:
    """Build a Cookie header from captured browser cookies."""
    return "; ".join(
        f"{cookie.name}={cookie.value}"
        for cookie in cookies
        if cookie.name and cookie.value
    )


def captured_cookie_from_qt(cookie: QNetworkCookie) -> CapturedCookie:
    """Convert a Qt cookie object into the UI-independent cookie model."""
    return CapturedCookie(
        name=_qt_byte_array_to_text(cookie.name()),
        value=_qt_byte_array_to_text(cookie.value()),
    )


def _qt_byte_array_to_text(value: Any) -> str:
    data = value.data()
    if isinstance(data, memoryview):
        return data.tobytes().decode()
    return bytes(data).decode()


class LoginCookieCapture:
    """Collect login cookies until the WoCloud token cookie is available."""

    def __init__(self) -> None:
        self._cookies: dict[str, str] = {}
        self._completed = False

    def add_cookie(self, cookie: CapturedCookie) -> str | None:
        """Add a browser cookie and return a Cookie header once the token arrives."""
        if self._completed:
            return None

        if not cookie.name or not cookie.value:
            self._cookies.pop(cookie.name, None)
            return None

        self._cookies[cookie.name] = cookie.value
        if cookie.name == TOKEN_COOKIE_NAME:
            self._completed = True
            return self.cookie_header()
        return None

    def cookie_header(self) -> str:
        """Return the currently captured cookies as a Cookie header."""
        return build_cookie_header(
            CapturedCookie(name=name, value=value) for name, value in self._cookies.items()
        )


class LoginWindow(QDialog):
    """WebEngine login window for official WoPan authorization."""

    cookie_header_captured = Signal(str)

    def __init__(self, login_url: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(LOGIN_WINDOW_TITLE)
        self.resize(*LOGIN_WINDOW_DEFAULT_SIZE)
        self.setMinimumSize(*LOGIN_WINDOW_MINIMUM_SIZE)
        self._cookie_capture = LoginCookieCapture()
        self._error_state = LoginErrorState()

        self._web_view = QWebEngineView(self)
        page = self._web_view.page()
        if page is None:
            raise RuntimeError("QWebEngineView page is unavailable")
        profile = page.profile()
        if profile is None:
            raise RuntimeError("QWebEngineProfile is unavailable")
        profile.setHttpUserAgent(STANDARD_CHROME_USER_AGENT)
        cookie_store = profile.cookieStore()
        if cookie_store is None:
            raise RuntimeError("QWebEngineCookieStore is unavailable")
        cookie_store.cookieAdded.connect(self._on_cookie_added)

        header = QWidget(self)
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(20, 0, 12, 0)

        title = QLabel(LOGIN_WINDOW_TITLE, header)
        close_button = QPushButton("×", header)
        close_button.setFixedSize(32, 32)
        close_button.clicked.connect(self.close)

        header_layout.addWidget(title)
        header_layout.addStretch(1)
        header_layout.addWidget(close_button)
        header.setLayout(header_layout)
        header.setFixedHeight(56)

        self._error_label = QLabel("", self)
        self._error_label.setVisible(False)
        self._error_label.setWordWrap(True)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(self._web_view, 1)
        layout.addWidget(self._error_label)
        self.setLayout(layout)

        self.load_url(login_url or DEFAULT_LOGIN_URL)

    def load_url(self, login_url: str) -> None:
        """Load the official WoPan login page."""
        self._web_view.setUrl(QUrl(login_url))

    def current_url(self) -> str:
        """Return the currently loaded URL for orchestration and manual diagnostics."""
        return self._web_view.url().toString()

    def show_error(self, message: str) -> None:
        """Show a safe login error message."""
        self._apply_error_state(LoginErrorState.from_message(message))

    def clear_error(self) -> None:
        """Hide the login error message."""
        self._apply_error_state(LoginErrorState())

    def _apply_error_state(self, state: LoginErrorState) -> None:
        self._error_state = state
        self._error_label.setText(state.message)
        self._error_label.setVisible(state.is_visible)

    def _on_cookie_added(self, cookie: QNetworkCookie) -> None:
        cookie_header = self._cookie_capture.add_cookie(captured_cookie_from_qt(cookie))
        if cookie_header is not None:
            self.cookie_header_captured.emit(cookie_header)
