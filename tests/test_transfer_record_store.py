"""Tests for the SQLite transfer-record store and its app-layer adapter."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from openwopan.app.transfer_history import TransferHistoryAdapter
from openwopan.storage.transfer_records import (
    SCHEMA_VERSION,
    TransferRecordRow,
    TransferRecordStore,
    _row_from_values,
    transfer_records_db_path,
)
from openwopan.ui.main_window import TransferRecord


def _row(**overrides: object) -> TransferRecordRow:
    """Build one storage row with sensible defaults and optional overrides."""
    values: dict[str, object] = {
        "direction": "download",
        "task_id": "task-1",
        "name": "file.bin",
        "size": 100,
        "local_path": "/tmp/file.bin",
        "status": "已完成",
        "bytes_done": 100,
        "total_bytes": 100,
        "error": "",
        "active_connections": 2,
        "max_connections": 4,
        "upload_parent_id": None,
        "upload_name": None,
        "upload_retryable": False,
        "created_at": 10.0,
        "updated_at": 20.0,
    }
    values.update(overrides)
    return TransferRecordRow(**values)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[TransferRecordStore]:
    record_store = TransferRecordStore(tmp_path / "transfer_records" / "records.sqlite3")
    record_store.open()
    yield record_store
    record_store.close()


@pytest.fixture
def adapter(store: TransferRecordStore) -> TransferHistoryAdapter:
    return TransferHistoryAdapter(store)


def _user_version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _table_columns(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("PRAGMA table_info(transfer_records)").fetchall()
    return {str(row[1]) for row in rows}


def _dump_rows(path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        return list(connection.execute("SELECT * FROM transfer_records").fetchall())


# ---------------------------------------------------------------------------
# Store: open / migrations
# ---------------------------------------------------------------------------


def test_open_creates_database_with_schema_version(tmp_path: Path) -> None:
    db_path = tmp_path / "transfer_records" / "records.sqlite3"
    record_store = TransferRecordStore(db_path)

    assert not db_path.exists()
    record_store.open()

    assert db_path.exists()
    assert _user_version(db_path) == SCHEMA_VERSION
    assert _table_columns(db_path) == {
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
    }
    record_store.close()


def test_open_twice_is_idempotent_and_keeps_rows(store: TransferRecordStore) -> None:
    store.upsert(_row())

    store.open()

    assert [row.task_id for row in store.load_all()] == ["task-1"]
    assert _user_version(store.path) == SCHEMA_VERSION


def test_migrations_replay_from_reset_version(tmp_path: Path) -> None:
    db_path = tmp_path / "records.sqlite3"
    record_store = TransferRecordStore(db_path)
    record_store.open()
    record_store.upsert(_row())
    record_store.close()

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA user_version = 0")

    reopened = TransferRecordStore(db_path)
    reopened.open()

    assert _user_version(db_path) == SCHEMA_VERSION
    assert [row.task_id for row in reopened.load_all()] == ["task-1"]

    # A third open on an up-to-date database skips every migration step.
    up_to_date = TransferRecordStore(db_path)
    up_to_date.open()
    assert [row.task_id for row in up_to_date.load_all()] == ["task-1"]
    reopened.close()
    up_to_date.close()


def test_default_db_path_uses_user_data_layout() -> None:
    path = transfer_records_db_path()

    assert path.name == "records.sqlite3"
    assert path.parent.name == "transfer_records"


# ---------------------------------------------------------------------------
# Store: upsert / update_fields / delete / load_all
# ---------------------------------------------------------------------------


def test_upsert_inserts_then_replaces_existing_row(store: TransferRecordStore) -> None:
    store.upsert(_row())

    store.upsert(_row(status="失败", error="网络错误", bytes_done=40))

    rows = store.load_all()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "失败"
    assert row.error == "网络错误"
    assert row.bytes_done == 40
    assert row.created_at == 10.0


def test_upsert_round_trips_optional_and_zero_fields(store: TransferRecordStore) -> None:
    store.upsert(
        _row(
            size=None,
            total_bytes=0,
            local_path=None,
            upload_parent_id="parent-1",
            upload_name="name.bin",
            upload_retryable=True,
            active_connections=0,
        )
    )

    row = store.load_all()[0]
    assert row.size is None
    assert row.total_bytes == 0
    assert row.local_path is None
    assert row.upload_parent_id == "parent-1"
    assert row.upload_name == "name.bin"
    assert row.upload_retryable is True
    assert row.active_connections == 0


def test_upsert_rejects_unknown_direction(store: TransferRecordStore) -> None:
    with pytest.raises(ValueError, match="unsupported transfer direction"):
        store.upsert(_row(direction="sideload"))


def test_load_all_orders_rows_by_updated_time(store: TransferRecordStore) -> None:
    store.upsert(_row(task_id="task-old", updated_at=5.0))
    store.upsert(_row(task_id="task-new", direction="upload", updated_at=50.0))
    store.upsert(_row(task_id="task-mid", updated_at=20.0))

    loaded = store.load_all()

    assert [row.task_id for row in loaded] == ["task-old", "task-mid", "task-new"]


def test_update_fields_changes_only_requested_columns(
    store: TransferRecordStore,
) -> None:
    store.upsert(_row())

    store.update_fields(
        "download", "task-1", {"bytes_done": 55, "active_connections": 3, "updated_at": 99.0}
    )

    row = store.load_all()[0]
    assert row.bytes_done == 55
    assert row.active_connections == 3
    assert row.updated_at == 99.0
    assert row.status == "已完成"
    assert row.total_bytes == 100


def test_update_fields_is_noop_for_missing_row(store: TransferRecordStore) -> None:
    store.upsert(_row())

    store.update_fields("download", "missing", {"bytes_done": 1})

    assert store.load_all()[0].bytes_done == 100


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({"speed_bps": 1.0}, "unsupported transfer record fields"),
        ({"direction": "download"}, "unsupported transfer record fields"),
    ],
)
def test_update_fields_rejects_unknown_columns(
    store: TransferRecordStore, fields: dict[str, object], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        store.update_fields("download", "task-1", fields)


def test_update_fields_with_empty_mapping_is_noop(store: TransferRecordStore) -> None:
    store.upsert(_row())

    store.update_fields("download", "task-1", {})

    assert store.load_all()[0].bytes_done == 100


def test_delete_removes_only_matching_direction_rows(store: TransferRecordStore) -> None:
    store.upsert(_row())
    store.upsert(_row(direction="upload", task_id="task-1", upload_name="file.bin"))

    store.delete("upload", {"task-1"})
    store.delete("download", set())

    remaining = store.load_all()
    assert [row.direction for row in remaining] == ["download"]


def test_delete_ignores_invalid_direction(store: TransferRecordStore) -> None:
    with pytest.raises(ValueError, match="unsupported transfer direction"):
        store.delete("sideload", {"task-1"})


def test_load_all_skips_structurally_invalid_rows(
    store: TransferRecordStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store.upsert(_row())
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("UPDATE transfer_records SET direction = 'sideload'")

    with caplog.at_level(logging.WARNING, logger="openwopan.storage.transfer_records"):
        rows = store.load_all()

    assert rows == []
    assert any("transfer_records.row.skipped" in message for message in caplog.messages)


# ---------------------------------------------------------------------------
# Store: lifecycle guards
# ---------------------------------------------------------------------------


def _store_operations() -> list[tuple[str, Callable[..., object]]]:
    record_store = TransferRecordStore(Path("/nonexistent/records.sqlite3"))
    return [
        ("upsert", lambda: record_store.upsert(_row())),
        (
            "update_fields",
            lambda: record_store.update_fields("download", "t", {"bytes_done": 1}),
        ),
        ("delete", lambda: record_store.delete("download", {"t"})),
        ("load_all", record_store.load_all),
    ]


@pytest.mark.parametrize(("name", "operation"), _store_operations())
def test_operations_before_open_raise_runtime_error(
    name: str, operation: Callable[..., object]
) -> None:
    with pytest.raises(RuntimeError, match="not open"):
        operation()


def test_close_is_idempotent_and_blocks_further_operations(
    store: TransferRecordStore,
) -> None:
    store.close()
    store.close()

    with pytest.raises(RuntimeError, match="not open"):
        store.load_all()


def test_open_failure_closes_connection_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration failure must not leave a leaked connection behind."""
    record_store = TransferRecordStore(tmp_path / "records.sqlite3")
    calls = {"count": 0}

    def flaky_apply(connection: sqlite3.Connection) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise sqlite3.OperationalError("boom")
        TransferRecordStore._apply_migrations(record_store, connection)

    monkeypatch.setattr(record_store, "_apply_migrations", flaky_apply)
    with pytest.raises(sqlite3.OperationalError, match="boom"):
        record_store.open()

    monkeypatch.undo()
    record_store.open()
    record_store.upsert(_row())

    assert [row.task_id for row in record_store.load_all()] == ["task-1"]
    record_store.close()


# ---------------------------------------------------------------------------
# Store: no credential material is persisted
# ---------------------------------------------------------------------------


def test_persisted_rows_never_contain_credential_material(
    store: TransferRecordStore,
) -> None:
    store.upsert(_row(local_path="/Users/user/Downloads/file.bin"))
    store.upsert(_row(direction="upload", task_id="task-2", upload_name="src.bin"))

    columns = _table_columns(store.path)
    serialized = repr(_dump_rows(store.path))

    assert columns == columns - {
        "url",
        "download_url",
        "cookie",
        "token",
        "signed_url",
    }
    for forbidden in ("http://", "https://", "Cookie", "Accesstoken", "Bearer "):
        assert forbidden not in serialized


# ---------------------------------------------------------------------------
# Adapter: conversions and round trip
# ---------------------------------------------------------------------------


def _record(**overrides: object) -> TransferRecord:
    values: dict[str, object] = {
        "task_id": "upload-1",
        "direction": "upload",
        "name": "src.bin",
        "size": 256,
        "target_path": Path("/tmp/src.bin"),
        "status": "上传中",
        "bytes_done": 128,
        "total_bytes": 256,
        "error": "",
        "upload_parent_id": "parent-1",
        "upload_name": "src.bin",
        "upload_retryable": False,
        "created_at_epoch": 100.0,
        "updated_at_epoch": 200.0,
    }
    values.update(overrides)
    return TransferRecord(**values)


def test_adapter_saves_and_loads_history(adapter: TransferHistoryAdapter) -> None:
    adapter.save_record(_record())
    adapter.save_record(
        _record(
            task_id="download-1",
            direction="download",
            name="movie.mp4",
            target_path=Path("/tmp/movie.mp4"),
            status="下载中",
            created_at_epoch=150.0,
            updated_at_epoch=300.0,
        )
    )

    history = adapter.load_history()

    assert [record.task_id for record in history] == ["upload-1", "download-1"]
    upload = history[0]
    assert upload.created_at_epoch == 100.0
    assert upload.updated_at_epoch == 200.0
    assert upload.target_path == Path("/tmp/src.bin")
    # Restored rows are display-only: runtime state resets.
    assert upload.speed_bps == 0.0
    assert upload.active_connections == 0
    assert upload.can_resume is False
    # Monotonic fields are re-baselined for the speed sampler by __post_init__.
    assert upload.created_at > 0
    assert upload.updated_at >= upload.created_at


def test_adapter_preserves_optional_path_and_upload_context(
    adapter: TransferHistoryAdapter,
) -> None:
    adapter.save_record(_record(target_path=None, size=None, total_bytes=None))

    restored = adapter.load_history()[0]

    assert restored.target_path is None
    assert restored.size is None
    assert restored.total_bytes is None


def test_adapter_progress_update_touches_progress_columns_only(
    adapter: TransferHistoryAdapter,
) -> None:
    adapter.save_record(_record())

    adapter.update_record_progress(
        "upload",
        "upload-1",
        bytes_done=250,
        active_connections=4,
        updated_at=999.0,
    )
    # A row that no longer exists must not be resurrected by a late flush.
    adapter.update_record_progress(
        "upload",
        "upload-missing",
        bytes_done=1,
        active_connections=1,
        updated_at=1.0,
    )

    restored = adapter.load_history()[0]
    assert restored.bytes_done == 250
    assert restored.updated_at_epoch == 999.0
    assert restored.status == "上传中"
    assert [record.task_id for record in adapter.load_history()] == ["upload-1"]


def test_adapter_delete_records_removes_matching_rows(
    adapter: TransferHistoryAdapter,
) -> None:
    adapter.save_record(_record())
    adapter.save_record(
        _record(task_id="download-1", direction="download", name="movie.mp4")
    )

    adapter.delete_records("upload", ["upload-1"])
    adapter.delete_records("upload", [])

    assert [record.task_id for record in adapter.load_history()] == ["download-1"]


# ---------------------------------------------------------------------------
# Adapter: degraded failures never raise
# ---------------------------------------------------------------------------


def test_adapter_degrades_to_memory_when_store_cannot_open(tmp_path: Path) -> None:
    blocking_file = tmp_path / "blocking"
    blocking_file.write_text("not a directory", encoding="utf-8")
    broken_store = TransferRecordStore(blocking_file / "records.sqlite3")

    broken_adapter = TransferHistoryAdapter(broken_store)

    broken_adapter.save_record(_record())
    broken_adapter.update_record_progress(
        "upload", "upload-1", bytes_done=1, active_connections=1, updated_at=1.0
    )
    broken_adapter.delete_records("upload", ["upload-1"])
    assert broken_adapter.load_history() == ()


@pytest.mark.parametrize("error", [sqlite3.OperationalError("disk I/O error"), OSError("no space")])
def test_adapter_swallows_database_errors(
    adapter: TransferHistoryAdapter,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    def raise_error(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(adapter._store, "upsert", raise_error)
    monkeypatch.setattr(adapter._store, "update_fields", raise_error)
    monkeypatch.setattr(adapter._store, "delete", raise_error)
    monkeypatch.setattr(adapter._store, "load_all", raise_error)

    adapter.save_record(_record())
    adapter.update_record_progress(
        "upload", "upload-1", bytes_done=1, active_connections=1, updated_at=1.0
    )
    adapter.delete_records("upload", ["upload-1"])

    assert adapter.load_history() == ()


# ---------------------------------------------------------------------------
# Row reconstruction guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "values",
    [
        ("download",),  # wrong column count
        (
            "sideload", "task-1", "f.bin", None, None, "已完成",
            0, None, "", 0, 1, None, None, 0, 1.0, 2.0,
        ),  # direction outside the CHECK set
        (None, "task-1", "f.bin", None, None, "已完成", 0, None, "", 0, 1, None, None, 0, 1.0, 2.0),
        ("download", "", "f.bin", None, None, "已完成", 0, None, "", 0, 1, None, None, 0, 1.0, 2.0),
        ("download", "task-1", "f.bin", None, None, "", 0, None, "", 0, 1, None, None, 0, 1.0, 2.0),
        (
            "download", "task-1", "f.bin", None, None, None,
            0, None, "", 0, 1, None, None, 0, 1.0, 2.0,
        ),  # empty status
    ],
)
def test_row_from_values_rejects_structurally_invalid_rows(values: tuple[object, ...]) -> None:
    assert _row_from_values(values) is None


def test_row_from_values_coerces_anomalous_field_types() -> None:
    row = _row_from_values(
        (
            "download",
            "task-1",
            42,  # non-text name falls back to empty
            "100",  # non-int size falls back to None
            7,  # non-text local_path falls back to None
            "已完成",
            "5",  # non-int bytes_done falls back to 0
            "100",  # non-int total_bytes falls back to None
            None,  # non-text error falls back to empty
            "3",  # non-int active_connections falls back to 0
            None,  # non-int max_connections falls back to 1
            1,  # non-text upload_parent_id falls back to None
            2,  # non-text upload_name falls back to None
            "1",  # non-int upload_retryable falls back to False
            "1.0",  # non-float created_at falls back to 0.0
            True,  # non-float updated_at falls back to 0.0
        )
    )

    assert row is not None
    assert row.name == ""
    assert row.size is None
    assert row.local_path is None
    assert row.bytes_done == 0
    assert row.total_bytes is None
    assert row.error == ""
    assert row.active_connections == 0
    assert row.max_connections == 1
    assert row.upload_parent_id is None
    assert row.upload_name is None
    assert row.upload_retryable is False
    assert row.created_at == 0.0
    assert row.updated_at == 0.0
