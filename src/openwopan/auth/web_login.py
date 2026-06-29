from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Protocol
from urllib.parse import unquote

from openwopan.auth.session import AuthSession
from openwopan.storage.credentials import CredentialStore

LOGGER = logging.getLogger(__name__)


class SessionValidator(Protocol):
    """Minimal protocol for objects that can validate a captured login."""

    def validate_session(self, token: str) -> ValidatedLoginUser:
        """Validate a captured token and return a user summary."""


class WebLoginError(Exception):
    """Base error for web-login orchestration failures."""


class WebLoginCancelledError(WebLoginError):
    """Raised when the user closes or cancels login before completion."""


class WebLoginTokenError(WebLoginError):
    """Raised when a login result does not contain the expected token."""


def extract_wocloud_token(cookie_header: str) -> str:
    """Extract and normalize the WoCloud web token from a Cookie header."""
    cookie = SimpleCookie()
    cookie.load(cookie_header)
    morsel = cookie.get("WoCloud-Web-Token")
    if morsel is None:
        raise WebLoginTokenError("WoCloud-Web-Token not found")
    token = unquote(morsel.value).strip()
    if len(token) >= 2 and token[0] == token[-1] == '"':
        token = token[1:-1]
    if not token:
        raise WebLoginTokenError("WoCloud-Web-Token is empty")
    return token


def build_token_preview(token: str) -> str:
    """Build a non-sensitive token preview for UI/app state."""
    if len(token) <= 12:
        return "***"
    return f"{token[:6]}...{token[-6:]}"


@dataclass(frozen=True, slots=True)
class WebLoginResult:
    """Result produced after a user completes official web login."""

    cookie_header: str = field(repr=False)
    token: str = field(repr=False)
    token_preview: str

    @classmethod
    def from_cookie_header(cls, cookie_header: str) -> WebLoginResult:
        """Create a login result from a captured Cookie header."""
        token = extract_wocloud_token(cookie_header)
        return cls(
            cookie_header=cookie_header,
            token=token,
            token_preview=build_token_preview(token),
        )


@dataclass(frozen=True, slots=True)
class ValidatedLoginUser:
    """Validated WoPan user summary returned by the protocol layer."""

    account_id: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class RestoredWebLogin:
    """Validated persisted login data used during application startup."""

    session: AuthSession
    cookie_header: str = field(repr=False)


class WebLoginCoordinator:
    """Coordinates web login validation and credential persistence."""

    def __init__(
        self,
        credential_store: CredentialStore,
        validator_factory: Callable[[str], SessionValidator],
    ) -> None:
        self._credential_store = credential_store
        self._validator_factory = validator_factory

    def complete(self, login_result: WebLoginResult) -> AuthSession:
        """Validate a web login result, persist credentials, and return a safe session."""
        LOGGER.info("web_login.complete.start")
        validator = self._validator_factory(login_result.cookie_header)
        user = validator.validate_session(login_result.token)
        session = AuthSession(
            account_id=user.account_id,
            display_name=user.display_name,
            token_preview=login_result.token_preview,
        )
        self._credential_store.save_session_cookie(session.account_id, login_result.cookie_header)
        self._credential_store.save_last_account_id(session.account_id)
        LOGGER.info("web_login.complete.success account_id=%s", session.account_id)
        return session

    def restore_last_session(self) -> RestoredWebLogin | None:
        """Validate and return the last persisted session, if one is available."""
        LOGGER.info("web_login.restore.start")
        account_id = self._credential_store.get_last_account_id()
        if account_id is None:
            LOGGER.info("web_login.restore.no_last_account")
            return None
        cookie_header = self._credential_store.get_session_cookie(account_id)
        if cookie_header is None:
            LOGGER.info("web_login.restore.no_cookie account_id=%s", account_id)
            return None

        login_result = WebLoginResult.from_cookie_header(cookie_header)
        validator = self._validator_factory(cookie_header)
        user = validator.validate_session(login_result.token)
        session = AuthSession(
            account_id=user.account_id,
            display_name=user.display_name,
            token_preview=login_result.token_preview,
        )
        if session.account_id != account_id:
            self._credential_store.save_session_cookie(session.account_id, cookie_header)
            self._credential_store.save_last_account_id(session.account_id)
        LOGGER.info("web_login.restore.success account_id=%s", session.account_id)
        return RestoredWebLogin(session=session, cookie_header=cookie_header)
