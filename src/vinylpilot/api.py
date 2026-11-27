"""Core control API on 127.0.0.1, used by the separate web interface process.

Live levels are only sent while a client is subscribed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from aiohttp import WSMsgType, web

logger = logging.getLogger(__name__)

API_HOST = "127.0.0.1"
API_PORT = 8099
FRAME_INTERVAL_S = 1 / 15


class LiveLevels:
    """Latest levels for subscribed clients (the level meters in the web interface)."""

    def __init__(self) -> None:
        self.subscribers = 0
        self.rms = (-120.0, -120.0)
        self.peak = (-120.0, -120.0)

    def feed(self, pcm: bytes, rms: tuple[float, float], peak: tuple[float, float]) -> None:
        if self.subscribers:
            self.rms, self.peak = rms, peak

    def take_frame(self) -> dict[str, Any]:
        return {"rms": [round(v, 1) for v in self.rms], "peak": [round(v, 1) for v in self.peak]}


class CoreApi:
    def __init__(
        self,
        get_status: Callable[[], dict[str, Any]],
        handlers: dict[str, Callable[[dict[str, Any]], Any]],
        live: LiveLevels,
        host: str = API_HOST,
        port: int = API_PORT,
    ) -> None:
        self.get_status = get_status
        self.handlers = handlers
        self.live = live
        self.host, self.port = host, port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/status", self._status)
        app.router.add_get("/ws", self._ws)
        app.router.add_post("/action/{name}", self._action)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        logger.info("Control API on http://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _status(self, _req: web.Request) -> web.Response:
        return web.json_response(self.get_status())

    async def _action(self, req: web.Request) -> web.Response:
        name = req.match_info["name"]
        handler = self.handlers.get(name)
        if handler is None:
            return web.json_response({"ok": False, "error": "unknown action"}, status=404)
        try:
            body = await req.json() if req.can_read_body else {}
        except json.JSONDecodeError:
            body = {}
        try:
            result = handler(body if isinstance(body, dict) else {})
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as err:  # noqa: BLE001
            return web.json_response({"ok": False, "error": str(err)}, status=400)
        return web.json_response({"ok": True, **(result or {})})

    async def _ws(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(req)
        self.live.subscribers += 1
        try:
            reader = asyncio.ensure_future(self._drain(ws))
            while not ws.closed and not reader.done():
                await ws.send_json(self.live.take_frame())
                await asyncio.sleep(FRAME_INTERVAL_S)
            reader.cancel()
        except (ConnectionResetError, RuntimeError):
            pass
        finally:
            self.live.subscribers -= 1
        return ws

    @staticmethod
    async def _drain(ws: web.WebSocketResponse) -> None:
        async for msg in ws:
            if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
