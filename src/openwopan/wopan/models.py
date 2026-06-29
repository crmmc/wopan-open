from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class WopanItemKind(StrEnum):
    """OpenWoPan-owned item kind names."""

    FILE = "file"
    FOLDER = "folder"


@dataclass(frozen=True, slots=True)
class WopanItem:
    """Internal file item model independent from upstream reference projects."""

    item_id: str
    name: str
    kind: WopanItemKind
    parent_id: str | None = None
    file_type: str | None = None
    download_id: str | None = None
    size: int | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("item_id must not be empty")
        if not self.name:
            raise ValueError("name must not be empty")
        if self.download_id == "":
            raise ValueError("download_id must not be empty")
        if self.size is not None and self.size < 0:
            raise ValueError("size must be non-negative")


@dataclass(frozen=True, slots=True)
class DownloadInfo:
    """Download metadata returned by the protocol layer."""

    url: str
    file_name: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class WopanCloudUsage:
    """Cloud storage usage summary owned by OpenWoPan."""

    used_bytes: int
    total_bytes: int
    vip_level: str | None = None
    expire_time: str | None = None

    def __post_init__(self) -> None:
        if self.used_bytes < 0:
            raise ValueError("used_bytes must be non-negative")
        if self.total_bytes <= 0:
            raise ValueError("total_bytes must be positive")
