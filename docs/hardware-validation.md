# Hardware validation

Run on the development Pi with the turntable connected via USB. Replace `1` with the card number from `arecord -l`.

## 1. Detection

```bash
python3 --version
lsusb
cat /proc/asound/cards
arecord -l
```

## 2. Capabilities

```bash
cat /proc/asound/card1/stream0
arecord -D hw:1,0 --dump-hw-params -d 1 /dev/null
```

## 3. Recording and levels

```bash
sudo apt install -y sox
arecord -D hw:1,0 -f S16_LE -r 48000 -c 2 -d 30 ~/vinyl-48k16.wav    # record playing
arecord -D hw:1,0 -f S16_LE -r 48000 -c 2 -d 10 ~/vinyl-silence.wav  # needle up
sox ~/vinyl-48k16.wav -n stats
sox ~/vinyl-silence.wav -n stats
```

- `Device or resource busy` on a desktop image: `systemctl --user stop pipewire pipewire-pulse wireplumber`
- 24-bit capture: use `-f S32_LE` (the PS-HX500 does not offer `S24_3LE`)

## Results

| Check | Sony PS-HX500 |
|---|---|
| USB ID | `054c:0baa` Sony Corp. PS-HX500, USB 2.0 high speed |
| ALSA card name | `PSHX500` (`USB-Audio`, standard `snd-usb-audio` driver, no vendor driver) |
| Capture formats | `S16_LE` (16-bit), `S32_LE` (24-bit and 32-bit). **No `S24_3LE`** → use `S32_LE` for 24-bit |
| Rates | 44.1, 48, 88.2, 96, 176.4, 192, 352.8 kHz |
| Channels | 2 (FL FR) |
| Endpoint | 0x83 IN, **asynchronous** (device-clocked) |
| Recording / playback | ✅ 48 kHz / 16-bit recording plays back cleanly |
| Peak / RMS (playing) | pending |
| Noise floor RMS (needle up) | pending |
| Python version | 3.13.5 (Raspberry Pi OS, Pi 5) |

Tested 2026-11-18 on Raspberry Pi 5 (Raspberry Pi OS Desktop).

Note: `arecord --dump-hw-params` without `-f` ends with `Sample format non available`. That is only because
arecord defaults to 8-bit; it is not a device problem.
