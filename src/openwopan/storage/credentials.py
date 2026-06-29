from __future__ import annotations

import keyring


class CredentialStore:
    """Small wrapper around the system credential store."""

    def __init__(self, service_name: str = "openwopan") -> None:
        self._service_name = service_name

    def save_session_cookie(self, account_id: str, cookie_header: str) -> None:
        """Persist a user session cookie header in the OS credential store."""
        keyring.set_password(self._service_name, self._session_key(account_id), cookie_header)

    def get_session_cookie(self, account_id: str) -> str | None:
        """Read a user session cookie header from the OS credential store."""
        return keyring.get_password(self._service_name, self._session_key(account_id))

    def delete_session_cookie(self, account_id: str) -> None:
        """Delete a user session cookie header from the OS credential store."""
        keyring.delete_password(self._service_name, self._session_key(account_id))

    def save_last_account_id(self, account_id: str) -> None:
        """Persist the account id used for startup session restore."""
        if not account_id:
            raise ValueError("account_id must not be empty")
        keyring.set_password(self._service_name, self._last_account_key(), account_id)

    def get_last_account_id(self) -> str | None:
        """Return the account id used for startup session restore."""
        account_id = keyring.get_password(self._service_name, self._last_account_key())
        if not account_id:
            return None
        return account_id

    def delete_last_account_id(self) -> None:
        """Delete the account id used for startup session restore."""
        keyring.delete_password(self._service_name, self._last_account_key())

    @staticmethod
    def _session_key(account_id: str) -> str:
        if not account_id:
            raise ValueError("account_id must not be empty")
        return f"wopan-session:{account_id}"

    @staticmethod
    def _last_account_key() -> str:
        return "wopan-session:last-account-id"
