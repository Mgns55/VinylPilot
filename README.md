<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/svg/logo-horizontal-dark.svg">
    <img alt="VinylPilot — Analog music. Digital sound." src="assets/brand/svg/logo-horizontal-light.svg" width="480">
  </picture>
</p>

<p align="center"><b>Nordparklab · Audio Systems</b></p>

---

**VinylPilot** turns a Raspberry Pi into a dedicated network streamer for USB turntables.
It captures the turntable's digital output and publishes it to [Music Assistant](https://www.music-assistant.io/)
as a native **Sendspin Live Input**, for synchronized multiroom playback and Home Assistant integration.

> **Status: pre-alpha.** Hardware validated; MVP code ready for its first test against Music Assistant.
> Test guide: [docs/mvp-test.md](docs/mvp-test.md).
> See [docs/ROADMAP.md](docs/ROADMAP.md).

## How it works

```
USB turntable ──USB──▶ Raspberry Pi (VinylPilot) ──Sendspin source@v1──▶ Music Assistant ──▶ any MA player / group
                              │
                              └── MQTT (optional) ──▶ Home Assistant
```

- Captures audio through standard Linux ALSA (USB Audio Class)
- Streams lossless PCM (default 48 kHz / 16-bit) via the [Sendspin](https://github.com/Sendspin/spec) source role
- Detects when a record is playing and reports the signal, so Music Assistant can autostart playback
- Runs headless on Raspberry Pi OS Lite; target hardware is a Raspberry Pi Zero 2 W
- Web interface at http://vinylpilot.local: live VU meters, now playing, stylus hours, digitising to a NAS,
  settings, diagnostics and one-click updates ([docs/web-interface.md](docs/web-interface.md))
- Home Assistant via MQTT Discovery, optional track recognition ([docs/home-assistant.md](docs/home-assistant.md))

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Requirements (planned)

- Raspberry Pi OS (Trixie-based, Python ≥ 3.12)
- Music Assistant ≥ 2.10 with the **Sendspin Source** plugin enabled
- A USB turntable or USB phono preamp that is a class-compliant USB audio device

## Tested hardware

| Device | Status |
|---|---|
| Sony PS-HX500 | ✅ USB capture verified on Pi 5 (16/24/32-bit, 44.1–352.8 kHz) |

## License

Apache License 2.0, see [LICENSE](LICENSE). Brand assets in `assets/brand/` are part of the VinylPilot identity;
no trademark clearance has been performed.
