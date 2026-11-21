# Home Assistant (MQTT)

VinylPilot publishes its state to an MQTT broker and announces itself through
Home Assistant MQTT Discovery as one device, **VinylPilot**. Nothing extra is needed on the Pi.

## Enable

1. Create an MQTT user for VinylPilot on your broker (e.g. `vinylpilot`).
2. On the Pi, put the password into the root-only secrets file:
   ```bash
   sudo nano /etc/vinylpilot/secrets.env      # VINYLPILOT_MQTT_PASSWORD=...
   ```
3. Add the broker to the arguments:
   ```bash
   sudo nano /etc/default/vinylpilot
   # VINYLPILOT_ARGS="--mqtt-host 192.168.178.70 --mqtt-user vinylpilot"
   sudo systemctl restart vinylpilot
   journalctl -u vinylpilot -n 20 --no-pager   # expect: MQTT connected to ...
   ```

## Entities

| Entity | Type | Notes |
|---|---|---|
| Availability | — | retained `online`/`offline`; last will marks the device unavailable if the Pi drops |
| Turntable connected | binary sensor | USB capture running |
| Record playing | binary sensor | signal detector (threshold / activation / hold) |
| Streaming | binary sensor | Music Assistant is receiving audio |
| Music Assistant connected | binary sensor (diagnostic) | |
| Level left / right | sensor, dBFS | every 5 s |
| CPU, Memory, VinylPilot memory, CPU temperature, Wi-Fi signal, Last boot, Version | sensors (diagnostic) | every 30 s |
| Detection threshold, Silence hold | number (config) | changes are applied live and saved on the Pi |

Topics: `vinylpilot/<hostname>/{availability,state,levels,system,set/threshold,set/hold}`.

Tip: exclude the two level sensors from the recorder if you do not need their history.

## Track recognition (optional, Shazam)

VinylPilot can identify the playing song and publish it as **Now playing** (artist – title, with
album, cover URL and Shazam link as attributes) plus a **Cover** image entity.

```bash
sudo nano /etc/default/vinylpilot
# add: --recognize shazam
curl -fsSL https://raw.githubusercontent.com/Mgns55/VinylPilot/main/scripts/install.sh | sudo bash
```

How it works: ~14 s after a record starts and after every pause between tracks (window level below
`--gap-threshold`, default −55 dBFS, for ≥ 1 s) VinylPilot sends a 12 s excerpt to Shazam; a re-check
runs every 3 minutes for gapless sides. Lookups run in a short-lived, low-priority helper process, so
the recognition library only uses memory during a lookup.

**Caveats:** this uses the unofficial, reverse-engineered Shazam API (`shazamio`). It needs internet
access, sends audio fingerprints to Shazam, is not endorsed by Shazam/Apple and may stop working
at any time. It is off by default. Music Assistant itself keeps showing the source name, because
Sendspin has no way for a source to send track metadata to the server.
