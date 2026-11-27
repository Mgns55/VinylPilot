"""The digitising process, plus the client the main process talks to it with.

The recorder runs in a separate process so that nothing it does - network share
hiccups, encoding, tagging, the Python garbage collector - can stall the live stream.

Wire format, main -> worker (stdin): 1 type byte + 4-byte big-endian length + payload.
  b"A": ``<?f`` (playing, window dBFS) followed by raw PCM
  b"J": a JSON command ``{"id": n, "cmd": ..., ...}``
Worker -> main (stdout): one JSON object per line, ``{"status": ...}`` and
``{"id": n, "ok": bool, "error": str|None, "status": ...}`` replies.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import struct
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AUDIO, JSONCMD = b"A", b"J"
_HDR = struct.Struct(">cI")
_AUDIO_HDR = struct.Struct("<?f")


# ---------------------------------------------------------------- worker side --

async def _serve(args: argparse.Namespace) -> None:
    from .recorder import Recorder

    loop = asyncio.get_running_loop()
    rec = Recorder(Path(args.dir), args.rate, args.chunk_s, gap_db=args.gap_db, fmt=args.fmt,
                   mount_check=not args.no_mount_check)
    reader = asyncio.StreamReader(limit=1 << 22)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    out = sys.stdout

    def send(obj: dict[str, Any]) -> None:
        out.write(json.dumps(obj, ensure_ascii=False) + "\n")
        out.flush()

    async def report() -> None:
        while True:
            await asyncio.to_thread(rec.target_ready)  # touches the share: never inline
            send({"status": rec.status()})
            await asyncio.sleep(2)

    def handle(msg: dict[str, Any]) -> None:
        cmd = msg.get("cmd")
        error = None
        try:
            if cmd == "start":
                rec.start(str(msg.get("mode", "all")), bool(msg.get("continuous", False)))
            elif cmd == "stop":
                rec.stop(str(msg.get("reason", "manual")))
            elif cmd == "meta":
                rec.set_track_meta(msg.get("meta"))
            elif cmd == "settings":
                if "gap_db" in msg:
                    rec.gap_db = float(msg["gap_db"])
                if "fmt" in msg:
                    rec.fmt = str(msg["fmt"])
            else:
                error = f"unknown command {cmd!r}"
        except (RuntimeError, ValueError, OSError) as err:
            error = str(err)
        if "id" in msg:
            send({"id": msg["id"], "ok": error is None, "error": error, "status": rec.status()})

    reporter = loop.create_task(report())
    try:
        while True:
            try:
                kind, size = _HDR.unpack(await reader.readexactly(_HDR.size))
                payload = await reader.readexactly(size)
            except asyncio.IncompleteReadError:
                break  # main process gone or shutting down
            if kind == AUDIO:
                playing, db = _AUDIO_HDR.unpack_from(payload)
                rec.feed(payload[_AUDIO_HDR.size:], db, playing)
            elif kind == JSONCMD:
                handle(json.loads(payload))
    finally:
        reporter.cancel()
        rec.stop("shutdown")
        await rec.drain(timeout=40)


def main() -> None:
    p = argparse.ArgumentParser(description="VinylPilot recorder process (started by vinylpilot)")
    p.add_argument("--dir", required=True)
    p.add_argument("--rate", type=int, default=48000)
    p.add_argument("--chunk-s", type=float, default=0.02)
    p.add_argument("--gap-db", type=float, default=-55.0)
    p.add_argument("--fmt", default="flac")
    p.add_argument("--no-mount-check", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve(args))


# ---------------------------------------------------------------- main side ----

class RecorderClient:
    """Drop-in for ``Recorder`` in the main process; the real one runs in a child process.

    Audio is only forwarded while a recording is active. If the child cannot keep up,
    chunks are dropped here (counted) instead of ever blocking the audio loop.
    """

    MAX_BUFFER = 8 << 20  # ~45 s of PCM waiting in our write buffer

    def __init__(self, base_dir: Path, sample_rate: int, chunk_s: float, *, gap_db: float = -55.0,
                 fmt: str = "flac", extra_args: list[str] | None = None,
                 nice: int = 10) -> None:
        self.cmd = [
            *(["nice", "-n", str(nice)] if nice else []),
            sys.executable, "-m", "vinylpilot.record_worker",
            "--dir", str(base_dir), "--rate", str(sample_rate), "--chunk-s", str(chunk_s),
            "--gap-db", str(gap_db), "--fmt", fmt, *(extra_args or []),
        ]
        self.base_dir = base_dir
        self._gap_db = gap_db
        self._fmt = fmt
        self._proc: asyncio.subprocess.Process | None = None
        self._status: dict[str, Any] = {"active": False, "target": str(base_dir), "target_ready": False,
                                        "target_status": "missing", "finished": [], "skipped": [],
                                        "continuous": False, "mode": "all"}
        self._futures: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._tasks: list[asyncio.Task[None]] = []
        self._closing = False
        self.ipc_dropped = 0

    # -- lifecycle --

    async def start(self) -> None:
        await self._spawn()
        self._tasks.append(asyncio.get_running_loop().create_task(self._supervise()))

    async def _spawn(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self.cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=None, limit=1 << 20)  # stderr: straight into our journal
        self._tasks.append(asyncio.get_running_loop().create_task(self._read(self._proc)))

    async def _supervise(self) -> None:
        while not self._closing:
            proc = self._proc
            assert proc is not None
            await proc.wait()
            if self._closing:
                return
            logger.warning("Recorder process exited (%s); restarting", proc.returncode)
            self._status = {**self._status, "active": False}
            for fut in self._futures.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("recorder restarted"))
            self._futures.clear()
            await asyncio.sleep(3)
            await self._spawn()

    async def _read(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while line := await proc.stdout.readline():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "status" in msg:
                self._status = msg["status"]
            fut = self._futures.pop(msg["id"], None) if "id" in msg else None
            if fut is not None and not fut.done():
                fut.set_result(msg)

    async def close(self, timeout: float = 12.0) -> None:
        """Stop recording and give the child time to finish the current track."""
        self._closing = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            if proc.stdin is not None:
                with contextlib.suppress(Exception):
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout)
            except TimeoutError:
                proc.kill()
        for t in self._tasks:
            t.cancel()

    # -- sending --

    def _write(self, kind: bytes, payload: bytes) -> bool:
        proc = self._proc
        if proc is None or proc.returncode is not None or proc.stdin is None:
            return False
        transport = proc.stdin.transport
        if transport.is_closing() or transport.get_write_buffer_size() > self.MAX_BUFFER:
            return False
        proc.stdin.write(_HDR.pack(kind, len(payload)) + payload)
        return True

    def send(self, cmd: str, **kw: Any) -> None:
        self._write(JSONCMD, json.dumps({"cmd": cmd, **kw}).encode())

    async def request(self, cmd: str, timeout: float = 15.0, **kw: Any) -> dict[str, Any]:
        self._next_id += 1
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._futures[self._next_id] = fut
        if not self._write(JSONCMD, json.dumps({"id": self._next_id, "cmd": cmd, **kw}).encode()):
            self._futures.pop(self._next_id, None)
            raise RuntimeError("recorder process not available")
        msg = await asyncio.wait_for(fut, timeout)
        if not msg.get("ok"):
            raise RuntimeError(msg.get("error") or "failed")
        return msg["status"]

    # -- Recorder-like API --

    @property
    def active(self) -> bool:
        return bool(self._status.get("active"))

    @property
    def continuous(self) -> bool:
        return bool(self._status.get("continuous"))

    @property
    def gap_db(self) -> float:
        return self._gap_db

    @gap_db.setter
    def gap_db(self, value: float) -> None:
        if value != self._gap_db:
            self._gap_db = value
            self.send("settings", gap_db=value)

    @property
    def fmt(self) -> str:
        return self._fmt

    @fmt.setter
    def fmt(self, value: str) -> None:
        if value != self._fmt:
            self._fmt = value
            self.send("settings", fmt=value)

    async def start_recording(self, mode: str = "all", continuous: bool = False) -> dict[str, Any]:
        self._status = await self.request("start", mode=mode, continuous=continuous)
        return self._status

    def stop(self, reason: str = "manual") -> None:
        self.send("stop", reason=reason)
        self._status = {**self._status, "active": False}

    def set_track_meta(self, meta: dict[str, Any] | None) -> None:
        if self.active:
            self.send("meta", meta=meta)

    def feed(self, pcm: bytes, window_db: float, playing: bool) -> None:
        if not self.active:
            return
        if not playing:  # silence: no audio, but the child counts the time
            self._write(AUDIO, _AUDIO_HDR.pack(False, window_db))
            return
        if not self._write(AUDIO, _AUDIO_HDR.pack(True, window_db) + pcm):
            self.ipc_dropped += 1

    def status(self) -> dict[str, Any]:
        return {**self._status, "ipc_dropped": self.ipc_dropped}


if __name__ == "__main__":
    main()
