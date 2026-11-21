#!/usr/bin/env bash
# VinylPilot installer for Raspberry Pi OS (Trixie or newer). Re-run to update.
# Usage: curl -fsSL https://raw.githubusercontent.com/Mgns55/VinylPilot/main/scripts/install.sh | sudo bash
set -euo pipefail

REPO="https://github.com/Mgns55/VinylPilot.git"
PREFIX="/opt/vinylpilot"
SERVICE="vinylpilot"

[[ $EUID -eq 0 ]] || { echo "Please run with sudo." >&2; exit 1; }

python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' \
  || { echo "Python 3.12+ required (Raspberry Pi OS Trixie)." >&2; exit 1; }

echo "==> Packages"
PKGS=(git python3-venv alsa-utils)
MISSING=()
for p in "${PKGS[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p"); done
if (( ${#MISSING[@]} )); then
  apt-get update -qq || echo "warning: apt-get update reported errors, continuing"
  apt-get install -y -qq "${MISSING[@]}" >/dev/null
else
  echo "    already installed"
fi

echo "==> Service user"
id -u vinylpilot >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin --groups audio vinylpilot

echo "==> Code in $PREFIX"
if [[ -d "$PREFIX/.git" ]]; then
  git -C "$PREFIX" pull --ff-only
else
  git clone --depth 1 "$REPO" "$PREFIX"
fi

echo "==> Python environment (first run on a Zero 2 W takes a few minutes)"
[[ -x "$PREFIX/.venv/bin/python" ]] || python3 -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" install -q --upgrade pip
EXTRAS=""
grep -q -- "--recognize shazam" /etc/default/vinylpilot 2>/dev/null && EXTRAS="[shazam]"
"$PREFIX/.venv/bin/pip" install -q "$PREFIX$EXTRAS"

echo "==> Config"
[[ -f /etc/default/vinylpilot ]] || cat > /etc/default/vinylpilot <<'CONF'
# Extra arguments for vinylpilot, e.g.:
# VINYLPILOT_ARGS="--name VinylPilot --mqtt-host 192.168.178.70 --mqtt-user vinylpilot"
# The MQTT password goes into /etc/vinylpilot/secrets.env, not here.
VINYLPILOT_ARGS=""
CONF
install -d -m 0700 /etc/vinylpilot
[[ -f /etc/vinylpilot/secrets.env ]] || install -m 0600 /dev/stdin /etc/vinylpilot/secrets.env <<'CONF'
# Root-only. Loaded by systemd and passed to VinylPilot as environment variables.
VINYLPILOT_MQTT_PASSWORD=
CONF

echo "==> Keep logs in RAM (no SD card writes)"
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/vinylpilot.conf <<'CONF'
[Journal]
Storage=volatile
RuntimeMaxUse=16M
CONF
systemctl restart systemd-journald

echo "==> Wi-Fi power saving off (prevents audio dropouts)"
if [[ -d /etc/NetworkManager/conf.d ]]; then
  printf '[connection]\nwifi.powersave = 2\n' > /etc/NetworkManager/conf.d/vinylpilot-wifi-powersave.conf
  systemctl reload NetworkManager || true
fi
command -v iw >/dev/null && iw dev wlan0 set power_save off 2>/dev/null || true

echo "==> Watchdogs (network + hardware)"
install -m 0755 "$PREFIX/deploy/vinylpilot-netwatch.sh" /usr/local/sbin/vinylpilot-netwatch
install -m 0644 "$PREFIX/deploy/vinylpilot-netwatch.service" /etc/systemd/system/vinylpilot-netwatch.service
install -m 0644 "$PREFIX/deploy/vinylpilot-netwatch.timer" /etc/systemd/system/vinylpilot-netwatch.timer
mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/vinylpilot-watchdog.conf <<'CONF'
[Manager]
# Hardware watchdog: reboot if the system freezes for 15 s
RuntimeWatchdogSec=15s
CONF
systemctl daemon-reexec

echo "==> systemd service"
install -m 0644 "$PREFIX/deploy/vinylpilot.service" /etc/systemd/system/vinylpilot.service
systemctl daemon-reload
systemctl enable --now "$SERVICE"
systemctl enable --now vinylpilot-netwatch.timer
systemctl restart "$SERVICE"

echo
echo "VinylPilot is running. Follow the log (pairing code appears here):"
echo "  journalctl -u $SERVICE -f"
