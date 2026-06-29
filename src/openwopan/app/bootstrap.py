from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from openwopan.app.file_browser import FileBrowserService, build_file_browser_service
from openwopan.auth.web_login import ValidatedLoginUser, WebLoginCoordinator
from openwopan.storage.credentials import CredentialStore
from openwopan.storage.settings import AppSettings
from openwopan.wopan.client import WopanClient


class _WopanSessionValidator:
    """Adapter from protocol-layer validation to auth-layer user summary."""

    def __init__(self, cookie_header: str) -> None:
        self._client = WopanClient(cookie_header)

    def validate_session(self, token: str) -> ValidatedLoginUser:
        user = self._client.validate_session(token)
        return ValidatedLoginUser(
            account_id=user.account_id,
            display_name=user.display_name,
        )


@dataclass(frozen=True, slots=True)
class AppDependencies:
    """Application-level dependency container."""

    credential_store: CredentialStore
    web_login_coordinator: WebLoginCoordinator
    file_browser_factory: Callable[[str, AppSettings | None], FileBrowserService]
    settings: AppSettings = AppSettings()


def build_dependencies() -> AppDependencies:
    """Build the minimal dependency graph for application startup."""
    credential_store = CredentialStore()
    return AppDependencies(
        credential_store=credential_store,
        web_login_coordinator=WebLoginCoordinator(credential_store, _WopanSessionValidator),
        file_browser_factory=build_file_browser_service,
    )
