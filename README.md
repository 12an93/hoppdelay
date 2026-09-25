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
- One or two cameras (e.g. 1 m and 3 m, or side and front). TV layouts: camera 1, camera 2,
  side by side, picture in picture.
- Show a replay in a corner of the TV while the delayed picture keeps running.
- Automatic dive list: draw a zone in the air in front of the board; every dive through it is listed
  (and can be saved automatically).
- StroMotion picture: the diver pasted in every few frames along the path, in one image.
- Automatic analysis of a dive: path, highest point, distance out from the board, and number of
  somersaults and turns per second.
- Optional AI body pose (RTMPose): skeleton on the replay, hip and knee angles, when the diver
  opens, somersaults counted from the trunk (works in tuck too), and body line at entry.
- Optional AI feedback written from the measurements, using any OpenAI-compatible endpoint (e.g. Ollama).
- Own Wi-Fi hotspot, starts by itself when power is connected.

## Hardware

- Mini PC with Intel graphics (tested: ASUS VivoMini VM65) and HDMI to a TV.
- USB camera with MJPEG up to 1080p at 30–60 fps (tested: Jabra PanaCast 20 with Intelligent Zoom turned off in
  Jabra Direct). The largest size is used, then the highest frame rate up to 60 fps
  (cameras that only offer 90 or 120 fps, like some global shutter cameras, run at that). Autofocus is locked 10 s after
  start, so it does not hunt when a diver passes.
- **The PanaCast 20 must be connected with a USB 2 cable** (e.g. a phone charging cable). On USB 3 it
  only offers MJPEG in 4K.
- Optional: a second USB camera, and a keyboard or presenter clicker with a USB dongle.

## Install (Debian 13, no desktop)

In the BIOS, set *Restore AC Power Loss → Power On* so the box starts when power is connected.

```
sudo sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=0/' /etc/default/grub && sudo update-grub && echo 'FSCKFIX=yes' | sudo tee -a /etc/default/rcS
sudo apt install -y avahi-daemon openssl iw dnsmasq-base v4l-utils python3-evdev python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly intel-media-va-driver python3-opencv python3-numpy
sudo install -m755 hoppdelay.py /usr/local/bin/hoppdelay.py && sudo install -m644 hoppdelay.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now hoppdelay
```

If a desktop is installed: `sudo systemctl set-default multi-user.target`.

Every USB camera with MJPEG up to 1080p at 30 fps or more is used (at most two). To choose, set
`HOPPDELAY_CAMS=/dev/v4l/by-id/...,/dev/v4l/by-id/...` in `hoppdelay.service` (list cameras with
`ls /dev/v4l/by-id/`). Cameras without MJPEG up to 1080p are skipped with a note in the log.

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
| R | Rotate camera 1 by 90° |
| L | Next TV layout (two cameras) |

## Measurements

Mark **Takeoff** and **Water** (and optionally **Top** and **Opening**) while stepping frame by frame,
and pick the board height. The numbers come from projectile motion of the centre of mass, assuming it
drops about the board height from takeoff to entry. At 30 fps one frame is ±0.033 s, which is roughly
±0.1–0.2 m on the height; at 60 fps it is ±0.017 s and half the error. The page shows the camera's real
frame rate – if it drops below what the camera should give, there is usually too little light.

## Automatic dives

Take a replay from camera 1, choose the **Zone** tool and tap two corners of a box in the air just in
front of the board, where only the diver passes (not the board, the water or the stands). Each dive
through the box shows up under **Dives today** with a few seconds before and after; tap **Show** to open
it as a replay. With **Save automatically** on, each one is also saved as a clip.

## Automatic analysis

**Analyse path and rotation** finds the diver against the background in every frame (the camera must
not move) and draws the path on the replay. Mark **Takeoff** and **Water** first and calibrate once
for metres. Rotation is counted from the body axis, so it works well in straight and pike, less well in
tuck – the page says when it is uncertain.

## AI body pose (optional)

**Analyse the body (AI)** runs [RTMPose](https://github.com/open-mmlab/mmpose/tree/main/projects/rtmpose)
(Apache-2.0) on the CPU, a few seconds per dive. The diver is cut out and turned upright before the
model sees her, since pose models are trained on people standing up. Install once, with internet:

```
sudo apt install -y python3-pip && sudo pip install --break-system-packages onnxruntime
curl -fsSLo /tmp/rtm.zip https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/onnx_sdk/rtmpose-m_simcc-body7_pt-body7_420e-256x192-e48f03d0_20230504.zip && sudo mkdir -p /var/lib/hoppdelay/models && sudo python3 -c "import zipfile; z=zipfile.ZipFile('/tmp/rtm.zip'); open('/var/lib/hoppdelay/models/rtmpose-m.onnx','wb').write(z.read([n for n in z.namelist() if n.endswith('end2end.onnx')][0]))" && sudo systemctl restart hoppdelay
```

The button shows up when the model is in place. The page reports the tightest hip angle, the most bent
knee, when the hip opens past 150° (one tap sets it as the **Opening** mark), somersaults counted from
the trunk, and the body line at the water. It is an estimate: check it against the video.

## AI feedback (optional)

Set an OpenAI-compatible chat endpoint in `hoppdelay.service`, for example Ollama on another machine:

```
Environment=HOPPDELAY_LLM_URL=http://192.168.1.10:11434/v1/chat/completions
Environment=HOPPDELAY_LLM_MODEL=qwen3:8b
```

(`HOPPDELAY_LLM_KEY` for services that need a key.) Only the measured numbers are sent, never video.
The box must be able to reach the endpoint, which usually means not at the pool.

## License

Created by Jesper ([@Tolvers2026](https://github.com/12an93)). Videos and pictures made by Hoppdelay carry a faint
@Tolvers2026 mark in the lower right corner.


[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/): free to use, share and
modify for non-commercial purposes – clubs, coaches and schools are welcome. Credit the author and
share changes under the same license. Selling it or using it in a commercial product is not allowed.
