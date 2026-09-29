"""Tests for the pure-logic per-task transfer speed estimator."""

from __future__ import annotations

import math

import pytest

from openwopan.tasks.transfer_rate import TransferRateEstimator

TAU = 3.0


class _FakeClock:
    """Injected monotonic clock; tests never sleep."""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _estimator(clock: _FakeClock) -> TransferRateEstimator:
    return TransferRateEstimator(tau_seconds=TAU, monotonic=clock)


def test_first_sample_only_establishes_baseline() -> None:
    clock = _FakeClock()
    estimator = _estimator(clock)

    assert estimator.sample(4096) == 0.0
    assert estimator.speed == 0.0


@pytest.mark.parametrize(
    ("elapsed", "delta", "expected"),
    [
        (1.0, 1000, 1000 * (1 - math.exp(-1.0 / TAU))),
        (3.0, 3000, (3000 / 3.0) * (1 - math.exp(-1.0))),
        (0.5, 500, (500 / 0.5) * (1 - math.exp(-0.5 / TAU))),
        (6.0, 6000, (6000 / 6.0) * (1 - math.exp(-2.0))),
    ],
    ids=["one-second", "one-tau", "half-second", "two-tau"],
)
def test_normal_increment_follows_time_aware_ema(
    elapsed: float, delta: int, expected: float
) -> None:
    clock = _FakeClock()
    estimator = _estimator(clock)

    assert estimator.sample(0) == 0.0
    clock.advance(elapsed)
    speed = estimator.sample(delta)

    assert speed == pytest.approx(expected)
    assert estimator.speed == pytest.approx(expected)


def test_smoothed_speed_converges_toward_steady_rate() -> None:
    clock = _FakeClock()
    estimator = _estimator(clock)
    estimator.sample(0)

    previous = 0.0
    for step in range(1, 11):
        clock.advance(1.0)
        speed = estimator.sample(step * 1000)
        # 每一步都向瞬时速率收敛且不会越过它（上升序列）
        assert previous < speed < 1000.0
        previous = speed

    # 10 秒后收敛到 r * (1 - exp(-n*t/tau))
    assert previous == pytest.approx(1000 * (1 - math.exp(-10.0 / TAU)), rel=1e-9)


def test_short_burst_does_not_become_display_value() -> None:
    clock = _FakeClock()
    estimator = _estimator(clock)
    estimator.sample(0)
    for step in range(1, 6):
        clock.advance(1.0)
        estimator.sample(step * 1000)
    steady_speed = estimator.speed

    burst_instant = 100000.0
    clock.advance(1.0)
    speed = estimator.sample(5000 + int(burst_instant))  # one 100 kB/s burst

    alpha = 1 - math.exp(-1.0 / TAU)
    assert speed == pytest.approx(alpha * burst_instant + (1 - alpha) * steady_speed)
    assert speed < burst_instant


@pytest.mark.parametrize("steps", [1, 3, 10], ids=["one", "three", "ten"])
def test_no_progress_decays_toward_zero(steps: int) -> None:
    clock = _FakeClock()
    estimator = _estimator(clock)
    estimator.sample(0)
    for step in range(1, 6):
        clock.advance(1.0)
        estimator.sample(step * 1000)
    base_speed = estimator.speed

    for _ in range(steps):
        clock.advance(1.0)
        estimator.sample(5 * 1000)

    assert estimator.speed == pytest.approx(base_speed * math.exp(-steps / TAU), rel=1e-9)


def test_sustained_no_progress_decays_practically_to_zero() -> None:
    clock = _FakeClock()
    estimator = _estimator(clock)
    estimator.sample(0)
    for step in range(1, 6):
        clock.advance(1.0)
        estimator.sample(step * 1000)

    for _ in range(40):
        clock.advance(1.0)
        estimator.sample(5 * 1000)

    assert estimator.speed < 0.01


def test_byte_regression_rebaselines_without_negative_rate() -> None:
    clock = _FakeClock()
    estimator = _estimator(clock)
    estimator.sample(0)
    clock.advance(1.0)
    estimator.sample(3000)
    speed_before = estimator.speed
    assert speed_before > 0.0

    # 计数回退（重试/恢复）：只重置基准，不产生负速率
    regression_speed = estimator.sample(1000)
    assert regression_speed == pytest.approx(speed_before)

    clock.advance(1.0)
    speed_after = estimator.sample(2000)
    decay = math.exp(-1.0 / TAU)
    alpha = 1 - decay
    # 新基准下的瞬时速率 1000 B/s 与回退前平滑速度的 EMA 混合
    assert speed_after == pytest.approx(alpha * 1000 + decay * speed_before)


def test_non_positive_elapsed_sample_is_ignored() -> None:
    clock = _FakeClock()
    estimator = _estimator(clock)
    estimator.sample(0)
    clock.advance(1.0)
    estimator.sample(1000)
    speed_before = estimator.speed

    # 时钟未前进：采样无可观测信息，速度与基准都不变
    assert estimator.sample(2000) == pytest.approx(speed_before)
    assert estimator.sample(3000) == pytest.approx(speed_before)

    clock.advance(1.0)
    speed = estimator.sample(4000)  # 覆盖从上次基准起的完整区间
    decay = math.exp(-1.0 / TAU)
    alpha = 1 - decay
    # 忽略样本期间的字节并入下一次可观测区间：瞬时速率 (4000-1000)/1
    assert speed == pytest.approx(alpha * 3000 + decay * speed_before)


def test_reset_reestablishes_baseline_without_old_speed() -> None:
    clock = _FakeClock()
    estimator = _estimator(clock)
    estimator.sample(0)
    clock.advance(1.0)
    estimator.sample(3000)
    assert estimator.speed > 0.0

    estimator.reset()

    assert estimator.speed == 0.0
    assert estimator.sample(0) == 0.0  # 首采样语义重新生效
    clock.advance(1.0)
    speed = estimator.sample(1000)
    assert speed == pytest.approx(1000 * (1 - math.exp(-1.0 / TAU)))


@pytest.mark.parametrize("tau", [0, -1.0], ids=["zero", "negative"])
def test_invalid_tau_is_rejected(tau: float) -> None:
    with pytest.raises(ValueError, match="tau_seconds"):
        TransferRateEstimator(tau_seconds=tau)


def test_default_clock_is_monotonic() -> None:
    estimator = TransferRateEstimator()

    assert estimator.sample(0) == 0.0
    assert estimator.sample(0) == 0.0
    assert estimator.speed == 0.0
