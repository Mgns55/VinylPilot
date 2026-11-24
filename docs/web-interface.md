# Web interface

**http://vinylpilot.local** (port 80, no port number needed)

Runs as its own service (`vinylpilot-web`), separate from the audio core: closing the page or
restarting the web service never interrupts playback. Live levels and the waveform are only
computed and transmitted while the overview is open in a visible browser tab.

| Tab | Contents |
|---|---|
| Übersicht | status, now playing with cover, stereo VU meters (dBFS), live waveform, format, stylus hours |
| Digitalisieren | record to the Synology share, one file per track, named via Shazam |
| System | CPU, memory, temperature, Wi-Fi, uptimes, version |
| Einstellungen | detection threshold, hold, gap threshold, Shazam on/off, stylus life, recording format |
| Diagnose | USB devices, ALSA capture devices, router/Music Assistant/MQTT, watchdog, log |
| Update | installed vs. latest version, one-click update |

## Optional password

```bash
sudo nano /etc/vinylpilot/web.env      # VINYLPILOT_WEB_PASSWORD=...
sudo systemctl restart vinylpilot-web
```

## Update button

Starts `vinylpilot-update.service`, which runs the installer (`git pull`, dependencies, services).
The web service may only start exactly this unit (polkit rule `/etc/polkit-1/rules.d/50-vinylpilot.rules`);
no other commands can be run from the browser.

## Digitising to the Synology

One-time setup on the Pi (SMB share, e.g. a `Vinyl` folder in the `music` share):

```bash
sudo /opt/vinylpilot/scripts/setup-nas.sh //192.168.178.70/music/Vinyl <nas-user>
```

The script asks for the NAS password, stores it root-only in `/etc/vinylpilot/nas-credentials` and
mounts the share at `/mnt/vinylpilot` on demand (systemd automount).

Then: **Digitalisieren → Aufnahme starten**, put the needle down. VinylPilot

- starts a new file at every pause between tracks (≥ 30 s per track) and when a side ends,
- files recognised tracks as `Artist/Album/NN - Title.flac` (a new folder per album; numbering continues
  across sides and later sessions of the same album), unrecognised ones as `Unbekannt/Vinyl <date>/NN - Track NN`,
- tags FLAC files (TITLE, ARTIST, ALBUMARTIST, ALBUM, DATE, GENRE, TRACKNUMBER), embeds the cover and
  saves it as `cover.jpg` in the album folder,
- converts each finished track to FLAC at low priority (`flac`), keeps WAV on failure,
- stops automatically after 30 minutes without music.

Audio is written by a background thread; if the network share stalls, chunks are dropped and
logged instead of disturbing the stream.

## Stylus hours

Counts the time a record is detected as playing; saved at most every 5 minutes. Reset it with
**Neue Nadel eingesetzt** (or the MQTT button). Also published to Home Assistant as `Stylus hours`.
