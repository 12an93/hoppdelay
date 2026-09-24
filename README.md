# Hoppdelay

*[Svenska](README.sv.md)*

Delayed video replay for diving practice. A camera films the board and a TV shows the picture with a
delay (30 s by default), so the diver can climb out of the pool and watch their dive. Everything is
controlled from a keyboard/presenter clicker or from a phone through a built-in web page. No internet
or pool network needed: the box runs its own Wi-Fi.

## Features

- Delayed picture on HDMI, scaled to any screen (max 1080p), landscape or portrait.
- The whole session is recorded to disk (up to 40 % of the disk, oldest minute dropped first).
- Rewind, pause, change the delay and rotate – from the keyboard or the phone.
- Replay on the phone while the TV keeps running: slow motion (½×, ¼×, ⅒×) and frame by frame.
- Measure a dive by marking takeoff, top, opening and water entry: flight time, height of the
  highest point above takeoff, and height above the water at the opening – from the board height,
  no tracking needed.
- Drawing tools on the replay: line, angle, and calibration against a known length for distances in metres.
- Compare two saved dives side by side or overlaid, aligned on the takeoff, frame by frame.
- Save clips as MP4 named by diver, dive and height; filter, rename, delete, and save straight to
  Photos on iPhone.
- Own Wi-Fi hotspot, starts by itself when power is connected.

## Hardware

- Mini PC with Intel graphics (tested: ASUS VivoMini VM65) and HDMI to a TV.
- USB camera with MJPEG 1080p30 (tested: Jabra PanaCast 20 with Intelligent Zoom turned off in Jabra Direct).
- **The PanaCast 20 must be connected with a USB 2 cable** (e.g. a phone charging cable). On USB 3 it
  only offers MJPEG in 4K.
- Optional: keyboard or presenter clicker with a USB dongle.

## Install (Debian 13, no desktop)

In the BIOS, set *Restore AC Power Loss → Power On* so the box starts when power is connected.

```
sudo sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=0/' /etc/default/grub && sudo update-grub && echo 'FSCKFIX=yes' | sudo tee -a /etc/default/rcS
sudo apt install -y avahi-daemon openssl iw dnsmasq-base v4l-utils python3-evdev python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly intel-media-va-driver
sudo install -m755 hoppdelay.py /usr/local/bin/hoppdelay.py && sudo install -m644 hoppdelay.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now hoppdelay
```

If a desktop is installed: `sudo systemctl set-default multi-user.target`.

The first USB camera is used. To pick another one, set `HOPPDELAY_CAM` in `hoppdelay.service`
(list cameras with `ls /dev/v4l/by-id/`).

Log: `sudo journalctl -u hoppdelay -n 30 --no-pager`

### Wi-Fi hotspot

Choose your own password (at least 8 characters) instead of `change-me-123`:

```
sudo nmcli dev wifi hotspot ifname wlp3s0 con-name Hoppdelay ssid Hoppdelay password change-me-123 && sudo nmcli con modify Hoppdelay connection.autoconnect yes connection.autoconnect-priority 100
```

The Wi-Fi interface may have another name than `wlp3s0` – check with `nmcli dev`. With the hotspot
on, reach the box over an Ethernet cable or by joining the Hoppdelay network.

### iPhone

1. Join the *Hoppdelay* Wi-Fi and open `http://hoppdelay.local` (or `http://10.42.0.1`).
2. Follow the box at the top: download the certificate, install the profile, and enable
   *Hoppdelay-CA* under Settings → General → About → Certificate Trust Settings.
3. Open `https://hoppdelay.local` and add it to the Home Screen.

HTTPS is required for iPhone to save clips straight to Photos. The certificates are created in
`/etc/hoppdelay` on first start, unique to each box.

## Keyboard

| Key | Action |
|---|---|
| ↑ / ↓ | Delay +5 s / −5 s |
| ← / PageUp | Back 5 s |
| → / PageDown | Forward 5 s |
| Space / B | Pause / play |
| Enter / Esc | Back to normal delay |
| R | Rotate 90° |

## Measurements

Mark **Takeoff** and **Water** (and optionally **Top** and **Opening**) while stepping frame by frame,
and pick the board height. The numbers come from projectile motion of the centre of mass, assuming it
drops about the board height from takeoff to entry. At 30 fps one frame is ±0.03 s, which is roughly
±0.1–0.2 m on the height. A 60 fps camera halves that.

## License

[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/): free to use, share and
modify for non-commercial purposes – clubs, coaches and schools are welcome. Credit the author and
share changes under the same license. Selling it or using it in a commercial product is not allowed.
