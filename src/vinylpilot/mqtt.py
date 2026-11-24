"""Optional MQTT publishing with Home Assistant MQTT Discovery.

Runs as its own asyncio task and never blocks audio. Reconnects with backoff.
Availability uses a retained last-will message, so Home Assistant shows the device
as unavailable when the Pi or the process goes away.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import aiomqtt

from . import __version__, system

logger = logging.getLogger(__name__)


@dataclass
class Status:
    """Live state shared between the audio loop and MQTT."""

    turntable_connected: bool = False
    turntable_name: str = ""
    record_playing: bool = False
    streaming: bool = False
    server_connected: bool = False
    level_left: float = -120.0
    level_right: float = -120.0
    threshold_db: float = -65.0
    hold_s: float = 10.0
    now_playing: dict[str, Any] | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class MqttSettings:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    discovery_prefix: str = "homeassistant"
    level_interval_s: float = 5.0
    system_interval_s: float = 30.0


# Shown as cover when nothing is recognised; an empty URL makes Home Assistant log image errors.
COVER_PLACEHOLDER = (
    "https://raw.githubusercontent.com/Mgns55/VinylPilot/main/assets/brand/png/icon-square-dark-512.png"
)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_") or "vinylpilot"


class MqttPublisher:
    def __init__(
        self,
        settings: MqttSettings,
        status: Status,
        *,
        name: str,
        on_threshold: Callable[[float], None],
        on_hold: Callable[[float], None],
    ) -> None:
        self.s = settings
        self.status = status
        self.name = name
        self.node = _slug(system.hostname())
        self.base = f"vinylpilot/{self.node}"
        self.avail = f"{self.base}/availability"
        self._on_threshold = on_threshold
        self._on_hold = on_hold
        self._cpu = system.CpuMeter()

    # ---- discovery ---------------------------------------------------------

    def _device(self) -> dict[str, Any]:
        return {
            "identifiers": [f"vinylpilot_{self.node}"],
            "name": self.name,
            "manufacturer": "Nordparklab",
            "model": "VinylPilot",
            "sw_version": __version__,
        }

    def _entities(self) -> list[tuple[str, str, dict[str, Any]]]:
        st = f"{self.base}/state"
        sysst = f"{self.base}/system"
        b = lambda key, name, dc=None, **kw: ("binary_sensor", key, {  # noqa: E731
            "name": name, "state_topic": st, "value_template": f"{{{{ 'ON' if value_json.{key} else 'OFF' }}}}",
            **({"device_class": dc} if dc else {}), **kw})
        s = lambda key, name, topic, **kw: ("sensor", key, {  # noqa: E731
            "name": name, "state_topic": topic, "value_template": f"{{{{ value_json.{key} }}}}", **kw})
        diag = {"entity_category": "diagnostic"}
        return [
            b("turntable_connected", "Turntable connected", "connectivity"),
            b("record_playing", "Record playing", "sound"),
            b("streaming", "Streaming", "running"),
            b("server_connected", "Music Assistant connected", "connectivity", **diag),
            s("level_left", "Level left", f"{self.base}/levels", unit_of_measurement="dBFS",
              state_class="measurement", suggested_display_precision=1),
            s("level_right", "Level right", f"{self.base}/levels", unit_of_measurement="dBFS",
              state_class="measurement", suggested_display_precision=1),
            s("cpu", "CPU", sysst, unit_of_measurement="%", state_class="measurement", **diag),
            s("memory", "Memory", sysst, unit_of_measurement="%", state_class="measurement", **diag),
            s("process_memory", "VinylPilot memory", sysst, unit_of_measurement="MB",
              state_class="measurement", **diag),
            s("temperature", "CPU temperature", sysst, unit_of_measurement="°C",
              device_class="temperature", state_class="measurement", **diag),
            s("wifi_rssi", "Wi-Fi signal", sysst, unit_of_measurement="dBm",
              device_class="signal_strength", state_class="measurement", **diag),
            s("boot_time", "Last boot", sysst, device_class="timestamp", **diag),
            s("version", "Version", sysst, **diag),
            ("sensor", "now_playing", {
                "name": "Now playing", "state_topic": f"{self.base}/now_playing",
                "value_template": "{{ (value_json.artist ~ ' – ' ~ value_json.title) if value_json.title else 'None' }}",
                "json_attributes_topic": f"{self.base}/now_playing", "icon": "mdi:album"}),
            ("image", "cover", {
                "name": "Cover", "url_topic": f"{self.base}/now_playing",
                "url_template": "{{ value_json.cover if value_json.cover else '" + COVER_PLACEHOLDER + "' }}"}),
            ("number", "threshold", {
                "name": "Detection threshold", "state_topic": st,
                "value_template": "{{ value_json.threshold_db }}",
                "command_topic": f"{self.base}/set/threshold", "min": -90, "max": -30, "step": 1,
                "unit_of_measurement": "dBFS", "mode": "box", "entity_category": "config"}),
            ("number", "hold", {
                "name": "Silence hold", "state_topic": st, "value_template": "{{ value_json.hold_s }}",
                "command_topic": f"{self.base}/set/hold", "min": 1, "max": 300, "step": 1,
                "unit_of_measurement": "s", "mode": "box", "entity_category": "config"}),
        ]

    async def _publish_discovery(self, client: aiomqtt.Client) -> None:
        for component, key, cfg in self._entities():
            cfg = {
                **cfg,
                "unique_id": f"vinylpilot_{self.node}_{key}",
                "object_id": f"vinylpilot_{key}",
                "availability_topic": self.avail,
                "device": self._device(),
            }
            topic = f"{self.s.discovery_prefix}/{component}/vinylpilot_{self.node}/{key}/config"
            await client.publish(topic, json.dumps(cfg), retain=True)

    # ---- state -------------------------------------------------------------

    def _state(self) -> dict[str, Any]:
        st = self.status
        return {
            "turntable_connected": st.turntable_connected,
            "record_playing": st.record_playing,
            "streaming": st.streaming,
            "server_connected": st.server_connected,
            "threshold_db": st.threshold_db,
            "hold_s": st.hold_s,
        }

    def _system(self) -> dict[str, Any]:
        boot = system.boot_time()
        return {
            "cpu": self._cpu.percent(),
            "memory": system.memory_used_percent(),
            "process_memory": system.process_rss_mb(),
            "temperature": system.cpu_temperature(),
            "wifi_rssi": system.wifi_rssi(),
            "boot_time": datetime.fromtimestamp(boot, UTC).isoformat(timespec="seconds") if boot else None,
            "version": __version__,
        }

    async def _publish_loop(self, client: aiomqtt.Client) -> None:
        loop = asyncio.get_running_loop()
        last_state: dict[str, Any] | None = None
        last_track: Any = object()
        next_levels = next_system = 0.0
        while True:
            now = loop.time()
            state = self._state()
            if state != last_state:
                await client.publish(f"{self.base}/state", json.dumps(state), retain=True)
                last_state = state
            if self.status.now_playing != last_track:
                last_track = self.status.now_playing
                await client.publish(f"{self.base}/now_playing",
                                     json.dumps(last_track or {"title": "", "artist": "", "album": "",
                                                               "cover": COVER_PLACEHOLDER, "url": ""}),
                                     retain=True)
            if now >= next_levels:
                await client.publish(f"{self.base}/levels", json.dumps({
                    "level_left": round(self.status.level_left, 1),
                    "level_right": round(self.status.level_right, 1)}))
                next_levels = now + self.s.level_interval_s
            if now >= next_system:
                await client.publish(f"{self.base}/system", json.dumps(self._system()), retain=True)
                next_system = now + self.s.system_interval_s
            self.status.changed.clear()
            try:
                await asyncio.wait_for(self.status.changed.wait(), timeout=1.0)
            except TimeoutError:
                pass

    async def _command_loop(self, client: aiomqtt.Client) -> None:
        await client.subscribe(f"{self.base}/set/#")
        async for message in client.messages:
            topic = str(message.topic)
            try:
                value = float(message.payload.decode())  # type: ignore[union-attr]
            except (ValueError, AttributeError):
                continue
            if topic.endswith("/threshold"):
                self._on_threshold(max(-90.0, min(-30.0, value)))
            elif topic.endswith("/hold"):
                self._on_hold(max(1.0, min(300.0, value)))
            self.status.changed.set()

    async def run(self) -> None:
        backoff = 1.0
        will = aiomqtt.Will(self.avail, "offline", qos=1, retain=True)
        while True:
            try:
                async with aiomqtt.Client(
                    self.s.host, self.s.port,
                    username=self.s.username, password=self.s.password,
                    identifier=f"vinylpilot-{self.node}", will=will, keepalive=30,
                ) as client:
                    logger.info("MQTT connected to %s:%d", self.s.host, self.s.port)
                    backoff = 1.0
                    await self._publish_discovery(client)
                    await client.publish(self.avail, "online", qos=1, retain=True)
                    try:
                        async with asyncio.TaskGroup() as tg:
                            tg.create_task(self._publish_loop(client))
                            tg.create_task(self._command_loop(client))
                    finally:
                        # Clean shutdown: the broker will not send the will, so say goodbye.
                        try:
                            await asyncio.wait_for(
                                client.publish(self.avail, "offline", qos=1, retain=True), 2
                            )
                        except Exception:  # noqa: BLE001
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                logger.warning("MQTT unavailable (%s); retrying in %.0f s", err, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
