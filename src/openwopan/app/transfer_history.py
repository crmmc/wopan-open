"""App-layer adapter between UI transfer records and the SQLite history store.

The adapter implements the UI-side :class:`TransferRecordPersistence` protocol
(defined in ``openwopan.ui.main_window`` without Qt dependencies) and converts
``TransferRecord`` models to and from ``TransferRecordRow`` storage rows. All
database failures degrade to in-memory state with an exception-level log: the
transfer center keeps working even when the database is unusable, and the main
window never sees a persistence error.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from openwopan.storage.transfer_records import (
    TransferRecordRow,
    TransferRecordStore,
)
from openwopan.ui.main_window import TransferRecord

LOGGER = logging.getLogger(__name__)


class TransferHistoryAdapter:
    """Best-effort persistence boundary for transfer records."""

    def __init__(self, store: TransferRecordStore) -> None:
        self._store: TransferRecordStore | None = store
        try:
            store.open()
        except (sqlite3.Error, OSError):
            LOGGER.exception("transfer_history.store.open_failed")
            self._store = None

    def save_record(self, record: TransferRecord) -> None:
        """Upsert one full record row; failures degrade to memory-only state."""
        if self._store is None:
            return
        try:
            self._store.upsert(_to_row(record))
        except (sqlite3.Error, OSError):
            LOGGER.exception(
                "transfer_history.save.failed direction=%s task_id=%s",
                record.direction,
                record.task_id,
            )

    def update_record_progress(
        self,
        direction: str,
        task_id: str,
        *,
        bytes_done: int,
        active_connections: int,
        updated_at: float,
    ) -> None:
        """Persist progress-only fields; a no-op when the row is already gone."""
        if self._store is None:
            return
        try:
            self._store.update_fields(
                direction,
                task_id,
                {
                    "bytes_done": bytes_done,
                    "active_connections": active_connections,
                    "updated_at": updated_at,
                },
            )
        except (sqlite3.Error, OSError):
            LOGGER.exception(
                "transfer_history.progress.failed direction=%s task_id=%s",
                direction,
                task_id,
            )

    def delete_records(self, direction: str, task_ids: Iterable[str]) -> None:
        """Delete rows by (direction, task id); failures keep the UI state."""
        if self._store is None:
            return
        ids = [task_id for task_id in task_ids if task_id]
        if not ids:
            return
        try:
            self._store.delete(direction, ids)
        except (sqlite3.Error, OSError):
            LOGGER.exception(
                "transfer_history.delete.failed direction=%s count=%d",
                direction,
                len(ids),
            )

    def load_history(self) -> tuple[TransferRecord, ...]:
        """Load all persisted rows as display-only records ordered by update time."""
        if self._store is None:
            return ()
        try:
            rows = self._store.load_all()
        except (sqlite3.Error, OSError):
            LOGGER.exception("transfer_history.load.failed")
            return ()
        return tuple(_to_record(row) for row in rows)


def _to_row(record: TransferRecord) -> TransferRecordRow:
    """Project a UI record onto a storage row (no URL or credential fields exist)."""
    return TransferRecordRow(
        direction=record.direction,
        task_id=record.task_id,
        name=record.name,
        size=record.size,
        local_path=str(record.target_path) if record.target_path is not None else None,
        status=record.status,
        bytes_done=record.bytes_done,
        total_bytes=record.total_bytes,
        error=record.error,
        active_connections=record.active_connections,
        max_connections=record.max_connections,
        upload_parent_id=record.upload_parent_id,
        upload_name=record.upload_name,
        upload_retryable=record.upload_retryable,
        created_at=record.created_at_epoch,
        updated_at=record.updated_at_epoch,
    )


def _to_record(row: TransferRecordRow) -> TransferRecord:
    """Rebuild a display-only record; runtime state resets (speed, connections)."""
    return TransferRecord(
        task_id=row.task_id,
        direction=row.direction,
        name=row.name,
        size=row.size,
        target_path=Path(row.local_path) if row.local_path is not None else None,
        status=row.status,
        bytes_done=row.bytes_done,
        total_bytes=row.total_bytes,
        speed_bps=0.0,
        active_connections=0,
        max_connections=row.max_connections,
        can_resume=False,
        error=row.error,
        upload_parent_id=row.upload_parent_id,
        upload_name=row.upload_name,
        upload_retryable=row.upload_retryable,
        created_at_epoch=row.created_at,
        updated_at_epoch=row.updated_at,
    )
