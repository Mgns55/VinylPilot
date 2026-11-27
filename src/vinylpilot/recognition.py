"""Track recognition scheduling (opt-in).

Looks a song up shortly after a record starts and after every detected gap between
tracks, plus a periodic re-check for gapless sides. Lookups run in a separate
process (see ``recognize_worker``); the audio loop only copies chunks into a ring buffer.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import shutil
import sys
import time
import wave
from collections import deque
from collections.abc import Callable
from typing import Any

from .levels import GapTracker

logger = logging.getLogger(__name__)


class TrackRecognizer:
    def __init__(
        self,
        sample_rate: int,
        chunk_s: float,
        on_result: Callable[[dict[str, Any] | None], None],
        *,
        sample_s: float = 12.0,
        start_delay_s: float = 14.0,
        gap_db: float = -55.0,
        gap_min_s: float = 1.0,
        recheck_s: float = 90.0,
        retry_s: float = 30.0,
        max_retries: int = 3,
        timeout_s: float = 45.0,
        worker_cmd: list[str] | None = None,
    ) -> None:
        low_io = ["ionice", "-c", "3"] if shutil.which("ionice") else []
        self.worker_cmd = worker_cmd or [
            "nice", "-n", "19", *low_io, sys.executable, "-m", "vinylpilot.recognize_worker", "--serve"]
        self.rate = sample_rate
        self.chunk_s = chunk_s
        self.on_result = on_result
        self.sample_s = sample_s
        self.start_delay_s = start_delay_s
        self._gaps = GapTracker(chunk_s, floor_db=gap_db, min_s=gap_min_s)
        self.recheck_s = recheck_s
        self.retry_s = retry_s
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self._buf: deque[bytes] = deque(maxlen=int(sample_s / chunk_s) + 1)
        self._playing = False
        self._due: float | None = None
        self._last_lookup = 0.0
        self._misses = 0
        self._task: asyncio.Task[None] | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self.current: dict[str, Any] | None = None

    @property
    def gap_db(self) -> float:
        return self._gaps.floor_db

    @gap_db.setter
    def gap_db(self, value: float) -> None:
        self._gaps.floor_db = value

    # ---- called for every chunk -------------------------------------------

    def feed(self, pcm: bytes, window_db: float, playing: bool) -> None:
        now = time.monotonic()
        if not playing:
            if self._playing:
                self._playing = False
                self._due = None
                self._buf.clear()
                self._stop_worker()
                self._gaps.reset()
                if self.current is not None:
                    self.current = None
                    self.on_result(None)
            return
        if not self._playing:
            self._playing = True
            self._misses = 0
            self._schedule(now + self.start_delay_s, "record started")
        self._buf.append(pcm)

        # gap between tracks: quiet for a moment, then music again
        if self._gaps.update(window_db):
            self._misses = 0
            self._schedule(now + self.start_delay_s, "gap detected")

        if self._due is None and now - self._last_lookup >= self.recheck_s:
            self._schedule(now, "periodic re-check")

        if (
            self._due is not None
            and now >= self._due
            and (self._task is None or self._task.done())
            and len(self._buf) * self.chunk_s >= self.sample_s * 0.8
        ):
            self._due = None
            self._last_lookup = now
            sampled_from = time.time() - len(self._buf) * self.chunk_s
            self._task = asyncio.get_running_loop().create_task(self._lookup(self._wav(), sampled_from))

    # ---- internals ----------------------------------------------------------

    def _schedule(self, when: float, reason: str) -> None:
        if self._due is None or when > self._due:
            logger.debug("Recognition scheduled (%s)", reason)
            self._due = when

    def _wav(self) -> bytes:
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            w.writeframes(b"".join(self._buf))
        return out.getvalue()

    def reset(self) -> None:
        """Forget the current track (e.g. recognition switched off)."""
        self._stop_worker()
        self._playing = False
        self._due = None
        self._buf.clear()
        if self.current is not None:
            self.current = None
            self.on_result(None)

    async def _worker(self) -> asyncio.subprocess.Process:
        if self._proc is None or self._proc.returncode is not None:
            self._proc = await asyncio.create_subprocess_exec(
                *self.worker_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        return self._proc

    def _stop_worker(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
        self._proc = None

    async def _lookup(self, wav: bytes, sampled_from: float) -> None:
        try:
            proc = await self._worker()
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(len(wav).to_bytes(4, "big") + wav)
            await proc.stdin.drain()
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=self.timeout_s)
        except TimeoutError:
            logger.warning("Track recognition timed out")
            self._stop_worker()
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("Track recognition could not run: %s", e)
            self._stop_worker()
            return
        try:
            result = json.loads(line.decode().strip())
        except ValueError:
            logger.warning("Track recognition failed (worker exited)")
            self._stop_worker()
            return
        if not self._playing:
            return
        if "error" in result:
            logger.warning("Track recognition error: %s", result["error"])
            self._retry()
            return
        if not result.get("title"):
            logger.info("Track not recognised")
            self._retry()
            return
        self._misses = 0
        if self.current and self.current.get("key") == result.get("key"):
            self.current["sampled_from"] = sampled_from
            self.on_result(self.current)
            return
        result["recognized_at"] = int(time.time())
        result["sampled_from"] = sampled_from
        self.current = result
        logger.info("Now playing: %s – %s", result.get("artist"), result.get("title"))
        self.on_result(result)

    def close(self) -> None:
        """Stop any running lookup (used on shutdown)."""
        self._stop_worker()
        if self._task is not None:
            self._task.cancel()

    def _retry(self) -> None:
        self._misses += 1
        if self._misses <= self.max_retries:
            self._schedule(time.monotonic() + self.retry_s, "retry")
