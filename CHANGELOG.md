# Changelog

All notable changes to this project are documented here. Format: [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]
### Added
- Optional MQTT with Home Assistant Discovery: availability (last will), turntable/record/streaming/MA
  binary sensors, levels, system metrics, threshold and hold as numbers
- systemd service, installer/updater for Raspberry Pi OS, root-only secrets file
- Waits for the USB turntable at startup instead of exiting
- MVP core: ALSA capture via arecord with auto-detection and reconnect, level meter, signal detector,
  Sendspin source client (pairing, streaming, signal reporting), CLI
- Unit tests and an end-to-end test against a real Sendspin server
- Project scaffold: README, architecture proposal, roadmap, hardware validation guide
- Brand assets
- Apache-2.0 license
