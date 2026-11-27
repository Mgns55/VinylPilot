"""Shazam lookups in a separate, low-priority process.

``--serve``: stays running while a record plays; reads WAVs framed as a 4-byte
big-endian length plus data from stdin and prints one JSON line per lookup. Starting
Python and importing the library once per record instead of once per lookup keeps
the CPU/SD-card burst away from the audio loop.
Without arguments: one-shot (WAV on stdin, one JSON line).

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
    album = year = ""
    for section in track.get("sections") or []:
        for item in section.get("metadata") or []:
            if item.get("title") in ("Album", "Albüm", "Album:"):
                album = item.get("text", "")
            elif item.get("title") in ("Released", "Release", "Veröffentlicht"):
                year = str(item.get("text", ""))[:4]
    images = track.get("images") or {}
    return {
        "title": track.get("title", ""),
        "artist": track.get("subtitle", ""),
        "album": album,
        "cover": images.get("coverarthq") or images.get("coverart") or "",
        "url": track.get("url", ""),
        "key": str(track.get("key", "")),
        "year": year if year.isdigit() else "",
        "genre": (track.get("genres") or {}).get("primary", ""),
    }


async def _lookup(shazam: Any, data: bytes) -> dict[str, Any]:
    try:
        return _extract(await shazam.recognize(data))
    except Exception as err:  # noqa: BLE001
        return {"error": f"{type(err).__name__}: {err}"}


async def _main() -> int:
    from shazamio import Shazam  # imported here: only this process pays for it

    shazam = Shazam()
    if "--serve" not in sys.argv:
        result = await _lookup(shazam, sys.stdin.buffer.read())
        print(json.dumps(result))
        return 1 if "error" in result else 0
    stdin = sys.stdin.buffer
    while len(header := stdin.read(4)) == 4:
        data = stdin.read(int.from_bytes(header, "big"))
        print(json.dumps(await _lookup(shazam, data)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
