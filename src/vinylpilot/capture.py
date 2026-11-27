"""ALSA capture through a supervised ``arecord`` subprocess."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable

logger = logging.getLogger(__name__)

BYTES_PER_FRAME = 4  # S16_LE stereo


class ArecordCapture:
    """Reads raw S16_LE stereo PCM from an ALSA device, restarting arecord on failure.

    A USB disconnect makes arecord exit; we wait and restart it until the device is back.
    """

    def __init__(
        self,
        device: str,
        sample_rate: int,
        chunk_ms: int = 20,
        resolve: Callable[[], str | None] | None = None,
    ) -> None:
        # ``resolve`` is called before every (re)start, e.g. to find the USB card again
        # after it was unplugged or switched off; None means "not there yet".
        self.device = device
        self._resolve = resolve
        self.sample_rate = sample_rate
        self.chunk_frames = sample_rate * chunk_ms // 1000
        self.chunk_bytes = self.chunk_frames * BYTES_PER_FRAME
        self.chunk_s = self.chunk_frames / sample_rate
        self.running = False

    def _command(self) -> list[str]:
        return [
            "arecord",
            "-q",
            "-D", self.device,
            "-f", "S16_LE",
            "-r", str(self.sample_rate),
            "-c", "2",
            "-t", "raw",
            "-B", "500000",  # 500 ms ALSA buffer: rides out short scheduling hiccups
            "-F", "20000",  # 20 ms periods
        ]

    async def chunks(self) -> AsyncIterator[bytes]:
        """Yield fixed-size PCM chunks forever, surviving device loss.

        An empty ``b""`` chunk is yielded once whenever a running capture is lost,
        so the caller can treat a vanished turntable as silence.
        """
        backoff = 1.0
        waiting_logged = False
        while True:
            if self._resolve is not None:
                found = self._resolve()
                if found is None:
                    if not waiting_logged:
                        logger.warning("No USB audio capture device found; waiting for the turntable")
                        waiting_logged = True
                    await asyncio.sleep(2)
                    continue
                waiting_logged = False
                self.device = found
            proc = await asyncio.create_subprocess_exec(
                *self._command(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert proc.stdout is not None
            try:
                while True:
                    chunk = await proc.stdout.readexactly(self.chunk_bytes)
                    if not self.running:
                        logger.info("Capturing from %s at %d Hz", self.device, self.sample_rate)
                        self.running = True
                        backoff = 1.0
                    yield chunk
            except asyncio.IncompleteReadError:
                pass
            finally:
                if proc.returncode is None:
                    proc.terminate()
                stderr = b""
                if proc.stderr is not None:
                    try:
                        stderr = await asyncio.wait_for(proc.stderr.read(), timeout=2)
                    except (TimeoutError, asyncio.CancelledError):
                        pass
                await proc.wait()
            was_running, self.running = self.running, False
            if was_running:
                yield b""
            message = stderr.decode(errors="replace").strip().splitlines()
            logger.warning(
                "Capture stopped (%s); retrying in %.0f s",
                message[-1] if message else f"arecord exit {proc.returncode}",
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)
