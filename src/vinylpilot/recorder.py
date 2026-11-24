"""Digitise records to a network share, one file per track.

- Audio is written by a background thread, so a slow or stalled NAS can never block
  the audio loop (chunks are dropped, and counted, if the queue overflows).
- Tracks are split at pauses (window level below ``gap_db`` for >= ``gap_min_s``,
  only once the current track is at least ``min_track_s`` long) and when a side ends.
- If track recognition is on, the file is named and tagged from the Shazam result.
- Finished WAV files are converted to FLAC by the ``flac`` tool at low priority.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import re
import shutil
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _safe(text: str) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", text).strip().strip(".")
    return text[:120] or "Unknown"


@dataclass
class Track:
    number: int
    path: Path
    started: float
    frames: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


class _WavWriter(threading.Thread):
    def __init__(self, path: Path, rate: int) -> None:
        super().__init__(daemon=True)
        self.q: queue.Queue[bytes | None] = queue.Queue(maxsize=500)  # ~10 s
        self.path = path
        self.rate = rate
        self.error: str | None = None
        self.dropped = 0

    def run(self) -> None:
        try:
            with wave.open(str(self.path), "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(self.rate)
                while (chunk := self.q.get()) is not None:
                    w.writeframes(chunk)
        except OSError as err:
            self.error = str(err)
            logger.warning("Recording write failed: %s", err)

    def put(self, chunk: bytes) -> None:
        try:
            self.q.put_nowait(chunk)
        except queue.Full:
            self.dropped += 1

    def finish(self) -> None:
        try:
            self.q.put(None, timeout=5)
        except queue.Full:
            pass


class Recorder:
    def __init__(
        self,
        base_dir: Path,
        sample_rate: int,
        chunk_s: float,
        *,
        gap_db: float = -55.0,
        gap_min_s: float = 1.5,
        min_track_s: float = 30.0,
        auto_stop_silence_s: float = 1800.0,
        fmt: str = "flac",
        min_keep_s: float = 5.0,
    ) -> None:
        self.min_keep_s = min_keep_s
        self.base_dir = base_dir
        self.rate = sample_rate
        self.chunk_s = chunk_s
        self.gap_db = gap_db
        self.gap_min_s = gap_min_s
        self.min_track_s = min_track_s
        self.auto_stop_silence_s = auto_stop_silence_s
        self.fmt = fmt
        self.session_dir: Path | None = None
        self.started: float | None = None
        self.track: Track | None = None
        self._writer: _WavWriter | None = None
        self._count = 0
        self._quiet_s = 0.0
        self._silent_s = 0.0
        self._last_meta: dict[str, Any] | None = None
        self.finished: list[str] = []
        self.error: str | None = None
        self._jobs: set[asyncio.Task[None]] = set()

    # ---- control -----------------------------------------------------------

    @property
    def active(self) -> bool:
        return self.session_dir is not None

    def target_ready(self) -> tuple[bool, str]:
        """(ok, code) with code in: ready, missing, not_mounted, read_only."""
        if not self.base_dir.is_dir():
            return False, "missing"
        if not os.path.ismount(self.base_dir):
            return False, "not_mounted"
        if not os.access(self.base_dir, os.W_OK):
            return False, "read_only"
        return True, "ready"

    def start(self) -> None:
        if self.active:
            return
        ok, reason = self.target_ready()
        if not ok:
            raise RuntimeError(f"recording target {self.base_dir}: {reason}")
        name = datetime.now().strftime("Vinyl %Y-%m-%d %H-%M")
        self.session_dir = self.base_dir / name
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.started = time.time()
        self._count = 0
        self.finished = []
        self.error = None
        logger.info("Recording started: %s", self.session_dir)

    def stop(self) -> None:
        if not self.active:
            return
        self._close_track()
        logger.info("Recording stopped: %s", self.session_dir)
        self.session_dir = None
        self.started = None

    def set_track_meta(self, meta: dict[str, Any] | None) -> None:
        """Called with each recognition result; applies to the track playing now."""
        self._last_meta = meta
        if self.track is None or not meta or not meta.get("title"):
            return
        # Only if the analysed audio belongs to this track (not to the previous one).
        if meta.get("sampled_from", 0) >= self.track.started - 1:
            self.track.meta = dict(meta)

    # ---- audio path ----------------------------------------------------------

    def feed(self, pcm: bytes, window_db: float, playing: bool) -> None:
        if not self.active:
            return
        if not playing:
            if self.track is not None:
                self._close_track()  # side finished or needle lifted
            self._silent_s += self.chunk_s
            if self._silent_s >= self.auto_stop_silence_s:
                logger.info("Recording stopped after %d min without music", self.auto_stop_silence_s // 60)
                self.stop()
            return
        self._silent_s = 0.0
        if self.track is None:
            self._open_track()
        assert self.track is not None and self._writer is not None
        if window_db < self.gap_db:
            self._quiet_s += self.chunk_s
        else:
            track_len = self.track.frames / self.rate
            if self._quiet_s >= self.gap_min_s and track_len >= self.min_track_s:
                self._close_track()
                self._open_track()
            self._quiet_s = 0.0
        self._writer.put(pcm)
        self.track.frames += len(pcm) // 4

    # ---- tracks --------------------------------------------------------------

    def _open_track(self) -> None:
        assert self.session_dir is not None
        self._count += 1
        path = self.session_dir / f"{self._count:02d} - recording.wav"
        self.track = Track(self._count, path, time.time())
        self._writer = _WavWriter(path, self.rate)
        self._writer.start()
        self._quiet_s = 0.0

    def _close_track(self) -> None:
        track, writer = self.track, self._writer
        self.track, self._writer = None, None
        if track is None or writer is None:
            return
        writer.finish()
        if writer.dropped:
            logger.warning("Track %d: %d chunks dropped (network share too slow)", track.number, writer.dropped)
        if writer.error:
            self.error = writer.error
        if track.frames / self.rate < self.min_keep_s:
            job = asyncio.get_running_loop().create_task(self._discard(track, writer))
        else:
            job = asyncio.get_running_loop().create_task(self._finalise(track, writer))
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)

    async def _discard(self, track: Track, writer: _WavWriter) -> None:
        await asyncio.to_thread(writer.join, 10)
        try:
            track.path.unlink()
        except OSError:
            pass

    async def _finalise(self, track: Track, writer: _WavWriter) -> None:
        await asyncio.to_thread(writer.join, 30)
        meta = track.meta
        if meta.get("title"):
            stem = f"{track.number:02d} - {_safe(meta.get('artist', ''))} - {_safe(meta['title'])}"
        else:
            stem = f"{track.number:02d} - Track {track.number:02d}"
        if self.fmt == "flac" and shutil.which("flac"):
            target = track.path.with_name(stem + ".flac")
            cmd = ["nice", "-n", "15", "flac", "-s", "-f", "-5", "--delete-input-file", "-o", str(target)]
            for tag, key in (("TITLE", "title"), ("ARTIST", "artist"), ("ALBUM", "album")):
                if meta.get(key):
                    cmd += ["-T", f"{tag}={meta[key]}"]
            cmd += ["-T", f"TRACKNUMBER={track.number}", str(track.path)]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await proc.communicate()
            if proc.returncode != 0:
                logger.warning("FLAC conversion failed (%s); keeping WAV", err.decode(errors="replace").strip())
                target = track.path.with_name(stem + ".wav")
                await asyncio.to_thread(os.replace, track.path, target)
        else:
            target = track.path.with_name(stem + ".wav")
            await asyncio.to_thread(os.replace, track.path, target)
        self.finished.append(target.name)
        logger.info("Saved %s", target)

    def status(self) -> dict[str, Any]:
        ok, reason = self.target_ready()
        return {
            "active": self.active,
            "target": str(self.base_dir),
            "target_ready": ok,
            "target_status": reason,
            "session": self.session_dir.name if self.session_dir else None,
            "elapsed_s": int(time.time() - self.started) if self.started else 0,
            "track": self.track.number if self.track else None,
            "track_title": (self.track.meta.get("title") if self.track else None),
            "finished": self.finished[-20:],
            "error": self.error,
        }
