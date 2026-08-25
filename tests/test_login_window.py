from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtWidgets import QApplication, QWidget

import openwopan.ui.login_window as login_window_module
from openwopan.ui.login_window import (
    DEFAULT_LOGIN_URL,
    LOGIN_WINDOW_DEFAULT_SIZE,
    LOGIN_WINDOW_MINIMUM_SIZE,
    LOGIN_WINDOW_TITLE,
    STANDARD_CHROME_USER_AGENT,
    TOKEN_COOKIE_NAME,
    CapturedCookie,
    LoginCookieCapture,
    LoginErrorState,
    LoginWindow,
    build_cookie_header,
    captured_cookie_from_qt,
)


def test_login_window_exposes_default_login_url() -> None:
    assert DEFAULT_LOGIN_URL == "https://pan.wo.cn/"


def test_login_window_exposes_design_constants() -> None:
    assert LOGIN_WINDOW_TITLE == "登录联通云盘"
    assert LOGIN_WINDOW_DEFAULT_SIZE == (960, 720)
    assert LOGIN_WINDOW_MINIMUM_SIZE == (800, 600)


def test_login_error_state_hides_empty_message() -> None:
    state = LoginErrorState.from_message("")

    assert state.message == ""
    assert state.is_visible is False


def test_login_error_state_strips_and_shows_safe_message() -> None:
    state = LoginErrorState.from_message("  登录失败，请重试  ")

    assert state.message == "登录失败，请重试"
    assert state.is_visible is True


def test_login_error_state_treats_whitespace_as_hidden() -> None:
    state = LoginErrorState.from_message("   ")

    assert state.message == ""
    assert state.is_visible is False


def test_standard_chrome_user_agent_is_not_electron() -> None:
    assert "Chrome/" in STANDARD_CHROME_USER_AGENT
    assert "Electron" not in STANDARD_CHROME_USER_AGENT


def test_build_cookie_header_preserves_token_cookie() -> None:
    cookie_header = build_cookie_header(
        [
            CapturedCookie(name="foo", value="bar"),
            CapturedCookie(name=TOKEN_COOKIE_NAME, value="%22token-value%22"),
        ]
    )

    assert cookie_header == f"foo=bar; {TOKEN_COOKIE_NAME}=%22token-value%22"


def test_login_cookie_capture_returns_header_when_token_cookie_arrives() -> None:
    capture = LoginCookieCapture()

    assert capture.add_cookie(CapturedCookie(name="foo", value="bar")) is None
    cookie_header = capture.add_cookie(CapturedCookie(name=TOKEN_COOKIE_NAME, value="token-value"))

    assert cookie_header == f"foo=bar; {TOKEN_COOKIE_NAME}=token-value"


def test_login_cookie_capture_ignores_empty_token_cookie() -> None:
    capture = LoginCookieCapture()

    cookie_header = capture.add_cookie(CapturedCookie(name=TOKEN_COOKIE_NAME, value=""))

    assert cookie_header is None
    assert capture.cookie_header() == ""


def test_login_cookie_capture_returns_token_header_only_once() -> None:
    capture = LoginCookieCapture()

    first_header = capture.add_cookie(CapturedCookie(name=TOKEN_COOKIE_NAME, value="token-one"))
    second_header = capture.add_cookie(CapturedCookie(name=TOKEN_COOKIE_NAME, value="token-two"))

    assert first_header == f"{TOKEN_COOKIE_NAME}=token-one"
    assert second_header is None
    assert capture.cookie_header() == f"{TOKEN_COOKIE_NAME}=token-one"


def test_login_cookie_capture_ignores_empty_token_after_completion() -> None:
    capture = LoginCookieCapture()

    first_header = capture.add_cookie(CapturedCookie(name=TOKEN_COOKIE_NAME, value="token-one"))
    empty_header = capture.add_cookie(CapturedCookie(name=TOKEN_COOKIE_NAME, value=""))

    assert first_header == f"{TOKEN_COOKIE_NAME}=token-one"
    assert empty_header is None
    assert capture.cookie_header() == f"{TOKEN_COOKIE_NAME}=token-one"


# ---------------------------------------------------------------------------
# Qt cookie conversion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "value", "expected_name", "expected_value"),
    [
        (b"foo", b"bar", "foo", "bar"),
        (b"", b"", "", ""),
    ],
)
def test_captured_cookie_from_qt_converts_names_and_values(
    name: bytes, value: bytes, expected_name: str, expected_value: str
) -> None:
    cookie = QNetworkCookie(name, value)

    captured = captured_cookie_from_qt(cookie)

    assert captured == CapturedCookie(name=expected_name, value=expected_value)


# ---------------------------------------------------------------------------
# LoginWindow with an isolated WebEngine boundary
# ---------------------------------------------------------------------------


class FakeCookieStore(QObject):
    cookieAdded = Signal(object)


class FakeProfile:
    def __init__(self, cookie_store: FakeCookieStore) -> None:
        self._cookie_store = cookie_store
        self.user_agent: str | None = None

    def setHttpUserAgent(self, agent: str) -> None:
        self.user_agent = agent

    def cookieStore(self) -> FakeCookieStore:
        return self._cookie_store


class FakeWebPage:
    def __init__(self, profile: FakeProfile) -> None:
        self._profile = profile

    def profile(self) -> FakeProfile:
        return self._profile


class FakeWebView(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._url = QUrl()
        self.cookie_store = FakeCookieStore()
        self.profile = FakeProfile(self.cookie_store)
        self._page = FakeWebPage(self.profile)

    def page(self) -> FakeWebPage:
        return self._page

    def setUrl(self, url: QUrl) -> None:
        self._url = url

    def url(self) -> QUrl:
        return self._url


@pytest.fixture
def fake_web_view(monkeypatch: pytest.MonkeyPatch) -> type[FakeWebView]:
    monkeypatch.setattr(login_window_module, "QWebEngineView", FakeWebView)
    return FakeWebView


def test_login_window_loads_default_url_and_sets_chrome_user_agent(
    qapp: QApplication, fake_web_view: type[FakeWebView]
) -> None:
    window = LoginWindow()

    assert window.current_url() == DEFAULT_LOGIN_URL
    view = window._web_view
    assert isinstance(view, FakeWebView)
    assert view.profile.user_agent == login_window_module.STANDARD_CHROME_USER_AGENT
    assert window.windowTitle() == login_window_module.LOGIN_WINDOW_TITLE


def test_login_window_loads_custom_url(
    qapp: QApplication, fake_web_view: type[FakeWebView]
) -> None:
    window = LoginWindow(login_url="https://example.test/login")

    assert window.current_url() == "https://example.test/login"

    window.load_url("https://example.test/next")
    assert window.current_url() == "https://example.test/next"


def test_login_window_show_and_clear_error(qapp: QApplication, fake_web_view) -> None:
    window = LoginWindow()

    window.show_error("  登录失败  ")
    assert window._error_state.message == "登录失败"
    assert window._error_label.text() == "登录失败"
    assert not window._error_label.isHidden()

    window.clear_error()
    assert window._error_state.message == ""
    assert window._error_label.text() == ""
    assert window._error_label.isHidden()


def test_login_window_emits_cookie_header_when_token_cookie_arrives(
    qapp: QApplication, fake_web_view
) -> None:
    captured: list[str] = []
    window = LoginWindow(login_url="https://example.test/")
    window.cookie_header_captured.connect(captured.append)
    view = window._web_view
    assert isinstance(view, FakeWebView)

    view.cookie_store.cookieAdded.emit(QNetworkCookie(b"foo", b"bar"))
    assert captured == []

    view.cookie_store.cookieAdded.emit(
        QNetworkCookie(TOKEN_COOKIE_NAME.encode(), b"token-value")
    )
    assert captured == [f"foo=bar; {TOKEN_COOKIE_NAME}=token-value"]

    view.cookie_store.cookieAdded.emit(
        QNetworkCookie(TOKEN_COOKIE_NAME.encode(), b"ignored")
    )
    assert captured == [f"foo=bar; {TOKEN_COOKIE_NAME}=token-value"]


def test_qt_byte_array_conversion_handles_memoryview() -> None:
    from openwopan.ui.login_window import _qt_byte_array_to_text

    class MemoryViewWrapper:
        def data(self) -> memoryview:
            return memoryview(b"token")

    assert _qt_byte_array_to_text(MemoryViewWrapper()) == "token"


@pytest.mark.parametrize(
    ("page_result", "profile_result", "cookie_store_result", "expected_message"),
    [
        (None, "profile", "store", "QWebEngineView page is unavailable"),
        ("page", None, "store", "QWebEngineProfile is unavailable"),
        ("page", "profile", None, "QWebEngineCookieStore is unavailable"),
    ],
)
def test_login_window_reports_unavailable_webengine_objects(
    qapp: QApplication,
    fake_web_view: type[FakeWebView],
    monkeypatch: pytest.MonkeyPatch,
    page_result: object,
    profile_result: object,
    cookie_store_result: object,
    expected_message: str,
) -> None:
    class BrokenPage(FakeWebPage):
        def profile(self) -> object:
            return profile_result

    class BrokenView(FakeWebView):
        def page(self) -> object:
            if page_result is None:
                return None
            return BrokenView._broken_page(self)

        @staticmethod
        def _broken_page(self):
            page = FakeWebPage(FakeProfile(FakeCookieStore()))
            if profile_result is None:

                class NoProfilePage(FakeWebPage):
                    def __init__(self) -> None:
                        pass

                    def profile(self) -> None:
                        return None

                return NoProfilePage()
            if cookie_store_result is None:

                class NoStoreProfile(FakeProfile):
                    def cookieStore(self) -> None:
                        return None

                return FakeWebPage(NoStoreProfile(FakeCookieStore()))
            return page

    monkeypatch.setattr(login_window_module, "QWebEngineView", BrokenView)

    with pytest.raises(RuntimeError, match=expected_message):
        LoginWindow()
