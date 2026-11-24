import asyncio
import time

from vinylpilot.recorder import Recorder

CHUNK = b"\x10\x00\x10\x00" * 960  # 20 ms stereo


async def test_splits_tracks_and_names_from_recognition(tmp_path, monkeypatch):
    rec = Recorder(tmp_path, 48000, 0.02, gap_db=-55, gap_min_s=0.1, min_track_s=0.2, fmt="wav",
                   min_keep_s=0.1)
    monkeypatch.setattr(rec, "target_ready", lambda: (True, "ready"))
    rec.start()
    for _ in range(15):                      # track 1: 0.3 s music
        rec.feed(CHUNK, -30.0, True)
    rec.set_track_meta({"title": "Teardrop", "artist": "Massive Attack", "sampled_from": time.time()})
    for _ in range(10):                      # 0.2 s gap
        rec.feed(CHUNK, -70.0, True)
    for _ in range(15):                      # track 2
        rec.feed(CHUNK, -30.0, True)
    rec.stop()
    for _ in range(100):
        await asyncio.sleep(0.05)
        if len(rec.finished) == 2:
            break
    names = sorted(p.name for p in next(tmp_path.iterdir()).iterdir())
    assert names == ["01 - Massive Attack - Teardrop.wav", "02 - Track 02.wav"], names


async def test_refuses_without_mounted_share(tmp_path):
    rec = Recorder(tmp_path, 48000, 0.02)
    try:
        rec.start()
    except RuntimeError as err:
        assert "not_mounted" in str(err)
    else:
        raise AssertionError("expected RuntimeError")
