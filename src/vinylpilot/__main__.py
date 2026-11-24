"""VinylPilot core service entry point (MVP)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from pathlib import Path

from aiosendspin.models.types import AudioCodec

from . import __version__
from .capture import ArecordCapture
from .devices import list_usb_capture_devices, resolve_device
from . import settings as settings_mod
from . import system
from .api import API_PORT, CoreApi, LiveLevels
from .levels import LevelMeter, SignalDetector
from .mqtt import MqttPublisher, MqttSettings, Status
from .recognition import TrackRecognizer
from .recorder import Recorder
from .settings import RECORDINGS_DIR
from .stylus import StylusCounter
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
    p.add_argument("--mqtt-host", help="MQTT broker; enables Home Assistant integration")
    p.add_argument("--mqtt-port", type=int, default=1883)
    p.add_argument("--mqtt-user", help="MQTT username (password via VINYLPILOT_MQTT_PASSWORD)")
    p.add_argument("--recognize", choices=["off", "shazam"], default="off",
                   help="identify the playing track (shazam: unofficial API, needs internet)")
    p.add_argument("--gap-threshold", type=float, default=-55.0,
                   help="level below which a pause between tracks is assumed (default: -55 dBFS)")
    p.add_argument("--recordings-dir", type=Path, default=RECORDINGS_DIR,
                   help=f"mounted network share for recordings (default: {RECORDINGS_DIR})")
    p.add_argument("--api-port", type=int, default=API_PORT, help="local control API port (127.0.0.1)")
    p.add_argument("--show-levels", action="store_true", help="log input levels once per second")
    p.add_argument("--list-devices", action="store_true", help="list USB capture devices and exit")
    p.add_argument("--state-dir", type=Path, default=_default_state_dir(), help="where identity and pairing are stored")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    return p.parse_args(argv)


async def run(args: argparse.Namespace) -> None:
    # Never block startup on the turntable: Music Assistant and MQTT must come up even
    # when the turntable is switched off. Capture finds the device on its own.
    def resolve() -> str | None:
        try:
            return resolve_device(args.device)[0]
        except LookupError:
            return None

    device = args.device
    logger.info("VinylPilot %s — input: %s", __version__, device)
    started_at = time.time()

    settings_file = args.state_dir / "settings.json"
    cfg = settings_mod.load(settings_file, settings_mod.Settings(
        threshold_db=args.threshold, hold_s=args.hold, gap_db=args.gap_threshold,
        recognize=args.recognize == "shazam"))
    stylus = StylusCounter(args.state_dir / "stylus.json")

    capture = ArecordCapture(device, args.rate, resolve=resolve if args.device == "auto" else None)
    meter = LevelMeter(window_chunks=max(1, int(0.5 / capture.chunk_s)))  # 0.5 s window
    detector = SignalDetector(cfg.threshold_db, args.activation, cfg.hold_s)
    status = Status(turntable_name="", threshold_db=cfg.threshold_db, hold_s=cfg.hold_s)
    live = LiveLevels()
    recorder = Recorder(args.recordings_dir, args.rate, capture.chunk_s,
                        gap_db=cfg.gap_db, fmt=cfg.recording_format)
    source = SendspinSource(
        name=args.name,
        state_dir=args.state_dir,
        sample_rate=args.rate,
        codec=AudioCodec(args.codec),
        server_url=args.server_url,
        port=args.port,
    )
    await source.start()

    def on_track(track: dict | None) -> None:
        status.now_playing = track
        recorder.set_track_meta(track)
        status.changed.set()

    recognizer = TrackRecognizer(args.rate, capture.chunk_s, on_track, gap_db=cfg.gap_db)
    if cfg.recognize:
        logger.info("Track recognition enabled (Shazam)")

    def apply_settings(data: dict) -> dict:
        changed = cfg.update(data)
        detector.threshold_db = status.threshold_db = cfg.threshold_db
        detector.hold_s = status.hold_s = cfg.hold_s
        recognizer.gap_db = recorder.gap_db = cfg.gap_db
        recorder.fmt = cfg.recording_format
        if "recognize" in changed and not cfg.recognize:
            recognizer.reset()
        if changed:
            settings_mod.save(settings_file, cfg)
            logger.info("Settings changed: %s", ", ".join(changed))
            status.changed.set()
        return {"changed": changed, "settings": cfg.to_dict()}

    def reset_stylus(_data: dict) -> dict:
        stylus.reset()
        status.changed.set()
        return {}

    def start_recording(_data: dict) -> dict:
        recorder.start()
        return {"recording": recorder.status()}

    def stop_recording(_data: dict) -> dict:
        recorder.stop()
        return {"recording": recorder.status()}

    def get_status() -> dict:
        boot = system.boot_time()
        return {
            "version": __version__,
            "name": args.name,
            "started_at": started_at,
            "turntable": {"connected": capture.running, "device": capture.device,
                          "name": status.turntable_name},
            "format": {"sample_rate": args.rate, "bit_depth": 16, "channels": 2, "codec": args.codec},
            "record_playing": detector.present,
            "streaming": source.streaming,
            "music_assistant": source.connected,
            "mqtt": {"enabled": bool(args.mqtt_host), "host": args.mqtt_host,
                     "connected": publisher.connected if publisher else False},
            "levels": {"rms": [status.level_left, status.level_right]},
            "now_playing": status.now_playing,
            "settings": cfg.to_dict(),
            "stylus": {"hours": round(stylus.hours, 2), "life_h": cfg.stylus_life_h, "since": stylus.since},
            "recording": recorder.status(),
            "system": {
                "cpu": cpu.percent(), "memory": system.memory_used_percent(),
                "process_memory": system.process_rss_mb(), "temperature": system.cpu_temperature(),
                "wifi_rssi": system.wifi_rssi(), "boot_time": boot,
            },
        }

    cpu = system.CpuMeter()
    tasks: list[asyncio.Task[None]] = []
    publisher: MqttPublisher | None = None
    api = CoreApi(get_status, {
        "settings": apply_settings,
        "stylus_reset": reset_stylus,
        "record_start": start_recording,
        "record_stop": stop_recording,
    }, live, port=args.api_port)
    try:
        await api.start()
    except OSError as err:
        logger.warning("Control API unavailable: %s", err)

    if args.mqtt_host:
        publisher = MqttPublisher(
            MqttSettings(
                host=args.mqtt_host,
                port=args.mqtt_port,
                username=args.mqtt_user,
                password=os.environ.get("VINYLPILOT_MQTT_PASSWORD") or None,
            ),
            status,
            name=args.name,
            on_threshold=lambda v: apply_settings({"threshold_db": v}),
            on_hold=lambda v: apply_settings({"hold_s": v}),
            on_stylus_reset=lambda: reset_stylus({}),
            stylus_hours=lambda: round(stylus.hours, 2),
        )
        tasks.append(asyncio.create_task(publisher.run()))

    chunk_us = int(capture.chunk_s * 1_000_000)
    last_report = 0.0
    loop = asyncio.get_running_loop()
    try:
        async for pcm in capture.chunks():
            if not pcm:
                # Turntable vanished (USB unplugged, powered down): that is "no record playing".
                if detector.reset():
                    logger.info("Record stopped (input lost)")
                    await source.set_signal(False)
                recorder.feed(b"", -120.0, False)
                if cfg.recognize:
                    recognizer.feed(b"", -120.0, False)
                status.turntable_connected = status.record_playing = False
                status.changed.set()
                continue
            if not status.turntable_name:
                status.turntable_name = _card_name(capture.device)
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
            if detector.present:
                stylus.add(capture.chunk_s)
            if cfg.recognize:
                recognizer.feed(pcm, meter.window_rms_db, detector.present)
            recorder.feed(pcm, meter.window_rms_db, detector.present)
            live.feed(pcm, (levels.rms_left, levels.rms_right), (levels.peak_left, levels.peak_right))
            _update_status(status, capture, source, detector, levels)
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
        # Shut down quickly: every step is bounded so systemd never has to SIGKILL us.
        stylus.save()
        recorder.stop()
        recognizer.close()
        for task in tasks:
            task.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
        except TimeoutError:
            logger.warning("MQTT did not stop in time")
        for name, coro in (("API", api.stop()), ("Sendspin", source.stop())):
            try:
                await asyncio.wait_for(coro, 3)
            except (TimeoutError, Exception) as err:  # noqa: BLE001
                logger.warning("%s did not stop cleanly: %s", name, err or "timeout")
        logger.info("Stopped")


def _card_name(device: str) -> str:
    for d in list_usb_capture_devices():
        if d.alsa_name == device or device.endswith(d.card_id):
            return d.name
    return device


def _update_status(status: Status, capture, source, detector, levels) -> None:  # noqa: ANN001
    new = (capture.running, detector.present, source.streaming, source.connected)
    old = (status.turntable_connected, status.record_playing, status.streaming, status.server_connected)
    status.level_left, status.level_right = levels.rms_left, levels.rms_right
    if new != old:
        (status.turntable_connected, status.record_playing,
         status.streaming, status.server_connected) = new
        status.changed.set()


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
