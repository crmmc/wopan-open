"""SQLite DownloadTaskStore tests: row semantics, validation, JSON migration.

The store keeps part byte files on the filesystem; these tests cover the
database side only (task rows, part rows, bytes_done derivation, legacy JSON
import) plus the row-validation discipline shared with transfer_records.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from openwopan.tasks.download import (
    DownloadPartRecord,
    DownloadTaskState,
    DownloadTaskStore,
    _dump_task_state,
    _read_task_state,
)


def _state(
    task_id: str = "task-1",
    save_path: Path | None = None,
    parts: list[DownloadPartRecord] | None = None,
) -> DownloadTaskState:
    return DownloadTaskState(
        task_id=task_id,
        file_name="file.bin",
        save_path=save_path or Path("/tmp/file.bin"),
        parts=parts or [],
    )


def _part_record(
    index: int,
    start: int,
    end: int,
    *,
    actual_size: int | None = None,
    md5: str = "0" * 32,
    mtime_ns: int | None = None,
    algorithm: str = "md5",
) -> DownloadPartRecord:
    return DownloadPartRecord(
        index=index,
        start=start,
        end=end,
        expected_size=end - start + 1,
        actual_size=end - start + 1 if actual_size is None else actual_size,
        md5=md5,
        mtime_ns=mtime_ns,
        algorithm=algorithm,
    )


def _write_legacy_json(root: Path, state: DownloadTaskState) -> Path:
    """Create a pre-migration per-task JSON file the way the old store did."""
    path = root / "tasks" / f"{state.task_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_dump_task_state(state), ensure_ascii=False), encoding="utf-8")
    return path


def _task_row_count(root: Path, task_id: str) -> int:
    with sqlite3.connect(root / "tasks.sqlite3") as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM download_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()[0]
        )


def _part_row_count(root: Path, task_id: str) -> int:
    with sqlite3.connect(root / "tasks.sqlite3") as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM download_parts WHERE task_id = ?", (task_id,)
            ).fetchone()[0]
        )


# -- basic round-trip ---------------------------------------------------------


def test_store_creates_database_and_reports_missing_task(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path)

    assert store.load("missing") is None
    assert (tmp_path / "tasks.sqlite3").exists()


def test_store_roundtrips_all_task_and_part_fields(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path)
    state = _state(
        save_path=tmp_path / "file.bin",
        parts=[
            _part_record(0, 0, 3, mtime_ns=1234),
            _part_record(1, 4, 7, actual_size=2, md5="a" * 32),
        ],
    )
    state.total_bytes = 8
    state.status = "已暂停"
    state.download_id = "fid-1"
    state.part_size = 4
    state.max_connections = 3
    state.supports_resume = True
    state.error = "stopped"
    state.expected_sha256 = "B" * 64
    # save() persists bytes_done verbatim (callers maintain it, e.g. scheduler
    # progress may legitimately exceed sum(parts) mid-flight).
    state.bytes_done = 6
    created_at = state.created_at
    store.save(state)

    loaded = store.load("task-1")

    assert loaded is not None
    assert loaded.task_id == "task-1"
    assert loaded.file_name == "file.bin"
    assert loaded.save_path == tmp_path / "file.bin"
    assert loaded.status == "已暂停"
    assert loaded.download_id == "fid-1"
    assert loaded.total_bytes == 8
    assert loaded.bytes_done == 6
    assert loaded.part_size == 4
    assert loaded.max_connections == 3
    assert loaded.supports_resume is True
    assert loaded.error == "stopped"
    assert loaded.version == 1
    assert loaded.expected_sha256 == "B" * 64
    assert loaded.created_at == created_at
    assert [part.mtime_ns for part in loaded.parts] == [1234, None]
    assert [part.algorithm for part in loaded.parts] == ["md5", "md5"]


def test_store_zero_bytes_roundtrip_as_zero_not_none(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path)
    state = _state(save_path=tmp_path / "empty.bin")
    state.total_bytes = 0

    store.save(state)
    loaded = store.load("task-1")

    assert loaded is not None
    assert loaded.total_bytes == 0


def test_store_load_all_orders_by_created_at_then_task_id(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path)
    late = _state(task_id="task-b", save_path=tmp_path / "b.bin")
    late.created_at = 200.0
    early_low = _state(task_id="task-z", save_path=tmp_path / "z.bin")
    early_low.created_at = 100.0
    early_high = _state(task_id="task-a", save_path=tmp_path / "a.bin")
    early_high.created_at = 100.0
    store.save(late)
    store.save(early_low)
    store.save(early_high)

    states = store.load_all()

    assert [state.task_id for state in states] == ["task-a", "task-z", "task-b"]


def test_store_open_and_close_are_idempotent(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path)
    store.open()
    store.open()
    store.save(_state(save_path=tmp_path / "f.bin"))
    store.close()
    store.close()

    store.load("task-1")
    assert store.load("task-1") is not None


# -- record_part / remove_part_record row semantics ---------------------------


def test_record_part_upserts_one_row_and_derives_bytes_done(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path)
    state = _state(save_path=tmp_path / "f.bin", parts=[_part_record(0, 0, 3)])
    state.bytes_done = 4
    store.save(state)

    store.record_part("task-1", _part_record(1, 4, 7))

    loaded = store.load("task-1")
    assert loaded is not None
    assert [part.index for part in loaded.parts] == [0, 1]
    assert loaded.bytes_done == 8

    # Same index again: single-row replacement, not an append.
    store.record_part("task-1", _part_record(1, 4, 7, actual_size=1, md5="1" * 32))
    loaded = store.load("task-1")
    assert loaded is not None
    assert [part.index for part in loaded.parts] == [0, 1]
    assert loaded.parts[1].actual_size == 1
    assert loaded.bytes_done == 5
    assert _part_row_count(tmp_path, "task-1") == 2


def test_record_part_without_task_is_noop_without_orphan_rows(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path)

    store.record_part("missing", _part_record(0, 0, 3))

    assert store.load("missing") is None
    assert _task_row_count(tmp_path, "missing") == 0
    assert _part_row_count(tmp_path, "missing") == 0


def test_remove_part_record_deletes_row_files_and_refreshes_bytes_done(
    tmp_path: Path,
) -> None:
    store = DownloadTaskStore(tmp_path)
    state = _state(
        save_path=tmp_path / "f.bin",
        parts=[_part_record(0, 0, 3), _part_record(1, 4, 7)],
    )
    store.save(state)
    part_file = store.part_path("task-1", 0)
    part_file.parent.mkdir(parents=True, exist_ok=True)
    part_file.write_bytes(b"part")
    downloading_file = store.part_downloading_path("task-1", 1)
    downloading_file.write_bytes(b"partial")

    store.remove_part_record("task-1", 0)

    loaded = store.load("task-1")
    assert loaded is not None
    assert [part.index for part in loaded.parts] == [1]
    assert loaded.bytes_done == 4
    assert not part_file.exists()

    store.remove_part_record("task-1", 1)
    assert not downloading_file.exists()
    assert _part_row_count(tmp_path, "task-1") == 0
    assert store.load("task-1") is not None and store.load("task-1").bytes_done == 0  # type: ignore[union-attr]


# -- delete / cleanup_temp -----------------------------------------------------


def test_store_delete_removes_rows_and_temp_files(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path)
    state = _state(save_path=tmp_path / "f.bin", parts=[_part_record(0, 0, 3)])
    store.save(state)
    part_file = store.part_path("task-1", 0)
    part_file.parent.mkdir(parents=True, exist_ok=True)
    part_file.write_bytes(b"part")

    store.delete("task-1")

    assert store.load("task-1") is None
    assert _task_row_count(tmp_path, "task-1") == 0
    assert _part_row_count(tmp_path, "task-1") == 0
    assert not store.task_temp_dir("task-1").exists()
    store.delete("missing")  # no rows, no directory: must not raise


def test_store_cleanup_temp_keeps_metadata_rows(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path)
    state = _state(save_path=tmp_path / "f.bin", parts=[_part_record(0, 0, 3)])
    store.save(state)
    part_file = store.part_path("task-1", 0)
    part_file.parent.mkdir(parents=True, exist_ok=True)
    part_file.write_bytes(b"part")

    store.cleanup_temp("task-1")

    assert not part_file.exists()
    assert store.load("task-1") is not None
    assert _part_row_count(tmp_path, "task-1") == 1


# -- list_records --------------------------------------------------------------


def test_store_list_records_orders_by_task_id(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path)
    assert store.list_records() == ()

    store.save(_state(task_id="task-b", save_path=tmp_path / "b.bin"))
    store.save(_state(task_id="task-a", save_path=tmp_path / "a.bin"))

    records = store.list_records()
    assert [record.task_id for record in records] == ["task-a", "task-b"]
    assert records[0].name == "file.bin"


# -- corrupt row validation ----------------------------------------------------


def _corrupt_database(root: Path, statement: str, parameters: tuple) -> None:
    with sqlite3.connect(root / "tasks.sqlite3") as connection:
        connection.execute(statement, parameters)


def test_store_drops_invalid_part_rows_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = DownloadTaskStore(tmp_path)
    state = _state(
        save_path=tmp_path / "f.bin",
        parts=[_part_record(0, 0, 3), _part_record(1, 4, 7)],
    )
    store.save(state)
    # Externally corrupted row: actual_size larger than expected_size.
    _corrupt_database(
        tmp_path, "UPDATE download_parts SET actual_size = 99 WHERE part_index = 0", ()
    )

    with caplog.at_level(logging.WARNING, logger="openwopan.tasks.download"):
        loaded = store.load("task-1")
        states = store.load_all()

    assert loaded is not None
    assert [part.index for part in loaded.parts] == [1]
    assert len(states) == 1
    assert [part.index for part in states[0].parts] == [1]
    assert any("download.part_row.skipped" in message for message in caplog.messages)


def test_store_drops_invalid_task_rows_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = DownloadTaskStore(tmp_path)
    store.save(_state(task_id="task-1", save_path=tmp_path / "f.bin"))
    store.save(_state(task_id="task-2", save_path=tmp_path / "g.bin"))
    _corrupt_database(
        tmp_path,
        "UPDATE download_tasks SET file_name = '' WHERE task_id = 'task-1'",
        (),
    )

    with caplog.at_level(logging.WARNING, logger="openwopan.tasks.download"):
        loaded = store.load("task-1")
        states = store.load_all()
        records = store.list_records()

    assert loaded is None
    assert [state.task_id for state in states] == ["task-2"]
    assert [record.task_id for record in records] == ["task-2"]
    assert any("download.task_row.skipped" in message for message in caplog.messages)


# -- legacy JSON migration (R4 / AC2) ------------------------------------------


def test_migration_imports_valid_json_and_deletes_files(tmp_path: Path) -> None:
    root = tmp_path / "store"
    state = _state(
        task_id="legacy-1",
        save_path=root / "out.bin",
        parts=[_part_record(0, 0, 3, mtime_ns=None), _part_record(1, 4, 7, actual_size=2)],
    )
    state.total_bytes = 8
    state.status = "已暂停"
    state.supports_resume = True
    state.bytes_done = 6
    json_path = _write_legacy_json(root, state)

    store = DownloadTaskStore(root)
    loaded = store.load("legacy-1")

    assert loaded is not None
    assert loaded.status == "已暂停"
    assert loaded.total_bytes == 8
    assert loaded.bytes_done == 6
    assert [part.index for part in loaded.parts] == [0, 1]
    assert [part.mtime_ns for part in loaded.parts] == [None, None]
    assert [part.algorithm for part in loaded.parts] == ["md5", "md5"]
    assert not json_path.exists()
    assert _task_row_count(root, "legacy-1") == 1
    assert _part_row_count(root, "legacy-1") == 2


@pytest.mark.parametrize("payload", [b"not json", b"[1, 2]"])
def test_migration_keeps_unparsable_json_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, payload: bytes
) -> None:
    root = tmp_path / "store"
    path = root / "tasks" / "broken.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)

    store = DownloadTaskStore(root)

    assert store.load("broken") is None
    assert path.exists()
    with caplog.at_level(logging.WARNING, logger="openwopan.tasks.download"):
        DownloadTaskStore(root).load("broken")
    assert any("download.task_state.migrate_invalid" in message for message in caplog.messages)


def test_migration_is_idempotent_across_reopen(tmp_path: Path) -> None:
    root = tmp_path / "store"
    state = _state(
        task_id="legacy-1",
        save_path=root / "out.bin",
        parts=[_part_record(0, 0, 3), _part_record(1, 4, 7, actual_size=2)],
    )
    state.bytes_done = 6
    _write_legacy_json(root, state)

    first = DownloadTaskStore(root)
    loaded_first = first.load("legacy-1")
    assert loaded_first is not None and len(loaded_first.parts) == 2
    first.close()

    second = DownloadTaskStore(root)
    loaded_second = second.load("legacy-1")

    assert loaded_second is not None
    assert [part.index for part in loaded_second.parts] == [0, 1]
    assert loaded_second.bytes_done == 6
    assert _task_row_count(root, "legacy-1") == 1
    assert _part_row_count(root, "legacy-1") == 2
    assert not (root / "tasks" / "legacy-1.json").exists()


def test_migration_skips_when_tasks_dir_absent(tmp_path: Path) -> None:
    store = DownloadTaskStore(tmp_path / "store")

    assert store.load("anything") is None
    assert not (tmp_path / "store" / "tasks").exists()


def test_migration_keeps_json_when_database_write_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A real sqlite3.Error during import keeps the JSON file and logs a warning.

    The database is pre-created with the real columns plus one extra NOT NULL
    column and user_version=1, so migrations are skipped, reads keep working,
    and the first import INSERT fails with a genuine sqlite3 error (no mocks).
    """
    root = tmp_path / "store"
    root.mkdir()
    state = _state(task_id="legacy-1", save_path=root / "out.bin")
    json_path = _write_legacy_json(root, state)
    with sqlite3.connect(root / "tasks.sqlite3") as connection:
        connection.execute(
            """
            CREATE TABLE download_tasks (
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
                updated_at REAL NOT NULL DEFAULT 0,
                legacy_lock INTEGER NOT NULL
            )
            """
        )
        connection.execute("PRAGMA user_version = 1")

    store = DownloadTaskStore(root)

    with caplog.at_level(logging.WARNING, logger="openwopan.tasks.download"):
        assert store.load("legacy-1") is None
    assert json_path.exists()
    assert any(
        "download.task_state.migrate_failed" in message for message in caplog.messages
    )


# -- JSON serialization compatibility (used by the migration reader) -----------


def test_dump_and_read_task_state_roundtrip_new_fields(tmp_path: Path) -> None:
    state = _state(
        save_path=tmp_path / "f.bin",
        parts=[
            _part_record(0, 0, 3, mtime_ns=42),
            _part_record(1, 4, 7, actual_size=2, algorithm="md5"),
        ],
    )
    state.expected_sha256 = "c" * 64

    restored = _read_task_state(_dump_task_state(state))

    assert restored is not None
    assert restored.expected_sha256 == "c" * 64
    assert [part.mtime_ns for part in restored.parts] == [42, None]
    assert [part.algorithm for part in restored.parts] == ["md5", "md5"]


def test_read_task_state_defaults_new_fields_for_legacy_payloads() -> None:
    restored = _read_task_state(
        {
            "task_id": "t",
            "file_name": "f",
            "save_path": "/tmp/f",
            "parts": [
                {"index": 0, "start": 0, "end": 3, "expected_size": 4, "actual_size": 4, "md5": "m"}
            ],
        }
    )

    assert restored is not None
    assert restored.expected_sha256 is None
    assert restored.parts[0].mtime_ns is None
    assert restored.parts[0].algorithm == "md5"


def test_read_task_state_rejects_bool_mtime_ns() -> None:
    restored = _read_task_state(
        {
            "task_id": "t",
            "file_name": "f",
            "save_path": "/tmp/f",
            "parts": [
                {
                    "index": 0,
                    "start": 0,
                    "end": 3,
                    "expected_size": 4,
                    "actual_size": 4,
                    "md5": "m",
                    "mtime_ns": True,
                }
            ],
        }
    )

    assert restored is not None
    assert restored.parts[0].mtime_ns is None


def test_task_row_from_values_rejects_wrong_shape() -> None:
    from openwopan.tasks.download import _task_from_values

    assert _task_from_values(("only-one-column",)) is None


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("task_id", ""),
        ("task_id", 5),
        ("file_name", ""),
        ("save_path", ""),
    ],
)
def test_task_row_from_values_rejects_invalid_core_columns(
    column: str, value: object
) -> None:
    from openwopan.tasks.download import _task_from_values

    values: list[object] = [
        "task-1",
        "file.bin",
        "/tmp/f",
        "等待中",
        None,
        8,
        4,
        None,
        2,
        1,
        "",
        1,
        None,
        1.0,
        2.0,
    ]
    columns = (
        "task_id",
        "file_name",
        "save_path",
        "status",
        "download_id",
        "total_bytes",
        "bytes_done",
        "part_size",
        "max_connections",
        "supports_resume",
        "error",
        "version",
        "expected_sha256",
        "created_at",
        "updated_at",
    )
    values[columns.index(column)] = value

    assert _task_from_values(tuple(values)) is None


@pytest.mark.parametrize(
    ("overrides", "valid"),
    [
        ({"part_index": -1}, False),
        ({"start": -1}, False),
        ({"end_offset": 2, "start": 3}, False),
        ({"expected_size": 0}, False),
        ({"actual_size": 0}, False),
        ({"actual_size": 99}, False),
        ({"part_index": True}, False),
        ({"md5": ""}, False),
        ({"task_id": ""}, False),
    ],
)
def test_part_row_from_values_validates_columns(
    overrides: dict[str, object], valid: bool
) -> None:
    from openwopan.tasks.download import _part_row_from_values

    values: dict[str, object] = {
        "task_id": "task-1",
        "part_index": 0,
        "start": 0,
        "end_offset": 3,
        "expected_size": 4,
        "actual_size": 4,
        "md5": "m",
        "algorithm": "md5",
        "mtime_ns": None,
    }
    values.update(overrides)

    row = _part_row_from_values(
        (
            values["task_id"],
            values["part_index"],
            values["start"],
            values["end_offset"],
            values["expected_size"],
            values["actual_size"],
            values["md5"],
            values["algorithm"],
            values["mtime_ns"],
        )
    )

    assert (row is not None) is valid


def test_part_row_from_values_rejects_wrong_shape() -> None:
    from openwopan.tasks.download import _part_row_from_values

    assert _part_row_from_values(("task-1", 0)) is None


def test_part_row_from_values_defaults_empty_algorithm_to_md5() -> None:
    from openwopan.tasks.download import _part_row_from_values

    row = _part_row_from_values(("task-1", 0, 0, 3, 4, 4, "m", "", None))

    assert row is not None
    assert row[1].algorithm == "md5"


def test_task_row_from_values_coerces_non_numeric_clock_columns() -> None:
    from openwopan.tasks.download import _task_from_values

    values: list[object] = [
        "task-1",
        "file.bin",
        "/tmp/f",
        "等待中",
        None,
        8,
        4,
        None,
        2,
        1,
        "",
        1,
        None,
        "not-a-number",
        None,
    ]

    task = _task_from_values(tuple(values))  # type: ignore[arg-type]

    assert task is not None
    assert task.created_at == 0.0
    assert task.updated_at == 0.0


# -- real sqlite3 failure paths (no mocks) -------------------------------------


def _store_with_readonly_task_table(root: Path) -> DownloadTaskStore:
    """Pre-create the DB with ``download_tasks`` as a VIEW over a backing table.

    Reads keep working while every write to the task table fails with a
    genuine sqlite3 error, exercising the store's transaction rollback paths
    without any mocks. user_version=1 skips migrations on open.
    """
    root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(root / "tasks.sqlite3") as connection:
        connection.execute(
            """
            CREATE TABLE tasks_data (
                task_id TEXT PRIMARY KEY,
                file_name TEXT DEFAULT '',
                save_path TEXT DEFAULT '',
                status TEXT DEFAULT '',
                download_id TEXT,
                total_bytes INTEGER,
                bytes_done INTEGER DEFAULT 0,
                part_size INTEGER,
                max_connections INTEGER DEFAULT 1,
                supports_resume INTEGER DEFAULT 0,
                error TEXT DEFAULT '',
                version INTEGER DEFAULT 1,
                expected_sha256 TEXT,
                created_at REAL DEFAULT 0,
                updated_at REAL DEFAULT 0
            )
            """
        )
        connection.execute("CREATE VIEW download_tasks AS SELECT * FROM tasks_data")
        connection.execute(
            """
            CREATE TABLE download_parts (
                task_id TEXT NOT NULL,
                part_index INTEGER NOT NULL,
                start INTEGER NOT NULL,
                end_offset INTEGER NOT NULL,
                expected_size INTEGER NOT NULL,
                actual_size INTEGER NOT NULL,
                md5 TEXT NOT NULL DEFAULT '',
                algorithm TEXT NOT NULL DEFAULT 'md5',
                mtime_ns INTEGER,
                PRIMARY KEY (task_id, part_index)
            )
            """
        )
        connection.execute("PRAGMA user_version = 1")
    return DownloadTaskStore(root)


def _seed_task_row(root: Path, task_id: str) -> None:
    with sqlite3.connect(root / "tasks.sqlite3") as connection:
        connection.execute("INSERT INTO tasks_data (task_id) VALUES (?)", (task_id,))


def test_store_save_rolls_back_when_task_write_fails(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = _store_with_readonly_task_table(root)

    with pytest.raises(sqlite3.OperationalError):
        store.save(_state(save_path=tmp_path / "f.bin", parts=[_part_record(0, 0, 3)]))

    assert _task_row_count(root, "task-1") == 0
    assert _part_row_count(root, "task-1") == 0


def test_store_record_part_rolls_back_failed_refresh(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = _store_with_readonly_task_table(root)
    _seed_task_row(root, "task-1")

    with pytest.raises(sqlite3.OperationalError):
        store.record_part("task-1", _part_record(0, 0, 3))

    # The part upsert is rolled back together with the failed bytes_done refresh.
    assert _part_row_count(root, "task-1") == 0


def test_store_remove_part_record_rolls_back_failed_refresh(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = _store_with_readonly_task_table(root)
    _seed_task_row(root, "task-1")
    with sqlite3.connect(root / "tasks.sqlite3") as connection:
        connection.execute(
            "INSERT INTO download_parts "
            "(task_id, part_index, start, end_offset, expected_size, actual_size, md5) "
            "VALUES ('task-1', 0, 0, 3, 4, 4, 'm')"
        )

    with pytest.raises(sqlite3.OperationalError):
        store.remove_part_record("task-1", 0)

    # The part deletion is rolled back with the failed bytes_done refresh.
    assert _part_row_count(root, "task-1") == 1


def test_store_delete_rolls_back_when_task_write_fails(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = _store_with_readonly_task_table(root)
    _seed_task_row(root, "task-1")

    with pytest.raises(sqlite3.OperationalError):
        store.delete("task-1")

    assert _task_row_count(root, "task-1") == 1


def test_store_open_raises_on_corrupt_database_file(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    (root / "tasks.sqlite3").write_bytes(b"this is not a database")

    store = DownloadTaskStore(root)

    with pytest.raises(sqlite3.DatabaseError):
        store.load("task-1")
