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
    leftovers = [p.name for p in (tmp_path / ".vinylpilot-tmp").iterdir()]
    assert leftovers == [".ndignore"], leftovers


async def test_refuses_without_mounted_share(tmp_path):
    rec = Recorder(tmp_path, 48000, 0.02)
    try:
        rec.start()
    except RuntimeError as err:
        assert "not_mounted" in str(err)
    else:
        raise AssertionError("expected RuntimeError")


def _meta(title, album="Mezzanine", artist="Massive Attack"):
    return {"title": title, "artist": artist, "album": album, "sampled_from": time.time()}


def _play(rec, n=15, db=-30.0):
    for _ in range(n):
        rec.feed(CHUNK, db, True)


async def _settle(rec):
    for _ in range(100):
        await asyncio.sleep(0.05)
        if not rec._jobs:
            return


async def test_next_album_mode_stops_at_new_album_and_skips_duplicates(tmp_path, monkeypatch):
    # an earlier session already digitised "Teardrop" (folder name with a remaster note)
    old = tmp_path / "Massive Attack" / "Mezzanine (Remastered)"
    old.mkdir(parents=True)
    (old / "01 - Teardrop.flac").write_bytes(b"x")

    rec = Recorder(tmp_path, 48000, 0.02, gap_db=-55, gap_min_s=0.1, min_track_s=0.2, fmt="wav",
                   min_keep_s=0.1)
    monkeypatch.setattr(rec, "target_ready", lambda: (True, "ready"))
    rec.start("album")
    _play(rec)
    rec.set_track_meta(_meta("Teardrop"))            # duplicate -> skipped
    _play(rec, 10, -70.0)
    _play(rec)
    rec.set_track_meta(_meta("Inertia Creeps"))      # new -> kept, same folder, number 02
    _play(rec, 10, -70.0)
    _play(rec)
    rec.set_track_meta(_meta("Paranoid Android", album="OK Computer", artist="Radiohead"))
    assert not rec.active and rec.stop_reason == "album_done"
    await _settle(rec)
    files = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.*") if ".vinylpilot-tmp" not in str(p))
    assert files == ["Massive Attack/Mezzanine (Remastered)/01 - Teardrop.flac",
                     "Massive Attack/Mezzanine (Remastered)/02 - Inertia Creeps.wav"], files
    assert rec.skipped == ["Massive Attack/Mezzanine (Remastered)/01 - Teardrop.flac"]


async def test_album_mode_ends_after_long_silence(tmp_path, monkeypatch):
    rec = Recorder(tmp_path, 48000, 0.02, min_keep_s=0.1, fmt="wav", album_end_silence_s=0.2)
    monkeypatch.setattr(rec, "target_ready", lambda: (True, "ready"))
    rec.start("album")
    _play(rec)
    for _ in range(15):
        rec.feed(CHUNK, -90.0, False)
    assert not rec.active and rec.stop_reason == "album_done"
    await _settle(rec)


async def test_continuous_mode_survives_long_pauses(tmp_path, monkeypatch):
    rec = Recorder(tmp_path, 48000, 0.02, min_keep_s=0.1, fmt="wav", auto_stop_silence_s=0.2)
    monkeypatch.setattr(rec, "target_ready", lambda: (True, "ready"))
    rec.start("all", continuous=True)
    _play(rec)
    for _ in range(50):                      # 1 s of silence, far beyond auto_stop_silence_s
        rec.feed(CHUNK, -90.0, False)
    assert rec.active
    rec.stop()
    await _settle(rec)


import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which("flac"), reason="flac not installed")
async def test_flac_appears_atomically_with_tags(tmp_path, monkeypatch):
    rec = Recorder(tmp_path, 48000, 0.02, min_keep_s=0.1, fmt="flac")
    monkeypatch.setattr(rec, "target_ready", lambda: (True, "ready"))
    rec.start()
    for _ in range(15):
        rec.feed(CHUNK, -30.0, True)
    rec.stop()
    for _ in range(100):
        await asyncio.sleep(0.05)
        if rec.finished:
            break
    out = tmp_path / rec.finished[0]
    assert out.suffix == ".flac" and out.parent.parent.name == "Unbekannt"
    assert not list(out.parent.glob("*.wav"))
    if shutil.which("metaflac"):
        tags = subprocess.run(["metaflac", "--export-tags-to=-", str(out)],
                              capture_output=True, text=True).stdout
        assert "ARTIST=Unbekannt" in tags and f"ALBUM={out.parent.name}" in tags
