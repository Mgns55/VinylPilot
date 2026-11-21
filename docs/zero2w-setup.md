# Raspberry Pi Zero 2 W setup

## 1. Flash the SD card (Raspberry Pi Imager)

- Device: Raspberry Pi Zero 2 W
- OS: **Raspberry Pi OS Lite (64-bit)** (Trixie or newer)
- Customisation: hostname `vinylpilot`, user, Wi-Fi (country DE), **enable SSH**

## 2. Install

```bash
ssh <user>@vinylpilot.local
curl -fsSL https://raw.githubusercontent.com/Mgns55/VinylPilot/main/scripts/install.sh | sudo bash
```

The script installs VinylPilot to `/opt/vinylpilot`, runs it as the `vinylpilot` system user via systemd
(auto-start, restart on failure), keeps the journal in RAM and disables Wi-Fi power saving.
Re-running it updates VinylPilot.

## 3. Pair

```bash
journalctl -u vinylpilot -f
```

The Zero has a new identity, so Music Assistant pairs it as a new device. Stop VinylPilot on the
development Pi first and remove its old entry in Music Assistant.

## 4. Measure

```bash
systemctl status vinylpilot          # memory (Memory: …) and uptime
top -p "$(pgrep -f 'bin/vinylpilot')"   # CPU while streaming
vcgencmd measure_temp
iw dev wlan0 link                    # Wi-Fi signal
```

Targets: < 100 MB RAM, < 15 % CPU while streaming.

## Options

Edit `/etc/default/vinylpilot`, then `sudo systemctl restart vinylpilot`.

## Watchdogs

The Zero 2 W's Wi-Fi chip (BCM43430) occasionally fails to recover after a dropped connection.
The installer therefore sets up:

- **Network watchdog** (`vinylpilot-netwatch.timer`, every minute): pings the default gateway;
  after 3 failed minutes it toggles Wi-Fi, after 6 it reloads the `brcmfmac` driver, after 10 it reboots
  (only if the Pi has been up for more than 30 minutes, to avoid reboot loops while the router is down).
  Log: `journalctl -t vinylpilot-netwatch`
- **Hardware watchdog** (`RuntimeWatchdogSec=15s`): reboots if the whole system freezes.
