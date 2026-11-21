#!/usr/bin/env bash
# VinylPilot network watchdog: run every minute by vinylpilot-netwatch.timer.
# Escalates when the default gateway stops answering:
#   3 min -> toggle Wi-Fi, 6 min -> reload the Wi-Fi driver, 10 min -> reboot (only if up > 30 min)
set -u
STATE=/run/vinylpilot-netwatch
fails=$(cat "$STATE" 2>/dev/null || echo 0)

gw=$(ip -4 route show default 2>/dev/null | awk '{print $3; exit}')
if [[ -n "$gw" ]] && ping -c 2 -W 3 "$gw" >/dev/null 2>&1; then
  if (( fails > 0 )); then logger -t vinylpilot-netwatch "network back after ${fails} failed checks"; fi
  echo 0 > "$STATE"
  exit 0
fi

fails=$((fails + 1))
echo "$fails" > "$STATE"
logger -t vinylpilot-netwatch "gateway ${gw:-<none>} unreachable (check $fails)"

case "$fails" in
  3)
    logger -t vinylpilot-netwatch "toggling Wi-Fi"
    nmcli radio wifi off; sleep 3; nmcli radio wifi on
    ;;
  6)
    logger -t vinylpilot-netwatch "reloading Wi-Fi driver"
    modprobe -r brcmfmac_wcc 2>/dev/null; modprobe -r brcmfmac; sleep 2; modprobe brcmfmac
    ;;
esac

if (( fails >= 10 )); then
  up=$(cut -d. -f1 /proc/uptime)
  if (( up > 1800 )); then
    logger -t vinylpilot-netwatch "network still down, rebooting"
    systemctl reboot
  fi
fi
