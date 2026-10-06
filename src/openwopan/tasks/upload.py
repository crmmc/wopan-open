"""Pure local planning and persistence for uploads (no network access)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from platformdirs import user_cache_path

from openwopan.storage.settings import APP_AUTHOR, APP_NAME

LOGGER = logging.getLogger(__name__)

JUNK_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
UploadConflictResolution = Literal["skip", "copy", "merge"]
UPLOAD_SESSION_MAX_AGE_SECONDS = 24 * 3600


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
class MergeUploadEstimate:
    """Read-only estimate of what a merge upload would add.

    Same skip rules as the merge branch of folder preparation: same-name
    directories are reused, same-name same-size files count as uploaded.
    """

    files_to_upload: int
    files_skipped: int


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
    """Resolve a batch by skipping/copying conflicts, or merging into existing.

    ``merge`` keeps the original name for conflicts so folders can continue
    uploading into the existing cloud directory (``prepare_folder_upload``
    reuses directories and skips same-name files); conflicting top-level
    files are dropped by the caller, which knows path kinds.
    """
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
        if resolution == "merge":
            targets.append(UploadTarget(path, None))
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


# ---------------------------------------------------------------------------
# Upload session persistence (resumable uploads)
#
# Unlike downloads, uploaded parts are read directly from the source file and
# never exist as local half-part files: a part is either confirmed by the
# server (code "0000") or not, so only completed part indexes are persisted.
# ---------------------------------------------------------------------------


UploadTaskStatus = Literal["进行中", "失败", "已完成", "已暂停"]


@dataclass(frozen=True, slots=True)
class UploadPartRecord:
    """One server-confirmed uploaded part."""

    index: int


@dataclass(slots=True)
class UploadTaskState:
    """Persisted metadata for one resumable upload session."""

    task_id: str
    file_name: str
    local_path: Path
    parent_id: str
    upload_name: str | None
    file_size: int
    file_mtime: float
    part_size: int
    total_parts: int
    unique_id: str
    batch_no: str
    fid: str = ""
    error: str = ""
    status: UploadTaskStatus = "进行中"
    completed_indexes: list[int] = field(default_factory=list)
    version: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass(frozen=True, slots=True)
class UploadTaskRecord:
    """Persisted upload task summary surfaced to the transfer center."""

    task_id: str
    name: str
    local_path: Path
    target_parent_id: str
    status: str
    completed_parts: int
    total_parts: int
    file_size: int
    upload_name: str | None = None
    error: str = ""
    resumable: bool = False
    # True when the session was 进行中 when the previous run ended: the
    # restart recovery auto-continues exactly these (download parity).
    was_active: bool = False


def make_upload_task_id(parent_id: str, local_path: Path, upload_name: str | None) -> str:
    """Build a stable non-secret task id for one upload target."""
    raw = f"{parent_id}|{local_path.expanduser().resolve(strict=False)}|{upload_name or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class UploadTaskStore:
    """JSON-backed storage for resumable upload session metadata."""

    def __init__(self, root_path: Path | None = None) -> None:
        self._root_path = root_path or user_cache_path(APP_NAME, APP_AUTHOR) / "uploads"
        self._lock = threading.RLock()

    @property
    def root_path(self) -> Path:
        """Return the storage root for tests and diagnostics."""
        return self._root_path

    def task_path(self, task_id: str) -> Path:
        """Return one task metadata path."""
        return self._root_path / "tasks" / f"{task_id}.json"

    def load(self, task_id: str) -> UploadTaskState | None:
        """Load a persisted task state, tolerating corrupt metadata."""
        path = self.task_path(task_id)
        if not path.exists():
            return None
        with self._lock:
            try:
                with path.open("r", encoding="utf-8") as file:
                    raw = json.load(file)
            except (OSError, json.JSONDecodeError):
                LOGGER.warning("upload.task_state.invalid task_id=%s", task_id)
                return None
        if not isinstance(raw, dict):
            return None
        return _read_upload_state(raw)

    def load_all(self) -> list[UploadTaskState]:
        """Load all valid persisted task states in creation order."""
        tasks_path = self._root_path / "tasks"
        if not tasks_path.exists():
            return []
        with self._lock:
            paths = sorted(tasks_path.glob("*.json"))
        states: list[UploadTaskState] = []
        for path in paths:
            state = self.load(path.stem)
            if state is not None:
                states.append(state)
        states.sort(key=lambda state: (state.created_at, state.task_id))
        return states

    def save(self, state: UploadTaskState) -> None:
        """Persist task metadata atomically with normalized part indexes."""
        state.updated_at = time.time()
        state.completed_indexes = _normalize_completed_indexes(
            state.completed_indexes, state.total_parts
        )
        path = self.task_path(state.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _dump_upload_state(state)
        tmp_path = path.with_suffix(".json.tmp")
        with self._lock:
            with tmp_path.open("w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.write("\n")
            tmp_path.replace(path)

    def update(
        self, task_id: str, change: Callable[[UploadTaskState], None]
    ) -> UploadTaskState:
        """Apply a change against the latest persisted state under the store lock."""
        with self._lock:
            state = self.load(task_id)
            if state is None:
                raise KeyError(f"unknown upload task: {task_id}")
            change(state)
            self.save(state)
            return state

    def delete(self, task_id: str) -> None:
        """Delete one persisted task metadata file (best-effort cleanup).

        Called after uploads succeed or cancel: a Windows file-handle race on
        unlink must never flip an already-successful upload into a failure,
        so OSError is logged and swallowed.
        """
        with self._lock:
            try:
                self.task_path(task_id).unlink(missing_ok=True)
            except OSError as exc:
                LOGGER.warning(
                    "upload.task_state.delete_failed task_id=%s error_type=%s",
                    task_id,
                    type(exc).__name__,
                )


def _normalize_completed_indexes(indexes: list[int], total_parts: int) -> list[int]:
    """Dedup, sort, and clamp part indexes to the valid 1..total_parts range."""
    return sorted(
        {
            index
            for index in indexes
            if isinstance(index, int) and not isinstance(index, bool) and 1 <= index <= total_parts
        }
    )


def _dump_upload_state(state: UploadTaskState) -> dict[str, Any]:
    return {
        "version": state.version,
        "task_id": state.task_id,
        "file_name": state.file_name,
        "local_path": str(state.local_path),
        "parent_id": state.parent_id,
        "upload_name": state.upload_name,
        "file_size": state.file_size,
        "file_mtime": state.file_mtime,
        "part_size": state.part_size,
        "total_parts": state.total_parts,
        "unique_id": state.unique_id,
        "batch_no": state.batch_no,
        "fid": state.fid,
        "error": state.error,
        "status": state.status,
        "completed_indexes": sorted(set(state.completed_indexes)),
        "created_at": state.created_at,
        "updated_at": state.updated_at,
    }


def _read_upload_state(raw: dict[str, Any]) -> UploadTaskState | None:
    task_id = _read_text(raw.get("task_id"))
    file_name = _read_text(raw.get("file_name"))
    local_path_text = _read_text(raw.get("local_path"))
    parent_id = _read_text(raw.get("parent_id"))
    unique_id = _read_text(raw.get("unique_id"))
    batch_no = _read_text(raw.get("batch_no"))
    if not task_id or not file_name or not local_path_text:
        return None
    if not parent_id or not unique_id or not batch_no:
        return None
    total_parts = _read_positive_int(raw.get("total_parts"))
    part_size = _read_positive_int(raw.get("part_size"))
    completed_indexes = _normalize_completed_indexes(
        _read_index_list(raw.get("completed_indexes")), total_parts
    )
    return UploadTaskState(
        task_id=task_id,
        file_name=file_name,
        local_path=Path(local_path_text),
        parent_id=parent_id,
        upload_name=_read_optional_text(raw.get("upload_name")),
        file_size=_read_non_negative_int(raw.get("file_size")),
        file_mtime=_read_float(raw.get("file_mtime")),
        part_size=part_size,
        total_parts=total_parts,
        unique_id=unique_id,
        batch_no=batch_no,
        fid=_read_text(raw.get("fid")),
        error=_read_text(raw.get("error")),
        status=_read_upload_status(raw.get("status")),
        completed_indexes=completed_indexes,
        version=_read_non_negative_int(raw.get("version")) or 1,
        created_at=_read_float(raw.get("created_at")) or time.time(),
        updated_at=_read_float(raw.get("updated_at")) or time.time(),
    )


def _read_index_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    indexes: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            continue
        indexes.append(item)
    return indexes


def _read_upload_status(value: object) -> UploadTaskStatus:
    if value in {"进行中", "失败", "已完成", "已暂停"}:
        return value  # type: ignore[return-value]
    return "进行中"


def _read_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""


def _read_optional_text(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _read_non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _read_positive_int(value: object) -> int:
    parsed = _read_non_negative_int(value)
    return parsed if parsed > 0 else 1


def _read_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)
