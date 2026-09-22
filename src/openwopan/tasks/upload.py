"""Pure local planning logic for folder uploads (no network access)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

JUNK_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})


@dataclass(frozen=True, slots=True)
class PlannedFile:
    """One local file planned for upload, before cloud name dedup."""

    local_path: Path
    rel_dir: str
    name: str
    size: int


@dataclass(frozen=True, slots=True)
class PlannedUploadFile:
    """One file with its deduped cloud name and target cloud directory."""

    local_path: Path
    target_dir_id: str
    name: str
    size: int


@dataclass(frozen=True, slots=True)
class FolderUploadPlan:
    """Local scan result for one folder upload."""

    root_name: str
    folders: tuple[str, ...]
    files: tuple[PlannedFile, ...]


@dataclass(frozen=True, slots=True)
class FolderUploadJob:
    """Ready-to-run folder upload produced after the directory tree exists."""

    root_item_id: str
    root_name: str
    files: tuple[PlannedUploadFile, ...]
    total_bytes: int


def next_available_name(requested: str, used: set[str]) -> str:
    """Return ``requested``, or ``stem (n)suffix`` past the first conflict."""
    if requested not in used:
        return requested
    path = Path(requested)
    suffix = 1
    while True:
        candidate = f"{path.stem} ({suffix}){path.suffix}"
        if candidate not in used:
            return candidate
        suffix += 1


def scan_folder_tree(local_root: Path) -> FolderUploadPlan:
    """Scan one local folder tree into an upload plan without network access."""
    folders: list[str] = []
    files: list[PlannedFile] = []
    _scan_directory(local_root, "", folders, files)
    return FolderUploadPlan(
        root_name=local_root.name,
        folders=tuple(folders),
        files=tuple(files),
    )


def _scan_directory(
    directory: Path,
    rel_dir: str,
    folders: list[str],
    files: list[PlannedFile],
) -> None:
    try:
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
    except OSError as exc:
        raise OSError(f"无法读取文件夹：{directory}") from exc
    for entry in entries:
        entry_path = Path(entry.path)
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                child_rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                folders.append(child_rel)
                _scan_directory(entry_path, child_rel, folders, files)
            elif entry.name not in JUNK_FILE_NAMES:
                files.append(
                    PlannedFile(
                        local_path=entry_path,
                        rel_dir=rel_dir,
                        name=entry.name,
                        size=entry.stat(follow_symlinks=False).st_size,
                    )
                )
        except OSError as exc:
            raise OSError(f"无法读取：{entry_path}") from exc
