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


def _norm(text: str) -> str:
    """Comparable album/artist name: case, brackets, remaster notes and punctuation ignored."""
    plain = re.sub(r"[^a-z0-9äöüß]+", "", text.lower())
    text = re.sub(r"[\(\[].*?[\)\]]", " ", text.lower())
    text = re.sub(r"\b(remaster(ed)?|deluxe|edition|version|expanded|anniversary|\d{4})\b", " ", text)
    return re.sub(r"[^a-z0-9äöüß]+", "", text) or plain  # e.g. an album called "1989"


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
    discard: bool = False


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
        album_end_silence_s: float = 300.0,
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
        self.album_end_silence_s = album_end_silence_s
        self.mode = "all"
        self.continuous = False
        self.target_album: tuple[str, str] | None = None
        self.target_album_name: str | None = None
        self.skipped: list[str] = []
        self.stop_reason: str | None = None
        self.fmt = fmt
        self.session_dir: Path | None = None
        self.session_name: str | None = None
        self._album_numbers: dict[Path, int] = {}
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
        self._finalise_lock = asyncio.Lock()

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

    def start(self, mode: str = "all", continuous: bool = False) -> None:
        """mode 'album': stop after the next album; 'all': keep going until stopped.

        ``continuous`` (the permanent 'record everything' setting) never stops on silence.
        """
        if self.active:
            return
        if mode not in ("album", "all"):
            raise ValueError("mode must be 'album' or 'all'")
        ok, reason = self.target_ready()
        if not ok:
            raise RuntimeError(f"recording target {self.base_dir}: {reason}")
        self.session_name = datetime.now().strftime("Vinyl %Y-%m-%d %H-%M")
        self.session_dir = self.base_dir / ".vinylpilot-tmp" / self.session_name
        self.session_dir.mkdir(parents=True, exist_ok=True)
        try:  # Navidrome skips folders containing .ndignore (hidden folders too, but be explicit)
            (self.session_dir.parent / ".ndignore").touch(exist_ok=True)
        except OSError:
            pass
        self._album_numbers = {}
        self.started = time.time()
        self._count = 0
        self.finished = []
        self.skipped = []
        self.error = None
        self.mode = mode
        self.continuous = continuous and mode == "all"
        self.target_album = None
        self.target_album_name = None
        self.stop_reason = None
        self._silent_s = 0.0
        logger.info("Recording started (%s): %s", "next album" if mode == "album" else "all albums",
                    self.session_name)

    def stop(self, reason: str = "manual") -> None:
        if not self.active:
            return
        self.stop_reason = reason
        self._close_track()
        logger.info("Recording stopped: %s", self.session_name)
        tmp = self.session_dir
        self.session_dir = None
        job = asyncio.get_running_loop().create_task(self._cleanup_tmp(tmp))
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)
        self.started = None

    def set_track_meta(self, meta: dict[str, Any] | None) -> None:
        """Called with each recognition result; applies to the track playing now."""
        self._last_meta = meta
        if self.track is None or not meta or not meta.get("title"):
            return
        # Only if the analysed audio belongs to this track (not to the previous one).
        if meta.get("sampled_from", 0) < self.track.started - 1:
            return
        self.track.meta = dict(meta)
        if self.mode != "album" or not meta.get("album"):
            return
        key = (_norm(meta.get("artist", "")), _norm(meta["album"]))
        if self.target_album is None:
            self.target_album = key
            self.target_album_name = f"{meta.get('artist', '')} – {meta['album']}"
            logger.info("Recording album: %s", self.target_album_name)
        elif key != self.target_album:
            # The next record has started: this track belongs to it, so drop it and stop.
            logger.info("Next album detected (%s – %s); recording finished",
                        meta.get("artist"), meta["album"])
            self.track.discard = True
            self.stop("album_done")

    # ---- audio path ----------------------------------------------------------

    def feed(self, pcm: bytes, window_db: float, playing: bool) -> None:
        if not self.active:
            return
        if not playing:
            if self.track is not None:
                self._close_track()  # side finished or needle lifted
            self._silent_s += self.chunk_s
            if self.mode == "album" and self._count and self._silent_s >= self.album_end_silence_s:
                logger.info("Album finished (%d min without music)", self.album_end_silence_s // 60)
                self.stop("album_done")
            elif not self.continuous and self._silent_s >= self.auto_stop_silence_s:
                logger.info("Recording stopped after %d min without music", self.auto_stop_silence_s // 60)
                self.stop("silence")
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
        if track.discard or track.frames / self.rate < self.min_keep_s:
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

    def _album_dir(self, meta: dict[str, Any]) -> Path:
        """Artist/Album for recognised records, else Unbekannt/<session>.

        An existing folder whose name matches after normalisation is reused, so
        'Aftermath (Remastered)' and 'Aftermath' end up in the same place.
        """
        if not (meta.get("artist") and meta.get("album")):
            return self.base_dir / "Unbekannt" / datetime.now().strftime("Vinyl %Y-%m-%d")
        artist_dir = self.base_dir / _safe(meta["artist"])
        if artist_dir.is_dir():
            for d in artist_dir.iterdir():
                if d.is_dir() and _norm(d.name) == _norm(meta["album"]):
                    return d
        return artist_dir / _safe(meta["album"])

    @staticmethod
    def _already_recorded(album_dir: Path, title: str) -> Path | None:
        """A file of this album with the same title, from this or an earlier session."""
        if not album_dir.is_dir():
            return None
        want = _norm(title)
        for f in album_dir.iterdir():
            m = re.match(r"\d+ - (.+)\.(flac|wav)$", f.name)
            if m and _norm(m.group(1)) == want:
                return f
        return None

    def _next_number(self, album_dir: Path) -> int:
        """Continue numbering across sides and across sessions for the same album."""
        if album_dir not in self._album_numbers:
            existing = 0
            if album_dir.is_dir():
                for f in album_dir.iterdir():
                    m = re.match(r"(\d+) - ", f.name)
                    if m:
                        existing = max(existing, int(m.group(1)))
            self._album_numbers[album_dir] = existing
        self._album_numbers[album_dir] += 1
        return self._album_numbers[album_dir]

    async def _fetch_cover(self, url: str, album_dir: Path) -> Path | None:
        target = album_dir / "cover.jpg"
        if target.exists():
            return target
        if not url.startswith("https://"):
            return None
        try:
            import aiohttp

            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.get(url) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.read()
            await asyncio.to_thread(target.write_bytes, data)
            return target
        except Exception as err:  # noqa: BLE001
            logger.info("Cover not downloaded: %s", err)
            return None

    async def _finalise(self, track: Track, writer: _WavWriter) -> None:
        # One at a time, in recording order: keeps track numbers right and runs only
        # one FLAC encoder on the Zero.
        async with self._finalise_lock:
            await self._finalise_locked(track, writer)

    async def _finalise_locked(self, track: Track, writer: _WavWriter) -> None:
        await asyncio.to_thread(writer.join, 30)
        meta = track.meta
        album_dir = await asyncio.to_thread(self._album_dir, meta)
        title = meta.get("title")
        if title:
            existing = await asyncio.to_thread(self._already_recorded, album_dir, title)
            if existing is not None:
                await asyncio.to_thread(track.path.unlink, True)
                rel = str(existing.relative_to(self.base_dir))
                self.skipped.append(rel)
                logger.info("Skipped, already digitised: %s", rel)
                return
        await asyncio.to_thread(album_dir.mkdir, parents=True, exist_ok=True)
        number = self._next_number(album_dir)
        stem = f"{number:02d} - {_safe(title)}" if title else f"{number:02d} - Track {number:02d}"
        cover = await self._fetch_cover(meta.get("cover", ""), album_dir) if title else None
        if self.fmt == "flac" and shutil.which("flac"):
            target = album_dir / (stem + ".flac")
            # Encode inside the hidden temp folder and move the finished file in one
            # rename, so library scanners (Navidrome, Plex …) never see a half-written FLAC.
            part = track.path.with_suffix(".flac")
            cmd = ["nice", "-n", "15", "flac", "-s", "-f", "-5", "--delete-input-file", "-o", str(part)]
            if title:
                tags = {
                    "TITLE": title, "ARTIST": meta.get("artist"), "ALBUMARTIST": meta.get("artist"),
                    "ALBUM": meta.get("album"), "DATE": meta.get("year"), "GENRE": meta.get("genre"),
                    "TRACKNUMBER": str(number),
                }
            else:
                # Unrecognised: keep a day's unknown tracks together as one album.
                tags = {
                    "TITLE": f"Track {number:02d}", "ARTIST": "Unbekannt", "ALBUMARTIST": "Unbekannt",
                    "ALBUM": album_dir.name, "TRACKNUMBER": str(number),
                }
            for tag, value in tags.items():
                if value:
                    cmd += ["-T", f"{tag}={value}"]
            if cover is not None:
                cmd.append(f"--picture={cover}")
            cmd.append(str(track.path))
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await proc.communicate()
            if proc.returncode != 0:
                logger.warning("FLAC conversion failed (%s); keeping WAV", err.decode(errors="replace").strip())
                target = album_dir / (stem + ".wav")
                await asyncio.to_thread(shutil.move, str(track.path), str(target))
            else:
                await asyncio.to_thread(shutil.move, str(part), str(target))
        else:
            target = album_dir / (stem + ".wav")
            await asyncio.to_thread(shutil.move, str(track.path), str(target))
        rel = target.relative_to(self.base_dir)
        self.finished.append(str(rel))
        logger.info("Saved %s", rel)

    async def _cleanup_tmp(self, tmp: Path | None) -> None:
        """Remove the hidden session folder once all tracks are finalised."""
        if tmp is None:
            return
        while any(not j.done() for j in self._jobs if j is not asyncio.current_task()):
            await asyncio.sleep(1)
        await asyncio.to_thread(shutil.rmtree, tmp, True)

    def status(self) -> dict[str, Any]:
        ok, reason = self.target_ready()
        return {
            "active": self.active,
            "target": str(self.base_dir),
            "target_ready": ok,
            "target_status": reason,
            "session": self.session_name if self.session_dir else None,
            "elapsed_s": int(time.time() - self.started) if self.started else 0,
            "track": self.track.number if self.track else None,
            "track_title": (self.track.meta.get("title") if self.track else None),
            "finished": self.finished[-20:],
            "skipped": self.skipped[-20:],
            "mode": self.mode,
            "continuous": self.continuous,
            "album": self.target_album_name,
            "stop_reason": self.stop_reason,
            "error": self.error,
        }
