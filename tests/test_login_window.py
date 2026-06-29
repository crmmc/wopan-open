from __future__ import annotations

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
    build_cookie_header,
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
