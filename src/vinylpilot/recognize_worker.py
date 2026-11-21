"""One-shot Shazam lookup: reads a WAV from stdin, prints one JSON line.

Runs as a short-lived subprocess so the recognition library (~30 MB) is only in
memory during a lookup and can never stall the audio loop.

Uses the unofficial, reverse-engineered Shazam API via ``shazamio`` (MIT).
It may stop working at any time and is not endorsed by Shazam/Apple.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any


def _extract(answer: dict[str, Any]) -> dict[str, Any]:
    track = answer.get("track") or {}
    if not track:
        return {}
    album = ""
    for section in track.get("sections") or []:
        for item in section.get("metadata") or []:
            if item.get("title") in ("Album", "Albüm", "Album:"):
                album = item.get("text", "")
    images = track.get("images") or {}
    return {
        "title": track.get("title", ""),
        "artist": track.get("subtitle", ""),
        "album": album,
        "cover": images.get("coverarthq") or images.get("coverart") or "",
        "url": track.get("url", ""),
        "key": str(track.get("key", "")),
    }


async def _main() -> int:
    from shazamio import Shazam  # imported here: only this process pays for it

    data = sys.stdin.buffer.read()
    try:
        answer = await Shazam().recognize(data)
    except Exception as err:  # noqa: BLE001
        print(json.dumps({"error": f"{type(err).__name__}: {err}"}))
        return 1
    print(json.dumps(_extract(answer)))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
