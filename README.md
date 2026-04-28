# Raspberry Pi HiFiBerry CD Player

A full-featured CD and music library player built on a Raspberry Pi 4, with a portrait touchscreen, audiophile DAC output, joystick navigation, automatic metadata/album art fetching, and a web-based management interface.

![Player running on Raspberry Pi with portrait display]

---

## Features

- **CD playback** — insert a disc and it plays automatically. Metadata and album art fetched from MusicBrainz and iTunes.
- **Music library** — browse and play MP3/FLAC files organised by Artist → Album.
- **CD Vault** — store digital copies of scratched or copy-protected CDs. Player transparently plays the vault copy when that disc is inserted.
- **Web editor** — manage CD metadata, upload cover art, rename/delete library files at `http://<pi-ip>:8080`.
- **Samba shares** — music library and CD vault accessible as network folders from Windows.
- **Audiophile output** — bit-perfect playback via HiFiBerry DAC2 HD into your amplifier.
- **Joystick navigation** — no keyboard or mouse needed. Up/Down = track, Left/Right = volume, fire = select/back.

---

## Hardware

| Part | Notes |
|------|-------|
| **Raspberry Pi 4 Model B** (2 GB RAM or more) | Main board. Pi 5 should also work. |
| **HiFiBerry DAC2 HD** | Audio HAT. The HiFiBerry DAC2 HD is a high-end DAC for the Raspberry Pi. |
| **480 × 800 portrait HDMI display** | Any small HDMI display in portrait orientation works. |
| **USB CD/DVD drive** | Tested with ASUS SDRW-08D2S-U. Any USB optical drive should work. |
| **Joystick / controller** (optional) | Tested with STMicroelectronics e4you Retro Fun and the legendary Commodore 64 TAC-2. |
| **MicroSD card** (16 GB+) | For the OS and code. Use a quality card (SanDisk/Samsung). |
| **USB SSD** (recommended) | Better reliability for the music library. A 250 GB USB stick or Samsung T7 works great. |
| **USB-C power supply** (3 A) | Official Pi 4 PSU recommended. |
| **Micro HDMI → HDMI cable** | Pi 4 uses micro HDMI. |

**Estimated cost:** ~€400–500 depending on display and SSD choice.

---

## Software requirements

- Raspberry Pi OS (Debian Bookworm / Trixie), 64-bit
- Python 3.11+
- MPV 0.40+
- `pygame` (pip install)
- `mutagen` (pip install) — for reading MP3/FLAC tags
- `cd-discid` — for reading CD TOC (`sudo apt install cd-discid`)
- `cdparanoia` — for CD ripping (`sudo apt install cdparanoia`)
- `samba` — for network shares (`sudo apt install samba`)
- `python3-requests` — for metadata API calls

---

## Installation

### 1. Flash the OS

Flash Raspberry Pi OS (64-bit, Lite or Desktop) to your SD card using [Raspberry Pi Imager](https://www.raspberrypi.com/software/). Enable SSH and set your username/password in the imager before writing.

### 2. Configure the HiFiBerry DAC

Add to `/boot/firmware/config.txt`:
```
dtoverlay=hifiberry-dacplushd
```
And disable the built-in audio:
```
dtparam=audio=off
```

### 3. Configure the display (portrait 480×800)

Add to `/boot/firmware/config.txt`:
```
hdmi_cvt=480 800 60 6 0 0 0
display_rotate=1
```

### 4. Install dependencies

```bash
sudo apt update
sudo apt install -y mpv cd-discid cdparanoia samba python3-pip
pip install pygame mutagen requests
```

### 5. Deploy the player

```bash
sudo mkdir -p /opt/musicplayer
sudo chown $USER:$USER /opt/musicplayer
cp *.py /opt/musicplayer/
```

### 6. Set up the MPV service

Create `/etc/systemd/system/mpv.service`:
```ini
[Unit]
Description=MPV IPC daemon

[Service]
User=YOUR_USERNAME
ExecStart=mpv --idle=yes --no-video \
  --input-ipc-server=/tmp/mpv_ipc.sock \
  --audio-device=alsa/plughw:CARD=sndrpihifiberry,DEV=0 \
  --gapless-audio=yes --prefetch-playlist=yes
Restart=always

[Install]
WantedBy=multi-user.target
```

### 7. Set up the player service

Create `/etc/systemd/system/musicplayer.service`:
```ini
[Unit]
Description=HiFiBerry Music Player
After=mpv.service
Requires=mpv.service

[Service]
User=YOUR_USERNAME
Environment=SDL_VIDEODRIVER=kmsdrm
Environment=MUSIC_LIBRARY=/home/YOUR_USERNAME/music
WorkingDirectory=/opt/musicplayer
ExecStart=/usr/bin/python3 /opt/musicplayer/main.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

### 8. Set up the web editor service

Create `/etc/systemd/system/meta-editor.service`:
```ini
[Unit]
Description=Music Player Web Editor
After=network.target

[Service]
User=YOUR_USERNAME
Environment=MUSIC_LIBRARY=/home/YOUR_USERNAME/music
WorkingDirectory=/opt/musicplayer
ExecStart=/usr/bin/python3 /opt/musicplayer/meta_editor.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

### 9. Enable CD mount without password

```bash
sudo mkdir -p /mnt/cdrom
echo "YOUR_USERNAME ALL=(root) NOPASSWD: /bin/mount /dev/sr0 /mnt/cdrom, /bin/umount /dev/sr0" \
  | sudo tee /etc/sudoers.d/musicplayer-cdrom
sudo chmod 0440 /etc/sudoers.d/musicplayer-cdrom
```

### 10. Enable and start services

```bash
sudo systemctl daemon-reload
sudo systemctl enable mpv.service musicplayer.service meta-editor.service
sudo systemctl start mpv.service musicplayer.service meta-editor.service
```

---

## Music library structure

```
~/music/
  Artist Name/
    Album Title (Year)/
      01 - Track Name.flac
      02 - Track Name.flac
      cover.jpg
```

Accessible as `\\<pi-ip>\Music` from Windows.

---

## CD Vault

For CDs that have read errors or copy protection, you can store a ripped copy in the vault. When that disc is inserted, the player silently plays the vault copy instead.

Vault folder: `~/cd_vault/<disc-id>/` (disc ID shown in the web editor when a CD is inserted).

Accessible as `\\<pi-ip>\CDVault` from Windows. Rip with EAC on Windows and drop the files in the matching folder.

---

## Web interface

Open `http://<pi-ip>:8080` from any browser on the same network.

- **CD Info** — fix wrong metadata or cover art for the current disc
- **Music Library** — browse albums, upload files, rename, delete
- **Rip CD** — rip a CD to the vault (for discs that play but need a backup copy)

---

## Architecture

```
main.py          — state machine (menu → CD → library → playback)
player.py        — MPV IPC client (JSON-RPC over Unix socket)
ui.py            — pygame display (480×800 portrait)
cd_handler.py    — CD detection, TOC reading, data disc mounting
input_handler.py — joystick + keyboard input
library_manager.py  — scan and index music library
metadata_manager.py — MusicBrainz + iTunes metadata + art fetching
meta_editor.py   — web management UI (Python HTTPServer, port 8080)
```

---

## License

MIT
