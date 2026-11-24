"""Stylus (needle) play-time counter, persisted with few SD writes."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

SAVE_EVERY_S = 300.0  # at most one small write every 5 minutes of playing


class StylusCounter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.seconds = 0.0
        self.since = time.time()
        self._unsaved = 0.0
        try:
            data = json.loads(path.read_text())
            self.seconds = float(data.get("seconds", 0.0))
            self.since = float(data.get("since", self.since))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as err:
            logger.warning("Ignoring unreadable stylus counter: %s", err)

    @property
    def hours(self) -> float:
        return self.seconds / 3600.0

    def add(self, seconds: float) -> None:
        self.seconds += seconds
        self._unsaved += seconds
        if self._unsaved >= SAVE_EVERY_S:
            self.save()

    def reset(self) -> None:
        self.seconds = 0.0
        self.since = time.time()
        self.save()
        logger.info("Stylus counter reset")

    def save(self) -> None:
        self._unsaved = 0.0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"seconds": round(self.seconds, 1), "since": self.since}))
            os.replace(tmp, self.path)
        except OSError as err:
            logger.warning("Could not save stylus counter: %s", err)
