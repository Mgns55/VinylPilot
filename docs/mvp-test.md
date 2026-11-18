# MVP test: vinyl in Music Assistant

Goal: the turntable shows up as a **Live Input** in Music Assistant and plays on a network speaker.

Requirements: Music Assistant ≥ 2.10 with the **Sendspin Source** plugin enabled, Python ≥ 3.12 on the Pi.

## 1. Install (development Pi)

```bash
sudo apt install -y git python3-venv
git clone https://github.com/Mgns55/VinylPilot.git ~/VinylPilot
cd ~/VinylPilot
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/vinylpilot --list-devices
```

`--list-devices` should print `card 2: PS-HX500  ->  hw:CARD=PSHX500,DEV=0`.

## 2. Run

```bash
.venv/bin/vinylpilot --show-levels
```

Expected log lines:

- `Capturing from hw:CARD=PSHX500,DEV=0 at 48000 Hz`
- `Waiting for Music Assistant on port 8928 (mDNS _sendspin._tcp)`
- one level line per second; with a record playing, after ~1 s: `Record playing`

If capture fails with `Device or resource busy` on a desktop image:
`systemctl --user stop pipewire pipewire-pulse wireplumber`

## 3. Pair with Music Assistant

Music Assistant should discover VinylPilot via mDNS and connect. On first contact VinylPilot logs:

```
PAIRING: enter code 123 456 in Music Assistant
```

Enter that code where Music Assistant asks for it. Pairing is stored in `~/.local/state/vinylpilot/`
and survives restarts.

If Music Assistant never connects (e.g. mDNS does not reach its container), connect from the Pi instead:

```bash
.venv/bin/vinylpilot --show-levels --server-url ws://<MA-IP>:8927/sendspin
```

## 4. Play

In Music Assistant, open **Live Inputs**, pick **VinylPilot** and play it on a speaker.
VinylPilot logs `Streaming started: pcm 48000 Hz / 16-bit / 2 ch`.

Optional: set an autostart target player for the source in Music Assistant. VinylPilot reports
`signal present` once a record plays (threshold −65 dBFS, 1 s activation, 10 s hold).

## What to report back

- Did Music Assistant discover VinylPilot, or was `--server-url` needed?
- How did pairing look on the Music Assistant side?
- Audio quality, latency, dropouts during a full LP side
- `Record playing` / `Record stopped` timing
