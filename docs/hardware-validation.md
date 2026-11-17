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
- Format not supported: try `-f S24_3LE`

## Results

| Check | Sony PS-HX500 |
|---|---|
| USB ID | |
| ALSA card name | |
| Rates / formats | |
| Peak / RMS (playing) | |
| Noise floor RMS (needle up) | |
| Python version | |
