import asyncio
import json
import sys

from vinylpilot.recognition import TrackRecognizer
from vinylpilot.recognize_worker import _extract

SHAZAM_ANSWER = {
    "track": {
        "key": "123", "title": "Teardrop", "subtitle": "Massive Attack", "url": "https://shazam/x",
        "images": {"coverart": "https://img/c.jpg", "coverarthq": "https://img/hq.jpg"},
        "sections": [{"type": "SONG", "metadata": [{"title": "Album", "text": "Mezzanine"}]}],
    }
}


def test_extract():
    r = _extract(SHAZAM_ANSWER)
    assert r == {"title": "Teardrop", "artist": "Massive Attack", "album": "Mezzanine",
                 "cover": "https://img/hq.jpg", "url": "https://shazam/x", "key": "123",
                 "year": "", "genre": ""}
    assert _extract({"matches": []}) == {}


async def test_lookup_after_start_and_gap(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import sys, json\n"
        "sys.stdin.buffer.read()\n"
        f"print(json.dumps({json.dumps(_extract(SHAZAM_ANSWER))}))\n"
    )
    results = []
    rec = TrackRecognizer(48000, 0.02, results.append, sample_s=0.2, start_delay_s=0.1,
                          recheck_s=999, worker_cmd=[sys.executable, str(worker)])
    chunk = b"\x01\x00" * 1920
    for _ in range(20):  # 0.4 s of music
        rec.feed(chunk, -30.0, True)
        await asyncio.sleep(0.02)
    for _ in range(50):
        await asyncio.sleep(0.02)
        if results:
            break
    assert results and results[0]["title"] == "Teardrop"
    rec.feed(chunk, -30.0, False)  # record stopped -> cleared
    assert results[-1] is None and rec.current is None
