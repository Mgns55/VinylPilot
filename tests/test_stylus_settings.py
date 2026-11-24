from vinylpilot import settings as s
from vinylpilot.stylus import StylusCounter


def test_settings_validate_clamp_and_persist(tmp_path):
    cfg = s.Settings()
    changed = cfg.update({"threshold_db": -200, "recognize": 1, "recording_format": "mp3", "bogus": 1})
    assert cfg.threshold_db == -90.0 and cfg.recognize is True and cfg.recording_format == "flac"
    assert set(changed) == {"threshold_db", "recognize"}
    s.save(tmp_path / "settings.json", cfg)
    again = s.load(tmp_path / "settings.json", s.Settings())
    assert again.threshold_db == -90.0 and again.recognize is True


def test_stylus_counter_persists_and_resets(tmp_path):
    c = StylusCounter(tmp_path / "stylus.json")
    c.add(3600)
    c.save()
    assert StylusCounter(tmp_path / "stylus.json").hours == 1.0
    c.reset()
    assert StylusCounter(tmp_path / "stylus.json").hours == 0.0
