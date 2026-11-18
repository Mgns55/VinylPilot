"""VinylPilot core service entry point (MVP)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path

from aiosendspin.models.types import AudioCodec

from . import __version__
from .capture import ArecordCapture
from .devices import list_usb_capture_devices, resolve_device
from .levels import LevelMeter, SignalDetector
from .sendspin_source import LISTEN_PORT, SendspinSource

logger = logging.getLogger("vinylpilot")


def _default_state_dir() -> Path:
    # systemd sets STATE_DIRECTORY when the unit uses StateDirectory=
    if env := os.environ.get("STATE_DIRECTORY"):
        return Path(env)
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "vinylpilot"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="vinylpilot", description="USB turntable → Music Assistant")
    p.add_argument("--device", default="auto", help="ALSA capture device, e.g. hw:CARD=PSHX500,DEV=0 (default: auto)")
    p.add_argument("--rate", type=int, default=48000, help="sample rate in Hz (default: 48000)")
    p.add_argument("--codec", choices=["pcm", "flac"], default="pcm", help="stream codec (default: pcm)")
    p.add_argument("--name", default="VinylPilot", help="name shown in Music Assistant")
    p.add_argument("--server-url", help="connect to this server instead of waiting for discovery, e.g. ws://192.168.1.10:8927/sendspin")
    p.add_argument("--port", type=int, default=LISTEN_PORT, help=f"listen port (default: {LISTEN_PORT})")
    p.add_argument("--threshold", type=float, default=-65.0, help="signal threshold in dBFS RMS (default: -65)")
    p.add_argument("--activation", type=float, default=1.0, help="seconds above threshold before 'playing' (default: 1)")
    p.add_argument("--hold", type=float, default=10.0, help="seconds below threshold before 'stopped' (default: 10)")
    p.add_argument("--show-levels", action="store_true", help="log input levels once per second")
    p.add_argument("--list-devices", action="store_true", help="list USB capture devices and exit")
    p.add_argument("--state-dir", type=Path, default=_default_state_dir(), help="where identity and pairing are stored")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    return p.parse_args(argv)


async def run(args: argparse.Namespace) -> None:
    device, label = resolve_device(args.device)
    logger.info("VinylPilot %s — input: %s (%s)", __version__, label, device)

    capture = ArecordCapture(device, args.rate)
    meter = LevelMeter(window_chunks=max(1, int(0.5 / capture.chunk_s)))  # 0.5 s window
    detector = SignalDetector(args.threshold, args.activation, args.hold)
    source = SendspinSource(
        name=args.name,
        state_dir=args.state_dir,
        sample_rate=args.rate,
        codec=AudioCodec(args.codec),
        server_url=args.server_url,
        port=args.port,
    )
    await source.start()

    chunk_us = int(capture.chunk_s * 1_000_000)
    last_report = 0.0
    loop = asyncio.get_running_loop()
    try:
        async for pcm in capture.chunks():
            now_us = source.now_us()
            # The chunk's first sample was captured roughly one chunk ago.
            ts = now_us - chunk_us if now_us is not None else None
            levels = meter.process(pcm)
            if detector.update(meter.window_rms_db, capture.chunk_s):
                logger.info("Record %s", "playing" if detector.present else "stopped")
                await source.set_signal(detector.present)
            try:
                await source.feed(pcm, ts)
            except Exception:  # noqa: BLE001 - capture and detection keep running
                logger.exception("Unexpected error while streaming; continuing")
            if args.show_levels and loop.time() - last_report >= 1.0:
                last_report = loop.time()
                logger.info(
                    "L %6.1f  R %6.1f dBFS RMS | peak L %6.1f R %6.1f | window %6.1f | %s | %s",
                    levels.rms_left, levels.rms_right, levels.peak_left, levels.peak_right,
                    meter.window_rms_db,
                    "PLAYING" if detector.present else "idle",
                    "streaming" if source.streaming else "not streaming",
                )
    finally:
        await source.stop()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.verbose:
        logging.getLogger("aiosendspin").setLevel(logging.WARNING)

    if args.list_devices:
        devices = list_usb_capture_devices()
        if not devices:
            print("No USB capture devices found.")
        for d in devices:
            print(f"card {d.index}: {d.name}  ->  {d.alsa_name}")
        return

    async def _main() -> None:
        task = asyncio.current_task()
        assert task is not None
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, task.cancel)
        with contextlib.suppress(asyncio.CancelledError):
            await run(args)

    try:
        asyncio.run(_main())
    except LookupError as err:
        raise SystemExit(f"error: {err}") from None


if __name__ == "__main__":
    main()
