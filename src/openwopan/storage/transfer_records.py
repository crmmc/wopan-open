"""SQLite-backed persistence for transfer-center history records.

First database in OpenWoPan (task 09-23-persistent-transfer-records): a single
local SQLite file stores upload/download transfer rows so the transfer center
can restore history across restarts. The store owns no credential material:
download URLs, cookies, tokens, and signed parameters never reach this module,
and runtime display state (speed, resume permission) is not persisted either.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path

from openwopan.storage.settings import APP_AUTHOR, APP_NAME

LOGGER = logging.getLogger(__name__)

TRANSFER_RECORDS_DIR_NAME = "transfer_records"
TRANSFER_RECORDS_DB_NAME = "records.sqlite3"
SCHEMA_VERSION = 1
VALID_DIRECTIONS = frozenset({"upload", "download"})

_COLUMNS: tuple[str, ...] = (
    "direction",
    "task_id",
    "name",
    "size",
    "local_path",
    "status",
    "bytes_done",
    "total_bytes",
    "error",
    "active_connections",
    "max_connections",
    "upload_parent_id",
    "upload_name",
    "upload_retryable",
    "created_at",
    "updated_at",
)
# Progress-only updates may never resurrect a deleted row, so they go through
# a guarded UPDATE instead of a full upsert.
_UPDATABLE_COLUMNS = frozenset(_COLUMNS) - {"direction", "task_id"}

# Ordered, versioned, idempotent migrations applied against PRAGMA user_version.
# Future schema changes append a new (version, script) entry at the tail.
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS transfer_records (
            direction TEXT NOT NULL CHECK (direction IN ('upload', 'download')),
            task_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            size INTEGER,
            local_path TEXT,
            status TEXT NOT NULL DEFAULT '',
            bytes_done INTEGER NOT NULL DEFAULT 0,
            total_bytes INTEGER,
            error TEXT NOT NULL DEFAULT '',
            active_connections INTEGER NOT NULL DEFAULT 0,
            max_connections INTEGER NOT NULL DEFAULT 1,
            upload_parent_id TEXT,
            upload_name TEXT,
            upload_retryable INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (direction, task_id)
        )
        """,
    ),
)

_SQL_UPSERT = f"""
INSERT INTO transfer_records ({", ".join(_COLUMNS)})
VALUES ({", ".join("?" for _ in _COLUMNS)})
ON CONFLICT(direction, task_id) DO UPDATE SET
    name = excluded.name,
    size = excluded.size,
    local_path = excluded.local_path,
    status = excluded.status,
    bytes_done = excluded.bytes_done,
    total_bytes = excluded.total_bytes,
    error = excluded.error,
    active_connections = excluded.active_connections,
    max_connections = excluded.max_connections,
    upload_parent_id = excluded.upload_parent_id,
    upload_name = excluded.upload_name,
    upload_retryable = excluded.upload_retryable,
    created_at = excluded.created_at,
    updated_at = excluded.updated_at
"""
_SQL_SELECT_ALL = (
    f"SELECT {', '.join(_COLUMNS)} FROM transfer_records "
    "ORDER BY updated_at ASC, created_at ASC, direction ASC, task_id ASC"
)
_SQL_DELETE = "DELETE FROM transfer_records WHERE direction = ? AND task_id = ?"


def transfer_records_db_path() -> Path:
    """Return the default database location under the user data directory."""
    return (
        user_data_path(APP_NAME, APP_AUTHOR)
        / TRANSFER_RECORDS_DIR_NAME
        / TRANSFER_RECORDS_DB_NAME
    )


@dataclass(frozen=True, slots=True)
class TransferRecordRow:
    """One persisted transfer history row."""

    direction: str
    task_id: str
    name: str
    size: int | None
    local_path: str | None
    status: str
    bytes_done: int
    total_bytes: int | None
    error: str
    active_connections: int
    max_connections: int
    upload_parent_id: str | None
    upload_name: str | None
    upload_retryable: bool
    created_at: float
    updated_at: float


class TransferRecordStore:
    """Locked SQLite storage for transfer-center history rows.

    The connection is created with ``check_same_thread=False`` and every
    operation serializes on one :class:`threading.RLock`, matching the
    ``UploadTaskStore`` pattern: readers may run on worker threads while the
    GUI thread writes.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or transfer_records_db_path()
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        """Return the database file path for tests and diagnostics."""
        return self._path

    def open(self) -> None:
        """Connect and run idempotent migrations; creates the database on first use."""
        with self._lock:
            if self._connection is not None:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._path, check_same_thread=False)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                self._apply_migrations(connection)
                connection.commit()
            except BaseException:
                connection.close()
                raise
            self._connection = connection

    def close(self) -> None:
        """Commit and close the connection; safe to call repeatedly."""
        with self._lock:
            if self._connection is None:
                return
            connection, self._connection = self._connection, None
            connection.commit()
            connection.close()

    def upsert(self, row: TransferRecordRow) -> None:
        """Insert one full record row or replace an existing one."""
        _require_direction(row.direction)
        with self._lock:
            connection = self._require_connection()
            connection.execute(_SQL_UPSERT, _row_parameters(row))
            connection.commit()

    def update_fields(
        self, direction: str, task_id: str, fields: Mapping[str, object]
    ) -> None:
        """Update whitelisted columns of one row; a no-op when the row is gone."""
        _require_direction(direction)
        unknown = set(fields) - _UPDATABLE_COLUMNS
        if unknown:
            raise ValueError(f"unsupported transfer record fields: {sorted(unknown)}")
        if not fields or not task_id:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        parameters = (*fields.values(), direction, task_id)
        with self._lock:
            connection = self._require_connection()
            connection.execute(
                f"UPDATE transfer_records SET {assignments} "
                "WHERE direction = ? AND task_id = ?",
                parameters,
            )
            connection.commit()

    def delete(self, direction: str, task_ids: Iterable[str]) -> None:
        """Delete record rows for one direction by task id."""
        _require_direction(direction)
        ids = [task_id for task_id in task_ids if task_id]
        if not ids:
            return
        with self._lock:
            connection = self._require_connection()
            connection.executemany(_SQL_DELETE, [(direction, task_id) for task_id in ids])
            connection.commit()

    def load_all(self) -> list[TransferRecordRow]:
        """Return all rows ordered by updated time ascending with a stable tie-break."""
        with self._lock:
            connection = self._require_connection()
            values_list = connection.execute(_SQL_SELECT_ALL).fetchall()
        rows: list[TransferRecordRow] = []
        for values in values_list:
            row = _row_from_values(values)
            if row is None:
                LOGGER.warning(
                    "transfer_records.row.skipped columns=%d direction=%r",
                    len(values),
                    values[0] if values else None,
                )
                continue
            rows.append(row)
        return rows

    def _apply_migrations(self, connection: sqlite3.Connection) -> None:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        for version, script in MIGRATIONS:
            if version <= current:
                continue
            connection.executescript(script)
            # PRAGMA values cannot be parameterized; the version is an internal
            # constant, not external input.
            connection.execute(f"PRAGMA user_version = {int(version)}")

    def _require_connection(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            raise RuntimeError("transfer record store is not open")
        return connection


def _require_direction(direction: str) -> None:
    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"unsupported transfer direction: {direction}")


def _row_parameters(row: TransferRecordRow) -> tuple[object, ...]:
    return (
        row.direction,
        row.task_id,
        row.name,
        row.size,
        row.local_path,
        row.status,
        row.bytes_done,
        row.total_bytes,
        row.error,
        row.active_connections,
        row.max_connections,
        row.upload_parent_id,
        row.upload_name,
        int(row.upload_retryable),
        row.created_at,
        row.updated_at,
    )


def _row_from_values(values: tuple[object, ...]) -> TransferRecordRow | None:
    """Build a typed row from one database tuple; None when the shape is invalid."""
    if len(values) != len(_COLUMNS):
        return None
    (
        direction,
        task_id,
        name,
        size,
        local_path,
        status,
        bytes_done,
        total_bytes,
        error,
        active_connections,
        max_connections,
        upload_parent_id,
        upload_name,
        upload_retryable,
        created_at,
        updated_at,
    ) = values
    if direction not in VALID_DIRECTIONS:
        return None
    if not isinstance(task_id, str) or not task_id:
        return None
    if not isinstance(status, str) or not status:
        return None
    return TransferRecordRow(
        direction=direction,
        task_id=task_id,
        name=_text_or_empty(name),
        size=_optional_int(size),
        local_path=_optional_text(local_path),
        status=status,
        bytes_done=_required_int(bytes_done),
        total_bytes=_optional_int(total_bytes),
        error=_text_or_empty(error),
        active_connections=_required_int(active_connections),
        max_connections=_required_int(max_connections, 1),
        upload_parent_id=_optional_text(upload_parent_id),
        upload_name=_optional_text(upload_name),
        upload_retryable=bool(_required_int(upload_retryable)),
        created_at=_required_float(created_at),
        updated_at=_required_float(updated_at),
    )


def _optional_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _required_int(value: object, default: int = 0) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _text_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""


def _required_float(value: object) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return 0.0
