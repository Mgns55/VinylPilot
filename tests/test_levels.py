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
