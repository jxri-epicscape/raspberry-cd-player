#!/usr/bin/env python3
"""
meta_editor.py — Simple web UI for correcting CD metadata.
Open http://192.168.100.45:8080 in any browser on the same network.
Shows the current disc pre-filled; save writes to disc_overrides.json.
Eject and reinsert the CD to apply.
"""

import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT           = 8080
CACHE_DB       = os.path.expanduser("~/.local/share/musicplayer/disc_cache.db")
CACHE_DIR      = os.path.expanduser("~/.cache/musicplayer/art")
OVERRIDES_FILE = "/opt/musicplayer/disc_overrides.json"
CURRENT_DISC   = "/tmp/musicplayer_current_disc"
VAULT_KEY_FILE = "/tmp/musicplayer_vault_key"
CD_VAULT       = os.path.expanduser("~/cd_vault")
MUSIC_DIR      = os.environ.get("MUSIC_LIBRARY", os.path.expanduser("~/music"))
LIBRARY_JSON   = os.path.join(MUSIC_DIR, "library.json")

# ── Rip state (single global, one rip at a time) ──────────────────────────────
_rip_lock   = threading.Lock()
_rip_status = {"running": False, "ripped": 0, "total": 0,
               "done": False, "error": "", "vault_dir": ""}
os.makedirs(CACHE_DIR, exist_ok=True)
try:
    os.makedirs(MUSIC_DIR, exist_ok=True)
except OSError:
    pass


def invalidate_library():
    """Delete library.json so the player rescans on next Library open."""
    try:
        os.remove(LIBRARY_JSON)
    except OSError:
        pass


# ── Data helpers ──────────────────────────────────────────────────────────────
def current_cddb_id() -> str:
    try:
        with open(CURRENT_DISC) as f:
            return f.read().strip()
    except Exception:
        return ""


def get_disc(cddb_id: str) -> dict:
    overrides = load_overrides()
    if cddb_id in overrides:
        d = overrides[cddb_id]
        return {
            "artist":   d.get("artist", ""),
            "album":    d.get("album", ""),
            "year":     str(d.get("year", "")),
            "tracks":   d.get("tracks", []),
            "art_path": d.get("art_path", ""),
        }
    try:
        conn = sqlite3.connect(CACHE_DB)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM disc_cache WHERE cddb_id = ?", (cddb_id,)
        ).fetchone()
        conn.close()
        if row:
            return {
                "artist":   row["artist"],
                "album":    row["album"],
                "year":     row["year"],
                "tracks":   json.loads(row["tracks_json"] or "[]"),
                "art_path": row["art_path"] or "",
            }
    except Exception:
        pass
    return {"artist": "", "album": "", "year": "", "tracks": [], "art_path": ""}


def load_overrides() -> dict:
    try:
        if os.path.exists(OVERRIDES_FILE):
            with open(OVERRIDES_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_override(cddb_id: str, data: dict):
    overrides = load_overrides()
    overrides[cddb_id] = data
    with open(OVERRIDES_FILE, "w", encoding="utf-8") as f:
        json.dump(overrides, f, indent=2, ensure_ascii=False)


def delete_override(cddb_id: str):
    overrides = load_overrides()
    overrides.pop(cddb_id, None)
    with open(OVERRIDES_FILE, "w", encoding="utf-8") as f:
        json.dump(overrides, f, indent=2, ensure_ascii=False)


def save_art(data: bytes, cddb_id: str, filename: str) -> str:
    ext  = ".jpg" if any(x in filename.lower() for x in ("jpg", "jpeg")) else ".png"
    name = hashlib.md5(f"custom_{cddb_id}".encode()).hexdigest() + ext
    path = os.path.join(CACHE_DIR, name)
    with open(path, "wb") as f:
        f.write(data)
    return path


# ── Vault helpers ─────────────────────────────────────────────────────────────
def current_vault_key() -> str:
    try:
        with open(VAULT_KEY_FILE) as f:
            return f.read().strip()
    except Exception:
        return ""


def vault_files(key: str) -> list:
    if not key:
        return []
    d = os.path.join(CD_VAULT, key)
    if not os.path.isdir(d):
        return []
    exts = {".flac", ".wav", ".mp3", ".ogg", ".m4a"}
    return sorted(
        fn for fn in os.listdir(d)
        if os.path.splitext(fn)[1].lower() in exts
    )


def _do_rip(vault_dir: str, num_tracks: int):
    """Background thread: rip using MPV (lenient with damaged discs).
    MPV already plays problem CDs successfully — so we capture its output
    per-chapter to WAV, then convert to MP3 with ffmpeg."""
    global _rip_status

    def _set(**kw):
        with _rip_lock:
            _rip_status.update(kw)

    os.makedirs(vault_dir, exist_ok=True)
    have_ffmpeg = bool(shutil.which("ffmpeg"))

    try:
        # Stop services so MPV doesn't conflict with the daemon
        subprocess.run(["sudo", "systemctl", "stop", "musicplayer.service"],
                       timeout=12, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["sudo", "systemctl", "stop", "mpv.service"],
                       timeout=12, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        for track in range(num_tracks):
            with _rip_lock:
                if not _rip_status["running"]:
                    break

            wav_path = os.path.join(vault_dir, f"track{track+1:02d}.wav")
            mp3_path = os.path.join(vault_dir, f"track{track+1:02d}.mp3")

            # Try cdparanoia burst mode first (fast, no error correction).
            # Falls back to MPV if cdparanoia is unavailable.
            ripped_ok = False
            err_msg   = ""

            if shutil.which("cdparanoia"):
                try:
                    r = subprocess.run(
                        ["cdparanoia", "-Z", "-d", "/dev/sr0",
                         str(track + 1), wav_path],
                        timeout=90,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    ripped_ok = os.path.exists(wav_path) and os.path.getsize(wav_path) > 4096
                    if not ripped_ok:
                        err_msg = f"cdparanoia exit={r.returncode}"
                except subprocess.TimeoutExpired:
                    err_msg = "cdparanoia timed out"

            if not ripped_ok:
                # MPV fallback — also with a short timeout
                try:
                    r = subprocess.run(
                        ["mpv", "cdda:///dev/sr0",
                         "--no-video", "--no-terminal", "--no-cache",
                         "--ao=pcm", f"--ao-pcm-file={wav_path}",
                         f"--chapter={track}-{track}"],
                        timeout=90,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    ripped_ok = os.path.exists(wav_path) and os.path.getsize(wav_path) > 4096
                    if not ripped_ok:
                        err_msg = f"mpv exit={r.returncode}"
                except subprocess.TimeoutExpired:
                    err_msg = "mpv timed out (disc may be copy-protected)"

            if not ripped_ok:
                raise RuntimeError(
                    f"Track {track+1} failed: {err_msg}. "
                    f"Try ripping on Windows with EAC and copying via "
                    f"\\\\192.168.100.45\\CDVault\\{os.path.basename(vault_dir)}\\"
                )

            # Convert WAV → MP3
            if have_ffmpeg and os.path.exists(wav_path):
                subprocess.run(
                    ["ffmpeg", "-y", "-i", wav_path, "-q:a", "2", mp3_path],
                    timeout=120,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                try:
                    os.remove(wav_path)
                except OSError:
                    pass

            _set(ripped=track + 1)

        _set(done=True, running=False)

    except Exception as exc:
        _set(error=str(exc), done=True, running=False)

    finally:
        # Always restart services
        subprocess.run(["sudo", "systemctl", "start", "mpv.service"],
                       timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["sudo", "systemctl", "start", "musicplayer.service"],
                       timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ── HTML ──────────────────────────────────────────────────────────────────────
CSS = """
body{font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px;
     background:#111;color:#eee}
nav{display:flex;gap:4px;margin-bottom:28px;border-bottom:1px solid #333;padding-bottom:14px}
nav a{padding:8px 18px;border-radius:5px;color:#aaa;text-decoration:none;font-size:14px}
nav a.active{background:#ffa500;color:#000;font-weight:bold}
nav a:hover:not(.active){background:#222}
h1{color:#ffa500;margin-bottom:4px}
.sub{color:#555;font-size:12px;margin-bottom:24px}
label{display:block;margin-top:14px;color:#aaa;font-size:12px;
      text-transform:uppercase;letter-spacing:.5px}
input[type=text],textarea,select{width:100%;padding:9px;background:#1e1e1e;color:#eee;
             border:1px solid #444;border-radius:5px;
             box-sizing:border-box;font-size:15px;margin-top:4px}
textarea{height:210px;font-size:13px;line-height:1.6;resize:vertical}
.art-row{display:flex;align-items:center;gap:16px;margin-top:6px}
.art-thumb{width:80px;height:80px;object-fit:cover;border-radius:6px;
           border:1px solid #444;background:#222}
.art-placeholder{width:80px;height:80px;border-radius:6px;border:1px solid #333;
                 background:#1a1a1a;display:flex;align-items:center;
                 justify-content:center;color:#444;font-size:28px}
input[type=file]{flex:1;padding:8px;background:#1e1e1e;color:#aaa;
                 border:1px solid #444;border-radius:5px;cursor:pointer}
.btns{display:flex;gap:10px;margin-top:22px}
button{flex:1;padding:12px;border:none;border-radius:5px;
       font-size:15px;font-weight:bold;cursor:pointer}
.save{background:#ffa500;color:#000}
.reset{background:#333;color:#aaa}
.del{background:#c0392b;color:#fff}
.ok{color:#4caf50;font-size:18px;margin:20px 0}
.warn{color:#f44336;margin-bottom:16px}
a{color:#ffa500;text-decoration:none}
.album-list{list-style:none;padding:0;margin:0}
.album-list li{display:flex;align-items:center;gap:12px;padding:10px 0;
               border-bottom:1px solid #222}
.album-list li:last-child{border-bottom:none}
.album-art{width:48px;height:48px;object-fit:cover;border-radius:4px;background:#222;
           border:1px solid #333;flex-shrink:0}
.album-art-ph{width:48px;height:48px;border-radius:4px;background:#1a1a1a;
              border:1px solid #333;display:flex;align-items:center;
              justify-content:center;color:#333;font-size:22px;flex-shrink:0}
.album-info{flex:1;min-width:0}
.album-name{font-weight:bold;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.album-sub{font-size:12px;color:#666;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.file-list{list-style:none;padding:0;margin:0}
.file-list li{display:flex;justify-content:space-between;align-items:center;
              padding:8px 0;border-bottom:1px solid #222;font-size:13px;
              gap:10px}
.file-list li:last-child{border-bottom:none}
.file-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.del-btn{background:#c0392b;color:#fff;border:none;border-radius:4px;
         padding:4px 10px;cursor:pointer;font-size:12px;flex-shrink:0}
.small-btn{background:#ffa500;color:#000;border:none;border-radius:4px;
           padding:4px 8px;cursor:pointer;font-size:12px;flex-shrink:0}
.section{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:8px;
         padding:16px;margin-bottom:16px}
.section h3{margin:0 0 12px;color:#ffa500;font-size:15px}
"""


def page(body: str, active: str = "cd") -> bytes:
    nav = f"""<nav>
  <a href="/" class="{'active' if active=='cd' else ''}">CD Info</a>
  <a href="/library" class="{'active' if active=='lib' else ''}">Music Library</a>
  <a href="/rip" class="{'active' if active=='rip' else ''}">Rip CD</a>
</nav>"""
    return f"""<!DOCTYPE html>
<html lang="en"><head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Music Manager</title>
  <style>{CSS}</style>
</head><body>{nav}{body}</body></html>""".encode("utf-8")


def editor_page(cddb_id: str, disc: dict, msg: str = "") -> bytes:
    tracks_text  = "\n".join(t.get("title", "") for t in disc["tracks"])
    art_path     = disc.get("art_path", "")
    has_override = cddb_id in load_overrides()
    e = html.escape

    warn     = f"<p class='warn'>{e(msg)}</p>" if msg else ""
    no_disc  = "<p class='warn'>No disc detected — insert a CD first.</p>" if not cddb_id else ""
    disabled = "disabled" if not cddb_id else ""

    if art_path and os.path.exists(art_path):
        art_html = (f'<img class="art-thumb" '
                    f'src="/art/{e(os.path.basename(art_path))}" alt="cover">')
    else:
        art_html = "<div class='art-placeholder'>♪</div>"

    reset_btn = (
        "<button class='reset' type='submit' formaction='/reset'>↩ Reset to auto</button>"
        if has_override else ""
    )

    return page(f"""
  <h1>CD Metadata</h1>
  <p class="sub">Disc: {e(cddb_id) or '—'}</p>
  {warn}{no_disc}
  <form method="post" action="/save" enctype="multipart/form-data">
    <input type="hidden" name="cddb_id" value="{e(cddb_id)}">

    <label>Artist</label>
    <input type="text" name="artist" value="{e(disc['artist'])}"
           placeholder="Artist name" {disabled}>

    <label>Album</label>
    <input type="text" name="album" value="{e(disc['album'])}"
           placeholder="Album title" {disabled}>

    <label>Year</label>
    <input type="text" name="year" value="{e(disc['year'])}"
           placeholder="e.g. 1999" {disabled}>

    <label>Cover art</label>
    <div class="art-row">
      {art_html}
      <input type="file" name="art" accept="image/*" {disabled}>
    </div>

    <label>Tracks — one title per line</label>
    <textarea name="tracks" {disabled}>{e(tracks_text)}</textarea>

    <div class="btns">
      <button class="save" type="submit" {disabled}>💾 Save</button>
      {reset_btn}
    </div>
  </form>
""")


def rip_page() -> bytes:
    """HTML page for vault ripping."""
    e       = html.escape
    key     = current_vault_key()
    cddb_id = current_cddb_id()
    disc    = get_disc(cddb_id) if cddb_id else {}
    artist  = e(disc.get("artist", "Unknown Artist"))
    album   = e(disc.get("album",  "Unknown Album"))
    num_tr  = len(disc.get("tracks", []))

    files   = vault_files(key)
    in_vault = len(files)

    if not key:
        disc_info = "<p class='warn'>No CD detected — insert a disc first.</p>"
    else:
        disc_info = f"<p><b>{artist}</b> — {album}</p><p style='color:#666;font-size:12px'>Vault key: {e(key)}</p>"

    vault_status = ""
    if in_vault:
        flist = "".join(f"<li>{e(fn)}</li>" for fn in files)
        vault_status = f"""
        <div class="section">
          <h3>Already in vault ({in_vault} tracks)</h3>
          <ul class="file-list" style="font-size:12px">{flist}</ul>
          <form method="post" action="/rip" style="margin-top:10px">
            <input type="hidden" name="action" value="delete_vault">
            <button class="del" type="submit"
              onclick="return confirm('Delete all ripped files for this disc?')">
              🗑 Delete vault copy</button>
          </form>
        </div>"""

    rip_btn = ""
    if key and num_tr > 0 and not in_vault:
        rip_btn = f"""
        <form method="post" action="/rip" id="ripform">
          <input type="hidden" name="action" value="start">
          <button class="save" type="submit" id="ripbtn">
            ⏺ Rip {num_tr} tracks to vault</button>
        </form>"""
    elif key and not in_vault:
        rip_btn = """<form method="post" action="/rip">
          <input type="hidden" name="action" value="start">
          <button class="save" type="submit">⏺ Rip CD to vault</button>
        </form>"""

    progress_ui = """
    <div id="prog-box" style="display:none" class="section">
      <h3>Ripping in progress…</h3>
      <div id="prog-bar-wrap" style="background:#333;border-radius:4px;height:12px;margin:8px 0">
        <div id="prog-bar" style="background:#ffa500;height:12px;border-radius:4px;width:0%"></div>
      </div>
      <p id="prog-txt" style="color:#aaa;font-size:13px"></p>
      <p id="prog-err" style="color:#f44"></p>
    </div>
    <script>
    var polling = false;
    function poll(){
      fetch('/rip/status').then(r=>r.json()).then(d=>{
        var box=document.getElementById('prog-box');
        box.style.display='block';
        var pct = d.total>0 ? Math.round(d.ripped/d.total*100) : 0;
        document.getElementById('prog-bar').style.width=pct+'%';
        document.getElementById('prog-txt').textContent=
          'Track '+d.ripped+' / '+d.total+' ('+pct+'%)';
        if(d.error) document.getElementById('prog-err').textContent='Error: '+d.error;
        if(!d.done){ setTimeout(poll,2000); }
        else {
          document.getElementById('prog-txt').textContent=
            d.error ? 'Failed after '+d.ripped+' tracks' : 'Done! '+d.ripped+' tracks ripped. Reload to see files.';
          document.getElementById('prog-bar').style.background = d.error?'#f44':'#4caf50';
        }
      }).catch(()=>{ if(polling) setTimeout(poll,3000); });
    }
    // Auto-start polling if rip is running
    fetch('/rip/status').then(r=>r.json()).then(d=>{
      if(d.running){ polling=true; poll(); }
    });
    // Hook form submit
    var form=document.getElementById('ripform');
    if(form) form.addEventListener('submit',function(){
      polling=true;
      setTimeout(function(){ poll(); },1000);
    });
    </script>"""

    samba_section = ""
    if key:
        samba_path = e(f"\\\\192.168.100.45\\CDVault\\{key}\\")
        samba_section = f"""
        <div class="section">
          <h3>Option A — Copy from Windows (recommended for protected discs)</h3>
          <p style="color:#aaa;font-size:13px;margin:0 0 8px">
            Rip with EAC on Windows, then paste files into this network folder:
          </p>
          <code style="display:block;font-size:12px;color:#ffa500;
                       background:#0a0a0a;padding:8px;border-radius:4px;
                       word-break:break-all;user-select:all">{samba_path}</code>
          <p style="color:#555;font-size:11px;margin:6px 0 0">
            Any MP3 or FLAC files dropped there will play automatically when this disc is inserted.
          </p>
        </div>"""

    auto_rip_section = ""
    if key and not in_vault:
        auto_rip_section = f"""
        <div class="section">
          <h3>Option B — Auto-rip on Pi</h3>
          <p style="color:#888;font-size:12px;margin:0 0 8px">
            Works for scratched discs. May hang or fail on copy-protected discs.
          </p>
          {rip_btn}
          {progress_ui}
        </div>"""

    return page(f"""
  <h1>Rip CD to Vault</h1>
  <p class="sub">Save a digital copy of a problem CD — it plays silently instead of the disc.</p>
  {disc_info}
  {vault_status}
  {samba_section}
  {auto_rip_section}
""", active="rip")


# ── Request handler ───────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def _respond(self, body: bytes, status: int = 200,
                 content_type: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str = "/"):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _serve_art(self, filename: str):
        """Serve image files from CACHE_DIR at /art/<filename>."""
        safe = os.path.basename(filename)
        path = os.path.join(CACHE_DIR, safe)
        if not os.path.exists(path):
            self._respond(b"Not found", 404)
            return
        mime = mimetypes.guess_type(path)[0] or "image/jpeg"
        with open(path, "rb") as f:
            self._respond(f.read(), content_type=mime)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route  = parsed.path
        qs     = urllib.parse.parse_qs(parsed.query)

        if route.startswith("/art/"):
            self._serve_art(route[5:])
            return
        if route.startswith("/libart/"):
            self._serve_lib_art(route[8:])
            return
        if route == "/library":
            self._respond(self._library_browse())
            return
        if route == "/library/album":
            artist = qs.get("a", [""])[0]
            album  = qs.get("b", [""])[0]
            msg    = qs.get("msg", [""])[0]
            body   = self._library_album(artist, album, msg)
            if body:
                self._respond(body)
            return
        if route == "/rip":
            self._respond(rip_page())
            return
        if route == "/rip/status":
            with _rip_lock:
                data = dict(_rip_status)
            self._respond(json.dumps(data).encode(), content_type="application/json")
            return
        # Default: CD metadata editor
        cddb_id = current_cddb_id()
        disc    = get_disc(cddb_id) if cddb_id else \
                  {"artist": "", "album": "", "year": "", "tracks": [], "art_path": ""}
        self._respond(editor_page(cddb_id, disc))

    @staticmethod
    def _parse_multipart(body: bytes, boundary: str) -> dict:
        """Return dict of field_name -> (bytes_value, filename_or_None).
        For fields that appear multiple times (e.g. multi-file upload),
        the value is a list of (bytes, filename) tuples under key 'file_list:<name>'."""
        parts = {}
        sep   = ("--" + boundary).encode()
        for segment in body.split(sep)[1:]:
            if segment[:2] == b"--":
                break
            # Split headers / content on first blank line
            for delim in (b"\r\n\r\n", b"\n\n"):
                if delim in segment:
                    hdr_raw, content = segment.split(delim, 1)
                    break
            else:
                continue
            content  = content.rstrip(b"\r\n")
            hdr_text = hdr_raw.decode("utf-8", errors="replace")
            name = filename = None
            for line in hdr_text.splitlines():
                m = re.search(r'name="([^"]*)"', line)
                if m:
                    name = m.group(1)
                m = re.search(r'filename="([^"]*)"', line)
                if m:
                    filename = m.group(1)
            if name is not None:
                list_key = f"file_list:{name}"
                if list_key in parts:
                    parts[list_key].append((content, filename))
                elif name in parts:
                    # Second occurrence — promote to list
                    parts[list_key] = [parts[name], (content, filename)]
                    parts[name] = (content, filename)  # keep last for compat
                else:
                    parts[name] = (content, filename)
                    parts[list_key] = [(content, filename)]
        return parts

    def do_POST(self):
        route        = self.path.split("?")[0]
        content_type = self.headers.get("Content-Type", "")
        length       = int(self.headers.get("Content-Length", 0))

        mp_parts = {}
        if "multipart/form-data" in content_type:
            m = re.search(r'boundary=([^\s;]+)', content_type)
            boundary = m.group(1).strip('"') if m else ""
            body     = self.rfile.read(length)
            mp_parts = self._parse_multipart(body, boundary)
            def field(name, default=""):
                if name in mp_parts:
                    val, _ = mp_parts[name]
                    return val.decode("utf-8", errors="replace").strip()
                return default
        else:
            raw = self.rfile.read(length).decode("utf-8")
            qs  = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            def field(name, default=""):
                return qs.get(name, default).strip()

        # ── Library POST routes ───────────────────────────────────────────────
        if route == "/library/create":
            self._library_create(field("artist"), field("album"))
            return
        if route == "/library/upload":
            artist      = field("artist")
            album       = field("album")
            upload_type = field("upload_type")
            all_files   = mp_parts.get("file_list:file", [])
            saved = []
            for file_data, filename in all_files:
                if filename and file_data:
                    name = self._library_upload(artist, album, upload_type, file_data, filename)
                    if name:
                        saved.append(name)
            qa  = urllib.parse.quote(artist)
            qb  = urllib.parse.quote(album)
            msg = f"Uploaded {len(saved)} file(s)" if saved else "No files uploaded"
            self._redirect(f"/library/album?a={qa}&b={qb}&msg={urllib.parse.quote(msg)}")
            return
        if route == "/library/delete":
            self._library_delete(field("artist"), field("album"), field("filename"))
            return
        if route == "/library/delete-album":
            self._library_delete_album(field("artist"), field("album"))
            return
        if route == "/library/delete-artist":
            self._library_delete_artist(field("artist"))
            return
        if route == "/library/rename-album":
            self._library_rename_album(field("artist"), field("album"),
                                       field("new_artist"), field("new_album"))
            return
        if route == "/library/rename-artist":
            self._library_rename_artist(field("artist"), field("new_artist"))
            return
        if route == "/library/rename-file":
            self._library_rename_file(field("artist"), field("album"),
                                      field("old_name"), field("new_name"))
            return

        # ── Rip CD POST routes ────────────────────────────────────────────────
        if route == "/rip":
            action = field("action")
            if action == "start":
                key = current_vault_key()
                if key:
                    vault_dir = os.path.join(CD_VAULT, key)
                    cddb_for_rip = current_cddb_id()
                    disc_for_rip = get_disc(cddb_for_rip) if cddb_for_rip else {}
                    num_tr = len(disc_for_rip.get("tracks", [])) or 99
                    with _rip_lock:
                        if not _rip_status["running"]:
                            _rip_status.update({
                                "running": True, "ripped": 0, "total": num_tr,
                                "done": False, "error": "", "vault_dir": vault_dir,
                            })
                            t = threading.Thread(
                                target=_do_rip, args=(vault_dir, num_tr), daemon=True)
                            t.start()
            elif action == "delete_vault":
                key = current_vault_key()
                if key:
                    vault_dir = os.path.join(CD_VAULT, key)
                    if os.path.isdir(vault_dir):
                        shutil.rmtree(vault_dir)
            self._redirect("/rip")
            return

        cddb_id = field("cddb_id")
        if not cddb_id:
            self._redirect()
            return

        if route == "/reset":
            delete_override(cddb_id)
            self._respond(page("""
  <h1>CD Metadata</h1>
  <p class="ok">✓ Override removed — auto metadata restored.</p>
  <p style="color:#aaa">Eject and reinsert the CD to re-fetch.</p>
  <p><a href="/">← Back</a></p>
"""))
            return

        # /save
        artist     = field("artist")
        album      = field("album")
        year       = field("year")
        raw_tracks = field("tracks")

        if not artist or not album:
            disc = get_disc(cddb_id)
            disc.update({"artist": artist, "album": album, "year": year})
            self._respond(editor_page(cddb_id, disc, "Artist and Album are required."))
            return

        tracks = [
            {"num": i + 1, "title": ln.strip()}
            for i, ln in enumerate(raw_tracks.splitlines())
            if ln.strip()
        ]

        # Keep existing art unless a new file was uploaded
        art_path = get_disc(cddb_id).get("art_path", "")
        if "art" in mp_parts:
            raw_img, art_filename = mp_parts["art"]
            if art_filename and raw_img:
                art_path = save_art(raw_img, cddb_id, art_filename)

        save_override(cddb_id, {
            "artist":   artist,
            "album":    album,
            "year":     year,
            "tracks":   tracks,
            "art_path": art_path,
        })

        # Update the cache row too so it takes effect immediately (no re-insert needed)
        try:
            conn = sqlite3.connect(CACHE_DB)
            conn.execute(
                "UPDATE disc_cache SET artist=?, album=?, year=?, art_path=? WHERE cddb_id=?",
                (artist, album, year, art_path, cddb_id),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

        art_thumb = ""
        if art_path and os.path.exists(art_path):
            art_thumb = (
                f'<img style="width:80px;height:80px;object-fit:cover;'
                f'border-radius:6px;margin-top:10px" '
                f'src="/art/{html.escape(os.path.basename(art_path))}" alt="cover">'
            )

        self._respond(page(f"""
  <h1>🎵 CD Metadata</h1>
  <p class="ok">✓ Saved!</p>
  <p><strong>{html.escape(artist)}</strong> — {html.escape(album)}
  {(' (' + html.escape(year) + ')') if year else ''}</p>
  {art_thumb}
  <p style="color:#aaa;margin-top:14px">Changes apply immediately — no eject needed.</p>
  <p><a href="/">← Edit again</a></p>
"""))

    # ── Library routes ────────────────────────────────────────────────────────
    def _safe_path(self, *parts) -> str | None:
        """Resolve path under MUSIC_DIR; return None if outside."""
        target = os.path.realpath(os.path.join(MUSIC_DIR, *parts))
        if not target.startswith(os.path.realpath(MUSIC_DIR)):
            return None
        return target

    def _serve_lib_art(self, rel: str):
        """Serve cover art from MUSIC_DIR."""
        path = self._safe_path(urllib.parse.unquote(rel))
        if not path or not os.path.isfile(path):
            self._respond(b"Not found", 404)
            return
        mime = mimetypes.guess_type(path)[0] or "image/jpeg"
        with open(path, "rb") as f:
            self._respond(f.read(), content_type=mime)

    def _library_browse(self):
        e = html.escape
        base = os.path.realpath(MUSIC_DIR)
        # Build artist → albums structure
        artists = {}
        total_albums = 0
        for artist in sorted(os.listdir(base)):
            ap = os.path.join(base, artist)
            if not os.path.isdir(ap) or artist.startswith("."):
                continue
            albums_here = []
            for album in sorted(os.listdir(ap)):
                alp = os.path.join(ap, album)
                if not os.path.isdir(alp) or album.startswith("."):
                    continue
                audio_files = [f for f in os.listdir(alp)
                               if os.path.splitext(f)[1].lower()
                               in {".mp3", ".flac", ".ogg", ".m4a", ".wav", ".aac"}]
                cover = next(
                    (os.path.join(alp, f) for f in os.listdir(alp)
                     if f.lower() in ("cover.jpg", "cover.png", "folder.jpg")),
                    None,
                )
                albums_here.append({"name": album, "tracks": len(audio_files), "cover": cover})
                total_albums += 1
            if albums_here:
                artists[artist] = albums_here

        sections = []
        for artist, albums_here in artists.items():
            qa = urllib.parse.quote(artist)
            album_rows = []
            for ab in albums_here:
                qb = urllib.parse.quote(ab["name"])
                href = f'/library/album?a={e(qa)}&b={e(qb)}'
                art_html = (
                    f'<img class="album-art" src="/libart/'
                    f'{e(urllib.parse.quote(artist + "/" + ab["name"] + "/" + os.path.basename(ab["cover"])))}"> '
                    if ab["cover"] else '<div class="album-art-ph">♪</div>'
                )
                album_rows.append(f"""<li>
  {art_html}
  <div class="album-info">
    <div class="album-name"><a href="{href}">{e(ab['name'])}</a></div>
    <div class="album-sub">{ab['tracks']} tracks</div>
  </div>
  <form method="post" action="/library/delete-album" style="margin:0"
        onsubmit="return confirm('Delete album {e(ab['name'])} and all its files?')">
    <input type="hidden" name="artist" value="{e(artist)}">
    <input type="hidden" name="album" value="{e(ab['name'])}">
    <button class="del-btn" type="submit">Delete</button>
  </form>
</li>""")

            sections.append(f"""
<div class="section">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
    <h3 style="margin:0;flex:1">{e(artist)}</h3>
    <button class="reset" style="padding:4px 10px;font-size:12px;flex:0"
            onclick="toggleRename('{e(artist)}')">Rename artist</button>
    <form method="post" action="/library/delete-artist" style="margin:0"
          onsubmit="return confirm('Delete artist folder {e(artist)} and ALL albums inside?')">
      <input type="hidden" name="artist" value="{e(artist)}">
      <button class="del-btn" style="padding:4px 10px;font-size:12px" type="submit">Delete</button>
    </form>
  </div>
  <div id="rename-{e(artist)}" style="display:none;margin-bottom:12px">
    <form method="post" action="/library/rename-artist" style="display:flex;gap:8px;align-items:center">
      <input type="hidden" name="artist" value="{e(artist)}">
      <input type="text" name="new_artist" value="{e(artist)}" style="margin:0;flex:1">
      <button class="save" style="flex:0;padding:8px 14px" type="submit">Save</button>
    </form>
  </div>
  <ul class="album-list">{"".join(album_rows)}</ul>
</div>""")

        content = "".join(sections) if sections else '<p style="color:#555">No albums yet.</p>'

        return page(f"""
  <h1>Music Library</h1>
  <p class="sub">{total_albums} albums in {e(MUSIC_DIR)}</p>

  <div class="section">
    <h3>Add new album</h3>
    <form method="post" action="/library/create">
      <label>Artist</label>
      <input type="text" name="artist" placeholder="e.g. Radiohead" required>
      <label>Album</label>
      <input type="text" name="album" placeholder="e.g. OK Computer" required>
      <div class="btns">
        <button class="save" type="submit">+ Create folder</button>
      </div>
    </form>
  </div>

  {content}
  <script>
  function toggleEl(id) {{
    var el = document.getElementById(id);
    if (el) el.style.display = el.style.display === 'none' ? 'flex' : 'none';
  }}
  function toggleRename(artist) {{
    var el = document.getElementById('rename-' + artist);
    el.style.display = el.style.display === 'none' ? 'block' : 'none';
  }}
  </script>
""", active="lib")

    def _library_album(self, artist: str, album: str, msg: str = ""):
        e = html.escape
        path = self._safe_path(artist, album)
        if not path or not os.path.isdir(path):
            self._redirect("/library")
            return

        files = sorted(os.listdir(path))
        audio_exts = {".mp3", ".flac", ".ogg", ".m4a", ".wav", ".aac"}
        art_exts   = {".jpg", ".jpeg", ".png"}

        cover = next(
            (f for f in files if f.lower() in ("cover.jpg", "cover.png", "folder.jpg")),
            None,
        )
        art_html = (
            f'<img class="art-thumb" src="/libart/'
            f'{e(urllib.parse.quote(artist + "/" + album + "/" + cover))}">'
            if cover else '<div class="art-placeholder">♪</div>'
        )

        file_rows = []
        for fi, fn in enumerate(files):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in audio_exts and ext not in art_exts:
                continue
            qa = urllib.parse.quote(artist)
            qb = urllib.parse.quote(album)
            qf = urllib.parse.quote(fn)
            js_a = artist.replace("\\", "\\\\").replace("'", "\\'")
            js_b = album.replace("\\", "\\\\").replace("'", "\\'")
            js_f = fn.replace("\\", "\\\\").replace("'", "\\'")
            file_rows.append(f"""<li>
  <span class="file-name">{e(fn)}</span>
  <button class="small-btn" type="button"
          onclick="renameFile('{js_a}','{js_b}','{js_f}')">Rename</button>
  <form method="post" action="/library/delete" style="margin:0">
    <input type="hidden" name="artist" value="{e(artist)}">
    <input type="hidden" name="album" value="{e(album)}">
    <input type="hidden" name="filename" value="{e(fn)}">
    <button class="del-btn" type="submit"
            onclick="return confirm('Delete {e(fn)}?')">Delete</button>
  </form>
</li>""")

        qa = urllib.parse.quote(artist)
        qb = urllib.parse.quote(album)
        warn = f'<p class="ok">{e(msg)}</p>' if msg else ""

        return page(f"""
  <p><a href="/library">← Library</a></p>
  <h1>{e(album)}</h1>
  <p class="sub">{e(artist)}</p>
  {warn}

  <div class="section">
    <h3>Cover art</h3>
    <div class="art-row">
      {art_html}
      <form method="post" action="/library/upload" enctype="multipart/form-data" style="flex:1">
        <input type="hidden" name="artist" value="{e(artist)}">
        <input type="hidden" name="album" value="{e(album)}">
        <input type="hidden" name="upload_type" value="art">
        <input type="file" name="file" accept="image/*" style="margin-bottom:8px">
        <button class="save" type="submit" style="width:100%">Upload cover</button>
      </form>
    </div>
  </div>

  <div class="section">
    <h3>Upload music files</h3>
    <form method="post" action="/library/upload" enctype="multipart/form-data">
      <input type="hidden" name="artist" value="{e(artist)}">
      <input type="hidden" name="album" value="{e(album)}">
      <input type="hidden" name="upload_type" value="music">
      <input type="file" name="file" accept=".mp3,.flac,.ogg,.m4a,.wav,.aac" multiple>
      <div class="btns">
        <button class="save" type="submit">Upload</button>
      </div>
    </form>
  </div>

  <div class="section">
    <h3>Rename album</h3>
    <form method="post" action="/library/rename-album">
      <input type="hidden" name="artist" value="{e(artist)}">
      <input type="hidden" name="album" value="{e(album)}">
      <label>Artist name</label>
      <input type="text" name="new_artist" value="{e(artist)}" required>
      <label>Album name</label>
      <input type="text" name="new_album" value="{e(album)}" required>
      <div class="btns">
        <button class="save" type="submit">Rename</button>
        <form method="post" action="/library/delete-album" style="flex:1;margin:0"
              onsubmit="return confirm('Delete this entire album and all its files?')">
          <input type="hidden" name="artist" value="{e(artist)}">
          <input type="hidden" name="album" value="{e(album)}">
          <button class="del" type="submit" style="width:100%">Delete album</button>
        </form>
      </div>
    </form>
  </div>

  <div class="section">
    <h3>Files</h3>
    {'<ul class="file-list">' + "".join(file_rows) + '</ul>' if file_rows
      else '<p style="color:#555">No files yet.</p>'}
  </div>
  <script>
  function renameFile(artist, album, oldName) {{
    var newName = prompt('Rename file:', oldName);
    if (!newName || newName.trim() === oldName) return;
    var f = document.createElement('form');
    f.method = 'post'; f.action = '/library/rename-file';
    [['artist', artist], ['album', album],
     ['old_name', oldName], ['new_name', newName.trim()]
    ].forEach(function(kv) {{
      var i = document.createElement('input');
      i.type = 'hidden'; i.name = kv[0]; i.value = kv[1];
      f.appendChild(i);
    }});
    document.body.appendChild(f);
    f.submit();
  }}
  </script>
""", active="lib")

    def _library_create(self, artist: str, album: str):
        if not artist or not album:
            self._redirect("/library")
            return
        path = self._safe_path(artist, album)
        if not path:
            self._redirect("/library")
            return
        os.makedirs(path, exist_ok=True)
        invalidate_library()
        qa = urllib.parse.quote(artist)
        qb = urllib.parse.quote(album)
        self._redirect(f"/library/album?a={qa}&b={qb}")

    def _library_upload(self, artist: str, album: str, upload_type: str,
                        file_data: bytes, filename: str):
        path = self._safe_path(artist, album)
        if not path or not os.path.isdir(path):
            self._redirect("/library")
            return
        safe_name = os.path.basename(filename)
        if not safe_name:
            return None
        if upload_type == "art":
            ext = ".png" if safe_name.lower().endswith(".png") else ".jpg"
            safe_name = "cover" + ext
        dest = os.path.join(path, safe_name)
        with open(dest, "wb") as f:
            f.write(file_data)
        invalidate_library()
        return safe_name

    def _library_delete(self, artist: str, album: str, filename: str):
        path = self._safe_path(artist, album, filename)
        if path and os.path.isfile(path):
            os.remove(path)
        invalidate_library()
        qa = urllib.parse.quote(artist)
        qb = urllib.parse.quote(album)
        self._redirect(f"/library/album?a={qa}&b={qb}&msg=Deleted+{urllib.parse.quote(filename)}")

    def _library_delete_album(self, artist: str, album: str):
        import shutil
        path = self._safe_path(artist, album)
        if path and os.path.isdir(path):
            shutil.rmtree(path)
            ap = self._safe_path(artist)
            if ap and os.path.isdir(ap) and not os.listdir(ap):
                os.rmdir(ap)
        invalidate_library()
        self._redirect("/library")

    def _library_delete_artist(self, artist: str):
        import shutil
        path = self._safe_path(artist)
        if path and os.path.isdir(path):
            shutil.rmtree(path)
        invalidate_library()
        self._redirect("/library")

    def _library_rename_album(self, artist: str, album: str,
                               new_artist: str, new_album: str):
        if not new_artist or not new_album:
            self._redirect("/library")
            return
        src = self._safe_path(artist, album)
        dst = self._safe_path(new_artist, new_album)
        if not src or not dst or not os.path.isdir(src):
            self._redirect("/library")
            return
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(src, dst)
        if artist != new_artist:
            ap = self._safe_path(artist)
            if ap and os.path.isdir(ap) and not os.listdir(ap):
                os.rmdir(ap)
        invalidate_library()
        qa = urllib.parse.quote(new_artist)
        qb = urllib.parse.quote(new_album)
        self._redirect(f"/library/album?a={qa}&b={qb}&msg=Renamed+successfully")

    def _library_rename_artist(self, artist: str, new_artist: str):
        if not new_artist or artist == new_artist:
            self._redirect("/library")
            return
        src = self._safe_path(artist)
        dst = self._safe_path(new_artist)
        if not src or not dst or not os.path.isdir(src):
            self._redirect("/library")
            return
        os.rename(src, dst)
        invalidate_library()
        self._redirect("/library")

    def _library_rename_file(self, artist: str, album: str,
                              old_name: str, new_name: str):
        new_name = os.path.basename(new_name.strip())
        if not new_name:
            qa, qb = urllib.parse.quote(artist), urllib.parse.quote(album)
            self._redirect(f"/library/album?a={qa}&b={qb}")
            return
        # Keep original extension if user didn't type one
        old_ext = os.path.splitext(old_name)[1]
        new_ext = os.path.splitext(new_name)[1]
        if old_ext and not new_ext:
            new_name = new_name + old_ext
        if new_name == old_name:
            qa, qb = urllib.parse.quote(artist), urllib.parse.quote(album)
            self._redirect(f"/library/album?a={qa}&b={qb}")
            return
        src = self._safe_path(artist, album, old_name)
        dst = self._safe_path(artist, album, new_name)
        if src and dst and os.path.isfile(src) and not os.path.exists(dst):
            os.rename(src, dst)
            invalidate_library()
        qa, qb = urllib.parse.quote(artist), urllib.parse.quote(album)
        self._redirect(f"/library/album?a={qa}&b={qb}&msg=Renamed+to+{urllib.parse.quote(new_name)}")

    def log_message(self, *args):
        pass


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"Metadata editor → http://0.0.0.0:{PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
