# Roadmap

Legend: ✅ done · ⏳ in progress · ☐ open

## Phase 1 – Hardware validation (Raspberry Pi 5) ✅
- ✅ USB enumeration of the Sony PS-HX500 (`lsusb`, `dmesg`)
- ✅ ALSA capture device available (`arecord -l`)
- ✅ Supported sample rates / bit depths (`/proc/asound/cardN/stream0`)
- ✅ 30 s recording with a record playing, plus 10 s needle-up noise floor (`sox stats`)
- ✅ Playback check of the recording
- ✅ Python ≥ 3.12 available

See [hardware-validation.md](hardware-validation.md).

## Phase 2 – MVP ⏳
- ✅ Capture live audio (code; arecord, auto-detect, reconnect)
- ✅ Sendspin source client with pairing (aiosendspin 10.2.1, end-to-end test against a real Sendspin server)
- ✅ RMS signal detection + signal reporting (pulled forward from Phase 4)
- ✅ Appears as Live Input in Music Assistant, autostart works (2026-11-18)
- ✅ Play vinyl on existing network speakers
- ☐ Stability and audio-quality check (1 h continuous)
- ☐ Measure RAM/CPU on Pi 5 as an early indicator

## Phase 3 – Embedded deployment (Zero 2 W) ✅
- ✅ Measured on Zero 2 W (2026-11-20): 74 MB RSS, ~9 % of one core while streaming; desktop image needs GUI/PipeWire/PackageKit disabled (Lite recommended)
- ✅ systemd unit, install/update script, journald in RAM, Wi-Fi power save off ([zero2w-setup.md](zero2w-setup.md))
- ☐ Raspberry Pi OS Lite (64-bit, Trixie)
- ☐ Measure CPU / RAM / Wi-Fi stability
- ☐ systemd units, auto-start, auto-recovery, USB hot-plug

## Phase 4 – Intelligent features
- ✅ RMS signal detection, threshold, activation delay, hold
- ✅ Signal reporting → MA autostart
- ✅ MQTT + Home Assistant Discovery ([home-assistant.md](home-assistant.md)), tested against a local broker

## Phase 4b – Track recognition
- ✅ Optional Shazam recognition → MQTT Now playing + cover (code + tests; live lookup not yet verified)
- ☐ Propose source metadata for Sendspin / Music Assistant (so MA can show the track)

## Phase 5 – Web interface ⏳ (code done, not yet verified on the Zero)
- ✅ Dashboard, VU meters, waveform, now playing
- ✅ Settings, system monitoring, diagnostics, logs
- ✅ One-click update, optional password
- ✅ Digitise to NAS (per-track FLAC, Shazam naming), stylus hours

## Phase 6 – Open-source release
- ☐ Install / update / uninstall scripts
- ☐ Tests, CI, releases, changelog
- ☐ README, troubleshooting guide

## Open questions
- RAM footprint of aiosendspin with PyAV on the Zero 2 W?
