"""Discovery of ALSA USB capture devices via /proc/asound (no extra dependencies)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ASOUND = Path("/proc/asound")

# " 2 [PSHX500        ]: USB-Audio - PS-HX500"
_CARD_RE = re.compile(r"^\s*(\d+)\s+\[(\S+)\s*\]:\s+(\S+)\s+-\s+(.*)$")


@dataclass(frozen=True)
class CaptureDevice:
    """A USB audio card that offers a capture stream."""

    index: int
    card_id: str
    name: str

    @property
    def alsa_name(self) -> str:
        """Stable ALSA name; survives card renumbering after replugging."""
        return f"hw:CARD={self.card_id},DEV=0"


def list_usb_capture_devices(asound: Path = ASOUND) -> list[CaptureDevice]:
    """Return USB audio cards that have a capture stream."""
    cards_file = asound / "cards"
    try:
        lines = cards_file.read_text().splitlines()
    except OSError:
        return []
    devices: list[CaptureDevice] = []
    for line in lines:
        match = _CARD_RE.match(line)
        if not match:
            continue
        index, card_id, driver, name = match.groups()
        if driver != "USB-Audio":
            continue
        stream = asound / f"card{index}" / "stream0"
        try:
            if "Capture:" not in stream.read_text():
                continue
        except OSError:
            continue
        devices.append(CaptureDevice(int(index), card_id, name.strip()))
    return devices


def resolve_device(device: str) -> tuple[str, str]:
    """Resolve ``auto`` to the first USB capture card.

    Returns the ALSA device name and a human-readable label.
    Raises LookupError when ``auto`` finds nothing.
    """
    if device != "auto":
        return device, device
    found = list_usb_capture_devices()
    if not found:
        raise LookupError("No USB audio capture device found")
    first = found[0]
    return first.alsa_name, first.name
