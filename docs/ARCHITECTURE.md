# VinylPilot architecture

Status: proposal, 2026-11-17. Items marked **[unverified]** must be validated on real hardware.

## Decision: native Sendspin source

VinylPilot is a Sendspin `source@v1` **client** running directly on the Raspberry Pi.
Music Assistant (MA) connects to it, pairs once, and lists it under **Live Inputs**.

### Why Sendspin

- MA ships a built-in **Sendspin Source** plugin (stage: alpha) that turns `source@v1` clients into Live Inputs
  playable on any MA player, stereo pair or group.
- MA ≥ 2.10 already implements **autostart on signal**: a per-source target player, a 2 s debounce when the
  signal appears, and a 60 s hold after it disappears. VinylPilot only needs to report `signal: present | absent`
  (spec: `client/state`, source role). No MA API calls, no middleware.
- [`aiosendspin`](https://github.com/Sendspin/aiosendspin) (Apache-2.0, Open Home Foundation) provides the client,
  Noise-based pairing and `SourceCapture` (PCM / FLAC / Opus).
- Per spec, every server must accept `pcm` and `flac` source input.

### Caveats

- Sendspin is a technical preview; the MA plugin is alpha. Pin `aiosendspin` and track MA releases.
- `aiosendspin` requires Python ≥ 3.12 → Trixie-based Raspberry Pi OS. Bookworm (Python 3.11) is not supported.
- Source role requires a **paired** device; unpaired access does not enable it.

### Alternatives considered

| Option | Verdict |
|---|---|
| **VBAN** (MA VBAN Receiver plugin, since 2.7) | Fallback only. Simple UDP PCM, but no pairing/auth and no signal reporting → no MA autostart. |
| **jordancollins1/turntable-sendspin** | Reference only. Runs on a NAS and pulls audio from the Pi over SSH; repository has **no license**, so its code is not reused. |
| Rust (`sendspin` crate) / Go client | Kept in reserve if Python is too heavy on the Zero 2 W. |

## Components

```
┌──────────────────────── Raspberry Pi ────────────────────────┐
│ vinylpilot-core.service                                      │
│   ALSA capture (arecord) → level/RMS → signal detector       │
│                         ↘ aiosendspin SourceCapture → MA     │
│   MQTT / HA Discovery (optional)                             │
│   local status socket (unix)                                 │
│                                                              │
│ vinylpilot-web.service  (aiohttp, vanilla HTML/CSS/JS)       │
│   reads status socket; WebSocket telemetry only while open   │
└──────────────────────────────────────────────────────────────┘
```

- **Core and web are separate systemd units.** Restarting or closing the web UI never touches audio.
- **Capture:** `arecord` subprocess on `hw:` device (no resampling), supervised; restarts on USB disconnect.
- **Format:** PCM 48 kHz / 16-bit / stereo by default (~1.5 Mbit/s). FLAC optional (saves Wi-Fi, costs CPU).
  Higher resolution optional where the device supports it.
- **Signal detection:** RMS per chunk vs. configurable threshold (calibrated against the needle-up noise floor),
  configurable activation delay, and a configurable hold before reporting `absent`
  (on top of MA's fixed 60 s hold).
- **Web server:** aiohttp (already an aiosendspin dependency) instead of FastAPI, to avoid extra dependencies.
- **MQTT:** optional; HA MQTT Discovery for status, levels, system metrics and start/stop.
- **Config:** TOML. Secrets in a separate file with mode 0600. Only the pairing store and config are written to disk.

## Performance targets [unverified]

| Metric | Target | Risk |
|---|---|---|
| RAM | < 100 MB | Python + numpy + PyAV + cryptography may land at 60–100 MB |
| CPU (PCM streaming) | < 15 % | Low risk; FLAC adds load |
| Network | stable on 2.4 GHz | Zero 2 W Wi-Fi jitter; power saving must be off |

Fallback hardware: Raspberry Pi 3 Model B (Ethernet available).
