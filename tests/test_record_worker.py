"""The recorder in its own process, driven like the main process does."""

import asyncio
import shutil
import subprocess
import sys

import pytest

from vinylpilot.record_worker import RecorderClient

pytestmark = pytest.mark.skipif(not shutil.which("flac"), reason="flac not installed")


async def test_client_records_through_child_process(tmp_path):
    rec = RecorderClient(tmp_path, 48000, 0.02, fmt="flac", extra_args=["--no-mount-check"], nice=0)
    rec.cmd[0] = sys.executable
    await rec.start()
    try:
        st = await rec.start_recording("all", continuous=True)
        assert st["active"] and st["continuous"] and rec.active
        chunk = b"\x10\x00\x10\x00" * 960
        for i in range(500):                      # 10 s of "music"
            rec.feed(chunk, -30.0, True)
            if i % 50 == 0:
                await asyncio.sleep(0)
        rec.feed(b"", -120.0, False)              # side ends -> track written
        for _ in range(100):
            await asyncio.sleep(0.1)
            if rec.status().get("finished"):
                break
        finished = rec.status()["finished"]
        assert len(finished) == 1 and finished[0].startswith("Unbekannt/Vinyl "), finished
        out = tmp_path / finished[0]
        samples = subprocess.run(["metaflac", "--show-total-samples", str(out)],
                                 capture_output=True, text=True).stdout.strip()
        assert samples == str(500 * 960)
    finally:
        await rec.close()


async def test_start_fails_cleanly_without_share(tmp_path):
    rec = RecorderClient(tmp_path / "missing", 48000, 0.02, nice=0)
    await rec.start()
    try:
        with pytest.raises(RuntimeError, match="missing"):
            await rec.start_recording("all")
        assert not rec.active
        rec.feed(b"\x00" * 3840, -30.0, True)     # ignored, no error
    finally:
        await rec.close()
