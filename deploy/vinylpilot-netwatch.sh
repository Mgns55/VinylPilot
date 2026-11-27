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

# Keep evidence on the SD card (the journal lives in RAM and is gone after the reboot):
# power/throttling flags, Wi-Fi driver messages, memory. Small and capped.
snapshot() {
  local log=/var/lib/vinylpilot/netfail.log
  {
    echo "===== $(date -Is) check $fails ($1), uptime $(cut -d' ' -f1 /proc/uptime)s"
    command -v vcgencmd >/dev/null && echo "throttled: $(vcgencmd get_throttled 2>&1) temp: $(vcgencmd measure_temp 2>&1) volts: $(vcgencmd measure_volts core 2>&1)"
    iw dev wlan0 link 2>&1 | head -6
    ip -brief addr show wlan0 2>&1
    free -m | head -2
    dmesg 2>/dev/null | grep -iE "brcm|mmc1|sdio|voltage|throttl|oom|wlan|usb" | tail -25
  } >> "$log" 2>&1
  tail -c 200000 "$log" > "$log.tmp" && mv "$log.tmp" "$log"
  chmod 0644 "$log"
}
case "$fails" in 1|6|10) snapshot "before action" ;; esac

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
