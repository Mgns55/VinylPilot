"""Sendspin source role: pairing, connection handling, streaming and signal reporting.

Built on aiosendspin (Apache-2.0). aiosendspin has no public accessor for the admitted
connection, which is where source signal state is reported; that access is isolated in
``_connection()`` so a future public API can replace it in one place.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiosendspin.client import ClientListener, PairingSupport, SendspinClient
from aiosendspin.models.core import DeviceInfo
from aiosendspin.models.player import SupportedAudioFormat
from aiosendspin.models.source import ClientHelloSourceFeatures, ClientHelloSourceSupport
from aiosendspin.models.types import AudioCodec, Roles, SignalState
from aiosendspin.noise import FileClientPairingStore, Identity, b64url_decode, b64url_encode

from . import __version__

if TYPE_CHECKING:
    from aiohttp import web
    from aiosendspin.client import SourceCapture

logger = logging.getLogger(__name__)

LISTEN_PORT = 8928


def load_identity(path: Path) -> Identity:
    """Load the persistent client identity, creating it (mode 0600) on first run."""
    try:
        data = json.loads(path.read_text())
        return Identity.from_private_bytes(b64url_decode(data["private_key"]))
    except FileNotFoundError:
        pass
    identity = Identity.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"private_key": b64url_encode(identity.private_bytes)}))
    logger.info("Created new client identity in %s", path)
    return identity


class SendspinSource:
    """Exposes the turntable to Music Assistant as a Sendspin source."""

    def __init__(
        self,
        *,
        name: str,
        state_dir: Path,
        sample_rate: int,
        codec: AudioCodec = AudioCodec.PCM,
        server_url: str | None = None,
        port: int = LISTEN_PORT,
    ) -> None:
        self._name = name
        self._state_dir = state_dir
        self._format = SupportedAudioFormat(
            codec=codec, channels=2, sample_rate=sample_rate, bit_depth=16
        )
        self._server_url = server_url
        self._port = port
        self._client: SendspinClient | None = None
        self._listener: ClientListener | None = None
        self._capture: SourceCapture | None = None
        self._capture_conn: Any = None
        self._signal_conn: Any = None
        self.signal_present = False

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        identity = load_identity(self._state_dir / "identity.json")
        store = await FileClientPairingStore.open(self._state_dir / "pairing-store.json")
        self._client = SendspinClient(
            identity,
            self._name,
            [Roles.SOURCE],
            pairing_store=store,
            device_info=DeviceInfo(
                product_name="VinylPilot",
                manufacturer="Nordparklab",
                software_version=__version__,
            ),
            # line_sense: we report signal present/absent, which drives MA autostart.
            source_support=ClientHelloSourceSupport(
                features=ClientHelloSourceFeatures(line_sense=True)
            ),
            pairing_support=PairingSupport(pairing_code_display=self._show_pairing_code),
        )
        self._client.add_pairing_abort_listener(
            lambda reason: logger.warning("Pairing failed: %s", reason.value)
        )
        self._client.add_disconnect_listener(lambda: logger.info("Server connection closed"))
        logger.info("Sendspin client id: %s", identity.peer_id)

        if self._server_url:
            asyncio.get_running_loop().create_task(self._connect_loop(self._server_url))
        else:
            self._listener = ClientListener(
                client_id=identity.peer_id,
                on_connection=self._handle_incoming,
                port=self._port,
                client_name=self._name,
            )
            await self._listener.start()
            logger.info("Waiting for Music Assistant on port %d (mDNS _sendspin._tcp)", self._port)

    async def stop(self) -> None:
        if self._capture is not None:
            with contextlib.suppress(Exception):
                await self._capture.stop()
        if self._listener is not None:
            await self._listener.stop()
        if self._client is not None:
            await self._client.disconnect()

    async def _show_pairing_code(self, pairing_code: str | None, *, grouped: str | None) -> None:
        if grouped is not None:
            logger.warning("PAIRING: enter code %s in Music Assistant", grouped)

    async def _handle_incoming(self, ws: web.WebSocketResponse) -> None:
        assert self._client is not None
        logger.info("Incoming connection from a Sendspin server")
        try:
            await self._client.attach_websocket(ws)
        except Exception as err:  # noqa: BLE001
            logger.warning("Connection ended: %s", err)

    async def _connect_loop(self, url: str) -> None:
        assert self._client is not None
        backoff = 1.0
        while True:
            try:
                await self._client.connect(url)
                logger.info("Connected to %s", url)
                backoff = 1.0
                gone = asyncio.Event()
                unsubscribe = self._client.add_disconnect_listener(gone.set)
                await gone.wait()
                unsubscribe()
            except Exception as err:  # noqa: BLE001
                logger.warning("Cannot reach %s (%s); retrying in %.0f s", url, err, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # ---- per-chunk work --------------------------------------------------

    def _connection(self) -> Any:
        """The admitted connection, if any (private aiosendspin attribute)."""
        if self._client is None:
            return None
        return getattr(self._client, "_admitted_connection", None)

    @property
    def streaming(self) -> bool:
        conn = self._connection()
        return bool(self._capture is not None and conn is not None and conn.is_source_stream_active())

    async def set_signal(self, present: bool) -> None:
        self.signal_present = present
        self._signal_conn = None  # force a resend
        await self._sync_signal()

    async def _sync_signal(self) -> None:
        conn = self._connection()
        if conn is None or conn is self._signal_conn or not conn.is_time_synchronized():
            return
        try:
            await conn.send_source_signal(
                SignalState.PRESENT if self.signal_present else SignalState.ABSENT
            )
        except RuntimeError as err:
            logger.debug("Signal not sent yet: %s", err)
            return
        except Exception as err:  # noqa: BLE001 - a dropped socket must never stop capture
            logger.warning("Could not report signal (%s: %s)", type(err).__name__, err)
            return
        self._signal_conn = conn
        logger.info("Reported signal %s", "present" if self.signal_present else "absent")

    async def feed(self, pcm: bytes, capture_timestamp_us: int | None) -> None:
        """Called for every captured chunk: report signal, start/stop and stream."""
        assert self._client is not None
        await self._sync_signal()
        conn = self._connection()
        if conn is None:
            self._capture = None
            return

        if self._capture is not None and (
            self._capture_conn is not conn
            or not (conn.is_source_stream_active() or conn.is_source_start_authorized())
        ):
            logger.info("Streaming stopped")
            self._capture = None

        if self._capture is None and conn.is_source_start_authorized():
            if not conn.is_time_synchronized():
                return
            try:
                capture = self._client.create_source_capture(self._format)
                await capture.start()
            except RuntimeError as err:
                logger.debug("Cannot start stream yet: %s", err)
                return
            except Exception as err:  # noqa: BLE001
                logger.warning("Could not start stream (%s: %s)", type(err).__name__, err)
                return
            self._capture = capture
            self._capture_conn = conn
            fmt = capture.audio_format
            logger.info(
                "Streaming started: %s %d Hz / %d-bit / %d ch",
                capture.codec.value, fmt.sample_rate, fmt.bit_depth, fmt.channels,
            )

        if self._capture is not None and conn.is_source_stream_active():
            try:
                await self._capture.feed(pcm, capture_timestamp_us)
            except RuntimeError as err:
                logger.info("Stream ended: %s", err)
                self._capture = None
            except Exception as err:  # noqa: BLE001
                logger.warning("Stream interrupted (%s: %s)", type(err).__name__, err)
                self._capture = None

    def now_us(self) -> int | None:
        return self._client.now_us() if self._client is not None else None
