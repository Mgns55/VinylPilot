import numpy as np

from vinylpilot.levels import LevelMeter, SignalDetector


def _pcm(amplitude: float, frames: int = 960) -> bytes:
    t = np.arange(frames) / 48000
    wave = (amplitude * 32767 * np.sin(2 * np.pi * 1000 * t)).astype("<i2")
    return np.column_stack([wave, wave]).tobytes()


def test_sine_rms_is_minus_3db_of_peak():
    levels = LevelMeter(1).process(_pcm(0.5))
    assert abs(levels.peak_left - (-6.02)) < 0.1
    assert abs(levels.rms_left - (-9.03)) < 0.2


def test_silence_is_floor():
    levels = LevelMeter(1).process(bytes(3840))
    assert levels.rms_left == -120.0


def test_detector_activation_and_hold():
    d = SignalDetector(threshold_db=-65, activation_s=1.0, hold_s=10.0)
    changes = [d.update(-40, 0.02) for _ in range(49)]
    assert not any(changes) and not d.present
    assert d.update(-40, 0.02) and d.present  # 1.0 s reached
    # a 5 s quiet gap between tracks must not stop playback
    assert not any(d.update(-75, 0.02) for _ in range(250))
    assert d.present
    d.update(-40, 0.02)  # music resumes, resets hold
    for _ in range(499):
        assert not d.update(-75, 0.02)
    assert d.update(-75, 0.02) and not d.present  # 10 s of silence


def test_reset_forces_absent():
    d = SignalDetector(threshold_db=-65, activation_s=0.02, hold_s=10.0)
    assert d.update(-40, 0.02) and d.present
    assert d.reset() and not d.present
    assert not d.reset()
    assert d.update(-40, 0.02) and d.present  # a fresh edge after the device returns


def test_gap_tracker_finds_pauses_above_fixed_floor():
    from vinylpilot.levels import GapTracker
    g = GapTracker(0.02, floor_db=-55.0, drop_db=20.0, min_s=1.5)
    ended = [g.update(-30.0) for _ in range(1000)]       # 20 s music around -30 dBFS
    assert not any(ended)
    assert [g.update(-52.0) for _ in range(100)] == [False] * 100   # 2 s groove noise at -52
    assert g.update(-30.0) is True                       # counted as a gap (threshold -50)
    assert [g.update(-45.0) for _ in range(40)] == [False] * 40     # quiet bar, not 20 dB down
    assert g.update(-30.0) is False


def test_gap_tracker_ignores_short_dips():
    from vinylpilot.levels import GapTracker
    g = GapTracker(0.02, min_s=1.5)
    for _ in range(500):
        g.update(-30.0)
    for _ in range(50):                                  # 1 s only
        g.update(-70.0)
    assert g.update(-30.0) is False
