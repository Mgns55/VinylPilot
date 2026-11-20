"""Lightweight system metrics from /proc and /sys (no psutil)."""

from __future__ import annotations

import os
import time
from pathlib import Path


class CpuMeter:
    """System-wide CPU utilisation between two calls, from /proc/stat."""

    def __init__(self) -> None:
        self._last: tuple[int, int] | None = self._read()

    @staticmethod
    def _read() -> tuple[int, int] | None:
        try:
            fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        except (OSError, IndexError):
            return None
        values = [int(v) for v in fields]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return idle, sum(values)

    def percent(self) -> float | None:
        now = self._read()
        last, self._last = self._last, now
        if now is None or last is None or now[1] == last[1]:
            return None
        return round(100.0 * (1 - (now[0] - last[0]) / (now[1] - last[1])), 1)


def memory_used_percent() -> float | None:
    try:
        info = {
            line.split(":")[0]: int(line.split()[1])
            for line in Path("/proc/meminfo").read_text().splitlines()
        }
        return round(100.0 * (1 - info["MemAvailable"] / info["MemTotal"]), 1)
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return None


def process_rss_mb() -> float | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return None


def cpu_temperature() -> float | None:
    try:
        return round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
    except (OSError, ValueError):
        return None


def wifi_rssi(interface: str = "wlan0") -> int | None:
    """Signal level in dBm from /proc/net/wireless."""
    try:
        for line in Path("/proc/net/wireless").read_text().splitlines()[2:]:
            parts = line.split()
            if parts and parts[0].rstrip(":") == interface:
                return int(float(parts[3]))
    except (OSError, ValueError, IndexError):
        pass
    return None


def boot_time() -> float | None:
    try:
        return time.time() - float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def hostname() -> str:
    return os.uname().nodename
