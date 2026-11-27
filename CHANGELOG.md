# Changelog

All notable changes to this project are documented here. Format: [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

## [0.3.2] – 2026-11-27
### Changed
- Wi-Fi watchdog: when the Wi-Fi chip has dropped off its SDIO bus (`wlan0` gone - seen on the Zero 2 W, no driver reload can revive it), reboot after 2 minutes instead of 10.
- Recorder delay line 120 s instead of 150 s (~6 MB less memory).

## [0.3.1] – 2026-11-27
### Changed
- Web interface: the input display shows just the two level bars (with peak marker); the waveform, dB values and scale are gone. The core no longer computes waveform data.

## [0.3.0] – 2026-11-27
### Changed – digitising rebuilt for the Zero's Wi-Fi
- The recorder runs in its own process (nice 10). Nothing it does - share hiccups, encoding, tagging - can stall the live stream; if it cannot keep up, recording audio is dropped (counted), never streaming audio.
- Tracks are encoded to FLAC **while they play** and written straight to the share at a steady ~0.9 Mbit/s. No WAV files, no copying, splitting or re-encoding afterwards; finishing a track writes tags into the FLAC padding and renames it on the share.
- The last 150 s of audio stay in RAM (~29 MB) before they are written, so Shazam-driven cuts are made in memory.
- The live stream to Music Assistant uses FLAC by default (~40 % less Wi-Fi traffic); automatic fallback to PCM.
- systemd: `KillMode=mixed`, `TimeoutStopSec=25`, so the recorder can finish the current track on restart.

## [0.2.7] – 2026-11-27
### Fixed
- Removed the stream-clock re-alignment for short delays: every re-alignment was an audible jump (logs showed several per minute). Timestamps now follow the audio clock strictly; only a jump of more than 10 s re-anchors.
- Closing a recorded track no longer blocks the audio loop for up to 5 s when the network share is slow (source of the "Audio loop delayed by 3991 ms").
- Track numbering on the share is looked up off the audio loop.

## [0.2.6] – 2026-11-27
### Fixed
- ~1 s dropouts every 90 s: each Shazam lookup started a new Python process, and that burst stalled the audio loop for up to a second (logged as "Audio loop delayed by 948 ms"; no network stalls). The recognition worker now starts once per record and stays running, at nice 19 and idle I/O priority.
- arecord's output pipe is enlarged to 1 MiB (~5 s), so a busy moment can no longer make capture overrun and lose samples.
- Stream timestamps are only re-aligned when the audio clock and the wall clock stay apart for 2 s (real sample loss or drift), not while a short backlog is worked off - the 0.2.5 re-anchoring itself caused an audible jump.
- No more bogus "Audio loop delayed by 4261331 ms" after waiting for the turntable.

### Added
- "Slow audio step" warning when processing a single chunk takes over 100 ms.

## [0.2.5] – 2026-11-27
### Fixed
- Short dropouts on the speakers: stream timestamps now follow the audio clock (sample count) instead of the moment a chunk was processed, so any processing delay no longer produces overlapping or compressed timestamps at the receiver.
- Streaming runs in its own task behind a 2 s queue; a stalled Wi-Fi send no longer blocks capture, detection or recording.
- ALSA capture buffer raised from 200 to 500 ms.

### Added
- Network send stalls and audio-loop delays are logged and counted (Diagnose → "Stream-Hänger").

## [0.2.4] – 2026-11-27
### Fixed
- Tracks are also split when Shazam recognises a new song but no pause was found (segued albums, short or noisy gaps). The cut is placed at the quietest moment between the last sample of the old song and the first sample of the new one.
- Recognition re-checks every 90 s instead of 180 s, so short songs are not missed between checks.

## [0.2.3] – 2026-11-27
### Fixed
- Track splitting on real vinyl: pauses are now detected relative to the music's own level (20 dB below it, or below the gap threshold, whichever is higher). With the fixed −55 dBFS threshold the surface noise between tracks was too loud, so whole sides ended up in one file.
- Tracks interrupted by a crash or power cut are recovered on the next start into `Unbekannt/Wiederhergestellt <date>` (title kept, marked "unvollständig"); orphaned temp folders are cleaned up.
- WAV files are written without a header update per chunk (less SMB traffic over Wi-Fi).

### Added
- The Wi-Fi watchdog stores a snapshot (throttling/undervoltage flags, Wi-Fi driver messages, memory) in `/var/lib/vinylpilot/netfail.log` before each action, so the cause survives the reboot. Shown under Diagnose → "Letzte WLAN-Ausfälle".

## [0.2.2] – 2026-11-24
### Changed
- FLAC files are encoded in the hidden temp folder and moved into the album folder in one step, so Navidrome and other library scanners never pick up half-written files.
- `.vinylpilot-tmp` gets a `.ndignore` file.
- Unrecognised tracks are tagged `Unbekannt` / `Vinyl YYYY-MM-DD` so a day's unknown tracks show up as one album.

## [0.2.1] - 2026-11-24
### Changed
- 'Alle Alben aufzeichnen' is now a permanent setting (switch in Digitalisieren and Einstellungen):
  every record is digitised, also after long pauses, restarts and NAS outages; stopping by hand
  switches it off. Unrecognised tracks are grouped per day.

## [0.2.0] - 2026-11-24
### Added
- Digitising modes: 'Nächstes Album aufzeichnen' (stops when another album is recognised or after
  5 min without music) and 'Alle Alben aufzeichnen'
- Duplicate check: tracks already digitised (same artist/album/title, also from earlier sessions)
  are skipped; album folders are matched ignoring case, brackets and remaster notes
- Version scheme 0.2.x, the patch number increases with every update
### Fixed
- Cover image shows the VinylPilot icon when nothing is recognised instead of an empty URL
  (Home Assistant logged image errors)
- Fast shutdown: MQTT, Sendspin and a running Shazam lookup are stopped within a few seconds;
  systemd TimeoutStopSec=15 (a restart previously hung for 90 s before SIGKILL)
- Startup no longer waits for the turntable: Music Assistant and MQTT come up even when the
  turntable is off; capture finds the USB device on its own

### Added
- Update countdown in the web interface: estimate from the previous update's duration (75 s default),
  progress bar, 'takes longer' state, automatic page reload when the new version is up
- Digitising: one folder per album (`Artist/Album`), continuous numbering, embedded cover + cover.jpg,
  year and genre tags
- Web: logo groove turns at 33⅓ rpm while a record plays; spinning record as empty cover
  (also used as the Home Assistant cover placeholder)
- Web interface on port 80 (separate service): dashboard with live VU meters and waveform,
  now playing, stylus hours, digitising to a Synology share, settings, diagnostics, logs,
  one-click update (polkit-restricted), optional password
- Digitising: per-track files split at pauses, Shazam naming/tags, FLAC conversion, NAS setup script
- Stylus hour counter (web + MQTT sensor and reset button)
- Runtime settings (threshold, hold, gap, Shazam, stylus life, format) saved on the Pi
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
