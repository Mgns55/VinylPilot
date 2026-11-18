"""End-to-end: VinylPilot's SendspinSource against a real aiosendspin server.

Needs the aiosendspin server extra (pillow); skipped otherwise. Pairing is pre-staged
with a pairing PSK, which is what Music Assistant's code entry produces.
"""

from __future__ import annotations

import asyncio
import math
import struct
from typing import Any

import pytest

pytest.importorskip("PIL")

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402
from aiosendspin.models.types import SignalState  # noqa: E402
from aiosendspin.noise import FileClientPairingStore  # noqa: E402
from aiosendspin.noise.keys import Identity, generate_psk, psk_id_for  # noqa: E402
from aiosendspin.noise.trust_store import InMemoryServerPairingStore, PairingPsk  # noqa: E402
from aiosendspin.server.roles.source import SourceStreamStartedEvent  # noqa: E402
from aiosendspin.server.roles.source.events import SourceSignalChangedEvent  # noqa: E402
from aiosendspin.server.server import SendspinServer  # noqa: E402

from vinylpilot.sendspin_source import SendspinSource, load_identity  # noqa: E402


def _chunk(n: int, frames: int = 960) -> bytes:
    out = bytearray()
    for i in range(frames):
        out += struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * (n * frames + i) / 48000))) * 2
    return bytes(out)


async def test_source_pairs_streams_and_reports_signal(tmp_path):
    server_store = InMemoryServerPairingStore()
    server = SendspinServer(
        loop=asyncio.get_running_loop(),
        identity=Identity.generate(),
        server_name="test-ma",
        pairing_store=server_store,
    )
    identity = load_identity(tmp_path / "identity.json")
    client_store = await FileClientPairingStore.open(tmp_path / "pairing-store.json")
    psk = generate_psk()
    pp = PairingPsk(psk_id=psk_id_for(psk), psk=psk)
    await client_store.set_pairing_psk(pp)
    await server_store.stage_pairing_psk(identity.peer_id, pp)

    app = web.Application()
    app.router.add_get(SendspinServer.API_PATH, server.on_client_connect)
    ts = TestServer(app)
    await ts.start_server()
    url = f"ws://127.0.0.1:{ts.port}{SendspinServer.API_PATH}"

    source = SendspinSource(name="VinylPilot", state_dir=tmp_path, sample_rate=48000, server_url=url)
    events: list[Any] = []
    try:
        await source.start()

        async def pump() -> None:
            n = 0
            while True:
                now = source.now_us()
                await source.feed(_chunk(n), now - 20_000 if now is not None else None)
                n += 1
                await asyncio.sleep(0.02)

        pump_task = asyncio.create_task(pump())

        # Wait until the server has the client with an active source role (may need a reconnect after pairing).
        role = None
        async with asyncio.timeout(15):
            while role is None:
                sc = server.get_client(identity.peer_id)
                if sc is not None and "source@v1" in sc.active_role_ids:
                    role = sc.role("source@v1")
                    sc.add_event_listener(lambda _c, e: events.append(e))
                await asyncio.sleep(0.05)

        await source.set_signal(True)
        role.request_start()

        received = bytearray()
        async with asyncio.timeout(10):
            while not any(isinstance(e, SourceStreamStartedEvent) for e in events):
                await asyncio.sleep(0.02)
        handle = next(e for e in events if isinstance(e, SourceStreamStartedEvent)).handle

        async def drain() -> None:
            async for chunk, _ts in handle:
                received.extend(chunk)
                if len(received) > 48000 * 4 // 2:  # 0.5 s of audio
                    return

        await asyncio.wait_for(drain(), timeout=10)
        assert source.streaming
        assert any(
            isinstance(e, SourceSignalChangedEvent) and e.signal is SignalState.PRESENT for e in events
        )

        # Abrupt server loss mid-stream must not end VinylPilot.
        await ts.close()
        await server.close()
        await asyncio.sleep(1.5)
        assert not pump_task.done(), pump_task.exception() if pump_task.done() else None
        pump_task.cancel()
    finally:
        await source.stop()
        await ts.close()
        await server.close()
