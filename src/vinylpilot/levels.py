"""Audio level measurement and record-playing detection."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

SILENCE_DB = -120.0
_EPS = 1e-6  # float accumulation tolerance


def _to_db(mean_square: float) -> float:
    if mean_square <= 0.0:
        return SILENCE_DB
    return max(SILENCE_DB, 10.0 * math.log10(mean_square))


@dataclass(frozen=True)
class Levels:
    """Per-chunk levels in dBFS."""

    rms_left: float
    rms_right: float
    peak_left: float
    peak_right: float


class LevelMeter:
    """Computes per-chunk levels and a sliding-window RMS for S16_LE stereo PCM."""

    def __init__(self, window_chunks: int) -> None:
        self._window: deque[float] = deque(maxlen=max(1, window_chunks))
        self.last: Levels | None = None

    def process(self, pcm: bytes) -> Levels:
        samples = np.frombuffer(pcm, dtype="<i2").reshape(-1, 2).astype(np.float32) / 32768.0
        square = samples * samples
        ms_left, ms_right = (float(v) for v in square.mean(axis=0))
        peak_left, peak_right = (float(v) for v in np.abs(samples).max(axis=0))
        self._window.append((ms_left + ms_right) / 2.0)
        self.last = Levels(
            rms_left=_to_db(ms_left),
            rms_right=_to_db(ms_right),
            peak_left=_to_db(peak_left * peak_left),
            peak_right=_to_db(peak_right * peak_right),
        )
        return self.last

    @property
    def window_rms_db(self) -> float:
        """RMS over the sliding window, both channels combined."""
        if not self._window:
            return SILENCE_DB
        return _to_db(sum(self._window) / len(self._window))


class SignalDetector:
    """Hysteresis on the windowed RMS: a record is playing or not.

    - Becomes *present* after the level stays above ``threshold_db`` for ``activation_s``.
    - Becomes *absent* after the level stays below it for ``hold_s``, so quiet passages
      and gaps between tracks do not end playback.
    """

    def __init__(self, threshold_db: float, activation_s: float, hold_s: float) -> None:
        self.threshold_db = threshold_db
        self.activation_s = activation_s
        self.hold_s = hold_s
        self.present = False
        self._above_s = 0.0
        self._below_s = 0.0

    def reset(self) -> bool:
        """Force 'absent' (e.g. the input device vanished); return True if it changed."""
        changed = self.present
        self.present = False
        self._above_s = self._below_s = 0.0
        return changed

    def update(self, level_db: float, duration_s: float) -> bool:
        """Feed one measurement; return True if the state changed."""
        if level_db >= self.threshold_db:
            self._above_s += duration_s
            self._below_s = 0.0
            if not self.present and self._above_s >= self.activation_s - _EPS:
                self.present = True
                return True
        else:
            self._below_s += duration_s
            self._above_s = 0.0
            if self.present and self._below_s >= self.hold_s - _EPS:
                self.present = False
                return True
        return False


class GapTracker:
    """Finds the pauses between tracks, relative to the music's own loudness.

    A fixed threshold fails on real vinyl: the run-in groove between tracks carries
    surface noise that can sit above -55 dBFS. So a pause is a stretch either below
    ``floor_db`` or at least ``drop_db`` below the running music level, whichever
    threshold is higher. ``update`` returns True once, when music resumes after a
    pause of at least ``min_s``.
    """

    def __init__(self, chunk_s: float, floor_db: float = -55.0, drop_db: float = 20.0,
                 min_s: float = 1.5, tau_s: float = 20.0) -> None:
        self.chunk_s = chunk_s
        self.floor_db = floor_db
        self.drop_db = drop_db
        self.min_s = min_s
        self._alpha = min(1.0, chunk_s / tau_s)
        self.music_db: float | None = None
        self.quiet_s = 0.0

    @property
    def threshold_db(self) -> float:
        if self.music_db is None:
            return self.floor_db
        return max(self.floor_db, self.music_db - self.drop_db)

    def reset(self) -> None:
        self.music_db = None
        self.quiet_s = 0.0

    def update(self, window_db: float) -> bool:
        if window_db < self.threshold_db:
            self.quiet_s += self.chunk_s
            return False
        # music: follow its level slowly (pauses never pull it down)
        self.music_db = window_db if self.music_db is None else (
            self.music_db + self._alpha * (window_db - self.music_db))
        ended = self.quiet_s >= self.min_s - _EPS
        self.quiet_s = 0.0
        return ended
