"""Pure local planning logic for folder uploads (no network access)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

JUNK_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
UploadConflictResolution = Literal["skip", "copy"]


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


@dataclass(frozen=True, slots=True)
class UploadSummaryEntry:
    """One bounded preview entry from a batch upload scan."""

    local_path: Path
    display_name: str
    kind: str
    size: int


@dataclass(frozen=True, slots=True)
class UploadBatchSummary:
    """Safe, bounded summary of local upload inputs."""

    top_paths: tuple[Path, ...]
    file_count: int
    folder_count: int
    total_bytes: int
    preview: tuple[UploadSummaryEntry, ...]
    omitted_count: int


@dataclass(frozen=True, slots=True)
class UploadTarget:
    """One top-level local upload with its optional resolved cloud name."""

    local_path: Path
    upload_name: str | None


def next_available_name(requested: str, used: set[str]) -> str:
    """Return ``requested``, or a repeatedly suffixed ``(copy)`` name."""
    if requested not in used:
        return requested
    path = Path(requested)
    candidate = f"{path.stem} (copy){path.suffix}"
    while candidate in used:
        candidate_path = Path(candidate)
        candidate = f"{candidate_path.stem} (copy){candidate_path.suffix}"
    return candidate


def find_upload_conflicts(
    paths: tuple[Path, ...], existing_names: set[str]
) -> tuple[Path, ...]:
    """Return top-level paths whose cloud names are already occupied."""
    used_names = set(existing_names)
    conflicts: list[Path] = []
    for path in paths:
        if path.name in used_names:
            conflicts.append(path)
        used_names.add(path.name)
    return tuple(conflicts)


def resolve_upload_targets(
    paths: tuple[Path, ...],
    existing_names: set[str],
    resolution: UploadConflictResolution,
) -> tuple[UploadTarget, ...]:
    """Resolve a batch by skipping conflicts or assigning unique copy names."""
    used_names = set(existing_names)
    targets: list[UploadTarget] = []
    for path in paths:
        requested_name = path.name
        if requested_name not in used_names:
            targets.append(UploadTarget(path, None))
            used_names.add(requested_name)
            continue
        if resolution == "skip":
            continue
        if resolution != "copy":
            raise ValueError(f"不支持的上传冲突策略：{resolution}")
        upload_name = next_available_name(requested_name, used_names)
        targets.append(UploadTarget(path, upload_name))
        used_names.add(upload_name)
    return tuple(targets)


def scan_folder_tree(local_root: Path) -> FolderUploadPlan:
    """Scan one local folder tree into an upload plan without network access."""
    if local_root.is_symlink():
        raise ValueError("不能上传符号链接文件夹")
    folders: list[str] = []
    files: list[PlannedFile] = []
    _scan_directory(local_root, "", folders, files)
    return FolderUploadPlan(
        root_name=local_root.name,
        folders=tuple(folders),
        files=tuple(files),
    )


def format_upload_summary(summary: UploadBatchSummary) -> str:
    """Format the non-sensitive counts shown before batch upload confirmation."""
    return (
        f"顶层项目 {len(summary.top_paths)} 个 | 文件 {summary.file_count} 个 | "
        f"文件夹 {summary.folder_count} 个 | 总大小 {summary.total_bytes} 字节"
    )


def scan_upload_inputs(
    paths: tuple[Path, ...], *, preview_limit: int = 20
) -> UploadBatchSummary:
    """Scan local files and folders into a bounded upload confirmation summary."""
    if preview_limit < 0:
        raise ValueError("preview_limit 不能为负数")
    unique_paths = tuple(dict.fromkeys(paths))
    accepted_paths: list[Path] = []
    entries: list[UploadSummaryEntry] = []
    entry_count = 0
    top_preview_count = 0
    file_count = 0
    folder_count = 0
    total_bytes = 0

    def add_preview(entry: UploadSummaryEntry, *, top_level: bool = False) -> None:
        nonlocal entry_count, top_preview_count
        entry_count += 1
        if top_level and top_preview_count < preview_limit:
            entries.insert(top_preview_count, entry)
            top_preview_count += 1
            if len(entries) > preview_limit:
                entries.pop()
        elif not top_level and len(entries) < preview_limit:
            entries.append(entry)

    for path in unique_paths:
        if path.is_symlink():
            continue
        if path.is_file():
            if path.name in JUNK_FILE_NAMES:
                continue
            accepted_paths.append(path)
            size = path.stat().st_size
            add_preview(UploadSummaryEntry(path, path.name, "文件", size), top_level=True)
            file_count += 1
            total_bytes += size
            continue
        if path.is_dir():
            accepted_paths.append(path)
            plan = scan_folder_tree(path)
            folder_count += 1 + len(plan.folders)
            add_preview(UploadSummaryEntry(path, path.name, "文件夹", 0), top_level=True)
            for folder in plan.folders:
                add_preview(UploadSummaryEntry(path / folder, f"{path.name}/{folder}", "文件夹", 0))
            for planned in plan.files:
                display_name = f"{path.name}/{planned.rel_dir}/{planned.name}".replace("//", "/")
                add_preview(
                    UploadSummaryEntry(planned.local_path, display_name, "文件", planned.size)
                )
            file_count += len(plan.files)
            total_bytes += sum(planned.size for planned in plan.files)
            continue
        raise OSError(f"无法读取上传路径：{path}")
    return UploadBatchSummary(
        top_paths=tuple(accepted_paths),
        file_count=file_count,
        folder_count=folder_count,
        total_bytes=total_bytes,
        preview=tuple(entries),
        omitted_count=max(0, entry_count - len(entries)),
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
