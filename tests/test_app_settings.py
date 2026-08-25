from __future__ import annotations

import json
from pathlib import Path

import pytest

from openwopan.storage import settings as settings_module
from openwopan.storage.settings import (
    SETTINGS_FILE_NAME,
    AppSettings,
    app_settings_path,
    ensure_app_settings_file,
    load_app_settings,
    save_app_settings,
)


def test_app_settings_path_uses_config_dir() -> None:
    path = app_settings_path()

    assert path.name == SETTINGS_FILE_NAME


def test_load_app_settings_defaults_when_file_missing(tmp_path: Path) -> None:
    loaded = load_app_settings(tmp_path / "missing.json")

    assert loaded == AppSettings()


def test_load_app_settings_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    original = AppSettings(
        last_account_id="user-1",
        log_level="debug",
        stay_logged_in=False,
        default_download_path=tmp_path,
        ask_download_location=False,
        max_download_threads=8,
        retry_max_attempts=2,
        download_part_mode="fixed",
    )

    save_app_settings(original, path)
    loaded = load_app_settings(path)

    assert loaded == original


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("last_account_id", 7, "last_account_id must be a string"),
        ("log_level", 3, "log_level must be a string"),
        ("stay_logged_in", "yes", "stay_logged_in must be a bool"),
        ("default_download_path", 42, "default_download_path must be a string"),
        ("ask_download_location", "no", "ask_download_location must be a bool"),
    ],
)
def test_load_app_settings_rejects_invalid_types(
    tmp_path: Path, key: str, value: object, match: str
) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({key: value}), encoding="utf-8")

    with pytest.raises(ValueError, match=match):
        load_app_settings(path)


def test_load_app_settings_requires_json_object(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object"):
        load_app_settings(path)


def test_ensure_app_settings_file_keeps_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    saved = save_app_settings(AppSettings(log_level="WARNING"), path)

    ensured = ensure_app_settings_file(AppSettings(), path)

    assert ensured == path == saved
    assert load_app_settings(path).log_level == "WARNING"


def test_ensure_app_settings_file_creates_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "settings.json"

    ensured = ensure_app_settings_file(AppSettings(), path)

    assert ensured == path
    assert path.exists()


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        (None, 5, 5),
        (1.5, 5, 5),
        ("abc", 5, 5),
        ("3", 5, 3),
        (2, 5, 2),
    ],
)
def test_bounded_int_normalizes_values(value: object, default: int, expected: int) -> None:
    assert settings_module._bounded_int(value, default, 1, 5) == expected


def test_app_settings_validates_field_types() -> None:
    with pytest.raises(ValueError, match="ask_download_location"):
        AppSettings(ask_download_location="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="default_download_path"):
        AppSettings(default_download_path="/not/a/path")  # type: ignore[arg-type]
