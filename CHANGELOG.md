# Changelog

All notable changes to this project are documented here. Format: [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]
### Fixed
- Cover image shows the VinylPilot icon when nothing is recognised instead of an empty URL
  (Home Assistant logged image errors)
- Fast shutdown: MQTT, Sendspin and a running Shazam lookup are stopped within a few seconds;
  systemd TimeoutStopSec=15 (a restart previously hung for 90 s before SIGKILL)
- Startup no longer waits for the turntable: Music Assistant and MQTT come up even when the
  turntable is off; capture finds the USB device on its own

### Added
- Network watchdog (Wi-Fi toggle → driver reload → reboot) and hardware watchdog for the Zero 2 W
- Optional track recognition via the unofficial Shazam API (`--recognize shazam`): Now playing
  sensor and cover image in Home Assistant; lookups at record start, after track gaps, every 3 min
- A vanished USB input now counts as 'no signal', so Music Assistant autostart works again
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
