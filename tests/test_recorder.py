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
    rec.set_track_meta({"title": "Teardrop", "artist": "Massive Attack", "album": "Mezzanine",
                        "sampled_from": time.time()})
    for _ in range(10):                      # 0.2 s gap
        rec.feed(CHUNK, -70.0, True)
    for _ in range(15):                      # track 2 (same album)
        rec.feed(CHUNK, -30.0, True)
    rec.set_track_meta({"title": "Inertia Creeps", "artist": "Massive Attack", "album": "Mezzanine",
                        "sampled_from": time.time()})
    for _ in range(10):
        rec.feed(CHUNK, -70.0, True)
    for _ in range(15):                      # track 3: not recognised
        rec.feed(CHUNK, -30.0, True)
    rec.stop()
    for _ in range(100):
        await asyncio.sleep(0.05)
        if len(rec.finished) == 3:
            break
    await asyncio.sleep(1.5)  # tmp cleanup
    files = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.wav"))
    assert files[:2] == ["Massive Attack/Mezzanine/01 - Teardrop.wav",
                         "Massive Attack/Mezzanine/02 - Inertia Creeps.wav"], files
    assert files[2].startswith("Unbekannt/Vinyl ") and files[2].endswith("/01 - Track 01.wav"), files
    assert not (tmp_path / ".vinylpilot-tmp").exists() or not any((tmp_path / ".vinylpilot-tmp").iterdir())


async def test_refuses_without_mounted_share(tmp_path):
    rec = Recorder(tmp_path, 48000, 0.02)
    try:
        rec.start()
    except RuntimeError as err:
        assert "not_mounted" in str(err)
    else:
        raise AssertionError("expected RuntimeError")
