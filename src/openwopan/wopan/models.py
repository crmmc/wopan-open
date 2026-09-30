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
class WopanRecycleItem:
    """Recycle-bin entry model owned by OpenWoPan.

    ``delete_no`` is the operation handle used by restore/purge requests;
    ``item_id`` only carries the original object id for display and tracing.
    """

    delete_no: str
    item_id: str
    name: str
    kind: WopanItemKind
    size: int | None = None
    deleted_at: datetime | None = None
    keep_days: int | None = None
    file_type: str | None = None

    def __post_init__(self) -> None:
        if not self.delete_no:
            raise ValueError("delete_no must not be empty")
        if not self.name:
            raise ValueError("name must not be empty")
        if self.size is not None and self.size < 0:
            raise ValueError("size must be non-negative")
        if self.keep_days is not None and self.keep_days < 0:
            raise ValueError("keep_days must be non-negative")


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
