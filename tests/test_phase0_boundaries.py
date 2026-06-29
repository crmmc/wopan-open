from __future__ import annotations

import json

import pytest

from openwopan.app.bootstrap import AppDependencies, build_dependencies
from openwopan.auth.session import AuthSession
from openwopan.auth.web_login import WebLoginCoordinator
from openwopan.storage.credentials import CredentialStore
from openwopan.storage.settings import (
    AppSettings,
    ensure_app_settings_file,
    load_app_settings,
    save_app_settings,
)
from openwopan.wopan.errors import WopanBusinessError, WopanError


def test_bootstrap_builds_minimal_dependencies() -> None:
    dependencies = build_dependencies()

    assert isinstance(dependencies, AppDependencies)
    assert isinstance(dependencies.credential_store, CredentialStore)


def test_bootstrap_builds_web_login_coordinator() -> None:
    dependencies = build_dependencies()

    assert isinstance(dependencies.web_login_coordinator, WebLoginCoordinator)


def test_auth_session_requires_account_id() -> None:
    with pytest.raises(ValueError, match="account_id must not be empty"):
        AuthSession(account_id="")


def test_auth_session_allows_display_name_and_token_preview() -> None:
    session = AuthSession(
        account_id="user-1",
        display_name="User One",
        token_preview="abcd...wxyz",
    )

    assert session.account_id == "user-1"
    assert session.display_name == "User One"
    assert session.token_preview == "abcd...wxyz"


def test_app_settings_rejects_empty_last_account_id() -> None:
    with pytest.raises(ValueError, match="last_account_id must not be empty"):
        AppSettings(last_account_id="")


def test_app_settings_normalizes_log_level() -> None:
    settings = AppSettings(log_level="debug")

    assert settings.log_level == "DEBUG"


def test_app_settings_rejects_invalid_log_level() -> None:
    with pytest.raises(ValueError, match="log_level"):
        AppSettings(log_level="verbose")


def test_app_settings_rejects_invalid_stay_logged_in() -> None:
    with pytest.raises(ValueError, match="stay_logged_in"):
        AppSettings(stay_logged_in="yes")  # type: ignore[arg-type]


def test_app_settings_loads_user_config_and_creates_default_file(tmp_path: object) -> None:
    settings_path = tmp_path / "settings.json"  # type: ignore[operator]
    settings_path.write_text(
        json.dumps({"log_level": "warning", "stay_logged_in": False}),
        encoding="utf-8",
    )

    settings = load_app_settings(settings_path)
    created_path = ensure_app_settings_file(settings, tmp_path / "new-settings.json")  # type: ignore[operator]

    assert settings.log_level == "WARNING"
    assert settings.stay_logged_in is False
    assert created_path.exists()
    created = json.loads(created_path.read_text(encoding="utf-8"))
    assert created["log_level"] == "WARNING"
    assert created["stay_logged_in"] is False
    assert created["ask_download_location"] is True
    assert created["max_upload_threads"] == 16


def test_app_settings_save_round_trips_user_config(tmp_path: object) -> None:
    settings_path = tmp_path / "settings.json"  # type: ignore[operator]

    save_app_settings(
        AppSettings(
            log_level="error",
            stay_logged_in=False,
            default_download_path=tmp_path / "downloads",  # type: ignore[operator]
            ask_download_location=False,
            max_download_threads=8,
            max_upload_threads=4,
            max_concurrent_downloads=2,
            max_concurrent_uploads=1,
            retry_max_attempts=5,
            upload_part_size_mb=8,
            download_part_size_mb=16,
            download_part_mode="fixed",
        ),
        settings_path,
    )
    settings = load_app_settings(settings_path)

    assert settings.log_level == "ERROR"
    assert settings.stay_logged_in is False
    assert settings.default_download_path == tmp_path / "downloads"  # type: ignore[operator]
    assert settings.ask_download_location is False
    assert settings.max_download_threads == 8
    assert settings.max_upload_threads == 4
    assert settings.max_concurrent_downloads == 2
    assert settings.max_concurrent_uploads == 1
    assert settings.retry_max_attempts == 5
    assert settings.upload_part_size_mb == 8
    assert settings.download_part_size_mb == 16
    assert settings.download_part_mode == "fixed"


def test_app_settings_clamps_transfer_numeric_values(tmp_path: object) -> None:
    settings_path = tmp_path / "settings.json"  # type: ignore[operator]
    settings_path.write_text(
        json.dumps(
            {
                "max_download_threads": "oops",
                "max_upload_threads": 99,
                "max_concurrent_downloads": -3,
                "max_concurrent_uploads": "bad",
                "retry_max_attempts": None,
                "upload_part_size_mb": 100,
                "download_part_size_mb": "NaN",
                "download_part_mode": "bad",
            }
        ),
        encoding="utf-8",
    )

    settings = load_app_settings(settings_path)

    assert settings.max_download_threads == 1
    assert settings.max_upload_threads == 16
    assert settings.max_concurrent_downloads == 1
    assert settings.max_concurrent_uploads == 3
    assert settings.retry_max_attempts == 3
    assert settings.upload_part_size_mb == 16
    assert settings.download_part_size_mb == 5
    assert settings.download_part_mode == "auto"


def test_wopan_business_error_preserves_code_and_message() -> None:
    error = WopanBusinessError(code="1001", message="login expired")

    assert isinstance(error, WopanError)
    assert error.code == "1001"
    assert error.message == "login expired"
    assert str(error) == "[1001] login expired"
