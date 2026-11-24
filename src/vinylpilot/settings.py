"""Runtime settings, changeable from the web interface and MQTT, stored in the state dir."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RECORDINGS_DIR = Path("/mnt/vinylpilot")


@dataclass
class Settings:
    threshold_db: float = -65.0
    hold_s: float = 10.0
    gap_db: float = -55.0
    recognize: bool = False
    auto_record: bool = False  # digitise every record, permanently (survives pauses and restarts)
    stylus_life_h: float = 800.0
    recording_format: str = "flac"  # flac | wav

    LIMITS = {
        "threshold_db": (-90.0, -30.0),
        "hold_s": (1.0, 300.0),
        "gap_db": (-80.0, -30.0),
        "stylus_life_h": (50.0, 5000.0),
    }

    def update(self, data: dict[str, Any]) -> list[str]:
        """Apply validated values; return the names that changed."""
        changed = []
        for f in fields(self):
            if f.name not in data:
                continue
            value = data[f.name]
            if f.name in ("recognize", "auto_record"):
                value = bool(value)
            elif f.name == "recording_format":
                if value not in ("flac", "wav"):
                    continue
            else:
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
                lo, hi = self.LIMITS[f.name]
                value = max(lo, min(hi, value))
            if getattr(self, f.name) != value:
                setattr(self, f.name, value)
                changed.append(f.name)
        return changed

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load(path: Path, defaults: Settings) -> Settings:
    try:
        defaults.update(json.loads(path.read_text()))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as err:
        logger.warning("Ignoring unreadable settings %s: %s", path, err)
    return defaults


def save(path: Path, settings: Settings) -> None:
    """Atomic write (temp file + rename)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings.to_dict(), indent=1))
        os.replace(tmp, path)
    except OSError as err:
        logger.warning("Could not save settings: %s", err)
