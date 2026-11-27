"""VinylPilot web interface (separate process from the audio core).

Serves the static UI on port 80, forwards /api/core/* to the core's local control API,
and adds diagnostics and the update function. Restarting this process never touches audio.
No arbitrary shell commands: every subprocess here is a fixed, hard-coded command.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import logging
import os
import re
import time
from importlib import resources
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from . import __version__
from .api import API_HOST, API_PORT
from .devices import list_usb_capture_devices

logger = logging.getLogger("vinylpilot.web")

INSTALL_DIR = Path(os.environ.get("VINYLPILOT_INSTALL_DIR", "/opt/vinylpilot"))
UPDATE_UNIT = "vinylpilot-update.service"
DEFAULT_UPDATE_S = 75  # typical on a Pi Zero 2 W without dependency changes
STATIC = resources.files("vinylpilot") / "web_static"


async def _run(*cmd: str, timeout: float = 15) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return proc.returncode or 0, out.decode(errors="replace")
    except (OSError, TimeoutError) as err:
        return 127, str(err)


def _git(*args: str) -> tuple[str, ...]:
    return ("git", "-c", f"safe.directory={INSTALL_DIR}", "-C", str(INSTALL_DIR), *args)


def _tail(path: Path, lines: int) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()[-lines:]
    except OSError:
        return []


class WebApp:
    def __init__(self, core_url: str, password: str | None) -> None:
        self.core_url = core_url
        self.password = password
        self.session: aiohttp.ClientSession | None = None
        self._update_cache: tuple[float, dict[str, Any]] | None = None

    # ---- app ------------------------------------------------------------------

    def build(self) -> web.Application:
        app = web.Application(middlewares=[self._auth])
        app.on_startup.append(self._startup)
        app.on_cleanup.append(self._cleanup)
        app.router.add_get("/", self._index)
        app.router.add_get("/api/core/ws", self._proxy_ws)
        app.router.add_route("*", "/api/core/{path:.*}", self._proxy)
        app.router.add_get("/api/diagnostics", self._diagnostics)
        app.router.add_get("/api/logs", self._logs)
        app.router.add_get("/api/update", self._update_info)
        app.router.add_post("/api/update", self._update_start)
        app.router.add_static("/static/", str(STATIC), append_version=True)
        return app

    async def _startup(self, _app: web.Application) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

    async def _cleanup(self, _app: web.Application) -> None:
        if self.session:
            await self.session.close()

    @web.middleware
    async def _auth(self, req: web.Request, handler: Any) -> web.StreamResponse:
        if not self.password:
            return await handler(req)
        header = req.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
            except ValueError:
                pw = ""
            if hmac.compare_digest(pw, self.password):
                return await handler(req)
        return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="VinylPilot"'})

    async def _index(self, _req: web.Request) -> web.Response:
        html = (STATIC / "index.html").read_text()
        return web.Response(text=html.replace("{{version}}", __version__), content_type="text/html")

    # ---- core proxy -------------------------------------------------------------

    async def _proxy(self, req: web.Request) -> web.Response:
        assert self.session is not None
        path = req.match_info["path"]
        if not re.fullmatch(r"status|action/[a-z_]+", path):
            return web.json_response({"error": "not found"}, status=404)
        body = await req.read() if req.can_read_body else None
        try:
            async with self.session.request(
                req.method, f"{self.core_url}/{path}", data=body,
                headers={"Content-Type": "application/json"} if body else None,
            ) as resp:
                return web.Response(body=await resp.read(), status=resp.status,
                                    content_type="application/json")
        except (aiohttp.ClientError, TimeoutError):
            return web.json_response({"error": "core not running"}, status=503)

    async def _proxy_ws(self, req: web.Request) -> web.WebSocketResponse:
        assert self.session is not None
        client = web.WebSocketResponse(heartbeat=20)
        await client.prepare(req)
        try:
            async with self.session.ws_connect(f"{self.core_url}/ws", timeout=aiohttp.ClientWSTimeout(ws_close=5)) as core:
                async def pump_down() -> None:
                    async for msg in core:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await client.send_str(msg.data)
                down = asyncio.ensure_future(pump_down())
                async for _msg in client:  # until the browser goes away
                    pass
                down.cancel()
        except (aiohttp.ClientError, ConnectionResetError, TimeoutError):
            pass
        await client.close()
        return client

    # ---- diagnostics -------------------------------------------------------------

    async def _diagnostics(self, _req: web.Request) -> web.Response:
        usb = []
        for dev in sorted(Path("/sys/bus/usb/devices").glob("*")):
            product = dev / "product"
            if product.exists() and ":" not in dev.name:
                vid = (dev / "idVendor").read_text().strip() if (dev / "idVendor").exists() else ""
                pid = (dev / "idProduct").read_text().strip() if (dev / "idProduct").exists() else ""
                usb.append({"id": f"{vid}:{pid}", "name": product.read_text().strip()})
        alsa = [{"card": d.index, "name": d.name, "device": d.alsa_name} for d in list_usb_capture_devices()]
        gw = ""
        code, out = await _run("ip", "-4", "route", "show", "default")
        if m := re.search(r"via (\S+)", out):
            gw = m.group(1)
        gw_ok = False
        if gw:
            code, _ = await _run("ping", "-c", "1", "-W", "2", gw)
            gw_ok = code == 0
        _, netwatch = await _run("journalctl", "-t", "vinylpilot-netwatch", "-n", "5", "--no-pager", "-o", "short-iso")
        return web.json_response({
            "usb": usb, "alsa": alsa,
            "network": {"gateway": gw, "gateway_reachable": gw_ok, "netwatch": [
                ln for ln in netwatch.strip().splitlines()
                if ln and not ln.startswith("--") and "No journal" not in ln][-5:]},
            "hostname": os.uname().nodename,
            "netfail": _tail(Path("/var/lib/vinylpilot/netfail.log"), 60),
        })

    async def _logs(self, req: web.Request) -> web.Response:
        lines = min(int(req.query.get("lines", "150") or 150), 500)
        _, out = await _run("journalctl", "-u", "vinylpilot", "-u", "vinylpilot-web",
                            "-u", "vinylpilot-update", "-n", str(lines), "--no-pager", "-o", "short")
        return web.json_response({"lines": out.splitlines()})

    # ---- update ------------------------------------------------------------------

    async def _update_info(self, req: web.Request) -> web.Response:
        if self._update_cache and time.time() - self._update_cache[0] < 300 and "refresh" not in req.query:
            info = dict(self._update_cache[1])
        else:
            c1, local = await _run(*_git("rev-parse", "HEAD"))
            c2, date = await _run(*_git("log", "-1", "--format=%cs"))
            c3, remote = await _run(*_git("ls-remote", "origin", "refs/heads/main"), timeout=20)
            local = local.strip() if c1 == 0 else ""
            date = date if c2 == 0 else ""
            remote = (remote.split() or [""])[0] if c3 == 0 else ""
            info = {
                "version": __version__,
                "commit": local[:7],
                "commit_date": date.strip(),
                "latest": remote[:7],
                "update_available": bool(remote) and bool(local) and remote != local,
                "check_ok": bool(remote),
            }
            self._update_cache = (time.time(), info)
        _, state = await _run("systemctl", "is-active", UPDATE_UNIT)
        info["running"] = state.strip() in ("active", "activating")
        info["estimate_s"] = await self._last_update_duration() or DEFAULT_UPDATE_S
        _, log = await _run("journalctl", "-u", UPDATE_UNIT, "-n", "30", "--no-pager", "-o", "cat")
        info["log"] = log.strip().splitlines()[-30:]
        return web.json_response(info)

    async def _last_update_duration(self) -> int | None:
        """Duration of the previous update run, used as the next countdown."""
        code, out = await _run("systemctl", "show", UPDATE_UNIT, "-p",
                               "ExecMainStartTimestampMonotonic,ExecMainExitTimestampMonotonic")
        if code != 0:
            return None
        vals = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
        try:
            start = int(vals.get("ExecMainStartTimestampMonotonic", "0"))
            end = int(vals.get("ExecMainExitTimestampMonotonic", "0"))
        except ValueError:
            return None
        if start and end > start:
            # + time for the services to come back after the installer exits
            return min(900, max(30, round((end - start) / 1e6) + 15))
        return None

    async def _update_start(self, _req: web.Request) -> web.Response:
        # Allowed for the vinylpilot user by a polkit rule for exactly this unit.
        code, out = await _run("systemctl", "start", "--no-block", UPDATE_UNIT)
        self._update_cache = None
        if code != 0:
            return web.json_response({"ok": False, "error": out.strip()}, status=500)
        return web.json_response({"ok": True})


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="vinylpilot-web")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=80)
    p.add_argument("--core", default=f"http://{API_HOST}:{API_PORT}")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    password = os.environ.get("VINYLPILOT_WEB_PASSWORD") or None
    app = WebApp(args.core, password).build()
    logger.info("Web interface on http://%s:%d%s", args.host, args.port, " (password protected)" if password else "")
    web.run_app(app, host=args.host, port=args.port, access_log=None, print=None)


if __name__ == "__main__":
    main()
