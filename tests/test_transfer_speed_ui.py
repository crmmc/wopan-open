"""Transfer-center speed sampler tests (task 09-27-transfer-speed-smoothing).

Covers the R1-R6 UI contract: progress callbacks never compute speed, the
1-second GUI-thread sampler is the only speed writer, tasks are isolated by
(direction, task_id), direction totals are independent, pause/resume and
restart recovery re-baseline, idle speed decays to zero, terminal/removal
releases estimator state, and the sampler tick updates speed cells without a
table rebuild. The monotonic clock is faked; no event loop is needed because
tests drive ``_sample_speeds`` (the QTimer timeout slot) directly.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QTableWidgetItem

import openwopan.ui.main_window as main_window_module
from openwopan.tasks.transfer_rate import TransferRateEstimator
from openwopan.ui.main_window import (
    TRANSFER_COL_PROGRESS,
    TRANSFER_COL_SPEED,
    MainWindow,
    TransferInterface,
    TransferRecord,
)

# One-second sampling with tau=3s: alpha = 1 - exp(-1/3).
ALPHA_ONE_SECOND = 1.0 - math.exp(-1.0 / 3.0)
DECAY_ONE_SECOND = math.exp(-1.0 / 3.0)


class FakeClock:
    """Injectable monotonic clock for estimator baselines."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def speed_ui(qapp: QApplication) -> tuple[TransferInterface, FakeClock]:
    interface = TransferInterface()
    clock = FakeClock()
    interface._new_speed_estimator = lambda: TransferRateEstimator(monotonic=clock)
    return interface, clock


def _make_record(
    task_id: str, direction: str = "download", **overrides: Any
) -> TransferRecord:
    values: dict[str, Any] = {
        "task_id": task_id,
        "direction": direction,
        "name": f"{task_id}.txt",
        "size": 1_000_000,
        "status": "上传中" if direction == "upload" else "下载中",
    }
    values.update(overrides)
    return TransferRecord(**values)  # type: ignore[arg-type]


def _add_record(transfer: TransferInterface, record: TransferRecord) -> None:
    if record.direction == "upload":
        transfer.add_upload_record(record)
    else:
        transfer.add_download_record(record)


def _speed_text(transfer: TransferInterface, direction: str, task_id: str) -> str:
    table = transfer.upload_table if direction == "upload" else transfer.download_table
    visible = (
        transfer._filtered_upload_records()
        if direction == "upload"
        else transfer._filtered_download_records()
    )
    row = next(
        index for index, record in enumerate(visible) if record.task_id == task_id
    )
    speed_item = table.item(row, TRANSFER_COL_SPEED)
    assert speed_item is not None
    return speed_item.text()


# ---------------------------------------------------------------------------
# R1: progress callbacks never compute speed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_progress_bursts_do_not_create_speed(speed_ui: Any, direction: str) -> None:
    transfer, _ = speed_ui
    _add_record(transfer, _make_record("t-1", direction=direction))

    for byte_count in (64, 4096, 262_144, 5_000_000):  # bursty callback cadence
        transfer.update_record(direction, "t-1", bytes_done=byte_count)

    record = transfer._find_record(direction, "t-1")
    assert record is not None
    assert record.bytes_done == 5_000_000
    assert record.speed_bps == 0.0
    # No estimator may exist until the sampler ticks: callbacks are not samples.
    assert (direction, "t-1") not in transfer._speed_estimators


# ---------------------------------------------------------------------------
# R2/R3: fixed-cadence sampling and EMA smoothing
# ---------------------------------------------------------------------------


def test_first_sample_only_establishes_baseline(speed_ui: Any) -> None:
    transfer, _ = speed_ui
    _add_record(transfer, _make_record("d-1", bytes_done=1_000_000))

    transfer._sample_speeds()

    record = transfer._find_record("download", "d-1")
    assert record is not None
    assert record.speed_bps == 0.0
    assert _speed_text(transfer, "download", "d-1") == "--"
    assert ("download", "d-1") in transfer._speed_estimators


def test_second_sample_reports_smoothed_speed(speed_ui: Any) -> None:
    transfer, clock = speed_ui
    _add_record(transfer, _make_record("d-1"))
    transfer._sample_speeds()

    clock.advance(1.0)
    transfer.update_record("download", "d-1", bytes_done=3_000)
    transfer._sample_speeds()

    record = transfer._find_record("download", "d-1")
    assert record is not None
    expected = ALPHA_ONE_SECOND * 3_000.0
    assert record.speed_bps == pytest.approx(expected, rel=1e-9)
    assert _speed_text(transfer, "download", "d-1") == "850 B/s"


def test_ema_converges_toward_true_rate(speed_ui: Any) -> None:
    transfer, clock = speed_ui
    _add_record(transfer, _make_record("d-1"))
    transfer._sample_speeds()

    speeds: list[float] = []
    record = transfer._find_record("download", "d-1")
    assert record is not None
    for _ in range(12):
        clock.advance(1.0)
        record.bytes_done += 3_000
        transfer._sample_speeds()
        speeds.append(record.speed_bps)

    assert speeds == sorted(speeds)  # monotonic convergence from below
    # After 12 s the EMA has converged within ~2% of the true rate.
    assert speeds[-1] == pytest.approx(3_000.0, rel=0.05)


def test_callback_burst_between_ticks_never_shows_instant_peak(speed_ui: Any) -> None:
    transfer, clock = speed_ui
    _add_record(transfer, _make_record("d-1"))
    transfer._sample_speeds()

    clock.advance(1.0)
    transfer.update_record("download", "d-1", bytes_done=3_000)
    transfer._sample_speeds()
    record = transfer._find_record("download", "d-1")
    assert record is not None
    steady = record.speed_bps

    # A single 10 MB burst lands between two ticks.
    clock.advance(1.0)
    transfer.update_record("download", "d-1", bytes_done=3_000 + 10_000_000)
    transfer._sample_speeds()

    instant = 10_000_000.0
    assert record.speed_bps < instant * 0.35  # smoothed, not the burst peak
    assert record.speed_bps > steady  # ...but the burst does move the estimate


# ---------------------------------------------------------------------------
# R4: task isolation and direction totals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_tasks_in_one_direction_are_isolated(speed_ui: Any, direction: str) -> None:
    transfer, clock = speed_ui
    _add_record(transfer, _make_record("t-fast", direction=direction))
    _add_record(transfer, _make_record("t-idle", direction=direction))
    transfer._sample_speeds()

    fast = transfer._find_record(direction, "t-fast")
    idle = transfer._find_record(direction, "t-idle")
    assert fast is not None and idle is not None
    clock.advance(1.0)
    fast.bytes_done += 9_000
    transfer._sample_speeds()

    assert fast.speed_bps == pytest.approx(ALPHA_ONE_SECOND * 9_000.0, rel=1e-9)
    assert idle.speed_bps == 0.0
    assert (direction, "t-idle") in transfer._speed_estimators
    assert transfer._speed_estimators[(direction, "t-fast")] is not (
        transfer._speed_estimators[(direction, "t-idle")]
    )


def test_direction_totals_are_independent(speed_ui: Any) -> None:
    transfer, clock = speed_ui
    _add_record(transfer, _make_record("u-1", direction="upload"))
    _add_record(transfer, _make_record("d-1"))
    transfer._sample_speeds()

    upload = transfer._find_record("upload", "u-1")
    download = transfer._find_record("download", "d-1")
    assert upload is not None and download is not None
    clock.advance(1.0)
    upload.bytes_done += 6_000
    transfer._sample_speeds()

    assert upload.speed_bps > 0
    assert download.speed_bps == 0.0
    upload_total = transfer.upload_batch_buttons["speed"]
    download_total = transfer.download_batch_buttons["speed"]
    expected = main_window_module._format_speed(upload.speed_bps)
    assert upload_total.text() == f"总速度: {expected}"
    assert download_total.text() == "总速度: --"


def test_total_speed_sums_multiple_tasks_of_same_direction(speed_ui: Any) -> None:
    transfer, clock = speed_ui
    _add_record(transfer, _make_record("d-1"))
    _add_record(transfer, _make_record("d-2"))
    transfer._sample_speeds()

    first = transfer._find_record("download", "d-1")
    second = transfer._find_record("download", "d-2")
    assert first is not None and second is not None
    clock.advance(1.0)
    first.bytes_done += 3_000
    second.bytes_done += 1_000
    transfer._sample_speeds()

    total = transfer.download_batch_buttons["speed"]
    expected = main_window_module._format_speed(first.speed_bps + second.speed_bps)
    assert total.text() == f"总速度: {expected}"


# ---------------------------------------------------------------------------
# R5: lifecycle semantics
# ---------------------------------------------------------------------------


def test_pause_zeroes_speed_and_resume_rebaselines(speed_ui: Any) -> None:
    transfer, clock = speed_ui
    _add_record(transfer, _make_record("d-1"))
    transfer._sample_speeds()
    clock.advance(1.0)
    transfer.update_record("download", "d-1", bytes_done=3_000)
    transfer._sample_speeds()
    record = transfer._find_record("download", "d-1")
    assert record is not None
    assert record.speed_bps > 0

    # Pause: speed drops to zero immediately.
    transfer.update_record("download", "d-1", status="已暂停", can_resume=True)
    assert record.status == "已暂停"
    assert record.speed_bps == 0.0

    # Long paused span; resume, then one second of 3 KB growth.
    clock.advance(600.0)
    transfer.update_record("download", "d-1", status="下载中")
    clock.advance(1.0)
    transfer.update_record("download", "d-1", bytes_done=6_000)
    transfer._sample_speeds()
    # First sample after resume only re-baselines: the paused span is ignored.
    assert record.speed_bps == 0.0

    clock.advance(1.0)
    transfer.update_record("download", "d-1", bytes_done=9_000)
    transfer._sample_speeds()
    assert record.speed_bps == pytest.approx(ALPHA_ONE_SECOND * 3_000.0, rel=1e-9)


def test_idle_speed_decays_toward_zero(speed_ui: Any) -> None:
    transfer, clock = speed_ui
    _add_record(transfer, _make_record("d-1"))
    transfer._sample_speeds()
    clock.advance(1.0)
    transfer.update_record("download", "d-1", bytes_done=9_000)
    transfer._sample_speeds()
    record = transfer._find_record("download", "d-1")
    assert record is not None
    initial = record.speed_bps
    assert initial > 0

    observed: list[float] = []
    for _ in range(20):
        clock.advance(1.0)
        transfer._sample_speeds()  # no new bytes
        observed.append(record.speed_bps)

    assert observed == sorted(observed, reverse=True)  # strictly decaying
    assert observed[-1] < initial * DECAY_ONE_SECOND**19 * 1.01
    assert observed[-1] < initial * 0.01


@pytest.mark.parametrize("terminal_status", ["已完成", "失败", "已取消"])
def test_terminal_status_zeroes_and_releases_estimator(
    speed_ui: Any, terminal_status: str
) -> None:
    transfer, clock = speed_ui
    _add_record(transfer, _make_record("d-1"))
    transfer._sample_speeds()
    clock.advance(1.0)
    transfer.update_record("download", "d-1", bytes_done=3_000)
    transfer._sample_speeds()
    assert ("download", "d-1") in transfer._speed_estimators

    transfer.update_record("download", "d-1", status=terminal_status)

    record = transfer._find_record("download", "d-1")
    assert record is not None
    assert record.speed_bps == 0.0
    assert ("download", "d-1") not in transfer._speed_estimators
    # The sampler stops itself on its next tick once nothing is transferring.
    assert transfer._speed_sampler.isActive() is True
    transfer._sample_speeds()
    assert transfer._speed_sampler.isActive() is False


@pytest.mark.parametrize(
    ("status", "direction"),
    [
        ("等待中", "download"),
        ("校验中", "download"),
        ("合并中", "download"),
        ("创建目录中", "upload"),
        ("已暂停", "upload"),
    ],
)
def test_non_transfer_statuses_are_not_sampled(
    speed_ui: Any, status: str, direction: str
) -> None:
    transfer, _ = speed_ui
    _add_record(transfer, _make_record("x-1", direction=direction, status=status))

    transfer._sample_speeds()

    record = transfer._find_record(direction, "x-1")
    assert record is not None
    assert record.speed_bps == 0.0
    assert (direction, "x-1") not in transfer._speed_estimators
    assert transfer._speed_sampler.isActive() is False


def test_removal_releases_estimator_state(speed_ui: Any) -> None:
    transfer, clock = speed_ui
    _add_record(transfer, _make_record("d-1"))
    transfer._sample_speeds()
    clock.advance(1.0)
    transfer.update_record("download", "d-1", bytes_done=3_000)
    transfer._sample_speeds()
    assert ("download", "d-1") in transfer._speed_estimators

    transfer.remove_records("download", {"d-1"})

    assert ("download", "d-1") not in transfer._speed_estimators


def test_restart_recovery_readds_record_and_rebaselines(speed_ui: Any) -> None:
    transfer, clock = speed_ui
    _add_record(transfer, _make_record("d-1"))
    transfer._sample_speeds()
    clock.advance(1.0)
    transfer.update_record("download", "d-1", bytes_done=3_000)
    transfer._sample_speeds()
    record = transfer._find_record("download", "d-1")
    assert record is not None
    assert record.speed_bps > 0

    # Process restart: the recovered record replaces the old one with a
    # smaller cumulative counter; a fresh estimator must be used.
    recovered = _make_record("d-1", bytes_done=100)
    transfer.add_download_record(recovered)
    assert ("download", "d-1") not in transfer._speed_estimators

    record = transfer._find_record("download", "d-1")
    assert record is recovered
    transfer._sample_speeds()
    assert record.speed_bps == 0.0  # baseline only, no negative-rate artifact

    clock.advance(1.0)
    transfer.update_record("download", "d-1", bytes_done=3_100)
    transfer._sample_speeds()
    assert record.speed_bps == pytest.approx(ALPHA_ONE_SECOND * 3_000.0, rel=1e-9)


# ---------------------------------------------------------------------------
# R6: display updates without table rebuilds + timer lifecycle
# ---------------------------------------------------------------------------


def test_sampler_tick_updates_speed_cell_without_rebuild(
    speed_ui: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    transfer, clock = speed_ui
    _add_record(transfer, _make_record("d-1", bytes_done=0, total_bytes=90_000))
    transfer._sample_speeds()
    render_calls: list[object] = []
    original_render = transfer._render_table
    monkeypatch.setattr(
        transfer,
        "_render_table",
        lambda table, records, direction: (
            render_calls.append(direction), original_render(table, records, direction)
        )[1],
    )

    clock.advance(1.0)
    transfer.update_record("download", "d-1", bytes_done=30_000)
    transfer._sample_speeds()

    assert render_calls == []  # no full-table rebuild from the speed tick
    assert _speed_text(transfer, "download", "d-1") == "8.3 KB/s"
    # Progress-only renders stay behind the 150ms coalescing; flushing shows
    # the cumulative bytes were recorded correctly.
    transfer.flush_progress_render()
    progress_item = transfer.download_table.item(0, TRANSFER_COL_PROGRESS)
    assert progress_item is not None
    assert progress_item.text() == "33% (29.3 KB / 87.9 KB)"


def test_hidden_active_task_is_still_sampled(speed_ui: Any) -> None:
    transfer, clock = speed_ui
    _add_record(transfer, _make_record("u-1", direction="upload"))
    transfer.upload_filter_combo.setCurrentText("已完成")  # hides the active task
    transfer._sample_speeds()

    upload = transfer._find_record("upload", "u-1")
    assert upload is not None
    clock.advance(1.0)
    upload.bytes_done += 6_000
    transfer._sample_speeds()

    assert upload.speed_bps > 0  # sampled even though no row is visible
    total = transfer.upload_batch_buttons["speed"]
    assert total.text() != "总速度: --"


def test_sampler_starts_on_active_status_and_self_stops(speed_ui: Any) -> None:
    transfer, _ = speed_ui
    _add_record(transfer, _make_record("d-1", status="等待中"))
    assert transfer._speed_sampler.isActive() is False

    transfer.update_record("download", "d-1", status="下载中")
    assert transfer._speed_sampler.isActive() is True

    transfer.update_record("download", "d-1", status="已暂停", can_resume=True)
    transfer._sample_speeds()
    assert transfer._speed_sampler.isActive() is False


def test_close_event_stops_speed_sampler(qapp: QApplication) -> None:
    window = MainWindow()
    window.transfer_interface.add_download_record(_make_record("d-1"))
    assert window.transfer_interface._speed_sampler.isActive() is True

    event = QCloseEvent()
    window.closeEvent(event)

    assert event.isAccepted()
    assert window.transfer_interface._speed_sampler.isActive() is False


def test_sampler_skips_rows_with_stale_table_items(speed_ui: Any) -> None:
    transfer, clock = speed_ui
    _add_record(transfer, _make_record("d-1"))
    transfer._sample_speeds()
    clock.advance(1.0)
    transfer.update_record("download", "d-1", bytes_done=3_000)
    # Simulate a stale row whose speed cell belongs to another task.
    stale = QTableWidgetItem("stale")
    stale.setData(Qt.ItemDataRole.UserRole, "other-task")
    transfer.download_table.setItem(0, TRANSFER_COL_SPEED, stale)

    transfer._sample_speeds()

    assert stale.text() == "stale"  # untouched: task_id does not match the row
    record = transfer._find_record("download", "d-1")
    assert record is not None
    assert record.speed_bps > 0  # the estimate itself still advances


def test_sampler_interval_is_one_second(speed_ui: Any) -> None:
    transfer, _ = speed_ui
    assert transfer.SPEED_SAMPLE_INTERVAL_MS == 1000
    assert transfer._speed_sampler.interval() == 1000
