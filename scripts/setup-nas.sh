#!/usr/bin/env bash
# Mount a Synology (SMB) share at /mnt/vinylpilot for recordings.
# Usage: sudo /opt/vinylpilot/scripts/setup-nas.sh //192.168.178.70/music/Vinyl <nas-user>
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Please run with sudo." >&2; exit 1; }
SHARE="${1:-}"; NASUSER="${2:-}"
[[ "$SHARE" == //* && -n "$NASUSER" ]] || { echo "Usage: sudo $0 //<nas-ip>/<share>[/<folder>] <nas-user>" >&2; exit 1; }
MNT=/mnt/vinylpilot
CRED=/etc/vinylpilot/nas-credentials

dpkg -s cifs-utils >/dev/null 2>&1 || { apt-get update -qq || true; apt-get install -y -qq cifs-utils >/dev/null; }

read -r -s -p "Password for $NASUSER on the NAS: " NASPASS; echo
install -d -m 0700 /etc/vinylpilot
install -m 0600 /dev/stdin "$CRED" <<CONF
username=$NASUSER
password=$NASPASS
CONF
unset NASPASS

mkdir -p "$MNT"
OPTS="credentials=$CRED,uid=vinylpilot,gid=vinylpilot,file_mode=0664,dir_mode=0775,iocharset=utf8,vers=3.0,_netdev,nofail,x-systemd.automount,x-systemd.mount-timeout=20"
sed -i "\#[[:space:]]$MNT[[:space:]]#d" /etc/fstab
echo "$SHARE $MNT cifs $OPTS 0 0" >> /etc/fstab
systemctl daemon-reload
systemctl restart "$(systemd-escape -p --suffix=automount "$MNT")" 2>/dev/null || true

if sudo -u vinylpilot touch "$MNT/.vinylpilot-test" 2>/dev/null; then
  rm -f "$MNT/.vinylpilot-test"
  echo "OK: $SHARE is mounted at $MNT and writable for VinylPilot."
else
  echo "Mount or write test failed. Check share path, user and password:" >&2
  echo "  journalctl -b | grep -i cifs | tail" >&2
  exit 1
fi
