from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platformdirs import user_config_path

APP_AUTHOR = "OpenWoPan"
APP_NAME = "OpenWoPan"
SETTINGS_FILE_NAME = "settings.json"
DEFAULT_LOG_LEVEL = "INFO"
SUPPORTED_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
DEFAULT_DOWNLOAD_PATH = Path.home() / "Downloads"
SUPPORTED_DOWNLOAD_PART_MODES = frozenset({"auto", "fixed"})


def _bounded_int(value: object, default: int, min_value: int, max_value: int) -> int:
    if not isinstance(value, int | str):
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(min_value, min(max_value, parsed))


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Non-sensitive local settings boundary."""

    last_account_id: str | None = None
    log_level: str = DEFAULT_LOG_LEVEL
    stay_logged_in: bool = True
    default_download_path: Path = DEFAULT_DOWNLOAD_PATH
    ask_download_location: bool = True
    max_download_threads: int = 1
    max_upload_threads: int = 16
    max_concurrent_downloads: int = 5
    max_concurrent_uploads: int = 3
    retry_max_attempts: int = 3
    upload_part_size_mb: int = 5
    download_part_size_mb: int = 5
    download_part_mode: str = "auto"

    def __post_init__(self) -> None:
        if self.last_account_id == "":
            raise ValueError("last_account_id must not be empty")
        if not isinstance(self.stay_logged_in, bool):
            raise ValueError("stay_logged_in must be a bool")
        if not isinstance(self.ask_download_location, bool):
            raise ValueError("ask_download_location must be a bool")
        if not isinstance(self.default_download_path, Path):
            raise ValueError("default_download_path must be a Path")
        normalized_level = self.log_level.upper()
        if normalized_level not in SUPPORTED_LOG_LEVELS:
            raise ValueError(f"log_level must be one of: {', '.join(sorted(SUPPORTED_LOG_LEVELS))}")
        object.__setattr__(self, "log_level", normalized_level)
        object.__setattr__(
            self,
            "max_download_threads",
            _bounded_int(self.max_download_threads, 1, 1, 16),
        )
        object.__setattr__(
            self,
            "max_upload_threads",
            _bounded_int(self.max_upload_threads, 16, 1, 16),
        )
        object.__setattr__(
            self,
            "max_concurrent_downloads",
            _bounded_int(self.max_concurrent_downloads, 5, 1, 5),
        )
        object.__setattr__(
            self,
            "max_concurrent_uploads",
            _bounded_int(self.max_concurrent_uploads, 3, 1, 5),
        )
        object.__setattr__(
            self,
            "retry_max_attempts",
            _bounded_int(self.retry_max_attempts, 3, 0, 5),
        )
        object.__setattr__(
            self,
            "upload_part_size_mb",
            _bounded_int(self.upload_part_size_mb, 5, 5, 16),
        )
        object.__setattr__(
            self,
            "download_part_size_mb",
            _bounded_int(self.download_part_size_mb, 5, 4, 32),
        )
        if self.download_part_mode not in SUPPORTED_DOWNLOAD_PART_MODES:
            object.__setattr__(self, "download_part_mode", "auto")


def app_settings_path() -> Path:
    """Return the user-editable settings file path."""
    return user_config_path(APP_NAME, APP_AUTHOR) / SETTINGS_FILE_NAME


def load_app_settings(path: Path | None = None) -> AppSettings:
    """Load user settings from JSON, returning defaults when no file exists."""
    settings_path = path or app_settings_path()
    if not settings_path.exists():
        return AppSettings()

    with settings_path.open("r", encoding="utf-8") as file:
        raw = json.load(file)
    if not isinstance(raw, dict):
        raise ValueError("settings file must contain a JSON object")
    return _read_app_settings(raw)


def ensure_app_settings_file(settings: AppSettings, path: Path | None = None) -> Path:
    """Create the user settings file when it does not exist."""
    settings_path = path or app_settings_path()
    if settings_path.exists():
        return settings_path
    return save_app_settings(settings, settings_path)


def save_app_settings(settings: AppSettings, path: Path | None = None) -> Path:
    """Persist non-sensitive user settings to JSON."""
    settings_path = path or app_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with settings_path.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "last_account_id": settings.last_account_id,
                "log_level": settings.log_level,
                "stay_logged_in": settings.stay_logged_in,
                "default_download_path": str(settings.default_download_path),
                "ask_download_location": settings.ask_download_location,
                "max_download_threads": settings.max_download_threads,
                "max_upload_threads": settings.max_upload_threads,
                "max_concurrent_downloads": settings.max_concurrent_downloads,
                "max_concurrent_uploads": settings.max_concurrent_uploads,
                "retry_max_attempts": settings.retry_max_attempts,
                "upload_part_size_mb": settings.upload_part_size_mb,
                "download_part_size_mb": settings.download_part_size_mb,
                "download_part_mode": settings.download_part_mode,
            },
            file,
            indent=2,
        )
        file.write("\n")
    return settings_path


def _read_app_settings(raw: dict[str, Any]) -> AppSettings:
    last_account_id = raw.get("last_account_id")
    if last_account_id is not None and not isinstance(last_account_id, str):
        raise ValueError("last_account_id must be a string")
    log_level = raw.get("log_level", DEFAULT_LOG_LEVEL)
    if not isinstance(log_level, str):
        raise ValueError("log_level must be a string")
    stay_logged_in = raw.get("stay_logged_in", True)
    if not isinstance(stay_logged_in, bool):
        raise ValueError("stay_logged_in must be a bool")
    default_download_path = raw.get("default_download_path", str(DEFAULT_DOWNLOAD_PATH))
    if not isinstance(default_download_path, str):
        raise ValueError("default_download_path must be a string")
    ask_download_location = raw.get("ask_download_location", True)
    if not isinstance(ask_download_location, bool):
        raise ValueError("ask_download_location must be a bool")
    return AppSettings(
        last_account_id=last_account_id,
        log_level=log_level,
        stay_logged_in=stay_logged_in,
        default_download_path=Path(default_download_path),
        ask_download_location=ask_download_location,
        max_download_threads=raw.get("max_download_threads", 1),
        max_upload_threads=raw.get("max_upload_threads", 16),
        max_concurrent_downloads=raw.get("max_concurrent_downloads", 5),
        max_concurrent_uploads=raw.get("max_concurrent_uploads", 3),
        retry_max_attempts=raw.get("retry_max_attempts", 3),
        upload_part_size_mb=raw.get("upload_part_size_mb", 5),
        download_part_size_mb=raw.get("download_part_size_mb", 5),
        download_part_mode=str(raw.get("download_part_mode", "auto")),
    )
