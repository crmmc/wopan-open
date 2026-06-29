from __future__ import annotations

from dataclasses import dataclass

import pytest

from openwopan.auth.web_login import (
    RestoredWebLogin,
    ValidatedLoginUser,
    WebLoginCoordinator,
    WebLoginResult,
    WebLoginTokenError,
    build_token_preview,
    extract_wocloud_token,
)


def test_extract_wocloud_token_decodes_quoted_cookie_value() -> None:
    cookie_header = 'clientId=1001000021; WoCloud-Web-Token=%22abc-123%22; other=value'

    token = extract_wocloud_token(cookie_header)

    assert token == "abc-123"


def test_extract_wocloud_token_rejects_missing_token() -> None:
    with pytest.raises(WebLoginTokenError, match="WoCloud-Web-Token not found"):
        extract_wocloud_token("clientId=1001000021")


def test_build_token_preview_masks_full_token() -> None:
    token = "12345678-1234-1234-1234-123456789abc"

    preview = build_token_preview(token)

    assert preview == "123456...789abc"
    assert token not in preview


def test_web_login_result_exposes_token_preview_only() -> None:
    result = WebLoginResult.from_cookie_header(
        'WoCloud-Web-Token=%2212345678-1234-1234-1234-123456789abc%22'
    )

    assert result.token == "12345678-1234-1234-1234-123456789abc"
    assert result.token_preview == "123456...789abc"
    assert result.cookie_header.startswith("WoCloud-Web-Token=")


def test_web_login_result_repr_hides_cookie_and_token() -> None:
    token = "12345678-1234-1234-1234-123456789abc"
    result = WebLoginResult.from_cookie_header(f"WoCloud-Web-Token=%22{token}%22")

    result_repr = repr(result)

    assert token not in result_repr
    assert "WoCloud-Web-Token" not in result_repr
    assert result.token_preview in result_repr


@dataclass
class FakeCredentialStore:
    saved_account_id: str | None = None
    saved_cookie_header: str | None = None
    last_account_id: str | None = None
    cookies_by_account: dict[str, str] | None = None
    fail_save: bool = False

    def save_session_cookie(self, account_id: str, cookie_header: str) -> None:
        if self.fail_save:
            raise RuntimeError("keyring unavailable")
        self.saved_account_id = account_id
        self.saved_cookie_header = cookie_header
        if self.cookies_by_account is not None:
            self.cookies_by_account[account_id] = cookie_header

    def get_session_cookie(self, account_id: str) -> str | None:
        if self.cookies_by_account is None:
            return None
        return self.cookies_by_account.get(account_id)

    def save_last_account_id(self, account_id: str) -> None:
        self.last_account_id = account_id

    def get_last_account_id(self) -> str | None:
        return self.last_account_id


class FakeValidator:
    def __init__(self, user: ValidatedLoginUser | Exception) -> None:
        self.user = user
        self.seen_token: str | None = None

    def validate_session(self, token: str) -> ValidatedLoginUser:
        self.seen_token = token
        if isinstance(self.user, Exception):
            raise self.user
        return self.user


def test_web_login_coordinator_validates_saves_and_returns_session() -> None:
    store = FakeCredentialStore()
    validator = FakeValidator(ValidatedLoginUser(account_id="user-1", display_name="User One"))
    coordinator = WebLoginCoordinator(store, lambda _cookie: validator)
    result = WebLoginResult.from_cookie_header(
        'WoCloud-Web-Token=%2212345678-1234-1234-1234-123456789abc%22'
    )

    session = coordinator.complete(result)

    assert validator.seen_token == "12345678-1234-1234-1234-123456789abc"
    assert session.account_id == "user-1"
    assert session.display_name == "User One"
    assert session.token_preview == "123456...789abc"
    assert store.saved_account_id == "user-1"
    assert store.saved_cookie_header == result.cookie_header
    assert store.last_account_id == "user-1"


def test_web_login_coordinator_does_not_save_when_validation_fails() -> None:
    store = FakeCredentialStore()
    validator = FakeValidator(RuntimeError("invalid login"))
    coordinator = WebLoginCoordinator(store, lambda _cookie: validator)
    result = WebLoginResult.from_cookie_header('WoCloud-Web-Token=%22token-value%22')

    with pytest.raises(RuntimeError, match="invalid login"):
        coordinator.complete(result)

    assert store.saved_account_id is None
    assert store.saved_cookie_header is None


def test_web_login_coordinator_restores_last_session() -> None:
    cookie_header = 'WoCloud-Web-Token=%2212345678-1234-1234-1234-123456789abc%22'
    store = FakeCredentialStore(
        last_account_id="user-1",
        cookies_by_account={"user-1": cookie_header},
    )
    validator = FakeValidator(ValidatedLoginUser(account_id="user-1", display_name="User One"))
    coordinator = WebLoginCoordinator(store, lambda _cookie: validator)

    restored = coordinator.restore_last_session()

    assert isinstance(restored, RestoredWebLogin)
    assert restored.session.account_id == "user-1"
    assert restored.session.display_name == "User One"
    assert restored.session.token_preview == "123456...789abc"
    assert restored.cookie_header == cookie_header
    assert validator.seen_token == "12345678-1234-1234-1234-123456789abc"


def test_web_login_coordinator_returns_none_without_last_account() -> None:
    store = FakeCredentialStore()
    coordinator = WebLoginCoordinator(
        store,
        lambda _cookie: FakeValidator(ValidatedLoginUser(account_id="user-1")),
    )

    assert coordinator.restore_last_session() is None


def test_web_login_coordinator_does_not_return_session_when_save_fails() -> None:
    store = FakeCredentialStore(fail_save=True)
    validator = FakeValidator(ValidatedLoginUser(account_id="user-1"))
    coordinator = WebLoginCoordinator(store, lambda _cookie: validator)
    result = WebLoginResult.from_cookie_header('WoCloud-Web-Token=%22token-value%22')

    with pytest.raises(RuntimeError, match="keyring unavailable"):
        coordinator.complete(result)
