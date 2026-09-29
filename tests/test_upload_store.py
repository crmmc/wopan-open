"""Tests for the resumable-upload persistence layer in tasks/upload.py."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from openwopan.tasks.upload import (
    UPLOAD_SESSION_MAX_AGE_SECONDS,
    UploadPartRecord,
    UploadTaskState,
    UploadTaskStore,
    make_upload_task_id,
)


def _make_state(task_id: str = "task-1", total_parts: int = 3) -> UploadTaskState:
    return UploadTaskState(
        task_id=task_id,
        file_name="report.bin",
        local_path=Path("/tmp/report.bin"),
        parent_id="parent-1",
        upload_name=None,
        file_size=1024,
        file_mtime=1_700_000_000.0,
        part_size=512,
        total_parts=total_parts,
        unique_id="1730000000000",
        batch_no="20260926101010",
        completed_indexes=[3, 1, 2, 1],
    )


@pytest.fixture
def store(tmp_path: Path) -> UploadTaskStore:
    return UploadTaskStore(tmp_path / "uploads")


@pytest.fixture
def saved_state(store: UploadTaskStore) -> Iterator[UploadTaskState]:
    state = _make_state()
    store.save(state)
    yield state


def test_upload_session_max_age_matches_24h() -> None:
    assert UPLOAD_SESSION_MAX_AGE_SECONDS == 24 * 3600


def test_upload_part_record_holds_index() -> None:
    assert UploadPartRecord(index=2).index == 2


@pytest.mark.parametrize(
    ("parent_id", "upload_name"),
    [("parent-1", None), ("parent-1", "renamed.bin"), ("parent-2", None)],
)
def test_make_upload_task_id_is_stable_and_target_bound(
    tmp_path: Path, parent_id: str, upload_name: str | None
) -> None:
    local_path = tmp_path / "report.bin"
    local_path.write_bytes(b"x")

    first = make_upload_task_id(parent_id, local_path, upload_name)
    second = make_upload_task_id(parent_id, local_path, upload_name)

    assert first == second
    assert len(first) == 24
    assert make_upload_task_id(parent_id, tmp_path / "other.bin", upload_name) != first


def test_make_upload_task_id_ignores_path_aliasing(tmp_path: Path) -> None:
    local_path = tmp_path / "report.bin"
    local_path.write_bytes(b"x")

    direct = make_upload_task_id("parent-1", local_path, None)
    aliased = make_upload_task_id("parent-1", tmp_path / "." / "report.bin", None)

    assert direct == aliased


def test_store_save_load_roundtrip(store: UploadTaskStore) -> None:
    state = _make_state()
    store.save(state)

    loaded = store.load("task-1")

    assert loaded is not None
    assert loaded.task_id == "task-1"
    assert loaded.file_name == "report.bin"
    assert loaded.parent_id == "parent-1"
    assert loaded.file_size == 1024
    assert loaded.part_size == 512
    assert loaded.total_parts == 3
    assert loaded.unique_id == "1730000000000"
    assert loaded.batch_no == "20260926101010"
    assert loaded.completed_indexes == [1, 2, 3]
    assert loaded.updated_at >= loaded.created_at


def test_store_load_missing_returns_none(store: UploadTaskStore) -> None:
    assert store.load("missing") is None


def test_store_load_all_returns_empty_without_directory(
    store: UploadTaskStore,
) -> None:
    assert store.load_all() == []


def test_store_load_all_sorts_by_creation(store: UploadTaskStore) -> None:
    first = _make_state("task-1", total_parts=1)
    first.created_at = 200.0
    second = _make_state("task-2", total_parts=1)
    second.created_at = 100.0
    store.save(first)
    store.save(second)

    states = store.load_all()

    assert [state.task_id for state in states] == ["task-2", "task-1"]


def test_store_update_applies_and_persists_change(
    store: UploadTaskStore, saved_state: UploadTaskState
) -> None:
    updated = store.update(
        "task-1",
        lambda state: _record_upload_part(state, 1, "fid-1"),
    )

    assert updated.completed_indexes == [1, 2, 3]
    assert updated.fid == "fid-1"
    reloaded = store.load("task-1")
    assert reloaded is not None
    assert reloaded.completed_indexes == [1, 2, 3]
    assert reloaded.fid == "fid-1"


def _record_upload_part(state: UploadTaskState, part_index: int, fid: str) -> None:
    if part_index not in state.completed_indexes:
        state.completed_indexes.append(part_index)
    if fid and not state.fid:
        state.fid = fid


def test_store_update_unknown_task_raises_key_error(store: UploadTaskStore) -> None:
    with pytest.raises(KeyError):
        store.update("ghost", lambda state: None)


def test_store_delete_removes_metadata(store: UploadTaskStore) -> None:
    state = _make_state()
    store.save(state)
    assert store.load("task-1") is not None

    store.delete("task-1")

    assert store.load("task-1") is None


def test_store_delete_is_idempotent(store: UploadTaskStore) -> None:
    store.delete("never-existed")


def test_store_update_keeps_concurrent_part_writes(store: UploadTaskStore) -> None:
    state = _make_state(total_parts=12)
    state.completed_indexes = []
    store.save(state)
    barrier = threading.Barrier(6)

    def record_part(index: int) -> None:
        barrier.wait()
        store.update("task-1", lambda s: _record_upload_part(s, index, ""))

    threads = [threading.Thread(target=record_part, args=(index,)) for index in range(1, 7)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    loaded = store.load("task-1")
    assert loaded is not None
    assert loaded.completed_indexes == [1, 2, 3, 4, 5, 6]


@pytest.mark.parametrize(
    ("raw_indexes", "expected"),
    [
        ([3, 1, 2, 1], [1, 2, 3]),
        ([0, 4, 99], []),
        ([], []),
        (["2", True, 2.0], []),
    ],
)
def test_store_normalizes_completed_indexes_on_save(
    store: UploadTaskStore, raw_indexes: list[object], expected: list[int]
) -> None:
    state = _make_state(total_parts=3)
    state.completed_indexes = raw_indexes  # type: ignore[assignment]
    store.save(state)

    loaded = store.load("task-1")

    assert loaded is not None
    assert loaded.completed_indexes == expected


def test_store_loads_stale_tmp_file_without_effect(
    store: UploadTaskStore, saved_state: UploadTaskState
) -> None:
    tmp_path = store.task_path("task-1").with_suffix(".json.tmp")
    tmp_path.write_text("{ corrupt", encoding="utf-8")

    loaded = store.load("task-1")

    assert loaded is not None
    assert loaded.task_id == saved_state.task_id


@pytest.mark.parametrize(
    "raw",
    ["{ not json", "[1, 2]", '{"task_id": "task-1"}'],
)
def test_store_tolerates_corrupt_metadata(
    store: UploadTaskStore,
    caplog: pytest.LogCaptureFixture,
    raw: str,
) -> None:
    path = store.task_path("task-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")

    with caplog.at_level("WARNING", logger="openwopan.tasks.upload"):
        loaded = store.load("task-1")

    assert loaded is None
    if raw == "{ not json":
        assert any("upload.task_state.invalid" in record.getMessage() for record in caplog.records)


def test_store_read_filters_out_of_range_indexes(tmp_path: Path) -> None:
    store = UploadTaskStore(tmp_path / "uploads")
    state = _make_state(total_parts=3)
    store.save(state)
    # 直接改写落盘 JSON，模拟旧版本或被外部改坏的分片列表
    path = store.task_path("task-1")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["completed_indexes"] = [0, 2, 9, 2]
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = store.load("task-1")

    assert loaded is not None
    assert loaded.completed_indexes == [2]


@pytest.mark.parametrize(
    ("raw", "expected_status"),
    [("失败", "失败"), ("已完成", "已完成"), ("进行中", "进行中"), ("乱写", "进行中")],
)
def test_store_read_normalizes_status(
    tmp_path: Path, raw: str, expected_status: str
) -> None:
    store = UploadTaskStore(tmp_path / "uploads")
    state = _make_state(total_parts=1)
    store.save(state)
    path = store.task_path("task-1")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = raw
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = store.load("task-1")

    assert loaded is not None
    assert loaded.status == expected_status


@pytest.mark.parametrize(
    ("field", "raw_value", "expected"),
    [
        ("part_size", 0, 1),
        ("part_size", "bad", 1),
        ("total_parts", -3, 1),
        ("file_size", "x", 0),
        ("file_mtime", "x", 0.0),
    ],
)
def test_store_read_tolerates_invalid_numbers(
    tmp_path: Path, field: str, raw_value: object, expected: int
) -> None:
    store = UploadTaskStore(tmp_path / "uploads")
    state = _make_state(total_parts=1)
    store.save(state)
    path = store.task_path("task-1")
    data = json.loads(path.read_text(encoding="utf-8"))
    data[field] = raw_value
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = store.load("task-1")

    assert loaded is not None
    assert getattr(loaded, field) == expected


@pytest.mark.parametrize(
    "field", ["task_id", "file_name", "local_path", "parent_id", "unique_id", "batch_no"]
)
def test_store_rejects_state_missing_required_identity(
    tmp_path: Path, field: str
) -> None:
    store = UploadTaskStore(tmp_path / "uploads")
    state = _make_state(total_parts=1)
    store.save(state)
    path = store.task_path("task-1")
    data = json.loads(path.read_text(encoding="utf-8"))
    data[field] = ""
    path.write_text(json.dumps(data), encoding="utf-8")

    assert store.load("task-1") is None


def test_store_exposes_root_path(tmp_path: Path) -> None:
    store = UploadTaskStore(tmp_path / "uploads")

    assert store.root_path == tmp_path / "uploads"


def test_store_load_all_skips_corrupt_file_among_valid_ones(
    store: UploadTaskStore,
) -> None:
    state = _make_state("task-1", total_parts=1)
    store.save(state)
    corrupt = store.task_path("task-2")
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{ broken", encoding="utf-8")

    states = store.load_all()

    assert [loaded.task_id for loaded in states] == ["task-1"]


def test_store_roundtrip_preserves_optional_upload_name(
    store: UploadTaskStore,
) -> None:
    state = _make_state(total_parts=1)
    state.upload_name = "renamed.bin"
    state.file_mtime = 12.5
    store.save(state)

    loaded = store.load("task-1")

    assert loaded is not None
    assert loaded.upload_name == "renamed.bin"
    assert loaded.file_mtime == 12.5


@pytest.mark.parametrize(
    ("raw_indexes", "expected"),
    [
        ("1,2", []),
        ({"1": 1}, []),
        (["1", None, 2], [2]),
        ([1.0, 2], [2]),
    ],
    ids=["string", "object", "mixed-items", "float-items"],
)
def test_store_read_ignores_invalid_completed_indexes(
    tmp_path: Path, raw_indexes: object, expected: list[int]
) -> None:
    store = UploadTaskStore(tmp_path / "uploads")
    state = _make_state(total_parts=3)
    store.save(state)
    path = store.task_path("task-1")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["completed_indexes"] = raw_indexes
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = store.load("task-1")

    assert loaded is not None
    assert loaded.completed_indexes == expected
