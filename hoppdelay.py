#!/usr/bin/env python3
# Hoppdelay – delayed video replay for diving practice.
# Copyright (c) 2026 Jesper (@Tolvers2026). Licensed under CC BY-NC-SA 4.0 – keep this notice.
# Records one or two USB cameras to disk (whole session), plays back delayed on HDMI, and is
# controlled from a keyboard/clicker or a phone (web page, HTTP 80 / HTTPS 443).
# Keys:
#   Up / Down           delay +5 s / -5 s
#   Left / PageUp       go back 5 s
#   Right / PageDown    go forward 5 s
#   Space / B           pause / play
#   Enter / Esc         back to normal delay
#   R                   rotate camera 1 by 90 degrees
#   L                   next TV layout (with two cameras)
import bisect
import http.server
import json
import os
import pathlib
import queue
import re
import select
import shutil
import ssl
import subprocess
import threading
import time
import urllib.request
from urllib.parse import parse_qs, unquote, urlsplit

import cv2
import evdev
import gi
import numpy as np

try:  # optional: AI body pose (pip install onnxruntime + model, see README)
    import onnxruntime
except ImportError:
    onnxruntime = None

gi.require_version("Gst", "1.0")
from gi.repository import Gst

FPS = 30
WATERMARK = "@Tolvers2026"  # creator mark on every video and picture made here, intentionally hard-coded
# Faint GStreamer text in the lower right corner (ARGB colour: about 30 % white).
WATERMARK_GST = (f'textoverlay text="{WATERMARK}" valignment=bottom halignment=right font-desc="Sans 11" '
                 'color=0x4dffffff draw-shadow=false draw-outline=false xpad=24 ypad=16')
REC = pathlib.Path("/var/lib/hoppdelay")
CLIPS = REC / "clips"
CERTS = pathlib.Path("/etc/hoppdelay")  # own CA + server cert: iPhone needs HTTPS to share files to Photos
STATE = pathlib.Path.home() / ".hoppdelay.json"
SEG_S = 60  # one recording file per minute
MAX_DISK = shutil.disk_usage("/").total * 4 // 10  # recordings may use 40 % of the disk (shared by the cameras)
MAX_CLIP_S = 120
STEP_S = 5
ROTATIONS = ["none", "clockwise", "rotate-180", "counterclockwise"]
LAYOUTS = ["cam0", "cam1", "split", "pip"]
# Automatic clips: motion in the zone starts an event; the clip gets some time before and after.
EVENT_PRE_S, EVENT_POST_S, EVENT_GAP_S = 4.0, 2.0, 1.5
# Optional LLM feedback: any OpenAI-compatible chat endpoint, e.g. Ollama http://host:11434/v1/chat/completions
LLM_URL = os.environ.get("HOPPDELAY_LLM_URL", "")
LLM_MODEL = os.environ.get("HOPPDELAY_LLM_MODEL", "qwen3:8b")
LLM_KEY = os.environ.get("HOPPDELAY_LLM_KEY", "")
# Optional AI body pose: RTMPose-m (Apache-2.0, OpenMMLab), 17 COCO keypoints, run on the CPU.
POSE_MODEL = REC / "models" / "rtmpose-m.onnx"

E = evdev.ecodes
KEYMAP = {  # key -> command (same commands as the web page)
    E.KEY_UP: ("delay_by", STEP_S), E.KEY_DOWN: ("delay_by", -STEP_S),
    E.KEY_LEFT: ("step", -STEP_S), E.KEY_PAGEUP: ("step", -STEP_S),
    E.KEY_RIGHT: ("step", STEP_S), E.KEY_PAGEDOWN: ("step", STEP_S),
    E.KEY_SPACE: ("pause", 0), E.KEY_B: ("pause", 0),
    E.KEY_ENTER: ("live", 0), E.KEY_ESC: ("live", 0),
    E.KEY_R: ("rotate", 0), E.KEY_L: ("layout_next", 0),
}
COMMANDS = {"delay_by", "step", "seek", "pause", "live", "rotate", "layout", "layout_next", "autosave"}

Gst.init(None)
DECODER = "vajpegdec" if Gst.ElementFactory.find("vajpegdec") else "jpegdec"  # Intel GPU decode if available
ENCODER = "vah264enc" if Gst.ElementFactory.find("vah264enc") else "x264enc speed-preset=veryfast"

state = {"delay": 30, "rots": {}, "layout": "cam0", "zone": None, "autosave": False}
try:
    state.update(json.loads(STATE.read_text()))
except (OSError, ValueError):
    pass
if "rot" in state:  # older versions had one rotation
    state["rots"].setdefault("0", state.pop("rot"))
state_lock = threading.Lock()


def save_state():
    with state_lock:
        STATE.write_text(json.dumps(state))


def rot_of(i):
    return state["rots"].get(str(i), "none")


# --- Cameras ---------------------------------------------------------------------------------
def best_mjpeg_size(dev):
    # Largest MJPEG size up to 1080p that the camera offers at 30 fps, or None.
    out = subprocess.run(["v4l2-ctl", "-d", dev, "--list-formats-ext"], capture_output=True, text=True).stdout
    sizes, fmt, size = [], None, None
    for line in out.splitlines():
        if m := re.search(r"\[\d+\]: '(\w+)'", line):
            fmt = m.group(1)
        elif m := re.search(r"Size: Discrete (\d+)x(\d+)", line):
            size = (int(m.group(1)), int(m.group(2)))
        elif fmt == "MJPG" and size and "(30.000 fps)" in line and size[0] <= 1920 and size[1] <= 1080:
            sizes.append(size)
    return max(sizes, key=lambda s: s[0] * s[1]) if sizes else None


def find_cameras():
    # HOPPDELAY_CAMS (comma separated) or HOPPDELAY_CAM if set, otherwise every USB camera; at most two.
    env = os.environ.get("HOPPDELAY_CAMS") or os.environ.get("HOPPDELAY_CAM")
    devs = env.split(",") if env else [str(p) for p in sorted(pathlib.Path("/dev/v4l/by-id").glob("*-video-index0"))]
    found = []
    for dev in devs:
        size = best_mjpeg_size(dev.strip())
        if size:
            found.append((dev.strip(), *size))
        else:
            print(f"Skipping {dev}: no MJPEG up to 1080p30 (PanaCast 20: use a USB 2 cable)", flush=True)
    return found[:2]


class Camera:
    # One USB camera: JPEG frames appended to one file per minute, index in memory.
    def __init__(self, idx, dev, w, h, max_disk):
        self.idx, self.dev, self.w, self.h, self.max_disk = idx, dev, w, h, max_disk
        self.dir = REC / f"cam{idx}"
        self.dir.mkdir(parents=True, exist_ok=True)
        for f in self.dir.glob("*.mjpg"):  # the index lives in memory, so old files are unusable
            f.unlink()
        self.times, self.refs, self.segs, self.disk = [], [], {}, 0  # refs: (segment, offset, length)
        self.seg_no, self.seg_file, self.seg_start = -1, None, 0.0
        self.lock = threading.Lock()
        self.listeners = []  # called with (time, jpeg) for every frame, e.g. the motion detector
        self.pipe = Gst.parse_launch(
            f"v4l2src device={dev} ! image/jpeg,width={w},height={h},framerate={FPS}/1 ! "
            "appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false")
        self.pipe.get_by_name("sink").connect("new-sample", self.on_frame)

    def start(self):
        self.pipe.set_state(Gst.State.PLAYING)

    def seg_path(self, n):
        return self.dir / f"{n:06d}.mjpg"

    def on_frame(self, sink):
        buf = sink.emit("pull-sample").get_buffer()
        data = buf.extract_dup(0, buf.get_size())
        now = time.monotonic()
        if self.seg_file is None or now - self.seg_start >= SEG_S:
            if self.seg_file:
                self.seg_file.close()
            self.seg_no, self.seg_start = self.seg_no + 1, now
            self.seg_file = open(self.seg_path(self.seg_no), "wb", buffering=0)
            with self.lock:
                self.segs[self.seg_no] = 0
        offset = self.seg_file.tell()
        self.seg_file.write(data)
        with self.lock:
            self.times.append(now)
            self.refs.append((self.seg_no, offset, len(data)))
            self.segs[self.seg_no] += len(data)
            self.disk += len(data)
            while self.disk > self.max_disk and len(self.segs) > 1:  # drop the oldest minute
                old = min(self.segs)
                self.disk -= self.segs.pop(old)
                n = bisect.bisect_left(self.refs, (old + 1,))
                del self.times[:n], self.refs[:n]
                self.seg_path(old).unlink()
        for listener in self.listeners:
            listener(now, data)
        return Gst.FlowReturn.OK

    def read(self, ref):
        n, offset, length = ref
        with open(self.seg_path(n), "rb") as f:
            f.seek(offset)
            return f.read(length)

    def between(self, t0, t1):
        with self.lock:
            i, j = bisect.bisect_left(self.times, t0), bisect.bisect_right(self.times, t1)
            return self.times[i:j], self.refs[i:j]

    def at(self, t):
        # (capture time, ref) of the newest frame at or before t, or the oldest frame; None if empty.
        with self.lock:
            if not self.refs:
                return None
            i = max(bisect.bisect_right(self.times, t) - 1, 0)
            return self.times[i], self.refs[i]

    def span(self, now):
        with self.lock:
            return (now - self.times[0] if self.times else 0.0), (self.times[-1] if self.times else None)


def save_clip(cam, t0, t1, rot, name):
    # Re-encode the JPEG frames to H.264 MP4 (plays on iPhone), upright according to `rot`.
    ts, rs = cam.between(t0, t1)
    tmp = CLIPS / (name + ".part")
    p = Gst.parse_launch(
        f"appsrc name=src format=time block=true caps=image/jpeg,width={cam.w},height={cam.h},framerate={FPS}/1 ! "
        f"jpegparse ! {DECODER} ! videoflip method={rot} ! videoconvert ! {WATERMARK_GST} ! videoconvert ! video/x-raw,format=NV12 ! "
        f"{ENCODER} ! h264parse ! mp4mux ! filesink name=sink"
    )
    p.get_by_name("sink").set_property("location", str(tmp))  # names may contain spaces
    src = p.get_by_name("src")
    p.set_state(Gst.State.PLAYING)
    for t, ref in zip(ts, rs):
        try:
            buf = Gst.Buffer.new_wrapped(cam.read(ref))
        except FileNotFoundError:
            continue
        buf.pts = int((t - ts[0]) * Gst.SECOND)
        buf.duration = Gst.SECOND // FPS
        src.emit("push-buffer", buf)
    src.emit("end-of-stream")
    msg = p.get_bus().timed_pop_filtered(Gst.CLOCK_TIME_NONE, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    p.set_state(Gst.State.NULL)
    if msg and msg.type == Gst.MessageType.EOS:
        tmp.rename(CLIPS / name)
    else:
        print(f"Saving {name} failed: {msg.parse_error()[0].message if msg else 'no message'}", flush=True)
        tmp.unlink(missing_ok=True)


def new_clip(cam, t0, t1, rot, base, meta):
    # Pick a unique name, write the metadata and encode in the background. Returns the clip name.
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - (time.monotonic() - t0)))
    base = f"{base} {stamp}"
    name, n = base + ".mp4", 2
    while (CLIPS / name).exists() or (CLIPS / (name + ".part")).exists():  # same second saved twice
        name, n = f"{base}-{n}.mp4", n + 1
    meta_file(CLIPS / name).write_text(json.dumps(meta))
    (CLIPS / (name + ".part")).touch()  # shows up as "saving" right away
    threading.Thread(target=save_clip, args=(cam, t0, t1, rot, name), daemon=True).start()
    return name


# --- Image analysis (OpenCV) -------------------------------------------------------------------
CV_ROT = {"clockwise": cv2.ROTATE_90_CLOCKWISE, "rotate-180": cv2.ROTATE_180,
          "counterclockwise": cv2.ROTATE_90_COUNTERCLOCKWISE}
REDUCE = {1: cv2.IMREAD_COLOR, 2: cv2.IMREAD_REDUCED_COLOR_2, 4: cv2.IMREAD_REDUCED_COLOR_4, 8: cv2.IMREAD_REDUCED_COLOR_8}


def decode(jpeg, factor=1, rot="none"):
    # JPEG -> BGR image, decoded at 1/factor size (fast), rotated to how it is shown.
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), REDUCE[factor])
    return cv2.rotate(img, CV_ROT[rot]) if rot in CV_ROT else img


def view_to_raw(x, y, rot):
    # Normalised point on the rotated picture -> normalised point on the camera picture.
    return {"none": (x, y), "clockwise": (y, 1 - x), "rotate-180": (1 - x, 1 - y), "counterclockwise": (1 - y, x)}[rot]


def foreground(cam, t0, t1, rot, factor):
    # Everything that moves between t0 and t1 compared to the background (median of the clip),
    # ignoring spots that move most of the time (water, spectators). Yields (t, image, mask).
    ts, refs = cam.between(t0, t1)
    if len(refs) < 3:
        return
    sample = [decode(cam.read(r), factor, rot) for r in refs[:: max(1, len(refs) // 25)]]
    bg = np.median(np.stack(sample), axis=0).astype(np.uint8)
    gbg = cv2.GaussianBlur(cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    diff = lambda img: cv2.absdiff(cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (5, 5), 0), gbg) > 30
    busy = np.mean([diff(s) for s in sample], axis=0) > 0.35
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    yield None, bg, None, None
    for t, ref in zip(ts, refs):
        img = decode(cam.read(ref), factor, rot)
        m = (diff(img) & ~busy).astype(np.uint8)
        m = cv2.morphologyEx(cv2.morphologyEx(m, cv2.MORPH_OPEN, kernel), cv2.MORPH_CLOSE, kernel, iterations=2)
        yield t, img, m, ref


def largest_blob(mask, min_area):
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    if n < 2:
        return None
    k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return (labels == k).astype(np.uint8) if stats[k, cv2.CC_STAT_AREA] >= min_area else None


def stromotion(cam, t0, t1, rot, every):
    # One picture with the diver pasted in every `every` frames along the path (like Dartfish StroMotion).
    frames = foreground(cam, t0, t1, rot, max(1, analysis_factor(cam) // 2))  # a bit sharper for the picture
    head = next(frames, None)
    if head is None:
        return None
    out = head[1].copy()
    min_area = out.shape[0] * out.shape[1] // 2000
    for i, (t, img, m, _) in enumerate(frames):
        if i % every:
            continue
        blob = largest_blob(m, min_area)
        if blob is not None:
            blob = cv2.dilate(blob, np.ones((5, 5), np.uint8)).astype(bool)
            out[blob] = img[blob]
    watermark(out)
    return cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()


def analysis_factor(cam):
    # Decode at about 480 px width: fast, and the diver is still big enough to keep her shape.
    factor = 1
    while factor < 8 and cam.w / (factor * 2) >= 480:
        factor *= 2
    return factor


def track(cam, t0, t1, rot):
    # Centre and body axis of the diver in every frame, in pixels of the full-size rotated picture.
    factor, points = analysis_factor(cam), []
    frames = foreground(cam, t0, t1, rot, factor)
    head = next(frames, None)
    if head is None:
        return points
    min_area = head[1].shape[0] * head[1].shape[1] // 2000
    for t, img, m, _ in frames:
        blob = largest_blob(m, min_area)
        if blob is None:
            continue
        mo = cv2.moments(blob, binaryImage=True)
        cx, cy = mo["m10"] / mo["m00"], mo["m01"] / mo["m00"]
        a, b, c = mo["mu20"] / mo["m00"], mo["mu11"] / mo["m00"], mo["mu02"] / mo["m00"]
        root = np.sqrt(((a - c) / 2) ** 2 + b * b)
        l1, l2 = (a + c) / 2 + root, (a + c) / 2 - root  # spread along the body axis and across it
        points.append({"t": t, "x": cx * factor, "y": cy * factor, "angle": float(np.degrees(0.5 * np.arctan2(2 * b, a - c))),
                       "elong": float(np.sqrt(l1 / l2)) if l2 > 1e-6 else 99.0})
    return points


# --- AI body pose (RTMPose) ------------------------------------------------------------------
POSE_MEAN, POSE_STD = np.float32([123.675, 116.28, 103.53]), np.float32([58.395, 57.12, 57.375])
POSE_IN_W, POSE_IN_H = 192, 256
NOSE, SHO, ELB, WRI, HIP, KNE, ANK = 0, (5, 6), (7, 8), (9, 10), (11, 12), (13, 14), (15, 16)
pose_lock, pose_sess = threading.Lock(), None


def pose_available():
    return onnxruntime is not None and POSE_MODEL.exists()


def rtmpose(img):
    # 17 keypoints (x, y, score) of the person filling `img`, same pre/post-processing as rtmlib.
    global pose_sess
    with pose_lock:
        if pose_sess is None:
            pose_sess = onnxruntime.InferenceSession(str(POSE_MODEL), providers=["CPUExecutionProvider"])
    h, w = img.shape[:2]
    bw, bh = w * 1.25, h * 1.25
    if bw > bh * POSE_IN_W / POSE_IN_H:
        bh = bw * POSE_IN_H / POSE_IN_W
    else:
        bw = bh * POSE_IN_W / POSE_IN_H
    k = POSE_IN_W / bw
    m = np.float32([[k, 0, POSE_IN_W / 2 - k * w / 2], [0, k, POSE_IN_H / 2 - k * h / 2]])
    inp = cv2.warpAffine(img, m, (POSE_IN_W, POSE_IN_H), flags=cv2.INTER_LINEAR).astype(np.float32)
    inp = ((inp - POSE_MEAN) / POSE_STD).transpose(2, 0, 1)[None]
    sx, sy = pose_sess.run(None, {pose_sess.get_inputs()[0].name: np.ascontiguousarray(inp)})
    locs = np.stack([sx[0].argmax(1), sy[0].argmax(1)], 1) / 2.0  # SimCC split ratio 2
    score = (sx[0].max(1) + sy[0].max(1)) / 2
    return np.column_stack([(locs - m[:, 2]) / k, score])


def joint(kp, pair):
    # Mean of the left and right joint, weighted by confidence; None if neither is seen.
    a, b = kp[pair[0]], kp[pair[1]]
    wa, wb = max(a[2], 0), max(b[2], 0)
    if wa + wb < 0.6:
        return None
    return (a[:2] * wa + b[:2] * wb) / (wa + wb)


def angle_at(a, b, c):
    if a is None or b is None or c is None:
        return None
    v1, v2 = a - b, c - b
    n = np.linalg.norm(v1) * np.linalg.norm(v2)
    return float(np.degrees(np.arccos(np.clip(v1 @ v2 / n, -1, 1)))) if n > 0 else None


def plausible(a, b, c):
    # Thigh and shin are about equally long; a much shorter segment is a keypoint that went wrong.
    if a is None or b is None or c is None:
        return False
    l1, l2 = np.linalg.norm(a - b), np.linalg.norm(c - b)
    return l1 > 0 and 0.5 < l2 / l1 < 2.0


def body_pose(cam, t0, t1, rot):
    # Keypoints and joint angles per frame. The diver is cut out and turned upright first (from the
    # body axis found by the tracking), because pose models are trained on people standing up.
    factor, out = analysis_factor(cam), []
    frames = foreground(cam, t0, t1, rot, factor)
    head = next(frames, None)
    if head is None:
        return out
    min_area = head[1].shape[0] * head[1].shape[1] // 2000
    found = []  # first pass: where the diver is in each frame, and her biggest size in the whole dive
    for t, small, m, ref in frames:
        blob = largest_blob(m, min_area)
        if blob is not None:
            ys, xs = np.nonzero(blob)
            found.append((t, ref, blob, xs, ys, max(xs.max() - xs.min(), ys.max() - ys.min())))
    biggest = max((f[5] for f in found), default=0)
    for t, ref, blob, xs, ys, extent in found:
        mo = cv2.moments(blob, binaryImage=True)
        axis = np.degrees(0.5 * np.arctan2(2 * mo["mu11"], mo["mu20"] - mo["mu02"]))
        l1, l2 = mo["mu20"] + mo["mu02"], np.hypot(mo["mu20"] - mo["mu02"], 2 * mo["mu11"])
        elong = np.sqrt((l1 + l2) / max(l1 - l2, 1e-6))
        full = decode(cam.read(ref), 1, rot)
        cx, cy = (xs.min() + xs.max()) / 2 * factor, (ys.min() + ys.max()) / 2 * factor
        # generous: limbs in front of a similar background (the board, a wall) can be missing from the mask
        size = int(max(extent * 1.8, biggest * 1.4) * factor + 32)
        turns = [axis - 90, axis + 90] + ([axis, axis + 180] if elong < 1.6 else [])  # upright, and upside down
        best = None
        for turn in turns:
            mat = cv2.getRotationMatrix2D((cx, cy), turn, 1.0)
            mat[:, 2] += (size / 2 - cx, size / 2 - cy)
            kp = rtmpose(cv2.warpAffine(full, mat, (size, size)))
            if best is None or kp[:, 2].mean() > best[0][:, 2].mean():
                best = (kp, mat)
        kp, mat = best
        inv = cv2.invertAffineTransform(mat)
        kp[:, :2] = kp[:, :2] @ inv[:, :2].T + inv[:, 2]  # back to picture coordinates
        sho, hip, kne, ank = joint(kp, SHO), joint(kp, HIP), joint(kp, KNE), joint(kp, ANK)
        if not plausible(hip, kne, ank):  # e.g. the ankle collapsed onto the knee when the model was unsure
            ank = None
        trunk = float(np.degrees(np.arctan2(*(sho - hip)[::-1]))) if sho is not None and hip is not None else None
        line = float(np.degrees(np.arctan2(*(sho - ank)[::-1]))) if sho is not None and ank is not None else None
        out.append({"t": t, "kp": np.round(kp, 1).tolist(), "score": float(kp[:, 2].mean()),
                    "hip": angle_at(sho, hip, kne), "knee": angle_at(hip, kne, ank), "trunk": trunk, "line": line})
    return out


class Detector(threading.Thread):
    # Motion in the zone (camera 1) = a dive. Frames arrive from the capture thread and are dropped if busy.
    def __init__(self, cam):
        super().__init__(daemon=True)
        self.cam, self.q, self.events, self.lock = cam, queue.Queue(maxsize=8), [], threading.Lock()
        cam.listeners.append(self.feed)

    def feed(self, t, jpeg):
        try:
            self.q.put_nowait((t, jpeg))
        except queue.Full:
            pass

    def run(self):
        bg, key, start, last = None, None, None, 0.0
        while True:
            t, jpeg = self.q.get()
            zone = state.get("zone")
            if not zone:
                bg, start = None, None
                continue
            g = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_8)
            h, w = g.shape
            x0, y0, x1, y1 = int(zone[0] * w), int(zone[1] * h), int(zone[2] * w), int(zone[3] * h)
            if x1 - x0 < 4 or y1 - y0 < 4:
                continue
            g = cv2.GaussianBlur(g[y0:y1, x0:x1], (5, 5), 0).astype(np.float32)
            if bg is None or key != zone:
                bg, key, start = g, zone, None
                continue
            moving = np.mean(np.abs(g - bg) > 25) > 0.02
            # Always follow the background slowly: a passing diver covers each spot only for a few
            # frames, while someone who was in the zone at start fades out within a second or two.
            cv2.accumulateWeighted(g, bg, 0.05)
            if moving:
                start, last = (start if start is not None else t), t
                if t - start > 10:  # someone standing in the zone, not a dive
                    start = None
            elif start is not None and t - last > EVENT_GAP_S:
                if last - start >= 0.2:
                    self.add(start - EVENT_PRE_S, last + EVENT_POST_S)
                start = None

    def add(self, t0, t1):
        wall = time.strftime("%H:%M:%S", time.localtime(time.time() - (time.monotonic() - t0 - EVENT_PRE_S)))
        with self.lock:
            self.events.append({"t0": t0, "t1": t1, "wall": wall})
            del self.events[:-200]
        if state.get("autosave"):
            def later():  # wait until the end of the clip has been recorded
                time.sleep(max(0.0, t1 + 0.3 - time.monotonic()))
                new_clip(self.cam, t0, t1, rot_of(self.cam.idx), "Auto", {"board": 3.0, "marks": {}, "cam": self.cam.idx})
            threading.Thread(target=later, daemon=True).start()


def llm_feedback(text):
    body = {"model": LLM_MODEL, "temperature": 0.3, "messages": [
        {"role": "system", "content": (
            "Du är assistent åt en simhoppstränare. Du får mätvärden från ett videoanalysverktyg för ett hopp. "
            "Skriv 2–4 korta punkter på svenska: vad siffrorna visar och vad tränaren kan titta efter. "
            "Använd bara siffrorna du får, hitta inte på tekniska fel du inte kan se, och påminn kort om "
            "mätosäkerheten om den är stor.")},
        {"role": "user", "content": text}]}
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {LLM_KEY}"} if LLM_KEY else {})}
    req = urllib.request.Request(LLM_URL, data=json.dumps(body).encode(), headers=headers)
    reply = json.load(urllib.request.urlopen(req, timeout=120))["choices"][0]["message"]["content"]
    return re.sub(r"<think>.*?</think>", "", reply, flags=re.S).strip()  # reasoning models


# --- Display ---------------------------------------------------------------------------------
def screen_size():
    # Largest progressive mode up to 1080p on the first connected screen. Not the "preferred" mode:
    # AV receivers often report 640x480 as preferred. Re-checked so a different TV can be plugged in.
    for status_file in sorted(pathlib.Path("/sys/class/drm").glob("card*-*/status")):
        if status_file.read_text().strip() == "connected":
            modes = [tuple(map(int, m.split("x"))) for m in (status_file.parent / "modes").read_text().split() if not m.endswith("i")]
            modes = [m for m in modes if m[0] <= 1920 and m[1] <= 1080]
            conn_file = status_file.parent / "connector_id"  # tell kmssink which output to use
            conn = int(conn_file.read_text()) if conn_file.exists() else -1
            if modes:
                return (*max(modes, key=lambda m: m[0] * m[1]), conn)
    return 1920, 1080, -1


def display_jpeg(cam, rot, sw, sh, conn):
    # One camera full screen: JPEG decoded by the GPU, scaled with black borders, text on top.
    p = Gst.parse_launch(
        f"appsrc name=src is-live=true max-buffers=1 leaky-type=downstream do-timestamp=true format=time caps=image/jpeg,width={cam.w},height={cam.h},framerate={FPS}/1 ! "
        f"jpegparse ! {DECODER} ! videoflip method={rot} ! videoconvert ! "
        f"videoscale add-borders=true ! video/x-raw,width={sw},height={sh},pixel-aspect-ratio=1/1 ! "
        'textoverlay name=txt valignment=top halignment=left font-desc="Sans 20" ! '
        f"{WATERMARK_GST} ! "
        f"videoconvert ! kmssink sync=false force-modesetting=true connector-id={conn}"
    )
    p.set_state(Gst.State.PLAYING)
    return p, p.get_by_name("src"), p.get_by_name("txt")


def display_raw(sw, sh, conn):
    # Picture composed in Python (split screen, picture in picture, TV replay).
    p = Gst.parse_launch(
        f"appsrc name=src is-live=true max-buffers=1 leaky-type=downstream do-timestamp=true format=time "
        f"caps=video/x-raw,format=BGR,width={sw},height={sh},framerate={FPS}/1 ! "
        f"videoconvert ! kmssink sync=false force-modesetting=true connector-id={conn}"
    )
    p.set_state(Gst.State.PLAYING)
    return p, p.get_by_name("src"), None


def fit(img, bw, bh):
    h, w = img.shape[:2]
    s = min(bw / w, bh / h)
    return cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)


def paste(canvas, img, x, y, bw, bh, border=False):
    img = fit(img, bw, bh)
    h, w = img.shape[:2]
    x, y = x + (bw - w) // 2, y + (bh - h) // 2
    canvas[y:y + h, x:x + w] = img
    if border:
        cv2.rectangle(canvas, (x - 2, y - 2), (x + w + 1, y + h + 1), (255, 255, 255), 2)


def watermark(img):
    # Faint creator mark in the lower right corner, blended in at about 30 %.
    h, w = img.shape[:2]
    size, thick = h / 1500, max(1, round(h / 1000))
    (tw, th), base = cv2.getTextSize(WATERMARK, cv2.FONT_HERSHEY_SIMPLEX, size, thick)
    x0, y0, x1, y1 = w - tw - w // 60 - 2, h - th - base - h // 60 - 2, w - w // 60 + 2, h - h // 60 + 2
    roi = img[y0:y1, x0:x1]
    mark = roi.copy()
    cv2.putText(mark, WATERMARK, (2, th + 2), cv2.FONT_HERSHEY_SIMPLEX, size, (255, 255, 255), thick, cv2.LINE_AA)
    cv2.addWeighted(mark, 0.3, roi, 0.7, 0, dst=roi)


def put_text(canvas, text, x, y, size):
    for color, thick in (((0, 0, 0), 5), ((255, 255, 255), 2)):
        cv2.putText(canvas, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, size, color, thick, cv2.LINE_AA)


def decode_for(cam, jpeg, rot, box_w):
    # Decode no bigger than needed for a box `box_w` pixels wide.
    width = cam.h if rot in ("clockwise", "counterclockwise") else cam.w
    factor = 1
    while factor < 8 and width / (factor * 2) >= box_w:
        factor *= 2
    return decode(jpeg, factor, rot)


def compose(sw, sh, layout, shown, tv_frame, text):
    # shown: {camera index: (cam, jpeg)}; tv_frame: (cam, jpeg) of the TV replay or None.
    canvas = np.zeros((sh, sw, 3), np.uint8)
    if layout == "split" and len(shown) == 2:
        for k, (i, (cam, jpeg)) in enumerate(sorted(shown.items())):
            paste(canvas, decode_for(cam, jpeg, rot_of(i), sw // 2), k * sw // 2, 0, sw // 2, sh)
    else:
        main = 1 if layout == "cam1" and 1 in shown else 0
        cam, jpeg = shown[main]
        paste(canvas, decode_for(cam, jpeg, rot_of(main), sw), 0, 0, sw, sh)
        if layout == "pip" and 1 in shown:
            cam, jpeg = shown[1]
            paste(canvas, decode_for(cam, jpeg, rot_of(1), sw // 3), sw * 2 // 3 - 16, sh * 2 // 3 - 16, sw // 3, sh // 3, True)
    if tv_frame:
        cam, jpeg = tv_frame
        paste(canvas, decode_for(cam, jpeg, rot_of(cam.idx), sw // 3), 16, sh * 2 // 3 - 16, sw // 3, sh // 3, True)
        put_text(canvas, "Repris", 24, sh * 2 // 3, sh / 1000)
    put_text(canvas, text, 16, int(sh / 22), sh / 1200)
    watermark(canvas)
    return canvas


def scan(kbds):
    for path in evdev.list_devices():
        if path in kbds:
            continue
        dev = evdev.InputDevice(path)
        if set(KEYMAP) & set(dev.capabilities().get(E.EV_KEY, [])):
            dev.grab()  # keep keypresses away from the login console
            kbds[path] = dev
        else:
            dev.close()


def mmss(s):
    return f"{int(s) // 60}:{int(s) % 60:02d}"


# --- Saved clips -----------------------------------------------------------------------------
def clean_name(s):
    # Letters (incl. åäö), digits, space, - _ . ; always ends in .mp4
    s = "".join(ch for ch in s if ch.isalnum() or ch in " -_.").strip(" .").removesuffix(".mp4").strip(" .")[:80]
    return s + ".mp4" if s else ""


def clip_file(name):
    # A finished clip in CLIPS, or None. Rejects paths and anything else.
    f = CLIPS / name
    return f if name == clean_name(name) and f.is_file() else None


def meta_file(clip):
    # Marks, board height and camera saved next to the clip, used for measurements and comparisons.
    return clip.with_name(clip.name + ".json")


def clean_meta(m):
    marks = {k: float(v) for k, v in (m.get("marks") or {}).items()
             if k in ("takeoff", "apex", "open", "water") and isinstance(v, (int, float))}
    return {"board": float(m.get("board", 3)), "marks": marks}


# --- Web page for the phone -------------------------------------------------------------------
PAGE = r"""<!doctype html><html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes"><title>Hoppdelay</title>
<style>
body{margin:0;padding:16px;background:#111;color:#eee;font:17px -apple-system,sans-serif;user-select:none;-webkit-user-select:none}
h1{font-size:28px;margin:4px 0 2px}h2{font-size:20px;margin:28px 0 6px;border-top:1px solid #333;padding-top:16px}
h3{font-size:16px;color:#bbb;margin:16px 0 0}
#sub,#info,#cinfo{color:#999;margin:6px 0}
.row{display:flex;gap:8px;margin:10px 0}.row>*{flex:1;min-width:0}
button,select,input[type=text]{background:#2a2a2a;color:#eee;border:0;border-radius:12px;padding:18px 0;font-size:20px;text-align:center}
input[type=text]{padding:14px 10px;font-size:17px;text-align:left}
button:active{background:#444}.big{background:#1f6feb;width:100%}.on{background:#1f6feb}
.tools button,.marks button{padding:12px 0;font-size:15px}
input[type=range]{width:100%;height:40px}label{color:#999;font-size:14px}
#d{text-align:center;font-size:24px;align-self:center}
canvas{width:100%;height:auto;display:block;background:#000;border-radius:8px}
.box,#phys{background:#1b1b1b;border-radius:10px;padding:10px 12px;font-size:15px;line-height:1.5}
#phys small,.box small,.note{color:#888;font-size:14px}
.ev{display:flex;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid #222}.ev span{flex:1}.ev button{padding:10px 16px;font-size:16px}
a{color:#58a6ff}#tls{background:#1c2a3a;border-radius:12px;padding:12px 14px;margin-bottom:12px;font-size:15px}#tls ol{margin:6px 0 0;padding-left:20px}
.clip{display:flex;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid #222}
.clip a{flex:1;word-break:break-all}.clip span{color:#999;font-size:14px}.clip button{padding:10px 14px;font-size:18px}
</style></head><body>
<div id="tls" hidden><b>Spara klipp direkt i Bilder</b> (görs en gång per telefon):<ol>
<li><a href="/ca.crt">Hämta certifikatet</a> → Tillåt</li>
<li>Inställningar → Profil hämtad → Installera</li>
<li>Inställningar → Allmänt → Om → Certifikatinställningar → slå på <i>Hoppdelay-CA</i></li>
<li>Öppna <a id="https">https://hoppdelay.local</a> och lägg den på hemskärmen</li></ol></div>
<h1 id="h">…</h1><div id="sub"></div>
<label>Tidslinje (dra för att spola)</label>
<input type="range" id="t" min="0" max="0" step="0.1" value="0">
<div class="row"><button onclick="c('step/-10')">−10 s</button><button onclick="c('step/-2')">−2 s</button>
<button onclick="c('pause')" id="p">⏸</button><button onclick="c('step/2')">+2 s</button><button onclick="c('step/10')">+10 s</button></div>
<button class="big" onclick="c('live')">Tillbaka till delay</button>
<label>Delay</label>
<div class="row"><button onclick="c('delay_by/-5')">−5</button><div id="d"></div><button onclick="c('delay_by/5')">+5</button></div>
<div class="row" id="rots"></div>
<div id="multi" hidden><label>TV-layout</label>
<div class="row tools" id="lay"><button data-l="0">Kamera 1</button><button data-l="1">Kamera 2</button>
<button data-l="2">Sida vid sida</button><button data-l="3">Bild i bild</button></div></div>

<h2>Repris</h2>
<div class="row" id="rcamrow" hidden><select id="rcam"></select></div>
<div class="row"><select id="len"><option value="4">4 s</option><option value="7" selected>7 s</option>
<option value="10">10 s</option><option value="15">15 s</option></select>
<button onclick="grab()" style="flex:2">Ta repris av TV-bilden</button></div>
<div id="rp" hidden>
<div class="row tools" id="tb1"></div>
<canvas id="cv"></canvas><div id="info"></div>
<input type="range" id="rs" min="0" max="0" step="1" value="0">
<div class="row"><button onclick="stepf(-1)">◀︎|</button><button onclick="play()" id="pl">▶︎</button><button onclick="stepf(1)">|▶︎</button></div>
<div class="row" id="sp"><button data-s="1" class="on">1×</button><button data-s="0.5">½×</button><button data-s="0.25">¼×</button><button data-s="0.1">⅒×</button></div>
<h3>Mätning – markera bilderna</h3>
<div class="row marks" id="mk"><button data-m="takeoff">Upphopp</button><button data-m="apex">Topp</button>
<button data-m="open">Öppning</button><button data-m="water">Vatten</button><button data-m="clear">Rensa</button></div>
<div class="row"><label style="align-self:center;flex:0 0 auto">Svikt/torn</label><select id="board">
<option value="1">1 m</option><option value="3" selected>3 m</option><option value="5">5 m</option>
<option value="7.5">7,5 m</option><option value="10">10 m</option></select></div>
<div id="phys"></div>
<h3>Visa på TV</h3>
<div class="row"><button onclick="tvShow()">Visa reprisen i TV-hörnet</button><button onclick="tvHide()" id="tvoff">Stäng</button></div>
<h3>Analys</h3>
<div class="row"><select id="every"><option value="3">Var 3:e bild</option><option value="5" selected>Var 5:e bild</option>
<option value="8">Var 8:e bild</option></select><button onclick="stro()">StroMotion</button></div>
<img id="stroimg" hidden style="width:100%;border-radius:8px"><div id="strotip" class="note" hidden></div>
<button onclick="analyse()" style="width:100%">Analysera bana och rotation</button>
<div id="trk" class="box" hidden></div>
<button id="poseb" onclick="poseAnalyse()" hidden style="width:100%;margin-top:8px">Analysera kroppen (AI)</button>
<div id="pose" class="box" hidden></div>
<button id="fbb" onclick="feedback()" hidden style="width:100%;margin-top:8px">Skriv feedback (AI)</button>
<div id="fb" class="box" hidden></div>
<h3>Spara</h3>
<div class="row"><input type="text" id="diver" placeholder="Hoppare"><input type="text" id="dive" placeholder="Hopp, t.ex. 5231D"></div>
<button class="big" onclick="save()">Spara klipp</button>
</div>

<h2>Hopp idag</h2>
<div id="zinfo" class="note"></div>
<div class="row"><button id="asb" onclick="c('autosave/'+(S.autosave?0:1))">Spara automatiskt: av</button><button onclick="zoneOff()">Ta bort zon</button></div>
<div id="ev"></div>

<h2>Jämför två klipp</h2>
<div class="row"><select id="ca"></select><select id="cb"></select></div>
<div class="row"><button id="cmode" onclick="cMode()">Sida vid sida</button><button onclick="cLoad()">Ladda</button></div>
<div id="cp" hidden>
<div class="row tools" id="tb2"></div>
<canvas id="cc"></canvas><div id="cinfo"></div>
<div class="row"><button onclick="cStep(-1)">◀︎|</button><button onclick="cPlay()" id="cpl">▶︎</button><button onclick="cStep(1)">|▶︎</button></div>
<div class="row" id="csp"><button data-s="1" class="on">1×</button><button data-s="0.5">½×</button><button data-s="0.25">¼×</button><button data-s="0.1">⅒×</button></div>
<div class="row"><button onclick="cShift(-1)">B −1 bild</button><button onclick="cShift(1)">B +1 bild</button></div>
</div>

<h2>Sparade klipp</h2>
<input type="text" id="cf" placeholder="Filtrera (namn, hopp, höjd …)" style="width:100%;box-sizing:border-box">
<div id="cl">Inga än</div>

<script>
const $=id=>document.getElementById(id),enc=encodeURIComponent,G=9.81;
const esc=s=>String(s).replace(/[&<>"']/g,ch=>'&#'+ch.charCodeAt(0)+';');
const num=(v,d)=>v.toFixed(d).replace('.',',');
const ROT={none:0,clockwise:Math.PI/2,'rotate-180':Math.PI,counterclockwise:-Math.PI/2};
const store=(k,v)=>{try{if(v===undefined)return localStorage.getItem(k);localStorage.setItem(k,v);}catch(e){}};
let drag=false,S={},R=null,lastT=0;

// ---- Live control ------------------------------------------------------------------------
function c(x){fetch('/api/'+x,{method:'POST'}).then(u)}
$('t').oninput=()=>{drag=true;c('seek/'+(-$('t').value))};$('t').onchange=()=>{drag=false};
function f(s){s=Math.floor(s);return Math.floor(s/60)+':'+String(s%60).padStart(2,'0')}
function u(){fetch('/api/state').then(r=>r.json()).then(s=>{S=s;
 $('h').textContent=s.review?(s.paused?'Paus  ':'')+'−'+f(s.behind):'Delay '+s.delay+' s';
 $('sub').textContent='Inspelat '+f(s.span)+(s.review?' · tryck "Tillbaka" för delay':'');
 $('d').textContent=s.delay+' s';$('p').textContent=s.paused?'▶':'⏸';
 $('t').min=-s.span;if(!drag)$('t').value=-s.behind;
 const two=s.cams.length>1;$('multi').hidden=!two;$('rcamrow').hidden=!two;
 $('lay').querySelectorAll('button').forEach(b=>b.classList.toggle('on',['cam0','cam1','split','pip'][+b.dataset.l]===s.layout));
 const rh=two?s.cams.map(k=>'<button onclick="c(\'rotate/'+k.idx+'\')">Rotera '+(k.idx+1)+' ⟳</button>').join(''):'<button onclick="c(\'rotate/0\')">Rotera ⟳</button>';
 if($('rots').innerHTML!==rh)$('rots').innerHTML=rh;
 if($('rcam').options.length!==s.cams.length)$('rcam').innerHTML=s.cams.map(k=>'<option value="'+k.idx+'">Kamera '+(k.idx+1)+'</option>').join('');
 $('asb').textContent='Spara automatiskt: '+(s.autosave?'på':'av');$('asb').classList.toggle('on',s.autosave);
 $('tvoff').classList.toggle('on',s.tvrep);$('fbb').hidden=!s.llm;$('poseb').hidden=!s.pose;
 $('zinfo').textContent=s.zone?'Zonen är aktiv: varje hopp genom den hamnar i listan.':'Rita en zon: ta en repris från kamera 1, välj Zon och tryck två hörn i luften framför svikten, där bara hopparen passerar.';})}
$('lay').onclick=e=>{const b=e.target.closest('button');if(b)c('layout/'+b.dataset.l);};
setInterval(u,500);u();

// ---- Physics from marked frames ----------------------------------------------------------
// Projectile motion of the centre of mass: it drops about the board height from takeoff to entry.
function phys(m,H){
 if(m.takeoff==null||m.water==null)return null;
 const T=m.water-m.takeoff;if(T<=0)return null;
 const v=(G*T*T/2-H)/T,r={T,v,ta:Math.max(v/G,0),rise:v>0?v*v/(2*G):0};
 if(m.apex!=null&&m.apex>m.takeoff)r.riseApex=G*(m.apex-m.takeoff)**2/2;
 if(m.open!=null&&m.open>m.takeoff){const to=m.open-m.takeoff;r.to=to;r.openH=H+v*to-G*to*to/2;}
 return r;}
function physText(m,H){const r=phys(m,H);
 if(!r)return 'Markera minst <b>Upphopp</b> och <b>Vatten</b> (stega bild för bild).';
 let s='Flygtid <b>'+num(r.T,2)+' s</b><br>Högsta punkt <b>'+num(r.rise,2)+' m</b> över upphoppet, efter '+num(r.ta,2)+' s';
 if(r.riseApex!=null)s+='<br>Enligt toppmarkeringen: '+num(r.riseApex,2)+' m';
 if(r.to!=null)s+='<br>Öppning efter '+num(r.to,2)+' s, <b>'+num(r.openH,1)+' m</b> över vattnet';
 return s+'<br><small>±1 bild ≈ ±0,03 s. Räknat på att tyngdpunkten faller lika mycket som svikthöjden.</small>';}

// ---- Drawing tools (line, angle, calibration) -------------------------------------------
let SCALE=parseFloat(store('hd_scale'))||null; // metres per pixel, shared: the camera does not move
const dist=(a,b)=>Math.hypot(a[0]-b[0],a[1]-b[1]);
function annot(cv,redraw,bar,extra){
 const A={tool:null,pts:[],items:[]};
 bar.innerHTML=[['line','Linje'],['angle','Vinkel'],['cal','Kalibrera'],...(extra?[['zone','Zon']]:[]),['clear','Rensa']]
  .map(([k,l])=>'<button data-t="'+k+'">'+l+'</button>').join('');
 bar.onclick=e=>{const b=e.target.closest('button');if(!b)return;const k=b.dataset.t;
  if(k==='clear'){A.items=[];A.pts=[];A.tool=null;}else{A.tool=A.tool===k?null:k;A.pts=[];}
  bar.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x.dataset.t===A.tool));redraw();};
 cv.addEventListener('click',e=>{if(!A.tool)return;const r=cv.getBoundingClientRect();
  A.pts.push([(e.clientX-r.left)*cv.width/r.width,(e.clientY-r.top)*cv.height/r.height]);
  if(A.pts.length===(A.tool==='angle'?3:2)){
   if(A.tool==='zone'){extra(A.pts[0][0]/cv.width,A.pts[0][1]/cv.height,A.pts[1][0]/cv.width,A.pts[1][1]/cv.height);
    A.tool=null;bar.querySelectorAll('button').forEach(x=>x.classList.remove('on'));}
   else if(A.tool==='cal'){const m=parseFloat((prompt('Hur lång är linjen i meter? (t.ex. svikthöjden)','3')||'').replace(',','.'));
    if(m>0){SCALE=m/dist(A.pts[0],A.pts[1]);store('hd_scale',SCALE);}}
   else A.items.push({t:A.tool,p:A.pts});
   A.pts=[];}
  redraw();});
 A.paint=x=>{const lw=Math.max(2,x.canvas.width/350);x.save();x.lineWidth=lw;x.strokeStyle=x.fillStyle='#ffd400';
  x.font='bold '+Math.round(lw*9)+'px sans-serif';x.shadowColor='#000';x.shadowBlur=lw*2;
  const dot=p=>{x.beginPath();x.arc(p[0],p[1],lw*2,0,7);x.fill();};
  const path=ps=>{x.beginPath();ps.forEach((p,i)=>i?x.lineTo(p[0],p[1]):x.moveTo(p[0],p[1]));x.stroke();ps.forEach(dot);};
  for(const it of A.items){path(it.p);
   if(it.t==='line'){const d=dist(it.p[0],it.p[1]);
    x.fillText(SCALE?num(d*SCALE,2)+' m':Math.round(d)+' px (kalibrera)',(it.p[0][0]+it.p[1][0])/2+lw*4,(it.p[0][1]+it.p[1][1])/2-lw*4);}
   else{const[a,b,cc]=it.p;let g=Math.abs(Math.atan2(a[1]-b[1],a[0]-b[0])-Math.atan2(cc[1]-b[1],cc[0]-b[0]))*180/Math.PI;
    if(g>180)g=360-g;x.fillText(Math.round(g)+'°',b[0]+lw*5,b[1]-lw*5);}}
  if(A.pts.length)path(A.pts);x.restore();};
 return A;}

// ---- Replay on the phone: frames fetched as JPEG blobs, decoded only around the current frame
const A1=annot($('cv'),()=>draw(),$('tb1'),(x0,y0,x1,y1)=>{
 if(R.cam!==0){alert('Zonen ritas på kamera 1');return;}
 fetch('/api/zone',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({x0,y0,x1,y1,rot:R.rot})}).then(u);});
function zoneOff(){fetch('/api/zone',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(u);}
const rawToView=(x,y,rot)=>({none:[x,y],clockwise:[1-y,x],'rotate-180':[1-x,1-y],counterclockwise:[y,1-x]}[rot]);
$('board').value=store('hd_board')||'3';
$('board').onchange=()=>{store('hd_board',$('board').value);showPhys();};
function showPhys(){if(R)$('phys').innerHTML=physText(R.marks,+$('board').value);}
function grab(){loadReplay(+$('rcam').value||0,S.tv-$('len').value,S.tv);}
async function loadReplay(cam,t0,t1){
 const r=await(await fetch('/api/range/'+cam+'/'+t0+'/'+t1)).json();
 if(!r.times.length){$('rp').hidden=false;$('info').textContent='Inget inspelat i det intervallet ännu';return;}
 const k={cam,times:r.times,blobs:[],bm:new Map(),rot:S.cams[cam].rot,i:0,playing:false,speed:1,pos:0,marks:{},track:null,pose:null};
 ['stroimg','strotip','trk','fb','pose'].forEach(id=>$(id).hidden=true);
 R=k;$('rp').hidden=false;$('rs').max=k.times.length-1;setSpeed(1);$('pl').textContent='▶︎';showPhys();
 A1.items=[];A1.pts=[];$('mk').querySelectorAll('button').forEach(x=>x.classList.remove('on')); // new dive: fresh marks and drawings
 let next=0,done=0;
 const worker=async()=>{while(next<k.times.length&&R===k){const n=next++;
  k.blobs[n]=await(await fetch('/frame/'+cam+'/'+k.times[n])).blob();done++;
  if(n===0)draw();if(R===k&&!k.playing)$('info').textContent='Laddar '+done+'/'+k.times.length;}};
 await Promise.all([1,2,3,4,5,6].map(worker));if(R===k)draw();}
const rel=n=>R.times[n]-R.times[0];
async function draw(){const k=R,n=k&&k.i;if(!k||!k.blobs[n])return;
 let bm=k.bm.get(n);if(!bm){bm=await createImageBitmap(k.blobs[n]);k.bm.set(n,bm);
  if(k.bm.size>40){const[o,v]=k.bm.entries().next().value;v.close();k.bm.delete(o);}}
 if(k!==R||n!==k.i)return;
 const cv=$('cv'),q=k.rot==='clockwise'||k.rot==='counterclockwise',w=q?bm.height:bm.width,hh=q?bm.width:bm.height;
 if(cv.width!==w||cv.height!==hh){cv.width=w;cv.height=hh;}
 const x=cv.getContext('2d');x.save();x.translate(w/2,hh/2);x.rotate(ROT[k.rot]);
 x.drawImage(bm,-bm.width/2,-bm.height/2);x.restore();
 if(k.cam===0&&S.zone){const a=rawToView(S.zone[0],S.zone[1],k.rot),b=rawToView(S.zone[2],S.zone[3],k.rot);
  x.save();x.strokeStyle='#3fb950';x.lineWidth=Math.max(2,w/400);x.setLineDash([12,8]);
  x.strokeRect(Math.min(a[0],b[0])*w,Math.min(a[1],b[1])*hh,Math.abs(a[0]-b[0])*w,Math.abs(a[1]-b[1])*hh);x.restore();}
 if(k.track&&k.track.length){const tr=k.track,lw=Math.max(2,w/350),now=k.times[n];x.save();x.strokeStyle='#3fb950';x.fillStyle='#3fb950';x.lineWidth=lw;
  x.beginPath();tr.forEach((p,j)=>j?x.lineTo(p.x,p.y):x.moveTo(p.x,p.y));x.stroke();
  const ap=tr.reduce((a,b)=>b.y<a.y?b:a);x.beginPath();x.arc(ap.x,ap.y,lw*4,0,7);x.stroke();
  const cur=tr.reduce((a,b)=>Math.abs(b.t-now)<Math.abs(a.t-now)?b:a);
  if(Math.abs(cur.t-now)<0.05){x.beginPath();x.arc(cur.x,cur.y,lw*3,0,7);x.fill();
   const r=lw*30,ang=cur.angle*Math.PI/180;x.beginPath();x.moveTo(cur.x-r*Math.cos(ang),cur.y-r*Math.sin(ang));x.lineTo(cur.x+r*Math.cos(ang),cur.y+r*Math.sin(ang));x.stroke();}
  x.restore();}
 if(k.pose){const now=k.times[n],f=k.pose.reduce((a,b)=>Math.abs(b.t-now)<Math.abs(a.t-now)?b:a);
  if(Math.abs(f.t-now)<0.02){const lw=Math.max(2,w/300);x.save();x.strokeStyle=x.fillStyle='#ff7b72';x.lineWidth=lw;
   for(const[a,b]of EDGES){const p=f.kp[a],q=f.kp[b];if(p[2]>0.3&&q[2]>0.3){x.beginPath();x.moveTo(p[0],p[1]);x.lineTo(q[0],q[1]);x.stroke();}}
   f.kp.forEach(p=>{if(p[2]>0.3){x.beginPath();x.arc(p[0],p[1],lw*1.5,0,7);x.fill();}});
   if(f.hip!=null){const hp=f.kp[11][2]>f.kp[12][2]?f.kp[11]:f.kp[12];x.font='bold '+Math.round(w/40)+'px sans-serif';
    x.shadowColor='#000';x.shadowBlur=lw*2;x.fillText(Math.round(f.hip)+'°',hp[0]+lw*5,hp[1]);}
   x.restore();}}
 A1.paint(x);
 const here=Object.keys(k.marks).filter(m=>Math.abs(k.marks[m]-rel(n))<1e-6)
  .map(m=>({takeoff:'Upphopp',apex:'Topp',open:'Öppning',water:'Vatten'}[m]));
 $('rs').value=n;$('info').textContent='Bild '+(n+1)+'/'+k.times.length+' · '+num(rel(n),2)+' s · '+k.speed+'×'+(here.length?' · '+here.join(', '):'');}
$('mk').onclick=e=>{const b=e.target.closest('button');if(!b||!R)return;const m=b.dataset.m;
 if(m==='clear')R.marks={};else R.marks[m]=rel(R.i);
 $('mk').querySelectorAll('button').forEach(x=>x.classList.toggle('on',R.marks[x.dataset.m]!=null));showPhys();draw();};
function idx(t){let lo=0,hi=R.times.length-1;while(lo<hi){const m=(lo+hi+1)>>1;if(R.times[m]<=t)lo=m;else hi=m-1;}return lo;}
function seekTo(n){R.i=Math.min(Math.max(n,0),R.times.length-1);R.pos=rel(R.i);draw();}
function play(){if(!R)return;R.playing=!R.playing;if(R.playing&&R.i>=R.times.length-1)seekTo(0);$('pl').textContent=R.playing?'⏸':'▶︎';}
function stepf(dn){if(!R)return;R.playing=false;$('pl').textContent='▶︎';seekTo(R.i+dn);}
$('rs').oninput=()=>{R.playing=false;$('pl').textContent='▶︎';seekTo(+$('rs').value);};
function speedButtons(bar,set){bar.querySelectorAll('button').forEach(b=>b.onclick=()=>set(+b.dataset.s));}
function markSpeed(bar,v){bar.querySelectorAll('button').forEach(b=>b.classList.toggle('on',+b.dataset.s===v));}
function setSpeed(v){if(R)R.speed=v;markSpeed($('sp'),v);if(R)draw();}
speedButtons($('sp'),setSpeed);
async function save(){if(!R)return;
 const name=[$('diver').value,$('dive').value,$('board').value+'m'].map(s=>s.trim()).filter(Boolean).join(' ');
 await fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
  cam:R.cam,t0:R.times[0],t1:R.times[R.times.length-1],rot:R.rot,name,meta:{board:+$('board').value,marks:R.marks}})});
 clips();}

// ---- TV replay, StroMotion, automatic analysis, AI feedback --------------------------------
const post=(url,obj)=>fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)});
function diveRange(pad){ // marked takeoff..water with some margin, otherwise the whole replay
 const m=R.marks,a=R.times[0],b=R.times[R.times.length-1];
 return m.takeoff!=null&&m.water!=null?[Math.max(a,a+m.takeoff-pad),Math.min(b,a+m.water+pad)]:[a,b];}
function tvShow(){if(!R)return;const[t0,t1]=diveRange(0.7);post('/api/tvreplay',{cam:R.cam,t0,t1,speed:R.speed}).then(u);}
function tvHide(){post('/api/tvreplay',{}).then(u);}
function stro(){if(!R)return;const[t0,t1]=diveRange(0.1),img=$('stroimg');
 $('strotip').hidden=false;$('strotip').textContent='Beräknar…';img.hidden=true;
 img.onload=()=>{img.hidden=false;$('strotip').textContent='Håll fingret på bilden → Spara i Bilder';};
 img.onerror=()=>{$('strotip').textContent='Hittade ingen rörelse i reprisen.';};
 img.src='/api/stro?cam='+R.cam+'&t0='+t0+'&t1='+t1+'&rot='+R.rot+'&every='+$('every').value+'&_='+Date.now();}
async function analyse(){if(!R)return;const k=R,[t0,t1]=diveRange(0.2);$('trk').hidden=false;$('trk').textContent='Analyserar…';
 const pts=await(await fetch('/api/track?cam='+k.cam+'&t0='+t0+'&t1='+t1+'&rot='+k.rot)).json();
 if(k!==R)return;k.track=pts;draw();$('trk').innerHTML=trackText(pts,k);}
function trackText(pts,k){
 if(pts.length<5)return 'Hittade för lite rörelse. Markera Upphopp och Vatten och försök igen.';
 const near=t=>pts.reduce((a,b)=>Math.abs(b.t-t)<Math.abs(a.t-t)?b:a);
 const tk=k.marks.takeoff!=null?k.times[0]+k.marks.takeoff:null,tw=k.marks.water!=null?k.times[0]+k.marks.water:null;
 const first=tk!=null?(pts.find(p=>p.t>=tk-0.001)||near(tk)):pts[0]; // first frame in the air
 const lastp=tw!=null?([...pts].reverse().find(p=>p.t<=tw+0.001)||near(tw)):pts[pts.length-1];
 const apex=pts.reduce((a,b)=>b.y<a.y?b:a),m=d=>SCALE?num(d*SCALE,2)+' m':Math.round(d)+' px';
 let s='Högsta punkt <b>'+m(first.y-apex.y)+'</b> över upphoppet, '+m(Math.abs(apex.x-first.x))+' ut från upphoppet'
  +'<br>Vattenkontakt '+m(Math.abs(lastp.x-first.x))+' ut från upphoppet';
 // Somersaults: the body axis turns; the axis has no head/feet so it repeats every 180°.
 const lo=k.marks.takeoff!=null?first.t:-1e9,hi=k.marks.water!=null?lastp.t:1e9;
 const ok=pts.filter(p=>p.elong>1.6&&p.t>=lo&&p.t<=hi); // only while in the air
 if(ok.length>=5){let acc=0;for(let j=1;j<ok.length;j++){let d=ok[j].angle-ok[j-1].angle;d=((d+90)%180+180)%180-90;acc+=d;}
  const revs=Math.abs(acc)/360,secs=ok[ok.length-1].t-ok[0].t;
  s+='<br>Rotation ≈ <b>'+num(revs,1)+' varv</b>'+(secs>0?', '+num(revs/secs,1)+' varv/s':'');
  if(ok.length<pts.length*0.6)s+='<br><small>Osäker rotation: kroppen var ihopkrupen i många bilder.</small>';}
 else s+='<br><small>Rotation: kan inte mätas (kroppen syns inte som avlång).</small>';
 if(!SCALE)s+='<br><small>Kalibrera för att få meter.</small>';
 return s;}
// AI body pose: skeleton per frame, hip and knee angles, opening, rotation of the trunk, entry line
const EDGES=[[5,7],[7,9],[6,8],[8,10],[5,6],[5,11],[6,12],[11,12],[11,13],[13,15],[12,14],[14,16],[0,5],[0,6]];
async function poseAnalyse(){if(!R)return;const k=R,[t0,t1]=diveRange(0.1);$('pose').hidden=false;
 $('pose').textContent='AI:n analyserar kroppen… (några sekunder)';
 const r=await fetch('/api/pose?cam='+k.cam+'&t0='+t0+'&t1='+t1+'&rot='+k.rot);if(k!==R)return;
 if(!r.ok){$('pose').textContent='AI-modellen är inte installerad (se README).';return;}
 k.pose=await r.json();draw();$('pose').innerHTML=poseText(k.pose,k);}
function poseText(fr,k){
 const tk=k.marks.takeoff!=null?k.times[0]+k.marks.takeoff:null,tw=k.marks.water!=null?k.times[0]+k.marks.water:null;
 const air=fr.filter(f=>f.score>0.35&&(tk==null||f.t>=tk-0.001)&&(tw==null||f.t<=tw+0.001));
 if(air.length<5)return 'AI:n såg inte hopparen tydligt nog. Markera Upphopp och Vatten och försök igen.';
 const start=tk!=null?tk:air[0].t,rel=t=>num(t-start,2)+' s';let s='';
 // median of three frames in a row, so one wrong frame from the model is not reported as a result
 const med=(key,j)=>{const v=[air[j-1],air[j],air[j+1]].filter(Boolean).map(x=>x[key]).filter(v=>v!=null).sort((a,b)=>a-b);
  return v.length>1?v[Math.floor(v.length/2)]:null;};
 air.forEach((f,j)=>{f.hipS=med('hip',j);f.kneeS=med('knee',j);f.lineS=med('line',j);});
 const hips=air.filter(f=>f.hipS!=null).map(f=>({...f,hip:f.hipS}));
 if(hips.length){const tight=hips.reduce((a,b)=>b.hip<a.hip?b:a);
  s+='Tätaste höftvinkel <b>'+Math.round(tight.hip)+'°</b> efter '+rel(tight.t);
  const open=hips.find(f=>f.t>tight.t&&f.hip>150);
  if(open)s+='<br>Öppnar (höft över 150°) efter <b>'+rel(open.t)+'</b> <button onclick="useOpen('+open.t+')" style="padding:6px 10px;font-size:14px">Använd som Öppning</button>';}
 const knees=air.filter(f=>f.kneeS!=null).map(f=>({...f,knee:f.kneeS}));
 if(knees.length){const bent=knees.reduce((a,b)=>b.knee<a.knee?b:a);s+='<br>Mest böjda knä <b>'+Math.round(bent.knee)+'°</b> efter '+rel(bent.t);}
 const tr=air.filter(f=>f.trunk!=null); // the trunk has a head end, so it counts full turns even in tuck
 if(tr.length>=5){let acc=0;for(let j=1;j<tr.length;j++){let d=tr[j].trunk-tr[j-1].trunk;d=((d+180)%360+360)%360-180;acc+=d;}
  const revs=Math.abs(acc)/360,secs=tr[tr.length-1].t-tr[0].t;
  s+='<br>Rotation (bålen) ≈ <b>'+num(revs,1)+' varv</b>'+(secs>0?', '+num(revs/secs,1)+' varv/s':'');}
 // entry: shoulder-ankle line, or the trunk if the ankles are unsure; only frames right at the water mark
 const entry=tw==null?[]:air.filter(f=>Math.abs(f.t-tw)<0.07&&(f.lineS!=null||f.trunk!=null));
 if(entry.length){const e=entry.reduce((a,b)=>Math.abs(b.t-tw)<Math.abs(a.t-tw)?b:a),ang=e.lineS!=null?e.lineS:e.trunk;
  const dev=Math.abs(((ang-90)%360+540)%360-180);
  s+='<br>Kroppslinje vid vattnet: <b>'+Math.round(Math.min(dev,180-dev))+'°</b> från lodrätt'+(e.lineS!=null?'':' (bålen)');}
 return s+'<br><small>AI-uppskattning från '+air.length+' bilder. Stäm av mot videon.</small>';}
function useOpen(t){if(!R)return;R.marks.open=t-R.times[0];
 $('mk').querySelectorAll('button').forEach(x=>x.classList.toggle('on',R.marks[x.dataset.m]!=null));showPhys();draw();}
async function feedback(){if(!R)return;$('fb').hidden=false;$('fb').textContent='Skriver…';
 const plain=h=>h.replace(/<br>/g,'\n').replace(/<[^>]+>/g,'');
 const text='Hoppare: '+($('diver').value||'-')+'\nHopp: '+($('dive').value||'-')+'\nHöjd: '+$('board').value+' m\n'
  +plain($('phys').innerHTML)+'\n'+($('trk').hidden?'':plain($('trk').innerHTML))+'\n'+($('pose').hidden?'':plain($('pose').innerHTML));
 const r=await post('/api/feedback',{text});$('fb').textContent=r.ok?(await r.json()).text:'Ingen språkmodell konfigurerad.';}

// ---- Dives found automatically in the zone ------------------------------------------------
async function evs(){const l=await(await fetch('/api/events')).json();
 $('ev').innerHTML=l.length?l.map((e,j)=>'<div class="ev"><span>'+e.wall+' · '+num(e.t1-e.t0,0)+' s</span><button data-j="'+j+'">Visa</button></div>').join(''):'';
 $('ev').onclick=ev=>{const b=ev.target.closest('button');if(!b)return;const e=l[+b.dataset.j];
  loadReplay(0,e.t0,e.t1);$('rp').scrollIntoView({behavior:'smooth'});};}
setInterval(evs,3000);evs();

// ---- Compare two saved clips: side by side or overlaid, aligned on the takeoff mark ------
const C={A:null,B:null,ma:0,mb:0,p:0,shift:0,playing:false,speed:1,mode:'side',busy:false,dirty:false};
const A2=annot($('cc'),()=>drawC(),$('tb2'));
function cMode(){C.mode=C.mode==='side'?'overlay':'side';$('cmode').textContent=C.mode==='side'?'Sida vid sida':'Överlägg';if(C.A)cRender();}
async function cLoad(){const a=$('ca').value,b=$('cb').value;if(!a||!b)return;$('cp').hidden=false;$('cinfo').textContent='Laddar…';
 const mk=n=>{const v=document.createElement('video');v.muted=true;v.playsInline=true;v.setAttribute('playsinline','');
  v.preload='auto';v.src='/clips/'+enc(n);return v;};
 const A=mk(a),B=mk(b);
 [A,B].forEach(v=>{const p=v.play();if(p)p.then(()=>v.pause()).catch(()=>{});}); // iOS decodes frames only after play() in a tap
 await Promise.all([A,B].map(v=>new Promise(res=>{if(v.readyState>=2)return res();v.addEventListener('loadeddata',res,{once:true});})));
 A.pause();B.pause();
 const mark=n=>((CL[n]||{}).meta||{}).marks||{};
 Object.assign(C,{A,B,ma:mark(a).takeoff||0,mb:mark(b).takeoff||0,shift:0,playing:false,names:[a,b]});
 C.p=-Math.max(C.ma,C.mb);$('cpl').textContent='▶︎';cRender();}
const cRange=()=>[-Math.max(C.ma,C.mb),Math.max(C.A.duration-C.ma,C.B.duration-C.mb)];
const clampT=(v,t)=>Math.min(Math.max(t,0),Math.max(v.duration-0.001,0));
function seek(v,t){return new Promise(res=>{if(Math.abs(v.currentTime-t)<0.0005)return res();v.addEventListener('seeked',res,{once:true});v.currentTime=t;});}
async function cRender(){if(!C.A)return;if(C.busy){C.dirty=true;return;}C.busy=true;
 const mid=0.5/30; // aim at the middle of a frame, not its edge
 do{C.dirty=false;await Promise.all([seek(C.A,clampT(C.A,C.ma+C.p+mid)),seek(C.B,clampT(C.B,C.mb+C.p+C.shift/30+mid))]);drawC();}while(C.dirty);
 C.busy=false;}
function drawC(){if(!C.A)return;const{A,B}=C,w=A.videoWidth,h=A.videoHeight,side=C.mode==='side',cc=$('cc');
 const W=side?w*2:w;if(cc.width!==W||cc.height!==h){cc.width=W;cc.height=h;}
 const x=cc.getContext('2d');x.fillStyle='#000';x.fillRect(0,0,W,h);
 const fit=(v,x0,al)=>{const s=Math.min(w/v.videoWidth,h/v.videoHeight),vw=v.videoWidth*s,vh=v.videoHeight*s;
  x.globalAlpha=al;x.drawImage(v,x0+(w-vw)/2,(h-vh)/2,vw,vh);};
 fit(A,0,1);fit(B,side?w:0,side?1:0.5);x.globalAlpha=1;
 x.font='bold '+Math.round(w/25)+'px sans-serif';x.fillStyle='#ffd400';x.fillText('A',w/40,w/20);x.fillText('B',(side?w:w/12)+w/40,w/20);
 A2.paint(x);
 const info=n=>{const m=(CL[n]||{}).meta;const r=m&&phys(m.marks||{},m.board||3);return r?'flygtid '+num(r.T,2)+' s, topp '+num(r.rise,2)+' m':'ej mätt';};
 $('cinfo').innerHTML=(C.p>=0?'+':'')+num(C.p,2)+' s från upphopp · B '+(C.shift>=0?'+':'')+C.shift+' bild · '+C.speed+'×'
  +'<br>A: '+esc(C.names[0])+' – '+info(C.names[0])+'<br>B: '+esc(C.names[1])+' – '+info(C.names[1]);}
function cStep(d){if(!C.A)return;C.playing=false;$('cpl').textContent='▶︎';C.p+=d/30;cRender();}
function cShift(d){if(!C.A)return;C.shift+=d;cRender();}
function cPlay(){if(!C.A)return;C.playing=!C.playing;$('cpl').textContent=C.playing?'⏸':'▶︎';}
speedButtons($('csp'),v=>{C.speed=v;markSpeed($('csp'),v);if(C.A)drawC();});

function tick(ts){const dt=(ts-lastT)/1000;lastT=ts;
 if(R&&R.playing){R.pos+=dt*R.speed;if(R.pos>rel(R.times.length-1))R.pos=0;
  const n=idx(R.times[0]+R.pos);if(n!==R.i){R.i=n;draw();}}
 if(C.A&&C.playing){const[lo,hi]=cRange();C.p+=dt*C.speed;if(C.p>hi)C.p=lo;cRender();}
 requestAnimationFrame(tick);}
requestAnimationFrame(tick);

// ---- Saved clips: filter, share to Photos, rename, delete ---------------------------------
// Sharing a file (-> "Spara video" to Photos) needs HTTPS and a file fetched before the tap,
// so the first tap downloads (⬇︎), the second opens the share sheet (📲).
let CL={},LIST=[];const files={},loading=new Set();
const share=x=>!window.isSecureContext?'':'<button data-n="'+esc(x.name)+'" data-a="share"'
 +(files[x.name]?' class="on">📲':'>'+(loading.has(x.name)?'…':'⬇︎'))+'</button>';
function renderClips(){const q=$('cf').value.trim().toLowerCase();
 const l=LIST.filter(x=>!q||x.name.toLowerCase().includes(q));
 $('cl').innerHTML=l.length?l.map(x=>x.ready
  ?'<div class="clip"><a href="/clips/'+enc(x.name)+'">'+esc(x.name.replace(/[.]mp4$/,''))+'</a><span>'+x.mb+' MB</span>'+share(x)
   +'<button data-n="'+esc(x.name)+'" data-a="rename">✎</button><button data-n="'+esc(x.name)+'" data-a="delete">🗑</button></div>'
  :'<div class="clip">'+esc(x.name)+' · sparas…</div>').join(''):(LIST.length?'Inga träffar':'Inga än');
 for(const id of ['ca','cb']){const s=$(id),v=s.value,ready=LIST.filter(x=>x.ready);
  s.innerHTML='<option value="">'+(id==='ca'?'Klipp A':'Klipp B')+'</option>'+ready.map(x=>'<option value="'+esc(x.name)+'">'+esc(x.name.replace(/[.]mp4$/,''))+'</option>').join('');
  if(ready.some(x=>x.name===v))s.value=v;}}
async function clips(){LIST=await(await fetch('/api/clips')).json();CL={};LIST.forEach(x=>CL[x.name]=x);renderClips();}
$('cf').oninput=renderClips;
$('cl').onclick=async e=>{const b=e.target.closest('button');if(!b)return;const n=b.dataset.n;
 if(b.dataset.a==='share'){
  if(files[n]){try{await navigator.share({files:[files[n]]});}catch(err){}return;}
  if(loading.has(n))return;loading.add(n);renderClips();
  const bl=await(await fetch('/clips/'+enc(n))).blob();files[n]=new File([bl],n,{type:'video/mp4'});
  loading.delete(n);return renderClips();}
 delete files[n];
 if(b.dataset.a==='delete'){if(!confirm('Radera '+n+'?'))return;await fetch('/api/clip/delete/'+enc(n),{method:'POST'});}
 else{const v=prompt('Nytt namn',n.replace(/[.]mp4$/,''));if(!v)return;
  const r=await fetch('/api/clip/rename/'+enc(n)+'/'+enc(v),{method:'POST'});if(r.status===409)alert('Namnet är upptaget eller ogiltigt');}
 clips();};
setInterval(clips,3000);clips();
if(!window.isSecureContext){$('tls').hidden=false;const l=$('https');l.href='https://'+(location.hostname==='10.42.0.1'?'10.42.0.1':'hoppdelay.local')+'/';l.textContent=l.href;}
</script></body></html>"""


class Web(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        parts = [unquote(x) for x in url.path.strip("/").split("/")]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if parts == ["api", "state"]:
                return self.reply(200, json.dumps(status).encode(), "application/json")
            if parts == ["api", "events"]:
                with detector.lock:
                    return self.reply(200, json.dumps(detector.events[::-1]).encode(), "application/json")
            if parts[:2] == ["api", "range"] and len(parts) == 5:
                ts, _ = cam_of(parts[2]).between(float(parts[3]), float(parts[4]))
                return self.reply(200, json.dumps({"times": ts}).encode(), "application/json")
            if parts[0] == "frame" and len(parts) == 3:
                cam = cam_of(parts[1])
                hit = cam.at(float(parts[2]))
                return self.reply(200, cam.read(hit[1]), "image/jpeg") if hit else self.reply(404, b"", "text/plain")
            if parts == ["api", "stro"]:
                img = stromotion(cam_of(q["cam"]), float(q["t0"]), float(q["t1"]), rot_param(q), max(1, int(q.get("every", 5))))
                return self.reply(200, img, "image/jpeg") if img else self.reply(404, b"", "text/plain")
            if parts == ["api", "pose"]:
                if not pose_available():
                    return self.reply(404, b"", "text/plain")
                frames = body_pose(cam_of(q["cam"]), float(q["t0"]), float(q["t1"]), rot_param(q))
                return self.reply(200, json.dumps(frames).encode(), "application/json")
            if parts == ["api", "track"]:
                pts = track(cam_of(q["cam"]), float(q["t0"]), float(q["t1"]), rot_param(q))
                return self.reply(200, json.dumps(pts).encode(), "application/json")
            if parts == ["ca.crt"]:
                return self.reply(200, (CERTS / "ca.crt").read_bytes(), "application/x-x509-ca-cert")
            if parts == ["api", "clips"]:
                files = sorted((f for f in CLIPS.iterdir() if f.suffix != ".json"), key=lambda f: f.stat().st_mtime, reverse=True)
                clips = [{"name": f.name.removesuffix(".part"), "mb": round(f.stat().st_size / 1e6, 1),
                          "ready": f.suffix == ".mp4",
                          "meta": json.loads(meta_file(f).read_text()) if meta_file(f).exists() else None} for f in files]
                return self.reply(200, json.dumps(clips).encode(), "application/json")
            if parts[0] == "clips" and len(parts) == 2 and clip_file(parts[1]):
                return self.send_file(clip_file(parts[1]), "video/mp4")
        except (ValueError, KeyError, IndexError, FileNotFoundError):
            return self.reply(404, b"", "text/plain")
        self.reply(200, PAGE.encode(), "text/html; charset=utf-8")

    def do_POST(self):
        global tv_replay
        parts = [unquote(x) for x in self.path.strip("/").split("/")]
        try:
            if parts[:3] == ["api", "clip", "delete"] and len(parts) == 4 and clip_file(parts[3]):
                clip = clip_file(parts[3])
                clip.unlink()
                meta_file(clip).unlink(missing_ok=True)
                return self.reply(204, b"", "text/plain")
            if parts[:3] == ["api", "clip", "rename"] and len(parts) == 5 and clip_file(parts[3]):
                new = clean_name(parts[4])
                if not new or (CLIPS / new).exists():
                    return self.reply(409, b"", "text/plain")
                clip = clip_file(parts[3])
                if meta_file(clip).exists():
                    meta_file(clip).rename(meta_file(CLIPS / new))
                clip.rename(CLIPS / new)
                return self.reply(204, b"", "text/plain")
            if parts == ["api", "save"]:
                body = self.body()
                cam, t0, t1, rot = cam_of(body["cam"]), float(body["t0"]), float(body["t1"]), body["rot"]
                if not 0 < t1 - t0 <= MAX_CLIP_S or rot not in ROTATIONS:
                    return self.reply(400, b"", "text/plain")
                base = clean_name(str(body.get("name", ""))).removesuffix(".mp4") or "hopp"
                meta = {**clean_meta(body.get("meta") or {}), "cam": cam.idx}
                name = new_clip(cam, t0, t1, rot, base, meta)
                return self.reply(202, json.dumps({"name": name}).encode(), "application/json")
            if parts == ["api", "zone"]:
                body = self.body()
                if body:  # corners on the rotated picture -> camera picture
                    rot = body["rot"] if body.get("rot") in ROTATIONS else "none"
                    a = view_to_raw(float(body["x0"]), float(body["y0"]), rot)
                    b = view_to_raw(float(body["x1"]), float(body["y1"]), rot)
                    zone = [min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])]
                    state["zone"] = [round(min(max(v, 0.0), 1.0), 4) for v in zone]
                else:
                    state["zone"] = None
                save_state()
                return self.reply(204, b"", "text/plain")
            if parts == ["api", "tvreplay"]:
                body = self.body()
                if body:
                    t0, t1 = float(body["t0"]), float(body["t1"])
                    if not 0 < t1 - t0 <= MAX_CLIP_S:
                        return self.reply(400, b"", "text/plain")
                    tv_replay = {"cam": cam_of(body["cam"]), "t0": t0, "t1": t1,
                                 "speed": min(max(float(body.get("speed", 1)), 0.05), 2.0), "start": time.monotonic()}
                else:
                    tv_replay = None
                return self.reply(204, b"", "text/plain")
            if parts == ["api", "feedback"]:
                if not LLM_URL:
                    return self.reply(404, b"", "text/plain")
                try:
                    text = llm_feedback(str(self.body()["text"])[:4000])
                except OSError as e:
                    text = f"Kunde inte nå språkmodellen ({e})."
                return self.reply(200, json.dumps({"text": text}).encode(), "application/json")
            cmd = parts[1] if parts[0] == "api" and len(parts) in (2, 3) else ""
            v = float(parts[2]) if len(parts) == 3 else 0.0
        except (ValueError, IndexError, KeyError, TypeError, AttributeError):
            return self.reply(400, b"", "text/plain")
        if cmd not in COMMANDS:
            return self.reply(404, b"", "text/plain")
        cmds.put((cmd, v))
        self.reply(204, b"", "text/plain")

    def body(self):
        return json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 65536)) or b"{}")

    def send_file(self, path, ctype):
        # Byte ranges are required for video playback in iPhone Safari.
        size = path.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].partition("-")
            start, end = (int(a), int(b) if b else size - 1) if a else (max(size - int(b), 0), size - 1)
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            self.wfile.write(f.read(end - start + 1))

    def reply(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def cam_of(i):
    i = int(i)
    if not 0 <= i < len(cams):
        raise ValueError("no such camera")
    return cams[i]


def rot_param(q):
    return q["rot"] if q.get("rot") in ROTATIONS else "none"


def ensure_certs():
    # Own CA (installed once on the phone) signing a server cert for hoppdelay.local and the hotspot IP.
    if (CERTS / "server.crt").exists():
        return
    CERTS.mkdir(exist_ok=True)
    (CERTS / "ext.cnf").write_text("subjectAltName=DNS:hoppdelay.local,IP:10.42.0.1\nextendedKeyUsage=serverAuth\n"
                                   "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n")
    for cmd in (
        "req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -days 3650 -subj /CN=Hoppdelay-CA "
        "-addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign",
        "req -newkey rsa:2048 -nodes -keyout server.key -out server.csr -subj /CN=hoppdelay.local",
        # iOS rejects server certs valid for more than 825 days
        "x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -days 820 -extfile ext.cnf",
    ):
        subprocess.run(["openssl", *cmd.split()], cwd=CERTS, check=True, capture_output=True)


# --- Start -----------------------------------------------------------------------------------
CLIPS.mkdir(parents=True, exist_ok=True)  # saved clips are kept across restarts
for f in CLIPS.glob("*.part"):  # unfinished saves
    f.unlink()
found = find_cameras()
if not found:
    raise SystemExit("No camera with MJPEG up to 1080p30 found")
cams = [Camera(i, dev, w, h, MAX_DISK // len(found)) for i, (dev, w, h) in enumerate(found)]
detector = Detector(cams[0])
detector.start()
for c in cams:
    c.start()
cmds = queue.Queue()  # (command, value) from keyboard and web, applied in the main loop
status = {}  # snapshot for the web page, replaced every loop
tv_replay = None  # replay looping in a corner of the TV: {"cam", "t0", "t1", "speed", "start"}

ensure_certs()
tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
tls.load_cert_chain(CERTS / "server.crt", CERTS / "server.key")
https = http.server.ThreadingHTTPServer(("", 443), Web)
https.socket = tls.wrap_socket(https.socket, server_side=True, do_handshake_on_connect=False)  # handshake in the request thread
for server in (http.server.ThreadingHTTPServer(("", 80), Web), https):
    threading.Thread(target=server.serve_forever, daemon=True).start()

# --- Main loop -------------------------------------------------------------------------------
screen = screen_size()
disp, src, txt, disp_key = None, None, None, None
start = last = time.monotonic()
review, paused, behind = False, False, 0.0  # review: rewound/paused, showing `behind` seconds back
shown_key = None
kbds, next_scan = {}, 0.0

while True:
    now = time.monotonic()
    dt, last = now - last, now
    if now >= next_scan:
        scan(kbds)
        next_scan = now + 5
        if screen_size() != screen:  # another screen plugged in
            screen, disp_key = screen_size(), None
    span, _ = cams[0].span(now)
    for c in cams:
        newest = c.span(now)[1]
        if now - (newest if newest is not None else start) > 5:
            raise SystemExit(f"No frames from camera {c.idx + 1} for 5 s")  # systemd restarts us
    for p in [c.pipe for c in cams] + ([disp] if disp else []):
        msg = p.get_bus().pop_filtered(Gst.MessageType.ERROR)
        if msg:
            err, dbg = msg.parse_error()
            raise SystemExit(f"GStreamer error: {err.message} ({dbg})")

    ready, _, _ = select.select(list(kbds.values()), [], [], 1 / FPS)
    for dev in ready:
        try:
            for ev in dev.read():
                if ev.type == E.EV_KEY and ev.value == 1 and ev.code in KEYMAP:
                    cmds.put(KEYMAP[ev.code])
        except OSError:  # keyboard unplugged
            kbds.pop(dev.path, None)

    while not cmds.empty():
        cmd, v = cmds.get_nowait()
        if cmd == "delay_by":
            state["delay"] = max(1, int(state["delay"] + v))
            review = paused = False
        elif cmd == "live":
            review = paused = False
        elif cmd == "rotate":
            i = int(v) if 0 <= int(v) < len(cams) else 0
            state["rots"][str(i)] = ROTATIONS[(ROTATIONS.index(rot_of(i)) + 1) % len(ROTATIONS)]
        elif cmd == "layout" and 0 <= int(v) < len(LAYOUTS):
            state["layout"] = LAYOUTS[int(v)]
        elif cmd == "layout_next":
            state["layout"] = LAYOUTS[(LAYOUTS.index(state["layout"]) + 1) % len(LAYOUTS)]
        elif cmd == "autosave":
            state["autosave"] = bool(v)
        elif cmd in ("step", "seek", "pause"):  # enter review mode
            if not review:
                review, behind = True, float(state["delay"])
            if cmd == "step":
                behind = min(max(behind - v, 0.0), span)
            elif cmd == "seek":
                behind = min(max(v, 0.0), span)
            else:
                paused = not paused
        save_state()

    if paused:
        behind = min(behind + dt, span)  # frozen frame; stays inside the recording
    back = behind if review else state["delay"]
    layout = state["layout"] if len(cams) == 2 else "cam0"
    visible = {"cam0": [0], "cam1": [1], "split": [0, 1], "pip": [0, 1]}[layout]
    main = visible[0]
    tvr = tv_replay
    mode = "jpeg" if len(visible) == 1 and not tvr else "raw"
    want = (mode, main, rot_of(main), screen) if mode == "jpeg" else (mode, screen)
    if want != disp_key:
        if disp:
            disp.set_state(Gst.State.NULL)
        disp, src, txt = display_jpeg(cams[main], rot_of(main), *screen) if mode == "jpeg" else display_raw(*screen)
        disp_key, shown_key = want, None

    hits = {i: cams[i].at(now - back) for i in visible}
    if any(h is None for h in hits.values()):
        continue
    tv_hit = None
    if tvr:
        dur = tvr["t1"] - tvr["t0"]
        tv_hit = tvr["cam"].at(tvr["t0"] + ((now - tvr["start"]) * tvr["speed"]) % dur)
    tv = hits[main][0]
    status = {"delay": state["delay"], "review": review, "paused": paused, "behind": back, "span": span, "tv": tv,
              "layout": layout, "zone": state["zone"], "autosave": state["autosave"], "llm": bool(LLM_URL),
              "tvrep": bool(tvr), "pose": pose_available(), "cams": [{"idx": c.idx, "w": c.w, "h": c.h, "rot": rot_of(c.idx)} for c in cams]}

    if span < back and not review:
        text = f"Buffrar {span:.0f}/{back} s"
    elif review:
        text = f"{'Paus  ' if paused else ''}-{mmss(back)}   (Enter = tillbaka till {state['delay']} s)"
    else:
        text = f"Delay {state['delay']} s   inspelat {mmss(span)}"
    key = (tuple(h[1] for h in hits.values()), tv_hit[1] if tv_hit else None, text)
    if key == shown_key:
        continue
    try:
        jpegs = {i: cams[i].read(h[1]) for i, h in hits.items()}
        tv_frame = (tvr["cam"], tvr["cam"].read(tv_hit[1])) if tv_hit else None
    except FileNotFoundError:  # that minute was just deleted to free disk
        continue
    if mode == "jpeg":
        txt.set_property("text", text)
        src.emit("push-buffer", Gst.Buffer.new_wrapped(jpegs[main]))
    else:
        canvas = compose(screen[0], screen[1], layout, {i: (cams[i], j) for i, j in jpegs.items()}, tv_frame, text)
        src.emit("push-buffer", Gst.Buffer.new_wrapped(canvas.tobytes()))
    shown_key = key
