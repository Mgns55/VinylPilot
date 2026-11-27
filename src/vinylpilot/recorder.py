"""Digitise records to a network share, one file per track.

Runs in its own process (see ``record_worker``), so nothing here can ever stall the
live stream. Design goals for a Pi Zero 2 W on Wi-Fi:

- **Steady, small network load.** Each track is encoded to FLAC *while it plays* and
  written straight to the share (~0.9 Mbit/s). No WAV round trips, no copying,
  splitting or re-encoding afterwards; finishing a track only adds tags and renames it.
- **Delay line.** The last ``delay_s`` (150 s, ~29 MB) of audio stay in memory before
  they are written. When Shazam reports a new song without a pause having been found
  (segues, noisy run-in grooves), the cut is placed at the quietest moment in that
  window by simply re-assigning buffered audio to the next track.
- Tracks are also split live at pauses (``GapTracker``) and when a side ends.
- Finished files appear in one rename, so library scanners never see partial files.
- After a crash or power cut, tracks left in the temp folder are recovered on the next
  start into ``Unbekannt/Wiederhergestellt <date>``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .levels import GapTracker

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


def _write_json(path: Path, data: dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(data))
    except OSError:
        pass


def _repair_wav(path: Path, rate: int) -> int:
    """Rewrite the header of a WAV cut off by a crash; return its frame count."""
    size = path.stat().st_size
    data = max(0, size - 44) // 4 * 4
    header = (b"RIFF" + (36 + data).to_bytes(4, "little") + b"WAVEfmt "
              + (16).to_bytes(4, "little") + (1).to_bytes(2, "little") + (2).to_bytes(2, "little")
              + rate.to_bytes(4, "little") + (rate * 4).to_bytes(4, "little")
              + (4).to_bytes(2, "little") + (16).to_bytes(2, "little")
              + b"data" + data.to_bytes(4, "little"))
    with open(path, "r+b") as f:
        f.write(header)
        f.truncate(44 + data)
    return data // 4


def flac_available() -> bool:
    return bool(shutil.which("flac") and shutil.which("metaflac"))


class _Sink(threading.Thread):
    """Writes one track in the background: live FLAC encoder process, or a WAV file."""

    MAX_QUEUE = 15000  # 5 min of 20 ms chunks; beyond that the share is hopelessly stuck

    def __init__(self, path: Path, rate: int, fmt: str) -> None:
        super().__init__(daemon=True)
        self.q: queue.Queue[bytes | None] = queue.Queue()
        self.path = path
        self.rate = rate
        self.fmt = fmt
        self.error: str | None = None
        self.dropped = 0
        self._proc: subprocess.Popen[bytes] | None = None

    def run(self) -> None:
        try:
            if self.fmt == "flac":
                self._run_flac()
            else:
                self._run_wav()
        except (OSError, ValueError) as err:
            self.error = str(err)
            logger.warning("Recording write failed: %s", err)
            while self.q.get() is not None:  # keep draining so nothing waits on us
                pass

    def _chunks(self):  # type: ignore[no-untyped-def]
        while (chunk := self.q.get()) is not None:
            yield chunk

    def _run_flac(self) -> None:
        self._proc = subprocess.Popen(
            ["nice", "-n", "10", "flac", "-s", "-f", "-5", "--force-raw-format", "--endian=little",
             "--sign=signed", "--channels=2", "--bps=16", f"--sample-rate={self.rate}",
             "--padding=262144",  # room for tags and cover, so tagging never rewrites the file
             "-o", str(self.path), "-"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        assert self._proc.stdin is not None
        try:
            for chunk in self._chunks():
                self._proc.stdin.write(chunk)
        finally:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
            _, err = self._proc.communicate(timeout=120)
            if self._proc.returncode not in (0, -9):
                self.error = err.decode(errors="replace").strip()[-200:] or f"flac exit {self._proc.returncode}"

    def _run_wav(self) -> None:
        with wave.open(str(self.path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            for chunk in self._chunks():
                w.writeframesraw(chunk)

    def put(self, chunk: bytes) -> None:
        if self.q.qsize() > self.MAX_QUEUE:
            self.dropped += 1
        else:
            self.q.put_nowait(chunk)

    def finish(self) -> None:
        self.q.put_nowait(None)

    def abort(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()
        self.finish()


@dataclass(eq=False)
class Track:
    number: int
    path: Path                       # in the hidden temp folder until finished
    started: float                   # wall-clock time of the first sample
    frames: int = 0
    meta: dict[str, Any] = field(default_factory=dict)
    discard: bool = False
    closed: bool = False             # no more audio will be added
    pending: int = 0                 # chunks of this track still in the delay line
    sink: _Sink | None = None
    levels: list[tuple[int, float]] = field(default_factory=list)  # (frame, dBFS) every 0.5 s


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
        delay_s: float = 150.0,
        mount_check: bool = True,
    ) -> None:
        self.min_keep_s = min_keep_s
        self.base_dir = base_dir
        self.rate = sample_rate
        self.chunk_s = chunk_s
        self._gaps = GapTracker(chunk_s, floor_db=gap_db, min_s=gap_min_s)
        self.min_track_s = min_track_s
        self.auto_stop_silence_s = auto_stop_silence_s
        self.album_end_silence_s = album_end_silence_s
        self.delay_chunks = max(0, int(delay_s / chunk_s))
        self.mount_check = mount_check
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
        self._line: deque[list[Any]] = deque()  # [track, frame, pcm]: the delay line
        self._count = 0
        self._silent_s = 0.0
        self.finished: list[str] = []
        self.error: str | None = None
        self.dropped = 0
        self.target_state: tuple[bool, str] = (False, "missing")
        self._jobs: set[asyncio.Task[None]] = set()
        self._finalise_lock = asyncio.Lock()

    @property
    def gap_db(self) -> float:
        return self._gaps.floor_db

    @gap_db.setter
    def gap_db(self, value: float) -> None:
        self._gaps.floor_db = value

    @property
    def _fmt(self) -> str:
        return "flac" if self.fmt == "flac" and flac_available() else "wav"

    # ---- control -----------------------------------------------------------

    @property
    def active(self) -> bool:
        return self.session_dir is not None

    def target_ready(self) -> tuple[bool, str]:
        """(ok, code) with code in: ready, missing, not_mounted, read_only."""
        if not self.base_dir.is_dir():
            state = (False, "missing")
        elif self.mount_check and not os.path.ismount(self.base_dir):
            state = (False, "not_mounted")
        elif not os.access(self.base_dir, os.W_OK):
            state = (False, "read_only")
        else:
            state = (True, "ready")
        self.target_state = state
        return state

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
        self.dropped = 0
        self.mode = mode
        self.continuous = continuous and mode == "all"
        self.target_album = None
        self.target_album_name = None
        self.stop_reason = None
        self._silent_s = 0.0
        logger.info("Recording started (%s, %s): %s", "next album" if mode == "album" else "all albums",
                    self._fmt.upper(), self.session_name)
        self._spawn(self._recover_orphans(self.session_dir))

    def stop(self, reason: str = "manual") -> None:
        if not self.active:
            return
        self.stop_reason = reason
        self._close_track()
        self._flush(all_=True)
        logger.info("Recording stopped: %s", self.session_name)
        tmp = self.session_dir
        self.session_dir = None
        self._spawn(self._cleanup_tmp(tmp))
        self.started = None

    async def drain(self, timeout: float = 60.0) -> None:
        """Wait for running finalisations (used on shutdown)."""
        pending = [j for j in self._jobs if not j.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    def set_track_meta(self, meta: dict[str, Any] | None) -> None:
        """Called with each recognition result; applies to the track playing now."""
        if self.track is None or not meta or not meta.get("title"):
            return
        # Only if the analysed audio belongs to this track (not to the previous one).
        if meta.get("sampled_from", 0) < self.track.started - 1:
            return
        old_title = self.track.meta.get("title")
        if old_title and _norm(old_title) != _norm(meta["title"]):
            self._split_by_recognition(meta)
        self.track.meta = dict(meta)
        # Sidecar next to the audio, so a crash does not lose the title.
        asyncio.get_running_loop().run_in_executor(
            None, _write_json, self.track.path.with_suffix(".json"), self.track.meta)
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
                self._gaps.reset()
                self._flush(all_=True)  # nothing can be re-assigned any more
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
        assert self.track is not None
        if self._gaps.update(window_db) and self.track.frames / self.rate >= self.min_track_s:
            self._close_track()
            self._open_track()
            assert self.track is not None
        track = self.track
        self._line.append([track, track.frames, pcm])
        track.pending += 1
        if track.frames % (self.rate // 2) < len(pcm) // 4:  # every 0.5 s
            track.levels.append((track.frames, window_db))
        track.frames += len(pcm) // 4
        self._flush()

    # ---- delay line ----------------------------------------------------------

    def _flush(self, all_: bool = False) -> None:
        keep = 0 if all_ else self.delay_chunks
        while len(self._line) > keep:
            track, _frame, pcm = self._line.popleft()
            track.pending -= 1
            if not track.discard:
                if track.sink is None:
                    track.sink = _Sink(track.path, self.rate, self._fmt)
                    track.sink.start()
                track.sink.put(pcm)
            if track.closed and track.pending == 0:
                self._done(track)

    def _open_track(self, started: float | None = None) -> None:
        assert self.session_dir is not None
        self._count += 1
        ext = ".flac" if self._fmt == "flac" else ".wav"
        path = self.session_dir / f"{self._count:02d} - recording{ext}"
        self.track = Track(self._count, path, started if started is not None else time.time())

    def _close_track(self) -> None:
        track, self.track = self.track, None
        if track is None:
            return
        track.closed = True
        if track.pending == 0:
            self._done(track)

    def _done(self, track: Track) -> None:
        """All audio of a closed track has left the delay line."""
        sink = track.sink
        if sink is not None:
            if track.discard:
                sink.abort()
            else:
                sink.finish()
            if sink.dropped:
                self.dropped += sink.dropped
                logger.warning("Track %d: %d chunks dropped (network share too slow)", track.number, sink.dropped)
        if track.discard or sink is None or track.frames / self.rate < self.min_keep_s:
            self._spawn(self._discard(track))
        else:
            self._spawn(self._finalise(track))

    def _split_by_recognition(self, meta: dict[str, Any]) -> None:
        """Shazam heard a new song but no pause was found (segue, noisy groove): cut at
        the quietest moment between the last sample of the old song and the first sample
        of the new one, by handing the buffered audio after it to the next track."""
        track = self.track
        assert track is not None
        sample_s = 12.0  # length of a recognition sample, rounded up
        lo = int((track.meta.get("sampled_from", track.started) + sample_s - track.started) * self.rate)
        hi = int((meta["sampled_from"] - track.started) * self.rate)
        first_buffered = next((e[1] for e in self._line if e[0] is track), None)
        if first_buffered is None:
            return
        window = [(f, db) for f, db in track.levels if max(lo, first_buffered) <= f <= hi]
        cut = min(window, key=lambda x: x[1])[0] if window else max(lo, hi, first_buffered)
        if cut < self.min_track_s * self.rate or cut >= track.frames:
            return
        logger.info("Split by recognition: '%s' ended at %d s", track.meta.get("title"), cut // self.rate)
        self._open_track(started=track.started + cut / self.rate)
        new = self.track
        assert new is not None
        for entry in self._line:
            if entry[0] is track and entry[1] >= cut:
                entry[0], entry[1] = new, entry[1] - cut
                track.pending -= 1
                new.pending += 1
        new.frames = track.frames - cut
        new.levels = [(f - cut, db) for f, db in track.levels if f >= cut]
        track.frames = cut
        track.closed = True
        if track.pending == 0:
            self._done(track)

    def _spawn(self, coro: Any) -> None:
        job = asyncio.get_running_loop().create_task(coro)
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)

    # ---- finishing -------------------------------------------------------------

    async def _discard(self, track: Track) -> None:
        if track.sink is not None:
            await asyncio.to_thread(track.sink.join, 30)
        for p in (track.path, track.path.with_suffix(".json")):
            try:
                p.unlink()
            except OSError:
                pass

    def _album_dir(self, meta: dict[str, Any]) -> Path:
        """Artist/Album for recognised records, else Unbekannt/Vinyl <date>.

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

    async def _finalise(self, track: Track, album_dir: Path | None = None) -> None:
        # One at a time, in recording order: keeps track numbers right.
        async with self._finalise_lock:
            try:
                await self._finalise_locked(track, album_dir)
            except Exception as err:  # noqa: BLE001 - one bad track must not stop the rest
                logger.warning("Could not finish track %d: %s", track.number, err)
                self.error = str(err)

    async def _finalise_locked(self, track: Track, album_dir: Path | None) -> None:
        if track.sink is not None:
            await asyncio.to_thread(track.sink.join, 180)
            if track.sink.error:
                self.error = track.sink.error
                logger.warning("Track %d: %s", track.number, track.sink.error)
        if not track.path.exists():
            return
        meta = track.meta
        recovered = album_dir is not None
        if album_dir is None:
            album_dir = await asyncio.to_thread(self._album_dir, meta)
        title = meta.get("title")
        if title and not recovered:
            existing = await asyncio.to_thread(self._already_recorded, album_dir, title)
            if existing is not None:
                await self._discard(track)
                rel = str(existing.relative_to(self.base_dir))
                self.skipped.append(rel)
                logger.info("Skipped, already digitised: %s", rel)
                return
        await asyncio.to_thread(album_dir.mkdir, parents=True, exist_ok=True)
        number = await asyncio.to_thread(self._next_number, album_dir)
        stem = f"{number:02d} - {_safe(title)}" if title else f"{number:02d} - Track {number:02d}"
        target = album_dir / (stem + track.path.suffix)
        if track.path.suffix == ".flac":
            cover = await self._fetch_cover(meta.get("cover", ""), album_dir) if title else None
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
            cmd = ["metaflac", "--remove-all-tags"]
            cmd += [f"--set-tag={k}={v}" for k, v in tags.items() if v]
            if cover is not None:
                cmd.append(f"--import-picture-from={cover}")
            cmd.append(str(track.path))
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await proc.communicate()
            if proc.returncode != 0:
                logger.warning("Tagging failed: %s", err.decode(errors="replace").strip())
        # Same share: a rename, no data copied over the network.
        await asyncio.to_thread(shutil.move, str(track.path), str(target))
        await asyncio.to_thread(track.path.with_suffix(".json").unlink, True)
        rel = target.relative_to(self.base_dir)
        self.finished.append(str(rel))
        logger.info("Saved %s", rel)

    async def _recover_orphans(self, current: Path | None) -> None:
        """Finish tracks left behind in the temp folder by a crash or power cut."""
        root = self.base_dir / ".vinylpilot-tmp"
        try:
            orphans = await asyncio.to_thread(
                lambda: [d for d in root.iterdir() if d.is_dir() and d != current])
        except OSError:
            return
        for session in sorted(orphans):
            audio = sorted([*session.glob("*.wav"), *session.glob("*.flac")])
            for f in audio:
                try:
                    if f.suffix == ".wav":
                        seconds = await asyncio.to_thread(_repair_wav, f, self.rate) / self.rate
                    else:
                        seconds = f.stat().st_size / 110_000  # FLAC of vinyl: ~0.9 Mbit/s
                except OSError as err:
                    logger.warning("Cannot recover %s: %s", f.name, err)
                    continue
                if seconds < max(self.min_keep_s, 30.0):
                    await asyncio.to_thread(f.unlink, True)
                    continue
                try:
                    meta = json.loads(f.with_suffix(".json").read_text())
                except (OSError, ValueError):
                    meta = {}
                target = self.base_dir / "Unbekannt" / f"Wiederhergestellt {session.name.removeprefix('Vinyl ')[:10]}"
                if meta.get("title"):
                    meta = {**meta, "title": f"{meta['title']} (unvollständig)", "album": target.name}
                logger.info("Recovering %s from %s (~%d s)", f.name, session.name, seconds)
                await self._finalise(Track(0, f, 0.0, int(seconds * self.rate), meta), album_dir=target)
            await asyncio.to_thread(shutil.rmtree, session, True)

    async def _cleanup_tmp(self, tmp: Path | None) -> None:
        """Remove the hidden session folder once all tracks are finalised."""
        if tmp is None:
            return
        while any(not j.done() for j in self._jobs if j is not asyncio.current_task()):
            await asyncio.sleep(1)
        await asyncio.to_thread(shutil.rmtree, tmp, True)

    def status(self) -> dict[str, Any]:
        ok, reason = self.target_state
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
            "format": self._fmt,
            "buffered_s": round(len(self._line) * self.chunk_s),
            "dropped": self.dropped,
        }
