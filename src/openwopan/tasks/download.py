from __future__ import annotations

import errno
import hashlib
import json
import logging
import math
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import httpx
from platformdirs import user_cache_path

from openwopan.storage.settings import APP_AUTHOR, APP_NAME, AppSettings

LOGGER = logging.getLogger(__name__)

DOWNLOAD_CHUNK_SIZE = 1024 * 256
BYTES_PER_MB = 1024 * 1024
RATE_LIMIT_STATUS_CODES = frozenset({429, 503})
RATE_LIMIT_BACKOFF_SECONDS = 2.0
MAX_RATE_LIMITS = 50
MAX_URL_REFRESHES = 3
TASK_METADATA_VERSION = 1
DOWNLOAD_DB_NAME = "tasks.sqlite3"
DOWNLOAD_SHA256_FAILURE_MESSAGE = "文件校验失败，临时数据已清理，请重新下载"
# Whole-file SHA256 of an empty download; the zero-byte path compares against it.
EMPTY_CONTENT_SHA256 = hashlib.sha256(b"").hexdigest()

DownloadStatus = Literal[
    "等待中",
    "校验中",
    "下载中",
    "合并中",
    "已暂停",
    "已完成",
    "失败",
    "已取消",
]
PartResult = Literal["ok", "paused", "cancelled", "rate_limited", "url_expired", "fatal"]
ProgressCallback = Callable[[int, int | None], None]
StatusCallback = Callable[[DownloadStatus], None]
ConnectionCallback = Callable[[int, int], None]
RefreshUrlCallback = Callable[[], str]


class DownloadError(RuntimeError):
    """UI-facing download error without sensitive URL details."""


class RangeDownloadUnsupported(DownloadError):
    """Raised when the server does not honor byte range requests."""


@dataclass(frozen=True, slots=True)
class DownloadCallbacks:
    """Callbacks used by UI or tests to observe one download task."""

    progress: ProgressCallback | None = None
    status: StatusCallback | None = None
    connections: ConnectionCallback | None = None


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Result returned by a download execution."""

    status: DownloadStatus
    task_id: str
    local_path: Path


@dataclass(frozen=True, slots=True)
class DownloadTaskRecord:
    """Persisted download task summary for the transfer center."""

    task_id: str
    name: str
    target_path: Path
    status: DownloadStatus
    bytes_done: int = 0
    total_bytes: int | None = None
    supports_resume: bool = False
    error: str = ""
    active_connections: int = 0
    max_connections: int = 1


@dataclass(frozen=True, slots=True)
class DownloadPart:
    """Stable byte range in one download task."""

    index: int
    start: int
    end: int

    @property
    def expected_size(self) -> int:
        return self.end - self.start + 1


@dataclass(frozen=True, slots=True)
class DownloadPartRecord:
    """Persisted completed part metadata.

    ``mtime_ns`` snapshots the part file's ``st_mtime_ns`` at record time so
    resume validation can reuse an untouched file without re-reading it.
    ``algorithm`` names the digest the ``md5`` column carries (only ``md5``
    exists today; the column reserves room for a future switch).
    """

    index: int
    start: int
    end: int
    expected_size: int
    actual_size: int
    md5: str
    mtime_ns: int | None = None
    algorithm: str = "md5"


@dataclass(slots=True)
class DownloadTaskState:
    """Mutable metadata persisted for resumable downloads."""

    task_id: str
    file_name: str
    save_path: Path
    status: DownloadStatus = "等待中"
    download_id: str | None = None
    total_bytes: int | None = None
    bytes_done: int = 0
    part_size: int | None = None
    max_connections: int = 1
    supports_resume: bool = False
    error: str = ""
    version: int = TASK_METADATA_VERSION
    expected_sha256: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    parts: list[DownloadPartRecord] = field(default_factory=list)


class DownloadTaskControl:
    """Thread-safe control surface for one active download task."""

    def __init__(self) -> None:
        self._pause_requested = threading.Event()
        self._cancel_requested = threading.Event()
        self.cleanup_on_cancel = False
        self._response_lock = threading.Lock()
        self._active_response: httpx.Response | None = None

    def request_pause(self) -> None:
        """Ask the download task to pause at the next safe point."""
        self._pause_requested.set()
        self._close_active_response()

    def request_cancel(self, *, cleanup: bool = False) -> None:
        """Ask the download task to cancel at the next safe point."""
        self.cleanup_on_cancel = cleanup
        self._cancel_requested.set()
        self._close_active_response()

    def stop_result(self) -> PartResult | None:
        """Return the requested stop result, if any."""
        if self._cancel_requested.is_set():
            return "cancelled"
        if self._pause_requested.is_set():
            return "paused"
        return None

    def set_active_response(self, response: httpx.Response | None) -> None:
        """Track the active response so pause/cancel can unblock network reads."""
        with self._response_lock:
            self._active_response = response

    def _close_active_response(self) -> None:
        with self._response_lock:
            response = self._active_response
        if response is None:
            return
        try:
            response.close()
        except RuntimeError:
            LOGGER.debug("download.active_response_close_failed")


# -- SQLite schema (resume index only; part byte files stay on the filesystem) --

# Ordered, versioned, idempotent migrations applied against PRAGMA user_version.
# Future schema changes append a new (version, script) entry at the tail.
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS download_tasks (
            task_id TEXT PRIMARY KEY,
            file_name TEXT NOT NULL DEFAULT '',
            save_path TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '等待中',
            download_id TEXT,
            total_bytes INTEGER,
            bytes_done INTEGER NOT NULL DEFAULT 0,
            part_size INTEGER,
            max_connections INTEGER NOT NULL DEFAULT 1,
            supports_resume INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 1,
            expected_sha256 TEXT,
            created_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS download_parts (
            task_id TEXT NOT NULL,
            part_index INTEGER NOT NULL,
            start INTEGER NOT NULL,
            end_offset INTEGER NOT NULL,
            expected_size INTEGER NOT NULL,
            actual_size INTEGER NOT NULL,
            md5 TEXT NOT NULL DEFAULT '',
            algorithm TEXT NOT NULL DEFAULT 'md5',
            mtime_ns INTEGER,
            PRIMARY KEY (task_id, part_index),
            FOREIGN KEY (task_id) REFERENCES download_tasks(task_id) ON DELETE CASCADE
        );
        """,
    ),
)

# Plain literal statements: every value flows through the ? placeholders and
# the SQL text itself is static, so no runtime formatting ever enters it.
_SQL_UPSERT_TASK = """
INSERT INTO download_tasks (
    task_id, file_name, save_path, status, download_id, total_bytes,
    bytes_done, part_size, max_connections, supports_resume, error,
    version, expected_sha256, created_at, updated_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(task_id) DO UPDATE SET
    file_name = excluded.file_name,
    save_path = excluded.save_path,
    status = excluded.status,
    download_id = excluded.download_id,
    total_bytes = excluded.total_bytes,
    bytes_done = excluded.bytes_done,
    part_size = excluded.part_size,
    max_connections = excluded.max_connections,
    supports_resume = excluded.supports_resume,
    error = excluded.error,
    version = excluded.version,
    expected_sha256 = excluded.expected_sha256,
    created_at = excluded.created_at,
    updated_at = excluded.updated_at
"""
_SQL_UPSERT_PART = """
INSERT INTO download_parts (
    task_id, part_index, start, end_offset, expected_size,
    actual_size, md5, algorithm, mtime_ns
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(task_id, part_index) DO UPDATE SET
    start = excluded.start,
    end_offset = excluded.end_offset,
    expected_size = excluded.expected_size,
    actual_size = excluded.actual_size,
    md5 = excluded.md5,
    algorithm = excluded.algorithm,
    mtime_ns = excluded.mtime_ns
"""
_SQL_SELECT_TASK = """
SELECT task_id, file_name, save_path, status, download_id, total_bytes,
       bytes_done, part_size, max_connections, supports_resume, error,
       version, expected_sha256, created_at, updated_at
FROM download_tasks WHERE task_id = ?
"""
# load_all keeps the legacy JSON ordering (created_at, then task_id); the
# transfer-center listing keeps the legacy file-name ordering.
_SQL_SELECT_ALL_TASKS = """
SELECT task_id, file_name, save_path, status, download_id, total_bytes,
       bytes_done, part_size, max_connections, supports_resume, error,
       version, expected_sha256, created_at, updated_at
FROM download_tasks ORDER BY created_at ASC, task_id ASC
"""
_SQL_SELECT_ALL_TASKS_BY_ID = """
SELECT task_id, file_name, save_path, status, download_id, total_bytes,
       bytes_done, part_size, max_connections, supports_resume, error,
       version, expected_sha256, created_at, updated_at
FROM download_tasks ORDER BY task_id ASC
"""
_SQL_SELECT_PARTS = """
SELECT task_id, part_index, start, end_offset, expected_size,
       actual_size, md5, algorithm, mtime_ns
FROM download_parts WHERE task_id = ? ORDER BY part_index ASC
"""
_SQL_SELECT_ALL_PARTS = """
SELECT task_id, part_index, start, end_offset, expected_size,
       actual_size, md5, algorithm, mtime_ns
FROM download_parts ORDER BY task_id ASC, part_index ASC
"""
_SQL_SELECT_TASK_EXISTS = "SELECT 1 FROM download_tasks WHERE task_id = ?"
_SQL_DELETE_TASK = "DELETE FROM download_tasks WHERE task_id = ?"
_SQL_DELETE_PARTS_OF_TASK = "DELETE FROM download_parts WHERE task_id = ?"
_SQL_DELETE_PART_ROW = (
    "DELETE FROM download_parts WHERE task_id = ? AND part_index = ?"
)
# bytes_done is always derived from the part rows, never stored independently.
_SQL_REFRESH_BYTES_DONE = """
UPDATE download_tasks
SET bytes_done = (SELECT COALESCE(SUM(actual_size), 0) FROM download_parts WHERE task_id = ?),
    updated_at = ?
WHERE task_id = ?
"""


class DownloadTaskStore:
    """SQLite-backed storage for resumable download metadata and part files.

    Task metadata and part checkpoints live in ``tasks.sqlite3`` (an index
    only); the part byte files (``part{n}``, ``part{n}.downloading``, ``merged``)
    stay on the filesystem at the same paths as before. Legacy per-task JSON
    metadata is imported into the database on first open and then deleted.
    """

    def __init__(self, root_path: Path | None = None) -> None:
        self._root_path = root_path or user_cache_path(APP_NAME, APP_AUTHOR) / "downloads"
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None

    @property
    def root_path(self) -> Path:
        """Return the storage root for tests and diagnostics."""
        return self._root_path

    def task_path(self, task_id: str) -> Path:
        """Return the legacy per-task JSON metadata path (migration source only)."""
        return self._root_path / "tasks" / f"{task_id}.json"

    def task_temp_dir(self, task_id: str) -> Path:
        """Return one task temporary part directory."""
        return self._root_path / "parts" / task_id

    def part_path(self, task_id: str, index: int) -> Path:
        """Return one completed part file path."""
        return self.task_temp_dir(task_id) / f"part{index}"

    def part_downloading_path(self, task_id: str, index: int) -> Path:
        """Return one partial part file path."""
        return self.task_temp_dir(task_id) / f"part{index}.downloading"

    def merged_path(self, task_id: str) -> Path:
        """Return the temporary merged file path."""
        return self.task_temp_dir(task_id) / "merged"

    def open(self) -> None:
        """Connect, run migrations, and import legacy JSON metadata.

        Idempotent and lock-guarded; every public method opens the store on
        first use, so callers never need to manage the connection themselves.
        """
        self._require_connection()

    def close(self) -> None:
        """Commit and close the connection; safe to call repeatedly."""
        with self._lock:
            if self._connection is None:
                return
            connection, self._connection = self._connection, None
            connection.commit()
            connection.close()

    def load(self, task_id: str) -> DownloadTaskState | None:
        """Load a persisted task state with its part rows."""
        with self._lock:
            connection = self._require_connection()
            task_values = connection.execute(_SQL_SELECT_TASK, (task_id,)).fetchone()
            if task_values is None:
                return None
            task = _task_from_values(task_values)
            if task is None:
                LOGGER.warning(
                    "download.task_row.skipped task_id=%s columns=%d",
                    task_id,
                    len(task_values),
                )
                return None
            task.parts = self._load_parts(connection, task_id)
            return task

    def load_all(self) -> list[DownloadTaskState]:
        """Load all valid persisted task states in creation order."""
        with self._lock:
            connection = self._require_connection()
            task_values = connection.execute(_SQL_SELECT_ALL_TASKS).fetchall()
            part_values = connection.execute(_SQL_SELECT_ALL_PARTS).fetchall()
        parts_by_task: dict[str, list[DownloadPartRecord]] = {}
        for values in part_values:
            row = _part_row_from_values(values)
            if row is None:
                LOGGER.warning(
                    "download.part_row.skipped columns=%d task_id=%r",
                    len(values),
                    values[0] if values else None,
                )
                continue
            parts_by_task.setdefault(row[0], []).append(row[1])
        states: list[DownloadTaskState] = []
        for values in task_values:
            task = _task_from_values(values)
            if task is None:
                LOGGER.warning(
                    "download.task_row.skipped columns=%d task_id=%r",
                    len(values),
                    values[0] if values else None,
                )
                continue
            task.parts = parts_by_task.get(task.task_id, [])
            states.append(task)
        return states

    def save(self, state: DownloadTaskState) -> None:
        """Persist the whole task state in one transaction (metadata + parts)."""
        state.updated_at = time.time()
        with self._lock:
            connection = self._require_connection()
            try:
                connection.execute(_SQL_UPSERT_TASK, _task_parameters(state))
                connection.execute(_SQL_DELETE_PARTS_OF_TASK, (state.task_id,))
                connection.executemany(
                    _SQL_UPSERT_PART,
                    [_part_parameters(record, state.task_id) for record in state.parts],
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def update(
        self, task_id: str, change: Callable[[DownloadTaskState], None]
    ) -> DownloadTaskState:
        """Apply a scheduler state change without overwriting concurrent part writes."""
        with self._lock:
            state = self.load(task_id)
            if state is None:
                raise KeyError(f"unknown download task: {task_id}")
            change(state)
            self.save(state)
            return state

    def delete(self, task_id: str) -> None:
        """Delete task metadata rows and temporary files."""
        with self._lock:
            connection = self._require_connection()
            try:
                connection.execute(_SQL_DELETE_PARTS_OF_TASK, (task_id,))
                connection.execute(_SQL_DELETE_TASK, (task_id,))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            shutil.rmtree(self.task_temp_dir(task_id), ignore_errors=True)

    def cleanup_temp(self, task_id: str) -> None:
        """Delete temporary part files while keeping task metadata."""
        shutil.rmtree(self.task_temp_dir(task_id), ignore_errors=True)

    def list_records(self) -> tuple[DownloadTaskRecord, ...]:
        """Return persisted transfer-center records in task-id order."""
        with self._lock:
            connection = self._require_connection()
            task_values = connection.execute(_SQL_SELECT_ALL_TASKS_BY_ID).fetchall()
        records: list[DownloadTaskRecord] = []
        for values in task_values:
            task = _task_from_values(values)
            if task is None:
                LOGGER.warning(
                    "download.task_row.skipped columns=%d task_id=%r",
                    len(values),
                    values[0] if values else None,
                )
                continue
            records.append(_state_to_record(task))
        return tuple(records)

    def record_part(self, task_id: str, record: DownloadPartRecord) -> None:
        """Upsert one part row and re-derive ``bytes_done`` in the same transaction."""
        with self._lock:
            connection = self._require_connection()
            exists = connection.execute(_SQL_SELECT_TASK_EXISTS, (task_id,)).fetchone()
            if exists is None:
                return
            try:
                connection.execute(_SQL_UPSERT_PART, _part_parameters(record, task_id))
                connection.execute(
                    _SQL_REFRESH_BYTES_DONE, (task_id, time.time(), task_id)
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def remove_part_record(self, task_id: str, index: int) -> None:
        """Delete one part row, refresh ``bytes_done``, and drop its files."""
        with self._lock:
            connection = self._require_connection()
            try:
                connection.execute(_SQL_DELETE_PART_ROW, (task_id, index))
                connection.execute(
                    _SQL_REFRESH_BYTES_DONE, (task_id, time.time(), task_id)
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            self.part_path(task_id, index).unlink(missing_ok=True)
            self.part_downloading_path(task_id, index).unlink(missing_ok=True)

    def _require_connection(self) -> sqlite3.Connection:
        """Return the live connection, creating and migrating it on first use."""
        with self._lock:
            if self._connection is not None:
                return self._connection
            self._root_path.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self._root_path / DOWNLOAD_DB_NAME, check_same_thread=False
            )
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                self._apply_migrations(connection)
                connection.commit()
            except BaseException:
                connection.close()
                raise
            self._connection = connection
            self._migrate_json_tasks(connection)
            return connection

    def _load_parts(
        self, connection: sqlite3.Connection, task_id: str
    ) -> list[DownloadPartRecord]:
        parts: list[DownloadPartRecord] = []
        for values in connection.execute(_SQL_SELECT_PARTS, (task_id,)).fetchall():
            row = _part_row_from_values(values)
            if row is None:
                LOGGER.warning(
                    "download.part_row.skipped columns=%d task_id=%r",
                    len(values),
                    values[0] if values else None,
                )
                continue
            parts.append(row[1])
        return parts

    def _apply_migrations(self, connection: sqlite3.Connection) -> None:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        for version, script in MIGRATIONS:
            if version <= current:
                continue
            connection.executescript(script)
            # PRAGMA values cannot be parameterized; the version is an internal
            # constant, not external input.
            connection.execute(  # nosemgrep: formatted-sql-query, sqlalchemy-execute-raw-query
                f"PRAGMA user_version = {int(version)}"
            )

    def _migrate_json_tasks(self, connection: sqlite3.Connection) -> None:
        """Import legacy per-task JSON metadata once; keep unparsable files.

        Idempotent by primary key (INSERT OR REPLACE): a crash mid-migration
        replays safely on the next open, and already-imported JSON files are
        deleted so they are never imported twice.
        """
        tasks_dir = self._root_path / "tasks"
        if not tasks_dir.is_dir():
            return
        for path in sorted(tasks_dir.glob("*.json")):
            raw = _read_json_metadata_file(path)
            state = _read_task_state(raw) if raw is not None else None
            if state is None:
                LOGGER.warning(
                    "download.task_state.migrate_invalid file=%s", path.name
                )
                continue
            try:
                connection.execute(_SQL_UPSERT_TASK, _task_parameters(state))
                connection.execute(_SQL_DELETE_PARTS_OF_TASK, (state.task_id,))
                connection.executemany(
                    _SQL_UPSERT_PART,
                    [_part_parameters(record, state.task_id) for record in state.parts],
                )
                connection.commit()
            except sqlite3.Error:
                LOGGER.exception(
                    "download.task_state.migrate_failed file=%s", path.name
                )
                continue
            path.unlink(missing_ok=True)


def _read_json_metadata_file(path: Path) -> dict[str, Any] | None:
    """Read one legacy JSON metadata file; None when unreadable or not an object."""
    try:
        with path.open("r", encoding="utf-8") as file:
            raw = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _task_parameters(state: DownloadTaskState) -> tuple[object, ...]:
    """Bind one task row's values; the SQL text itself is a static literal."""
    return (
        state.task_id,
        state.file_name,
        str(state.save_path),
        state.status,
        state.download_id,
        state.total_bytes,
        state.bytes_done,
        state.part_size,
        state.max_connections,
        int(state.supports_resume),
        state.error,
        state.version,
        state.expected_sha256,
        state.created_at,
        state.updated_at,
    )


def _part_parameters(record: DownloadPartRecord, task_id: str) -> tuple[object, ...]:
    """Bind one part row's values; the SQL text itself is a static literal."""
    return (
        task_id,
        record.index,
        record.start,
        record.end,
        record.expected_size,
        record.actual_size,
        record.md5,
        record.algorithm,
        record.mtime_ns,
    )


def _task_from_values(values: tuple[object, ...]) -> DownloadTaskState | None:
    """Build a typed task state from one database tuple; None when invalid."""
    if len(values) != 15:
        return None
    (
        task_id,
        file_name,
        save_path,
        status,
        download_id,
        total_bytes,
        bytes_done,
        part_size,
        max_connections,
        supports_resume,
        error,
        version,
        expected_sha256,
        created_at,
        updated_at,
    ) = values
    if not isinstance(task_id, str) or not task_id:
        return None
    if not isinstance(file_name, str) or not file_name:
        return None
    if not isinstance(save_path, str) or not save_path:
        return None
    return DownloadTaskState(
        task_id=task_id,
        file_name=file_name,
        save_path=Path(save_path),
        status=_read_status(status),
        download_id=_read_text(download_id) or None,
        total_bytes=_read_optional_non_negative_int(total_bytes),
        bytes_done=_read_non_negative_int(bytes_done),
        part_size=_read_optional_positive_int(part_size),
        max_connections=max(1, _read_non_negative_int(max_connections)),
        supports_resume=bool(_read_non_negative_int(supports_resume)),
        error=_read_text(error),
        version=_read_non_negative_int(version) or TASK_METADATA_VERSION,
        expected_sha256=_read_text(expected_sha256) or None,
        created_at=_row_float(created_at),
        updated_at=_row_float(updated_at),
    )


def _part_row_from_values(
    values: tuple[object, ...],
) -> tuple[str, DownloadPartRecord] | None:
    """Build one (task_id, part record) pair; None when the row is invalid."""
    if len(values) != 9:
        return None
    (
        task_id,
        part_index,
        start,
        end,
        expected_size,
        actual_size,
        md5,
        algorithm,
        mtime_ns,
    ) = values
    if not isinstance(task_id, str) or not task_id:
        return None
    index_value = _row_int(part_index)
    start_value = _row_int(start)
    end_value = _row_int(end)
    expected_value = _row_int(expected_size)
    actual_value = _row_int(actual_size)
    if (
        index_value is None
        or start_value is None
        or end_value is None
        or expected_value is None
        or actual_value is None
    ):
        return None
    if index_value < 0 or start_value < 0:
        return None
    if end_value < start_value or expected_value <= 0:
        return None
    if not 0 < actual_value <= expected_value:
        return None
    if not isinstance(md5, str) or not md5:
        return None
    return (
        task_id,
        DownloadPartRecord(
            index=index_value,
            start=start_value,
            end=end_value,
            expected_size=expected_value,
            actual_size=actual_value,
            md5=md5,
            mtime_ns=_row_int(mtime_ns),
            algorithm=_read_text(algorithm) or "md5",
        ),
    )


def _row_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _row_float(value: object) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return 0.0


def make_download_task_id(download_id: str, local_path: Path) -> str:
    """Build a stable non-secret task id from file id and local path."""
    raw = f"{download_id}|{local_path.expanduser().resolve(strict=False)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def download_url(
    http_client: httpx.Client,
    url: str,
    local_path: Path,
    *,
    settings: AppSettings,
    store: DownloadTaskStore,
    task_id: str,
    file_name: str,
    download_id: str | None = None,
    expected_sha256: str | None = None,
    refresh_url: RefreshUrlCallback | None = None,
    callbacks: DownloadCallbacks | None = None,
    control: DownloadTaskControl | None = None,
) -> DownloadResult:
    """Download one URL through the unified resumable Range executor.

    Every download plans byte-range parts from the probed file size and runs
    them through the same Range executor; ``max_download_threads=1`` is only a
    single-worker configuration. There is no single-stream fallback: when the
    file size cannot be probed or the server ignores Range requests, the task
    ends with an explicit download error (R7).
    """
    if not url:
        raise DownloadError("下载地址为空")
    callbacks = callbacks or DownloadCallbacks()
    control = control or DownloadTaskControl()

    state = store.load(task_id) or DownloadTaskState(
        task_id=task_id,
        file_name=file_name,
        save_path=local_path,
        download_id=download_id,
    )
    state.file_name = file_name
    state.save_path = local_path
    state.download_id = download_id or state.download_id
    state.expected_sha256 = expected_sha256 or state.expected_sha256
    store.save(state)

    total_size = _probe_download_size(http_client, url)
    if total_size is None:
        _mark_failed(store, state, callbacks, "无法获取文件大小，无法进行分片下载")
        raise DownloadError("无法获取文件大小，无法进行分片下载")
    if total_size == 0:
        return _complete_zero_byte_download(store, state, local_path, callbacks, control)
    part_size = state.part_size or _download_part_size(total_size, settings)

    try:
        return _download_with_ranges(
            http_client,
            url,
            local_path,
            total_size=total_size,
            part_size=part_size,
            settings=settings,
            store=store,
            state=state,
            refresh_url=refresh_url,
            callbacks=callbacks,
            control=control,
        )
    except RangeDownloadUnsupported as exc:
        LOGGER.info("download.range_unsupported task_id=%s", task_id)
        latest = store.load(task_id) or state
        latest.parts = []
        latest.bytes_done = 0
        latest.supports_resume = False
        latest.part_size = None
        store.save(latest)
        store.cleanup_temp(task_id)
        _mark_failed(store, latest, callbacks, "服务器不支持断点续传下载")
        raise DownloadError("服务器不支持断点续传下载") from exc


def _complete_zero_byte_download(
    store: DownloadTaskStore,
    state: DownloadTaskState,
    local_path: Path,
    callbacks: DownloadCallbacks,
    control: DownloadTaskControl,
) -> DownloadResult:
    latest = store.load(state.task_id) or state
    latest.total_bytes = 0
    latest.bytes_done = 0
    latest.part_size = None
    latest.max_connections = 1
    latest.supports_resume = False
    latest.error = ""
    latest.parts = []
    latest.status = "下载中"
    store.cleanup_temp(latest.task_id)
    store.save(latest)
    _emit_progress(callbacks, 0, 0)
    _emit_status(callbacks, "下载中")

    stop_result = control.stop_result()
    if stop_result in ("paused", "cancelled"):
        return _stop_range_download(stop_result, store, latest, callbacks, control)

    # Same whole-file integrity gate as the merge path: part digests are
    # trivially correct for zero bytes, so the upstream hash is the only
    # defense. Checked before the temporary file is created — a mismatched
    # expected hash can never produce a publishable output.
    expected_sha256 = latest.expected_sha256
    if expected_sha256 is not None and expected_sha256.lower() != EMPTY_CONTENT_SHA256:
        _fail_sha256_verification(store, latest, callbacks)
        raise DownloadError(DOWNLOAD_SHA256_FAILURE_MESSAGE)

    empty_path = store.merged_path(latest.task_id)
    try:
        empty_path.parent.mkdir(parents=True, exist_ok=True)
        empty_path.touch()
        stop_result = control.stop_result()
        if stop_result in ("paused", "cancelled"):
            return _stop_range_download(stop_result, store, latest, callbacks, control)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        _replace_output_file(empty_path, local_path)
    except DownloadError as exc:
        store.cleanup_temp(latest.task_id)
        _mark_failed(store, latest, callbacks, str(exc))
        raise
    except OSError as exc:
        store.cleanup_temp(latest.task_id)
        _mark_failed(store, latest, callbacks, "创建空下载文件失败")
        raise DownloadError("创建空下载文件失败") from exc

    latest.status = "已完成"
    store.save(latest)
    store.cleanup_temp(latest.task_id)
    _emit_status(callbacks, "已完成")
    _emit_connections(callbacks, 0, latest.max_connections)
    return DownloadResult(status="已完成", task_id=latest.task_id, local_path=local_path)


def _download_with_ranges(
    http_client: httpx.Client,
    url: str,
    local_path: Path,
    *,
    total_size: int,
    part_size: int,
    settings: AppSettings,
    store: DownloadTaskStore,
    state: DownloadTaskState,
    refresh_url: RefreshUrlCallback | None,
    callbacks: DownloadCallbacks,
    control: DownloadTaskControl,
) -> DownloadResult:
    max_workers = min(max(settings.max_download_threads, 1), 16)
    if state.total_bytes is not None and state.total_bytes != total_size:
        state.parts = []
        state.bytes_done = 0
        store.cleanup_temp(state.task_id)
    state.total_bytes = total_size
    state.part_size = part_size
    state.max_connections = max_workers
    state.supports_resume = True
    state.status = "校验中"
    state.error = ""
    _emit_status(callbacks, "校验中")
    store.save(state)

    parts = _build_parts(total_size, part_size)
    _clear_parts_if_plan_changed(store, state, parts)
    reused_bytes, reusable_indexes = _validate_existing_parts(store, state, parts)
    state = store.load(state.task_id) or state
    state.bytes_done = reused_bytes
    state.status = "下载中"
    store.save(state)
    _emit_progress(callbacks, reused_bytes, total_size)
    _emit_status(callbacks, "下载中")

    reusable = set(reusable_indexes)
    pending = [part for part in parts if part.index not in reusable]
    current_url = url
    allowed_workers = 1
    rate_limit_count = 0
    url_refresh_count = 0
    records_by_index = {record.index: record for record in state.parts}
    part_progress = {
        part.index: (
            part.expected_size
            if part.index in reusable
            else records_by_index[part.index].actual_size
        )
        for part in parts
        if part.index in reusable or part.index in records_by_index
    }
    progress_lock = threading.Lock()

    def report_part_progress(index: int, value: int) -> None:
        with progress_lock:
            part_progress[index] = value
            _emit_progress(callbacks, sum(part_progress.values()), total_size)

    while pending:
        stop_result = control.stop_result()
        if stop_result in ("paused", "cancelled"):
            return _stop_range_download(stop_result, store, state, callbacks, control)

        batch = pending[:allowed_workers]
        pending = pending[allowed_workers:]
        _emit_connections(callbacks, len(batch), max_workers)
        results: list[tuple[DownloadPart, PartResult]] = []
        with ThreadPoolExecutor(max_workers=len(batch)) as executor:
            futures = [
                executor.submit(
                    _download_range_part,
                    http_client,
                    current_url,
                    store,
                    state.task_id,
                    part,
                    settings.retry_max_attempts,
                    callbacks,
                    control,
                    report_part_progress,
                )
                for part in batch
            ]
            for part, future in zip(batch, futures, strict=True):
                try:
                    results.append((part, future.result()))
                except RangeDownloadUnsupported:
                    raise
        _emit_connections(callbacks, 0, max_workers)
        stop_result = control.stop_result()
        if stop_result in ("paused", "cancelled"):
            return _stop_range_download(stop_result, store, state, callbacks, control)

        for part, result in results:
            if result == "ok":
                allowed_workers = min(max_workers, allowed_workers + 1)
                url_refresh_count = 0
                continue
            if result in ("paused", "cancelled"):
                return _stop_range_download(result, store, state, callbacks, control)
            report_part_progress(part.index, 0)
            if result == "rate_limited":
                rate_limit_count += 1
                if rate_limit_count > MAX_RATE_LIMITS:
                    _mark_failed(store, state, callbacks, "分片下载被限流")
                    raise DownloadError("分片下载被限流，请稍后重试")
                allowed_workers = max(1, allowed_workers - 1)
                pending.append(part)
                time.sleep(RATE_LIMIT_BACKOFF_SECONDS)
                continue
            if result == "url_expired":
                url_refresh_count += 1
                if refresh_url is None or url_refresh_count > MAX_URL_REFRESHES:
                    _mark_failed(store, state, callbacks, "下载链接已过期或刷新失败")
                    raise DownloadError("下载链接已过期或刷新失败")
                current_url = refresh_url()
                allowed_workers = max(1, allowed_workers - 1)
                pending.append(part)
                continue
            _mark_failed(store, state, callbacks, "分片下载失败")
            raise DownloadError("分片下载失败")

    latest = store.load(state.task_id) or state
    latest.status = "合并中"
    latest.bytes_done = total_size
    store.save(latest)
    _emit_status(callbacks, "合并中")
    _emit_progress(callbacks, total_size, total_size)
    merged_sha256 = _merge_parts(store, latest, parts, control)

    stop_result = control.stop_result()
    if stop_result in ("paused", "cancelled"):
        return _stop_range_download(stop_result, store, latest, callbacks, control)

    expected_sha256 = latest.expected_sha256
    if expected_sha256 is None:
        LOGGER.debug("download.merge_sha256_skipped task_id=%s", latest.task_id)
    elif merged_sha256 is not None and merged_sha256.lower() != expected_sha256.lower():
        # Part digests can all be correct while the assembled file is not; the
        # bad part cannot be located, so everything is cleared for a fresh
        # non-resumable retry (no output file is ever published).
        _fail_sha256_verification(store, latest, callbacks)
        raise DownloadError(DOWNLOAD_SHA256_FAILURE_MESSAGE)
    merged_path = store.merged_path(latest.task_id)
    if merged_path.stat().st_size != total_size:
        _mark_failed(store, latest, callbacks, "下载分片合并后大小不一致")
        raise DownloadError("下载分片合并后大小不一致")
    local_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _replace_output_file(merged_path, local_path)
    except DownloadError as exc:
        _mark_failed(store, latest, callbacks, str(exc))
        raise
    store.delete(latest.task_id)
    _emit_status(callbacks, "已完成")
    _emit_connections(callbacks, 0, max_workers)
    return DownloadResult(status="已完成", task_id=latest.task_id, local_path=local_path)


def _stop_range_part(
    stop_result: PartResult,
    store: DownloadTaskStore,
    task_id: str,
    part: DownloadPart,
    temp_path: Path,
    progress_callback: Callable[[int, int], None],
) -> PartResult:
    if stop_result == "cancelled":
        store.remove_part_record(task_id, part.index)
        progress_callback(part.index, 0)
        return stop_result

    stat_result = temp_path.stat() if temp_path.exists() else None
    actual_size = stat_result.st_size if stat_result is not None else 0
    # mtime_ns is captured from the same stat call as the size so the record
    # always describes the exact bytes on disk at this moment (the rename to
    # part{index} below preserves it).
    mtime_ns = stat_result.st_mtime_ns if stat_result is not None else None
    if 0 < actual_size <= part.expected_size:
        digest = _compute_md5(temp_path)
        if actual_size == part.expected_size:
            temp_path.replace(store.part_path(task_id, part.index))
        store.record_part(
            task_id,
            DownloadPartRecord(
                index=part.index,
                start=part.start,
                end=part.end,
                expected_size=part.expected_size,
                actual_size=actual_size,
                md5=digest,
                mtime_ns=mtime_ns,
            ),
        )
        progress_callback(part.index, actual_size)
    else:
        store.remove_part_record(task_id, part.index)
        progress_callback(part.index, 0)
    return stop_result


def _download_range_part(
    http_client: httpx.Client,
    url: str,
    store: DownloadTaskStore,
    task_id: str,
    part: DownloadPart,
    retry_max_attempts: int,
    callbacks: DownloadCallbacks,
    control: DownloadTaskControl,
    progress_callback: Callable[[int, int], None],
) -> PartResult:
    part_dir = store.task_temp_dir(task_id)
    part_dir.mkdir(parents=True, exist_ok=True)
    final_path = store.part_path(task_id, part.index)
    temp_path = store.part_downloading_path(task_id, part.index)
    state = store.load(task_id)
    partial_record = next(
        (
            record
            for record in (state.parts if state is not None else [])
            if record.index == part.index and 0 < record.actual_size < record.expected_size
        ),
        None,
    )
    resume_size = partial_record.actual_size if partial_record is not None else 0
    total_size = state.total_bytes if state is not None else None
    attempts = retry_max_attempts + 1
    for attempt in range(attempts):
        stop_result = control.stop_result()
        if stop_result is not None:
            return _stop_range_part(stop_result, store, task_id, part, temp_path, progress_callback)
        bytes_done = resume_size if attempt == 0 else 0
        md5 = hashlib.md5()
        if bytes_done:
            with temp_path.open("rb") as existing:
                while chunk := existing.read(DOWNLOAD_CHUNK_SIZE):
                    md5.update(chunk)
        try:
            with http_client.stream(
                "GET",
                url,
                headers={"Range": f"bytes={part.start + bytes_done}-{part.end}"},
            ) as response:
                control.set_active_response(response)
                if response.status_code == 200:
                    store.remove_part_record(task_id, part.index)
                    raise RangeDownloadUnsupported("Range download unsupported")
                if response.status_code == 403:
                    store.remove_part_record(task_id, part.index)
                    progress_callback(part.index, 0)
                    return "url_expired"
                if response.status_code in RATE_LIMIT_STATUS_CODES:
                    store.remove_part_record(task_id, part.index)
                    progress_callback(part.index, 0)
                    return "rate_limited"
                response.raise_for_status()
                if response.status_code != 206:
                    store.remove_part_record(task_id, part.index)
                    progress_callback(part.index, 0)
                    return "fatal"
                content_range = response.headers.get("Content-Range", "")
                range_text, _, total_text = content_range.partition("/")
                if range_text != f"bytes {part.start + bytes_done}-{part.end}" or (
                    total_size is not None and total_text != str(total_size)
                ):
                    store.remove_part_record(task_id, part.index)
                    progress_callback(part.index, 0)
                    return "fatal"
                stop_requested: PartResult | None = None
                with temp_path.open("ab" if bytes_done else "wb") as output:
                    for chunk in response.iter_bytes(chunk_size=DOWNLOAD_CHUNK_SIZE):
                        stop_requested = control.stop_result()
                        if stop_requested is not None:
                            break
                        if not chunk:  # pragma: no cover - docs/testing-exemptions.md
                            continue
                        output.write(chunk)
                        md5.update(chunk)
                        bytes_done += len(chunk)
                        progress_callback(part.index, bytes_done)
            if stop_requested is not None:
                return _stop_range_part(
                    stop_requested, store, task_id, part, temp_path, progress_callback
                )
        except RangeDownloadUnsupported:
            raise
        except httpx.HTTPStatusError:
            store.remove_part_record(task_id, part.index)
        except httpx.HTTPError:
            stop_result = control.stop_result()
            if stop_result is not None:
                return _stop_range_part(
                    stop_result, store, task_id, part, temp_path, progress_callback
                )
            store.remove_part_record(task_id, part.index)
        except OSError:
            store.remove_part_record(task_id, part.index)
        finally:
            control.set_active_response(None)

        if temp_path.exists() and temp_path.stat().st_size == part.expected_size:
            temp_path.replace(final_path)
            # stat after the rename so mtime_ns describes the published part
            # file, never the transient .downloading one.
            store.record_part(
                task_id,
                DownloadPartRecord(
                    index=part.index,
                    start=part.start,
                    end=part.end,
                    expected_size=part.expected_size,
                    actual_size=part.expected_size,
                    md5=md5.hexdigest(),
                    mtime_ns=final_path.stat().st_mtime_ns,
                ),
            )
            return "ok"
        store.remove_part_record(task_id, part.index)
        progress_callback(part.index, 0)
        if attempt < attempts - 1:
            time.sleep(attempt + 1)
    return "fatal"


def _stop_range_download(
    stop_result: PartResult,
    store: DownloadTaskStore,
    state: DownloadTaskState,
    callbacks: DownloadCallbacks,
    control: DownloadTaskControl,
) -> DownloadResult:
    latest = store.load(state.task_id) or state
    if stop_result == "cancelled":
        latest.status = "已取消"
        latest.error = "用户取消下载"
        latest.bytes_done = 0
        latest.parts = []
        store.cleanup_temp(latest.task_id)
        _emit_status(callbacks, "已取消")
        _emit_connections(callbacks, 0, latest.max_connections)
        if control.cleanup_on_cancel:
            store.delete(latest.task_id)
        else:
            store.save(latest)
        return DownloadResult(status="已取消", task_id=latest.task_id, local_path=latest.save_path)
    latest.status = "已暂停"
    latest.error = ""
    latest.bytes_done = sum(part.actual_size for part in latest.parts)
    store.merged_path(latest.task_id).unlink(missing_ok=True)
    store.save(latest)
    _emit_status(callbacks, "已暂停")
    _emit_progress(callbacks, latest.bytes_done, latest.total_bytes)
    _emit_connections(callbacks, 0, latest.max_connections)
    return DownloadResult(status="已暂停", task_id=latest.task_id, local_path=latest.save_path)


def _mark_failed(
    store: DownloadTaskStore,
    state: DownloadTaskState,
    callbacks: DownloadCallbacks,
    message: str,
) -> None:
    latest = store.load(state.task_id) or state
    latest.status = "失败"
    latest.error = message
    latest.parts = [part for part in latest.parts if part.actual_size == part.expected_size]
    latest.bytes_done = sum(part.actual_size for part in latest.parts)
    temp_dir = store.task_temp_dir(latest.task_id)
    if temp_dir.exists():
        for entry in temp_dir.glob("part*.downloading"):
            _remove_partial_file(entry)
    store.save(latest)
    _emit_status(callbacks, "失败")
    _emit_connections(callbacks, 0, latest.max_connections)


def _fail_sha256_verification(
    store: DownloadTaskStore,
    state: DownloadTaskState,
    callbacks: DownloadCallbacks,
) -> None:
    """Clear every resumable artifact and enter a non-resumable failure state.

    All part digests were correct but the assembled file did not match the
    upstream SHA256, so the bad part cannot be located: temporary files and
    part rows are removed together and the task is marked 失败 with
    ``supports_resume=False`` so the only way forward is a fresh download.
    """
    store.cleanup_temp(state.task_id)

    def apply(latest: DownloadTaskState) -> None:
        latest.parts = []
        latest.bytes_done = 0
        latest.supports_resume = False
        latest.status = "失败"
        latest.error = DOWNLOAD_SHA256_FAILURE_MESSAGE

    latest = store.update(state.task_id, apply)
    _emit_status(callbacks, "失败")
    _emit_connections(callbacks, 0, latest.max_connections)


def _validate_existing_parts(
    store: DownloadTaskStore,
    state: DownloadTaskState,
    parts: list[DownloadPart],
) -> tuple[int, set[int]]:
    part_by_index = {part.index: part for part in parts}
    reusable: set[int] = set()
    partial: set[int] = set()
    downloaded = 0
    for record in list(state.parts):
        planned = part_by_index.get(record.index)
        if (
            planned is None
            or record.start != planned.start
            or record.end != planned.end
            or record.expected_size != planned.expected_size
        ):
            store.remove_part_record(state.task_id, record.index)
            continue
        if record.actual_size == planned.expected_size:
            path = store.part_path(state.task_id, record.index)
            expected_disk_size = planned.expected_size
            complete = True
        elif 0 < record.actual_size < planned.expected_size:
            path = store.part_downloading_path(state.task_id, record.index)
            expected_disk_size = record.actual_size
            complete = False
        else:
            store.remove_part_record(state.task_id, record.index)
            continue
        if not path.exists():
            store.remove_part_record(state.task_id, record.index)
            continue
        stat_result = path.stat()
        if stat_result.st_size != expected_disk_size:
            store.remove_part_record(state.task_id, record.index)
            continue
        if not _part_intact(store, state.task_id, record, path, stat_result):
            store.remove_part_record(state.task_id, record.index)
            continue
        if complete:
            reusable.add(record.index)
            downloaded += planned.expected_size
        else:
            partial.add(record.index)
            downloaded += record.actual_size

    temp_dir = store.task_temp_dir(state.task_id)
    if temp_dir.exists():
        for entry in temp_dir.iterdir():
            if entry.name == "merged":
                _remove_partial_file(entry)
                continue
            if not entry.name.startswith("part"):
                continue
            index_text = entry.name.removeprefix("part").removesuffix(".downloading")
            try:
                index = int(index_text)
            except ValueError:
                continue
            if entry.name.endswith(".downloading"):
                if index not in partial:
                    _remove_partial_file(entry)
            elif index not in reusable:
                _remove_partial_file(entry)
    return downloaded, reusable


def _part_intact(
    store: DownloadTaskStore,
    task_id: str,
    record: DownloadPartRecord,
    path: Path,
    stat_result: os.stat_result,
) -> bool:
    """Validate one part checkpoint with the mtime fast path.

    A recorded ``st_mtime_ns`` matching the file on disk means the bytes were
    never touched since the digest was computed, so the file is reused without
    reading it. Otherwise the recorded algorithm re-hashes the file once; a
    passing re-hash refreshes the stored mtime so the next resume takes the
    fast path again.
    """
    if record.mtime_ns is not None and stat_result.st_mtime_ns == record.mtime_ns:
        return True
    digest = _digest_for(record.algorithm, path)
    if digest is None or digest != record.md5:
        return False
    store.record_part(task_id, replace(record, mtime_ns=stat_result.st_mtime_ns))
    return True


def _digest_for(algorithm: str, path: Path) -> str | None:
    """Hash one part file with its recorded algorithm.

    Unknown algorithms fail validation so the stale record is removed instead
    of being trusted; only ``md5`` exists today.
    """
    if algorithm == "md5":
        return _compute_md5(path)
    LOGGER.warning("download.part_unknown_algorithm algorithm=%s", algorithm)
    return None


def _clear_parts_if_plan_changed(
    store: DownloadTaskStore,
    state: DownloadTaskState,
    parts: list[DownloadPart],
) -> None:
    if state.total_bytes is not None and parts and parts[-1].end + 1 != state.total_bytes:
        state.parts = []
        state.bytes_done = 0
        store.cleanup_temp(state.task_id)
        store.save(state)


def _merge_parts(
    store: DownloadTaskStore,
    state: DownloadTaskState,
    parts: list[DownloadPart],
    control: DownloadTaskControl,
) -> str | None:
    """Concatenate part files into ``merged`` while hashing the output once.

    The SHA256 is computed incrementally inside the same write loop, so no
    second disk read is needed. Returns the output digest, or ``None`` when a
    pause/cancel interrupted the merge before it finished.
    """
    merged_path = store.merged_path(state.task_id)
    merged_path.parent.mkdir(parents=True, exist_ok=True)
    _remove_partial_file(merged_path)
    sha256 = hashlib.sha256()
    try:
        with merged_path.open("wb") as output:
            for part in parts:
                stop_result = control.stop_result()
                if stop_result in ("paused", "cancelled"):
                    return None
                part_path = store.part_path(state.task_id, part.index)
                with part_path.open("rb") as input_file:
                    while chunk := input_file.read(DOWNLOAD_CHUNK_SIZE):
                        output.write(chunk)
                        sha256.update(chunk)
    except OSError as exc:
        raise DownloadError(f"合并分片文件失败：{exc}") from exc
    return sha256.hexdigest()


def _build_parts(total_size: int, part_size: int) -> list[DownloadPart]:
    return [
        DownloadPart(index=index, start=start, end=min(start + part_size - 1, total_size - 1))
        for index, start in enumerate(range(0, total_size, part_size))
    ]


def _download_part_size(total_size: int, settings: AppSettings) -> int:
    configured_size = settings.download_part_size_mb * BYTES_PER_MB
    if settings.download_part_mode == "fixed":
        return configured_size
    target_workers = max(1, min(settings.max_download_threads, 16))
    return max(configured_size, math.ceil(total_size / target_workers))


def _probe_download_size(http_client: httpx.Client, url: str) -> int | None:
    try:
        response = http_client.head(url)
        response.raise_for_status()
    except httpx.HTTPError:
        LOGGER.info("download.head_unavailable")
        return None
    content_length = _read_content_length(response.headers.get("Content-Length"))
    if content_length is None:
        return None
    return content_length


def _read_content_length(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    if parsed < 0:
        return None
    return parsed


def _compute_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 64):
            digest.update(chunk)
    return digest.hexdigest()


def _replace_output_file(source_path: Path, target_path: Path) -> None:
    """Publish a completed download only when the destination is still unoccupied."""
    try:
        os.link(source_path, target_path)
    except FileExistsError as exc:
        raise DownloadError("下载目标已存在，请重新选择保存路径") from exc
    except OSError as exc:
        if exc.errno in {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP}:
            _copy_output_exclusive(source_path, target_path)
        elif exc.errno == errno.EXDEV:
            descriptor, name = tempfile.mkstemp(
                prefix=f".{target_path.name}.", dir=target_path.parent
            )
            os.close(descriptor)
            tmp_path = Path(name)
            try:
                shutil.copy2(source_path, tmp_path)
                if tmp_path.stat().st_size != source_path.stat().st_size:
                    raise OSError("跨盘拷贝大小不匹配")
                try:
                    os.link(tmp_path, target_path)
                except FileExistsError as conflict:
                    raise DownloadError("下载目标已存在，请重新选择保存路径") from conflict
                except OSError as link_error:
                    if link_error.errno not in {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP}:
                        raise
                    _copy_output_exclusive(tmp_path, target_path)
            finally:
                tmp_path.unlink(missing_ok=True)
        else:
            raise
    source_path.unlink()


def _copy_output_exclusive(source_path: Path, target_path: Path) -> None:
    try:
        output = target_path.open("xb")
    except FileExistsError as exc:
        raise DownloadError("下载目标已存在，请重新选择保存路径") from exc
    try:
        with output, source_path.open("rb") as input_file:
            shutil.copyfileobj(input_file, output, DOWNLOAD_CHUNK_SIZE)
    except BaseException:
        target_path.unlink(missing_ok=True)
        raise


def _remove_partial_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("download.partial_cleanup_failed")


def _emit_progress(callbacks: DownloadCallbacks, bytes_done: int, total_bytes: int | None) -> None:
    if callbacks.progress is not None:
        callbacks.progress(bytes_done, total_bytes)


def _emit_status(callbacks: DownloadCallbacks, status: DownloadStatus) -> None:
    if callbacks.status is not None:
        callbacks.status(status)


def _emit_connections(callbacks: DownloadCallbacks, active: int, maximum: int) -> None:
    if callbacks.connections is not None:
        callbacks.connections(active, maximum)


def _state_to_record(state: DownloadTaskState) -> DownloadTaskRecord:
    return DownloadTaskRecord(
        task_id=state.task_id,
        name=state.file_name,
        target_path=state.save_path,
        status=state.status,
        bytes_done=state.bytes_done,
        total_bytes=state.total_bytes,
        supports_resume=state.supports_resume and state.status in {"已暂停", "失败"},
        error=state.error,
        max_connections=state.max_connections,
    )


def _dump_task_state(state: DownloadTaskState) -> dict[str, Any]:
    return {
        "version": state.version,
        "task_id": state.task_id,
        "file_name": state.file_name,
        "save_path": str(state.save_path),
        "status": state.status,
        "download_id": state.download_id,
        "total_bytes": state.total_bytes,
        "bytes_done": state.bytes_done,
        "part_size": state.part_size,
        "max_connections": state.max_connections,
        "supports_resume": state.supports_resume,
        "error": state.error,
        "expected_sha256": state.expected_sha256,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "parts": [
            {
                "index": part.index,
                "start": part.start,
                "end": part.end,
                "expected_size": part.expected_size,
                "actual_size": part.actual_size,
                "md5": part.md5,
                "mtime_ns": part.mtime_ns,
                "algorithm": part.algorithm,
            }
            for part in state.parts
        ],
    }


def _read_task_state(raw: dict[str, Any]) -> DownloadTaskState | None:
    task_id = _read_text(raw.get("task_id"))
    file_name = _read_text(raw.get("file_name"))
    save_path_text = _read_text(raw.get("save_path"))
    if not task_id or not file_name or not save_path_text:
        return None
    parts: list[DownloadPartRecord] = []
    raw_parts = raw.get("parts")
    if isinstance(raw_parts, list):
        for raw_part in raw_parts:
            part = _read_part_record(raw_part)
            if part is not None:
                parts.append(part)
    return DownloadTaskState(
        task_id=task_id,
        file_name=file_name,
        save_path=Path(save_path_text),
        status=_read_status(raw.get("status")),
        download_id=_read_text(raw.get("download_id")) or None,
        total_bytes=_read_optional_non_negative_int(raw.get("total_bytes")),
        bytes_done=_read_non_negative_int(raw.get("bytes_done")),
        part_size=_read_optional_positive_int(raw.get("part_size")),
        max_connections=max(1, _read_non_negative_int(raw.get("max_connections"))),
        supports_resume=bool(raw.get("supports_resume")),
        error=_read_text(raw.get("error")),
        version=_read_non_negative_int(raw.get("version")) or TASK_METADATA_VERSION,
        expected_sha256=_read_text(raw.get("expected_sha256")) or None,
        created_at=float(raw.get("created_at") or time.time()),
        updated_at=float(raw.get("updated_at") or time.time()),
        parts=parts,
    )


def _read_part_record(raw: object) -> DownloadPartRecord | None:
    if not isinstance(raw, dict):
        return None
    index = _read_non_negative_int(raw.get("index"))
    start = _read_non_negative_int(raw.get("start"))
    end = _read_non_negative_int(raw.get("end"))
    expected_size = _read_non_negative_int(raw.get("expected_size"))
    actual_size = _read_non_negative_int(raw.get("actual_size"))
    md5 = _read_text(raw.get("md5"))
    if (
        end < start
        or expected_size <= 0
        or actual_size <= 0
        or actual_size > expected_size
        or not md5
    ):
        return None
    raw_mtime_ns = raw.get("mtime_ns")
    mtime_ns = (
        raw_mtime_ns
        if isinstance(raw_mtime_ns, int) and not isinstance(raw_mtime_ns, bool)
        else None
    )
    return DownloadPartRecord(
        index=index,
        start=start,
        end=end,
        expected_size=expected_size,
        actual_size=actual_size,
        md5=md5,
        mtime_ns=mtime_ns,
        algorithm=_read_text(raw.get("algorithm")) or "md5",
    )


def _read_status(value: object) -> DownloadStatus:
    if value in {"等待中", "校验中", "下载中", "合并中", "已暂停", "已完成", "失败", "已取消"}:
        return value  # type: ignore[return-value]
    return "等待中"


def _read_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""


def _read_non_negative_int(value: object) -> int:
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str):
        try:
            return max(0, int(value))
        except ValueError:
            return 0
    return 0


def _read_optional_non_negative_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str):
        try:
            return max(0, int(value))
        except ValueError:
            return None
    return None


def _read_optional_positive_int(value: object) -> int | None:
    parsed = _read_non_negative_int(value)
    if parsed <= 0:
        return None
    return parsed
