"""Per-task transfer speed estimation with time-aware EMA smoothing.

Pure logic module: no Qt, no I/O, no persistence. The transfer center drives
one estimator per task on a fixed sampling cadence (about 1 second) with a
monotonic clock and owns estimator lifecycles (create on task start, drop on
terminal/remove, ``reset`` on pause/resume).
"""

from __future__ import annotations

import math
import time
from typing import Protocol

DEFAULT_SPEED_TAU_SECONDS = 3.0


class MonotonicClock(Protocol):
    """Zero-argument callable returning monotonic seconds."""

    def __call__(self) -> float: ...


class TransferRateEstimator:
    """Smoothed byte-rate estimator for one task's cumulative byte counter.

    Sampling contract (design: 传输速度计算优化 §3):

    - The first sample only establishes the baseline; the smoothed speed stays
      0 until a second sample arrives, so an insufficient sample count never
      produces a fake speed.
    - A byte-count regression (retry/resume rewound the counter) re-baselines
      instead of producing a negative rate; the previously smoothed value is
      kept and decays naturally if no new bytes arrive.
    - Zero byte growth feeds a zero instantaneous rate into the EMA, so the
      smoothed speed decays exponentially toward 0.
    - A non-positive sampling interval carries no observable information and
      is ignored (the next sample covers the full span).

    ``sample`` always returns the current smoothed speed in bytes per second.
    """

    def __init__(
        self,
        *,
        tau_seconds: float = DEFAULT_SPEED_TAU_SECONDS,
        monotonic: MonotonicClock = time.monotonic,
    ) -> None:
        if tau_seconds <= 0:
            raise ValueError("tau_seconds must be positive")
        self._tau_seconds = tau_seconds
        self._monotonic = monotonic
        self._baseline_bytes = 0
        self._baseline_at = 0.0
        self._established = False
        self._speed = 0.0

    @property
    def speed(self) -> float:
        """Current smoothed speed in bytes per second (0 before baseline)."""
        return self._speed

    def sample(self, bytes_done: int) -> float:
        """Feed one cumulative byte count; return the smoothed speed."""
        now = self._monotonic()
        if not self._established:
            self._baseline_bytes = bytes_done
            self._baseline_at = now
            self._established = True
            return self._speed
        if bytes_done < self._baseline_bytes:
            self._baseline_bytes = bytes_done
            self._baseline_at = now
            return self._speed
        elapsed = now - self._baseline_at
        if elapsed <= 0:
            return self._speed
        instant = (bytes_done - self._baseline_bytes) / elapsed
        alpha = 1.0 - math.exp(-elapsed / self._tau_seconds)
        self._speed = alpha * instant + (1.0 - alpha) * self._speed
        self._baseline_bytes = bytes_done
        self._baseline_at = now
        return self._speed

    def reset(self) -> None:
        """Drop baseline and smoothed speed for pause/resume/terminal states."""
        self._baseline_bytes = 0
        self._baseline_at = 0.0
        self._established = False
        self._speed = 0.0
