#!/usr/bin/env python3
# Delayed video playback for diving practice.
# Records to disk (whole session), plays back delayed on HDMI, controlled from a
# keyboard/clicker or a phone (web page on port 80).
# Keys:
#   Up / Down           delay +5 s / -5 s
#   Left / PageUp       go back 5 s
#   Right / PageDown    go forward 5 s
#   Space / B           pause / play
#   Enter / Esc         back to normal delay
#   R                   rotate image 90 degrees
import bisect
import http.server
import json
import pathlib
import queue
import select
import shutil
import ssl
import subprocess
import threading
import time
from urllib.parse import unquote

import evdev
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

CAM = "/dev/v4l/by-id/usb-GN_Jabra_A_S_Jabra_PanaCast_20_A9002000849C-0-video-index0"
W, H, FPS = 1920, 1080, 30
REC = pathlib.Path("/var/lib/hoppdelay")
SEG_S = 60  # one recording file per minute
MAX_DISK = shutil.disk_usage("/").total * 4 // 10  # recording may use 40 % of the disk
STEP_S = 5
STATE = pathlib.Path.home() / ".hoppdelay.json"
ROTATIONS = ["none", "clockwise", "rotate-180", "counterclockwise"]

E = evdev.ecodes
KEYMAP = {  # key -> command (same commands as the web page)
    E.KEY_UP: ("delay_by", STEP_S), E.KEY_DOWN: ("delay_by", -STEP_S),
    E.KEY_LEFT: ("step", -STEP_S), E.KEY_PAGEUP: ("step", -STEP_S),
    E.KEY_RIGHT: ("step", STEP_S), E.KEY_PAGEDOWN: ("step", STEP_S),
    E.KEY_SPACE: ("pause", 0), E.KEY_B: ("pause", 0),
    E.KEY_ENTER: ("live", 0), E.KEY_ESC: ("live", 0),
    E.KEY_R: ("rotate", 0),
}
COMMANDS = {"delay_by", "step", "seek", "pause", "live", "rotate"}
CLIPS = REC / "clips"
CERTS = pathlib.Path("/etc/hoppdelay")  # own CA + server cert: iPhone needs HTTPS to share files to Photos
MAX_CLIP_S = 120

Gst.init(None)
DECODER = "vajpegdec" if Gst.ElementFactory.find("vajpegdec") else "jpegdec"  # Intel GPU decode if available
ENCODER = "vah264enc" if Gst.ElementFactory.find("vah264enc") else "x264enc speed-preset=veryfast"

state = {"delay": 30, "rot": "none"}
try:
    state.update(json.loads(STATE.read_text()))
except (OSError, ValueError):
    pass

# --- Recording: JPEG frames appended to one file per minute; index in memory ---------------
CLIPS.mkdir(parents=True, exist_ok=True)  # saved clips are kept across restarts
for f in CLIPS.glob("*.part"):  # unfinished saves
    f.unlink()
for f in REC.glob("*.mjpg"):  # the index lives in memory, so old files are unusable
    f.unlink()
times, refs = [], []  # capture time (monotonic s), (segment, offset, length); oldest first
segs = {}  # segment number -> bytes
disk = 0
seg_no, seg_file, seg_start = -1, None, 0.0
lock = threading.Lock()


def seg_path(n):
    return REC / f"{n:06d}.mjpg"


def on_frame(sink):
    global disk, seg_no, seg_file, seg_start
    buf = sink.emit("pull-sample").get_buffer()
    data = buf.extract_dup(0, buf.get_size())
    now = time.monotonic()
    if seg_file is None or now - seg_start >= SEG_S:
        if seg_file:
            seg_file.close()
        seg_no, seg_start = seg_no + 1, now
        seg_file = open(seg_path(seg_no), "wb", buffering=0)
        with lock:
            segs[seg_no] = 0
    offset = seg_file.tell()
    seg_file.write(data)
    with lock:
        times.append(now)
        refs.append((seg_no, offset, len(data)))
        segs[seg_no] += len(data)
        disk += len(data)
        while disk > MAX_DISK and len(segs) > 1:  # drop the oldest minute
            old = min(segs)
            disk -= segs.pop(old)
            n = bisect.bisect_left(refs, (old + 1,))
            del times[:n], refs[:n]
            seg_path(old).unlink()
    return Gst.FlowReturn.OK


def read_frame(ref):
    n, offset, length = ref
    with open(seg_path(n), "rb") as f:
        f.seek(offset)
        return f.read(length)


def frames_between(t0, t1):
    with lock:
        i, j = bisect.bisect_left(times, t0), bisect.bisect_right(times, t1)
        return times[i:j], refs[i:j]


def frame_at(t):
    with lock:
        if not refs:
            return None
        ref = refs[max(bisect.bisect_right(times, t) - 1, 0)]
    return read_frame(ref)


def save_clip(t0, t1, rot, name):
    # Re-encode the JPEG frames to H.264 MP4 (plays on iPhone), upright according to `rot`.
    ts, rs = frames_between(t0, t1)
    tmp = CLIPS / (name + ".part")
    p = Gst.parse_launch(
        f"appsrc name=src format=time block=true caps=image/jpeg,width={W},height={H},framerate={FPS}/1 ! "
        f"jpegparse ! {DECODER} ! videoflip method={rot} ! videoconvert ! video/x-raw,format=NV12 ! "
        f"{ENCODER} ! h264parse ! mp4mux ! filesink location={tmp}"
    )
    src = p.get_by_name("src")
    p.set_state(Gst.State.PLAYING)
    for t, ref in zip(ts, rs):
        try:
            buf = Gst.Buffer.new_wrapped(read_frame(ref))
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


# --- Display ---------------------------------------------------------------------------------
def screen_size():
    # Largest progressive mode up to 1080p on the first connected screen. Not the "preferred" mode:
    # AV receivers often report 640x480 as preferred. Re-checked so a different TV can be plugged in.
    for status in sorted(pathlib.Path("/sys/class/drm").glob("card*-*/status")):
        if status.read_text().strip() == "connected":
            modes = [tuple(map(int, m.split("x"))) for m in (status.parent / "modes").read_text().split() if not m.endswith("i")]
            modes = [m for m in modes if m[0] <= 1920 and m[1] <= 1080]
            conn_file = status.parent / "connector_id"  # tell kmssink which output to use
            conn = int(conn_file.read_text()) if conn_file.exists() else -1
            if modes:
                return (*max(modes, key=lambda m: m[0] * m[1]), conn)
    return 1920, 1080, -1


def display(rot, sw, sh, conn):
    # Scale to the screen, keeping aspect ratio (black borders), in both landscape and portrait.
    p = Gst.parse_launch(
        f"appsrc name=src is-live=true max-buffers=1 leaky-type=downstream do-timestamp=true format=time caps=image/jpeg,width={W},height={H},framerate={FPS}/1 ! "
        f"jpegparse ! {DECODER} ! videoflip method={rot} ! videoconvert ! "
        f"videoscale add-borders=true ! video/x-raw,width={sw},height={sh},pixel-aspect-ratio=1/1 ! "
        'textoverlay name=txt valignment=top halignment=left font-desc="Sans 20" ! '
        f"videoconvert ! kmssink sync=false force-modesetting=true connector-id={conn}"
    )
    p.set_state(Gst.State.PLAYING)
    return p, p.get_by_name("src"), p.get_by_name("txt")


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


# --- Web page for the phone -------------------------------------------------------------------
cmds = queue.Queue()  # (command, value) from keyboard and web, applied in the main loop
status = {}  # snapshot for the web page, replaced every loop

PAGE = """<!doctype html><html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes"><title>Hoppdelay</title>
<style>
body{margin:0;padding:16px;background:#111;color:#eee;font:17px -apple-system,sans-serif;user-select:none;-webkit-user-select:none}
h1{font-size:28px;margin:4px 0 2px}h2{font-size:20px;margin:28px 0 6px;border-top:1px solid #333;padding-top:16px}
#sub,#info{color:#999;margin-bottom:10px}
.row{display:flex;gap:8px;margin:10px 0}.row>*{flex:1}
button,select{background:#2a2a2a;color:#eee;border:0;border-radius:12px;padding:18px 0;font-size:20px;text-align:center}
button:active{background:#444}.big{background:#1f6feb;width:100%}.on{background:#1f6feb}
input[type=range]{width:100%;height:40px}label{color:#999;font-size:14px}
#d{text-align:center;font-size:24px;align-self:center}
canvas{width:100%;max-height:65vh;object-fit:contain;background:#000;border-radius:8px}
a{color:#58a6ff}#tls{background:#1c2a3a;border-radius:12px;padding:12px 14px;margin-bottom:12px;font-size:15px}#tls ol{margin:6px 0 0;padding-left:20px}.clip{display:flex;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid #222}
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
<div class="row"><button onclick="c('rotate')">Rotera ⟳</button></div>

<h2>Repris</h2>
<div class="row"><select id="len"><option value="4">4 s</option><option value="7" selected>7 s</option>
<option value="10">10 s</option><option value="15">15 s</option></select>
<button onclick="grab()" style="flex:2">Ta repris av TV-bilden</button></div>
<div id="rp" hidden>
<canvas id="cv"></canvas><div id="info"></div>
<input type="range" id="rs" min="0" max="0" step="1" value="0">
<div class="row"><button onclick="stepf(-1)">◀︎|</button><button onclick="play()" id="pl">▶︎</button><button onclick="stepf(1)">|▶︎</button></div>
<div class="row" id="sp"><button data-s="1" class="on">1×</button><button data-s="0.5">½×</button><button data-s="0.25">¼×</button><button data-s="0.1">⅒×</button></div>
<button class="big" onclick="save()">Spara klipp</button>
</div>
<h2>Sparade klipp</h2><div id="cl">Inga än</div>

<script>
let drag=false,S={},R=null,lastT=0;const t=document.getElementById('t');
function c(x){fetch('/api/'+x,{method:'POST'}).then(u)}
t.oninput=()=>{drag=true;c('seek/'+(-t.value))};t.onchange=()=>{drag=false};
function f(s){s=Math.floor(s);return Math.floor(s/60)+':'+String(s%60).padStart(2,'0')}
function u(){fetch('/api/state').then(r=>r.json()).then(s=>{S=s;
 h.textContent=s.review?(s.paused?'Paus  ':'')+'−'+f(s.behind):'Delay '+s.delay+' s';
 sub.textContent='Inspelat '+f(s.span)+(s.review?' · tryck "Tillbaka" för delay':'');
 d.textContent=s.delay+' s';p.textContent=s.paused?'▶':'⏸';
 t.min=-s.span;if(!drag)t.value=-s.behind;})}
setInterval(u,500);u();

// Replay on the phone: frames are fetched as JPEG blobs, decoded only around the current frame.
async function grab(){
 const r=await(await fetch('/api/range/'+(S.tv-len.value)+'/'+S.tv)).json();
 if(!r.times.length){rp.hidden=false;info.textContent='Inget inspelat i det intervallet ännu';return;}
 const k={times:r.times,blobs:[],bm:new Map(),rot:S.rot,i:0,playing:false,speed:1,pos:0};
 R=k;rp.hidden=false;rs.max=k.times.length-1;setSpeed(1);pl.textContent='▶︎';
 let next=0,done=0;
 const worker=async()=>{while(next<k.times.length&&R===k){const n=next++;
  k.blobs[n]=await(await fetch('/frame/'+k.times[n])).blob();done++;
  if(n===0)draw();if(R===k&&!k.playing)info.textContent='Laddar '+done+'/'+k.times.length;}};
 await Promise.all([1,2,3,4,5,6].map(worker));if(R===k)draw();}
async function draw(){const k=R,n=k&&k.i;if(!k||!k.blobs[n])return;
 let bm=k.bm.get(n);if(!bm){bm=await createImageBitmap(k.blobs[n]);k.bm.set(n,bm);
  if(k.bm.size>40){const[o,v]=k.bm.entries().next().value;v.close();k.bm.delete(o);}}
 if(k!==R||n!==k.i)return;
 const q=k.rot==='clockwise'||k.rot==='counterclockwise',w=q?bm.height:bm.width,hh=q?bm.width:bm.height;
 if(cv.width!==w||cv.height!==hh){cv.width=w;cv.height=hh;}
 const x=cv.getContext('2d');x.save();x.translate(w/2,hh/2);
 x.rotate({none:0,clockwise:Math.PI/2,'rotate-180':Math.PI,counterclockwise:-Math.PI/2}[k.rot]);
 x.drawImage(bm,-bm.width/2,-bm.height/2);x.restore();
 rs.value=n;info.textContent='Bild '+(n+1)+'/'+k.times.length+' · '+(k.times[n]-k.times[0]).toFixed(2)+' s · '+k.speed+'×';}
function idx(t){let lo=0,hi=R.times.length-1;while(lo<hi){const m=(lo+hi+1)>>1;if(R.times[m]<=t)lo=m;else hi=m-1;}return lo;}
function tick(ts){if(R&&R.playing){R.pos+=(ts-lastT)/1000*R.speed;
 if(R.pos>R.times[R.times.length-1]-R.times[0])R.pos=0;
 const n=idx(R.times[0]+R.pos);if(n!==R.i){R.i=n;draw();}}lastT=ts;requestAnimationFrame(tick);}
requestAnimationFrame(tick);
function seekTo(n){R.i=Math.min(Math.max(n,0),R.times.length-1);R.pos=R.times[R.i]-R.times[0];draw();}
function play(){if(!R)return;R.playing=!R.playing;if(R.playing&&R.i>=R.times.length-1)seekTo(0);pl.textContent=R.playing?'⏸':'▶︎';}
function stepf(dn){if(!R)return;R.playing=false;pl.textContent='▶︎';seekTo(R.i+dn);}
rs.oninput=()=>{R.playing=false;pl.textContent='▶︎';seekTo(+rs.value);};
function setSpeed(v){if(R)R.speed=v;document.querySelectorAll('#sp button').forEach(b=>b.classList.toggle('on',+b.dataset.s===v));if(R)draw();}
document.querySelectorAll('#sp button').forEach(b=>b.onclick=()=>setSpeed(+b.dataset.s));
async function save(){if(!R)return;await fetch('/api/save/'+R.times[0]+'/'+R.times[R.times.length-1]+'/'+R.rot,{method:'POST'});clips();}
const esc=s=>s.replace(/[&<>"']/g,ch=>'&#'+ch.charCodeAt(0)+';'),enc=encodeURIComponent;
// Sharing a file (-> "Spara video" to Photos) needs HTTPS and a file fetched before the tap,
// so the first tap downloads (⬇︎), the second opens the share sheet (📲).
const files={},loading=new Set();
const share=x=>!window.isSecureContext?'':'<button data-n="'+esc(x.name)+'" data-a="share"'
 +(files[x.name]?' class="on">📲':'>'+(loading.has(x.name)?'…':'⬇︎'))+'</button>';
async function clips(){const l=await(await fetch('/api/clips')).json();
 cl.innerHTML=l.length?l.map(x=>x.ready
  ?'<div class="clip"><a href="/clips/'+enc(x.name)+'">'+esc(x.name.replace(/[.]mp4$/,''))+'</a><span>'+x.mb+' MB</span>'+share(x)
   +'<button data-n="'+esc(x.name)+'" data-a="rename">✎</button><button data-n="'+esc(x.name)+'" data-a="delete">🗑</button></div>'
  :'<div class="clip">'+esc(x.name)+' · sparas…</div>').join(''):'Inga än';}
cl.onclick=async e=>{const b=e.target.closest('button');if(!b)return;const n=b.dataset.n;
 if(b.dataset.a==='share'){
  if(files[n]){try{await navigator.share({files:[files[n]]});}catch(err){}return;}
  if(loading.has(n))return;loading.add(n);clips();
  const bl=await(await fetch('/clips/'+enc(n))).blob();files[n]=new File([bl],n,{type:'video/mp4'});
  loading.delete(n);return clips();}
 delete files[n];
 if(b.dataset.a==='delete'){if(!confirm('Radera '+n+'?'))return;await fetch('/api/clip/delete/'+enc(n),{method:'POST'});}
 else{const v=prompt('Nytt namn',n.replace(/[.]mp4$/,''));if(!v)return;
  const r=await fetch('/api/clip/rename/'+enc(n)+'/'+enc(v),{method:'POST'});if(r.status===409)alert('Namnet är upptaget eller ogiltigt');}
 clips();};
setInterval(clips,3000);clips();
if(!window.isSecureContext){tls.hidden=false;https.href='https://'+(location.hostname==='10.42.0.1'?'10.42.0.1':'hoppdelay.local')+'/';https.textContent=https.href;}
</script></body></html>"""


def clean_name(s):
    # Letters (incl. åäö), digits, space, - _ . ; always ends in .mp4
    s = "".join(ch for ch in s if ch.isalnum() or ch in " -_.").strip(" .").removesuffix(".mp4").strip(" .")[:80]
    return s + ".mp4" if s else ""


def clip_file(name):
    # A finished clip in CLIPS, or None. Rejects paths and anything else.
    f = CLIPS / name
    return f if name == clean_name(name) and f.is_file() else None


class Web(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parts = [unquote(x) for x in self.path.split("?")[0].strip("/").split("/")]
        try:
            if parts == ["api", "state"]:
                return self.reply(200, json.dumps(status).encode(), "application/json")
            if parts[:2] == ["api", "range"] and len(parts) == 4:
                ts, _ = frames_between(float(parts[2]), float(parts[3]))
                return self.reply(200, json.dumps({"times": ts}).encode(), "application/json")
            if parts[0] == "frame" and len(parts) == 2:
                frame = frame_at(float(parts[1]))
                return self.reply(200, frame, "image/jpeg") if frame else self.reply(404, b"", "text/plain")
            if parts == ["ca.crt"]:
                return self.reply(200, (CERTS / "ca.crt").read_bytes(), "application/x-x509-ca-cert")
            if parts == ["api", "clips"]:
                files = sorted(CLIPS.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
                clips = [{"name": f.name.removesuffix(".part"), "mb": round(f.stat().st_size / 1e6, 1),
                          "ready": f.suffix == ".mp4"} for f in files]
                return self.reply(200, json.dumps(clips).encode(), "application/json")
            if parts[0] == "clips" and len(parts) == 2 and clip_file(parts[1]):
                return self.send_file(clip_file(parts[1]), "video/mp4")
        except (ValueError, FileNotFoundError):
            return self.reply(404, b"", "text/plain")
        self.reply(200, PAGE.encode(), "text/html; charset=utf-8")

    def do_POST(self):
        parts = [unquote(x) for x in self.path.strip("/").split("/")]
        try:
            if parts[:3] == ["api", "clip", "delete"] and len(parts) == 4 and clip_file(parts[3]):
                clip_file(parts[3]).unlink()
                return self.reply(204, b"", "text/plain")
            if parts[:3] == ["api", "clip", "rename"] and len(parts) == 5 and clip_file(parts[3]):
                new = clean_name(parts[4])
                if not new or (CLIPS / new).exists():
                    return self.reply(409, b"", "text/plain")
                clip_file(parts[3]).rename(CLIPS / new)
                return self.reply(204, b"", "text/plain")
            if parts[:2] == ["api", "save"] and len(parts) == 5:
                t0, t1, rot = float(parts[2]), float(parts[3]), parts[4]
                if not 0 < t1 - t0 <= MAX_CLIP_S or rot not in ROTATIONS:
                    return self.reply(400, b"", "text/plain")
                base = time.strftime("hopp-%Y%m%d-%H%M%S", time.localtime(time.time() - (time.monotonic() - t0)))
                name, n = base + ".mp4", 2
                while (CLIPS / name).exists() or (CLIPS / (name + ".part")).exists():  # same second saved twice
                    name, n = f"{base}-{n}.mp4", n + 1
                (CLIPS / (name + ".part")).touch()  # shows up as "saving" right away
                threading.Thread(target=save_clip, args=(t0, t1, rot, name), daemon=True).start()
                return self.reply(202, json.dumps({"name": name}).encode(), "application/json")
            cmd = parts[1] if parts[0] == "api" and len(parts) in (2, 3) else ""
            v = float(parts[2]) if len(parts) == 3 else 0.0
        except (ValueError, IndexError):
            return self.reply(400, b"", "text/plain")
        if cmd not in COMMANDS:
            return self.reply(404, b"", "text/plain")
        cmds.put((cmd, v))
        self.reply(204, b"", "text/plain")

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


ensure_certs()
tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
tls.load_cert_chain(CERTS / "server.crt", CERTS / "server.key")
https = http.server.ThreadingHTTPServer(("", 443), Web)
https.socket = tls.wrap_socket(https.socket, server_side=True, do_handshake_on_connect=False)  # handshake in the request thread
for server in (http.server.ThreadingHTTPServer(("", 80), Web), https):
    threading.Thread(target=server.serve_forever, daemon=True).start()

# --- Main loop -------------------------------------------------------------------------------
cap = Gst.parse_launch(
    f"v4l2src device={CAM} ! image/jpeg,width={W},height={H},framerate={FPS}/1 ! "
    "appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
)
cap.get_by_name("sink").connect("new-sample", on_frame)
cap.set_state(Gst.State.PLAYING)
screen = screen_size()
disp, src, txt = display(state["rot"], *screen)

start = last = time.monotonic()
review, paused, behind = False, False, 0.0  # review: rewound/paused, showing `behind` seconds back
shown, label = None, None
kbds, next_scan = {}, 0.0

while True:
    now = time.monotonic()
    dt, last = now - last, now
    if now >= next_scan:
        scan(kbds)
        next_scan = now + 5
        if screen_size() != screen:  # another screen plugged in
            screen = screen_size()
            disp.set_state(Gst.State.NULL)
            disp, src, txt = display(state["rot"], *screen)
            shown = None
    with lock:
        newest = times[-1] if times else start
        span = now - times[0] if times else 0.0
    for p in (cap, disp):
        msg = p.get_bus().pop_filtered(Gst.MessageType.ERROR)
        if msg:
            err, dbg = msg.parse_error()
            raise SystemExit(f"GStreamer error: {err.message} ({dbg})")  # systemd restarts us
    if now - newest > 5:
        raise SystemExit("No frames from camera for 5 s")

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
            state["rot"] = ROTATIONS[(ROTATIONS.index(state["rot"]) + 1) % len(ROTATIONS)]
            disp.set_state(Gst.State.NULL)
            disp, src, txt = display(state["rot"], *screen)
            shown = None
        else:  # step / seek / pause enter review mode
            if not review:
                review, behind = True, float(state["delay"])
            if cmd == "step":
                behind = min(max(behind - v, 0.0), span)
            elif cmd == "seek":
                behind = min(max(v, 0.0), span)
            else:
                paused = not paused
        STATE.write_text(json.dumps(state))

    if paused:
        behind = min(behind + dt, span)  # frozen frame; stays inside the recording
    back = behind if review else state["delay"]
    with lock:
        if not refs:
            continue
        i = max(bisect.bisect_right(times, now - back) - 1, 0)
        ref, tv = refs[i], times[i]  # tv: capture time of the frame on the TV
    status = {"delay": state["delay"], "rot": state["rot"], "review": review,
              "paused": paused, "behind": back, "span": span, "tv": tv}

    if span < back and not review:
        text = f"Buffrar {span:.0f}/{back} s"
    elif review:
        text = f"{'Paus  ' if paused else ''}-{mmss(back)}   (Enter = tillbaka till {state['delay']} s)"
    else:
        text = f"Delay {state['delay']} s   inspelat {mmss(span)}"
    if ref is not shown or text != label:
        try:
            frame = read_frame(ref)
        except FileNotFoundError:  # that minute was just deleted to free disk
            continue
        txt.set_property("text", text)
        src.emit("push-buffer", Gst.Buffer.new_wrapped(frame))
        shown, label = ref, text
