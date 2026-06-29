from __future__ import annotations

from dataclasses import dataclass

from openwopan.storage.credentials import CredentialStore


@dataclass
class FakeKeyring:
    values: dict[tuple[str, str], str]

    def set_password(self, service_name: str, key: str, value: str) -> None:
        self.values[(service_name, key)] = value

    def get_password(self, service_name: str, key: str) -> str | None:
        return self.values.get((service_name, key))

    def delete_password(self, service_name: str, key: str) -> None:
        self.values.pop((service_name, key), None)


def test_credential_store_persists_last_account_id(monkeypatch: object) -> None:
    fake_keyring = FakeKeyring(values={})
    monkeypatch.setattr("openwopan.storage.credentials.keyring", fake_keyring)  # type: ignore[attr-defined]
    store = CredentialStore(service_name="test-openwopan")

    store.save_session_cookie("user-1", "cookie-header")
    store.save_last_account_id("user-1")

    assert store.get_last_account_id() == "user-1"
    assert store.get_session_cookie("user-1") == "cookie-header"

    store.delete_session_cookie("user-1")
    store.delete_last_account_id()

    assert store.get_last_account_id() is None
    assert store.get_session_cookie("user-1") is None
