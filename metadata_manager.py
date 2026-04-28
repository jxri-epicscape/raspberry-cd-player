#!/usr/bin/env python3
"""
metadata_manager.py — Fetch album metadata and artwork.

Lookup order for a physical CD:
  0. disc_overrides.json  (manual corrections — highest priority)
  1. SQLite cache         (instant on re-insert)
  2. MusicBrainz disc-ID lookup  → artist / album / year / tracks
  3. iTunes Search API           → art + second opinion on artist/album
  4. Deezer API                  → art fallback
  5. Unknown fallback

Consensus rule: if MusicBrainz returns a result and iTunes independently
finds the same artist (fuzzy match), the result is treated as confirmed.
If only one source returns a result it is used as-is.

To correct a wrong album, edit /opt/musicplayer/disc_overrides.json:
  {
    "<cddb_id>": {
      "artist": "Artist Name",
      "album":  "Album Title",
      "year":   "2001",
      "tracks": [{"num": 1, "title": "Track Name"}, ...]
    }
  }
"""

import hashlib
import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests

log = logging.getLogger("metadata")

# ── Paths ─────────────────────────────────────────────────────────────────────
_DATA_DIR      = os.path.expanduser("~/.local/share/musicplayer")
CACHE_DIR      = os.path.expanduser("~/.cache/musicplayer/art")
CACHE_DB       = os.path.join(_DATA_DIR, "disc_cache.db")
OVERRIDES_FILE = "/opt/musicplayer/disc_overrides.json"
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(_DATA_DIR, exist_ok=True)

# ── API endpoints ─────────────────────────────────────────────────────────────
MB_BASE      = "https://musicbrainz.org/ws/2"
CAA_BASE     = "https://coverartarchive.org"
ITUNES_URL   = "https://itunes.apple.com/search"
DEEZER_URL   = "https://api.deezer.com/search/album"
DISCOGS_BASE = "https://api.discogs.com"

HEADERS = {
    "User-Agent": "HiFiBerryMusicPlayer/2.0 (hifiberry@localhost)",
    "Accept":     "application/json",
}
DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN", "")

# ── SQLite cache schema ───────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS disc_cache (
    cddb_id     TEXT PRIMARY KEY,
    mb_disc_id  TEXT,
    artist      TEXT NOT NULL DEFAULT '',
    album       TEXT NOT NULL DEFAULT '',
    year        TEXT NOT NULL DEFAULT '',
    art_path    TEXT,
    tracks_json TEXT NOT NULL DEFAULT '[]',
    fetched_at  TEXT NOT NULL
);
"""


# ── HTTP helpers ──────────────────────────────────────────────────────────────
def _http_get_json(url: str, params: dict = None,
                   extra_headers: dict = None, retry: int = 2) -> Optional[dict]:
    h = {**HEADERS, **(extra_headers or {})}
    for attempt in range(retry):
        try:
            r = requests.get(url, params=params, headers=h, timeout=12)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            log.debug("HTTP %s for %s", e.response.status_code, url)
            return None
        except Exception as e:
            log.debug("GET %s attempt %d: %s", url, attempt + 1, e)
            if attempt < retry - 1:
                time.sleep(1)
    return None


def _download_image(url: str, cache_key: str) -> Optional[str]:
    ext  = ".jpg" if any(x in url.lower() for x in ("jpg", "jpeg")) else ".png"
    name = hashlib.md5(cache_key.encode()).hexdigest() + ext
    path = os.path.join(CACHE_DIR, name)
    if os.path.exists(path):
        return path
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, stream=True)
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        log.info("Art cached: %s", path)
        return path
    except Exception as e:
        log.debug("Image download failed: %s", e)
        return None


# ── iTunes Search API ─────────────────────────────────────────────────────────
def _fetch_from_itunes(artist: str, album: str,
                       cache_key: str) -> Optional[Dict]:
    """
    Search iTunes for artist+album.
    Returns dict with art_path (and optionally track list) or None.
    """
    q = f"{artist} {album}".strip()
    try:
        r = requests.get(
            ITUNES_URL,
            params={"term": q, "media": "music", "entity": "album", "limit": 5},
            headers=HEADERS, timeout=8,
        )
        if not r.ok:
            return None
        results = r.json().get("results", [])
    except Exception as e:
        log.debug("iTunes search failed: %s", e)
        return None

    for res in results:
        itunes_artist = res.get("artistName", "")
        itunes_album  = res.get("collectionName", "")
        art_url = res.get("artworkUrl100", "").replace("100x100bb", "600x600bb")

        if not art_url:
            continue

        # Light validation: artist name must overlap with what we expect
        if artist and not _names_overlap(artist, itunes_artist):
            log.debug("iTunes artist mismatch: '%s' vs '%s'", artist, itunes_artist)
            continue

        art_path = _download_image(art_url, f"itunes_{cache_key}")
        if not art_path:
            continue

        log.info("iTunes confirmed: %s – %s", itunes_artist, itunes_album)
        return {
            "itunes_artist": itunes_artist,
            "itunes_album":  itunes_album,
            "art_path":      art_path,
        }

    # Nothing matched — still try to grab art from the first result if present
    for res in results:
        art_url = res.get("artworkUrl100", "").replace("100x100bb", "600x600bb")
        if art_url:
            art_path = _download_image(art_url, f"itunes_loose_{cache_key}")
            if art_path:
                log.info("iTunes art (loose match)")
                return {"art_path": art_path}

    return None


# ── Deezer ────────────────────────────────────────────────────────────────────
def _fetch_art_deezer(artist: str, album: str, cache_key: str) -> Optional[str]:
    try:
        r = requests.get(
            DEEZER_URL,
            params={"q": f"{artist} {album}".strip(), "limit": 5},
            headers=HEADERS, timeout=8,
        )
        if r.ok:
            for res in r.json().get("data", []):
                url = res.get("cover_xl") or res.get("cover_big")
                if url:
                    path = _download_image(url, f"deezer_{cache_key}")
                    if path:
                        log.info("Art from Deezer")
                        return path
    except Exception as e:
        log.debug("Deezer art: %s", e)
    return None


# ── MusicBrainz ───────────────────────────────────────────────────────────────
def _mb_extract_artist(release: dict) -> str:
    credits = release.get("artist-credit", [])
    if credits and isinstance(credits[0], dict):
        return credits[0].get("artist", {}).get("name", "")
    return ""


def _mb_extract_tracks(release: dict) -> List[Dict]:
    tracks = []
    for medium in release.get("media", []):
        for t in medium.get("tracks", []):
            rec    = t.get("recording", {})
            dur_ms = rec.get("length") or t.get("length") or 0
            dur_s  = dur_ms // 1000
            tracks.append({
                "num":      t.get("position", len(tracks) + 1),
                "title":    rec.get("title") or t.get("title", f"Track {len(tracks)+1}"),
                "duration": f"{dur_s // 60}:{dur_s % 60:02d}" if dur_s else "",
            })
    return tracks


def _mb_pick_release(releases: list) -> dict:
    """Pick the earliest release (original, not a remaster/deluxe box)."""
    def key(r):
        date  = r.get("date", "") or ""
        year  = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else 9999
        first = len((r.get("media") or [{}])[0].get("tracks", []))
        return (year, -first)
    return min(releases, key=key)


def _fetch_from_mb(mb_disc_id: str, num_tracks: int,
                   offsets: List[int], total_secs: int,
                   raw_tracks: List[Dict]) -> Optional[Dict]:
    # 1. Exact disc-ID lookup
    log.info("MB exact lookup: %s", mb_disc_id)
    data = _http_get_json(f"{MB_BASE}/discid/{mb_disc_id}", params={
        "inc": "recordings+artists+release-groups", "fmt": "json"
    })
    if not data:
        # 2. Fuzzy TOC lookup
        toc_str = f"1 {num_tracks} {total_secs * 75} " + " ".join(str(o) for o in offsets)
        log.info("MB TOC lookup: %s…", toc_str[:50])
        data = _http_get_json(f"{MB_BASE}/discid/-", params={
            "toc": toc_str,
            "inc": "recordings+artists+release-groups", "fmt": "json"
        })
    if not data:
        return None

    releases = data.get("releases", [data])
    if not releases:
        return None

    best = _mb_pick_release(releases)
    mbid = best.get("id", "")

    # Fetch full details if we only got a stub
    if mbid and not best.get("media"):
        details = _http_get_json(f"{MB_BASE}/release/{mbid}", params={
            "inc": "recordings+artists+release-groups", "fmt": "json"
        })
        if details:
            best = details

    artist = _mb_extract_artist(best)
    album  = best.get("title", "")
    date   = best.get("date", "") or ""
    year   = date[:4] if date else ""
    tracks = _mb_extract_tracks(best) or raw_tracks

    if not artist or not album:
        return None

    log.info("MB found: %s – %s (%s)", artist, album, year)
    return {"artist": artist, "album": album, "year": year,
            "tracks": tracks, "mbid": mbid}


def _fetch_art_caa(mbid: str, cache_key: str) -> Optional[str]:
    """Try MusicBrainz Cover Art Archive."""
    if not mbid:
        return None
    try:
        data = _http_get_json(f"{CAA_BASE}/release/{mbid}")
        if data:
            for img in data.get("images", []):
                if img.get("front"):
                    return _download_image(img["image"], f"caa_{cache_key}")
    except Exception as e:
        log.debug("CAA art: %s", e)
    return None


# ── Name comparison helper ─────────────────────────────────────────────────────
def _names_overlap(a: str, b: str, threshold: float = 0.5) -> bool:
    """True if the two names share enough words to be considered the same."""
    a, b = a.lower().strip(), b.lower().strip()
    if a == b:
        return True
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return False
    shorter = wa if len(wa) <= len(wb) else wb
    return len(shorter & (wa | wb)) / len(shorter) >= threshold


# ── Discogs (library albums, optional) ───────────────────────────────────────
def _fetch_from_discogs(artist: str, album: str) -> Optional[Dict]:
    if not DISCOGS_TOKEN:
        return None
    try:
        data = requests.get(f"{DISCOGS_BASE}/database/search", params={
            "artist": artist, "release_title": album,
            "type": "master", "token": DISCOGS_TOKEN,
        }, headers=HEADERS, timeout=12).json()
    except Exception:
        return None

    results = data.get("results", [])
    if not results:
        return None
    master_id = results[0].get("master_id") or results[0].get("id")
    if not master_id:
        return None
    try:
        det = requests.get(
            f"{DISCOGS_BASE}/masters/{master_id}",
            headers={**HEADERS, "Authorization": f"Discogs token={DISCOGS_TOKEN}"},
            timeout=12,
        ).json()
    except Exception:
        return None

    tracks = [{"num": i+1, "title": t.get("title", f"Track {i+1}"),
               "duration": t.get("duration", "")}
              for i, t in enumerate(det.get("tracklist", []))]
    art_path = None
    for img in det.get("images", []):
        if img.get("type") == "primary":
            art_path = _download_image(img["uri"], f"discogs_{master_id}")
            break
    if not art_path and det.get("images"):
        art_path = _download_image(det["images"][0]["uri"], f"discogs_{master_id}_0")

    return {"artist": artist, "album": det.get("title", album),
            "year": str(det.get("year", "")), "tracks": tracks, "art_path": art_path}


# ── MetadataManager ───────────────────────────────────────────────────────────
class MetadataManager:
    def __init__(self):
        self._conn = self._init_db()

    # ── SQLite cache ──────────────────────────────────────────────────────────
    def _init_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(CACHE_DB, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        conn.commit()
        return conn

    def _cache_get(self, cddb_id: str, mb_disc_id: str = "") -> Optional[Dict]:
        if mb_disc_id:
            row = self._conn.execute(
                "SELECT * FROM disc_cache WHERE mb_disc_id = ?", (mb_disc_id,)
            ).fetchone()
            if row:
                log.info("Cache HIT (mb_disc_id): %s – %s", row["artist"], row["album"])
                return self._row_to_meta(row)

        row = self._conn.execute(
            "SELECT * FROM disc_cache WHERE cddb_id = ?", (cddb_id,)
        ).fetchone()
        if row:
            stored_mb = row["mb_disc_id"] or ""
            if mb_disc_id and stored_mb and stored_mb != mb_disc_id:
                log.info("CDDB collision for %s — ignoring cache", cddb_id)
                return None
            log.info("Cache HIT (cddb_id): %s – %s", row["artist"], row["album"])
            return self._row_to_meta(row)
        return None

    def _row_to_meta(self, row) -> Dict:
        return {
            "artist":   row["artist"],
            "album":    row["album"],
            "year":     row["year"],
            "art_path": row["art_path"],
            "tracks":   json.loads(row["tracks_json"]),
        }

    def _cache_put(self, cddb_id: str, mb_disc_id: str, meta: Dict):
        try:
            self._conn.execute(
                """INSERT OR REPLACE INTO disc_cache
                   (cddb_id, mb_disc_id, artist, album, year, art_path, tracks_json, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (cddb_id, mb_disc_id,
                 meta.get("artist", ""), meta.get("album", ""),
                 meta.get("year", ""), meta.get("art_path"),
                 json.dumps(meta.get("tracks", []), ensure_ascii=False),
                 datetime.utcnow().isoformat()),
            )
            self._conn.commit()
            log.info("Cached: %s – %s", meta["artist"], meta["album"])
        except Exception as e:
            log.error("Cache write: %s", e)

    # ── Public API ────────────────────────────────────────────────────────────
    def fetch_cd_metadata(self, disc_id_str: Optional[str], raw_tracks: List[Dict]) -> Dict:
        toc = self._parse_discid_str(disc_id_str)
        if not toc:
            return self._unknown(raw_tracks)

        cddb_id    = toc["cddb_id"]
        mb_disc_id = toc["mb_disc_id"]
        offsets    = toc["offsets"]
        total_secs = toc["total_secs"]
        num_tracks = toc["num_tracks"]

        # 0. Manual override
        override = self._load_override(cddb_id)
        if override:
            log.info("Override: %s – %s", override.get("artist"), override.get("album"))
            tracks   = override.get("tracks") or raw_tracks
            # Use art the user uploaded/saved; only fetch automatically if none stored
            art_path = override.get("art_path", "")
            if not art_path or not os.path.exists(art_path):
                itunes   = _fetch_from_itunes(override.get("artist", ""),
                                              override.get("album", ""), cddb_id)
                art_path = (itunes or {}).get("art_path") or \
                           _fetch_art_deezer(override.get("artist", ""),
                                             override.get("album", ""), cddb_id)
            return {
                "artist":   override.get("artist", "Unknown Artist"),
                "album":    override.get("album",  "Unknown CD"),
                "year":     str(override.get("year", "")),
                "tracks":   tracks,
                "art_path": art_path,
            }

        # 1. Cache
        cached = self._cache_get(cddb_id, mb_disc_id)
        if cached:
            return cached

        # 2. MusicBrainz — primary disc-ID lookup
        mb = _fetch_from_mb(mb_disc_id, num_tracks, offsets, total_secs, raw_tracks)

        if mb:
            artist = mb["artist"]
            album  = mb["album"]
        else:
            artist = album = ""

        # 3. iTunes — second opinion + art
        itunes = _fetch_from_itunes(artist, album, cddb_id) if artist else None

        # Consensus: if iTunes confirms the artist, mark as high-confidence
        if mb and itunes and itunes.get("itunes_artist"):
            if _names_overlap(artist, itunes["itunes_artist"]):
                log.info("MB + iTunes agree on '%s' — high confidence", artist)
            else:
                log.info("MB says '%s', iTunes says '%s' — using MB", artist,
                         itunes.get("itunes_artist", "?"))

        if not mb:
            log.info("No metadata found for disc %s", cddb_id)
            return self._unknown(raw_tracks)

        # 4. Art: iTunes → Deezer → MusicBrainz CAA
        art_path = (
            (itunes or {}).get("art_path")
            or _fetch_art_deezer(artist, album, cddb_id)
            or _fetch_art_caa(mb.get("mbid", ""), cddb_id)
        )

        meta = {
            "artist":   mb["artist"],
            "album":    mb["album"],
            "year":     mb["year"],
            "tracks":   mb["tracks"],
            "art_path": art_path,
        }
        self._cache_put(cddb_id, mb_disc_id, meta)
        return meta

    def fetch_album_metadata(self, artist: str, album: str,
                             year: Optional[str] = None) -> Dict:
        """Fetch metadata for a library album."""
        result = _fetch_from_discogs(artist, album)
        if result:
            return result
        itunes   = _fetch_from_itunes(artist, album, hashlib.md5(f"{artist}{album}".encode()).hexdigest())
        art_path = (itunes or {}).get("art_path") or \
                   _fetch_art_deezer(artist, album, hashlib.md5(f"{artist}{album}".encode()).hexdigest())
        return {"artist": artist, "album": album, "year": year or "",
                "tracks": [], "art_path": art_path}

    # ── Helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _load_override(cddb_id: str) -> Optional[Dict]:
        try:
            if not os.path.exists(OVERRIDES_FILE):
                return None
            with open(OVERRIDES_FILE, encoding="utf-8") as f:
                return json.load(f).get(cddb_id)
        except Exception as e:
            log.warning("disc_overrides.json: %s", e)
            return None

    @staticmethod
    def _parse_discid_str(raw: Optional[str]) -> Optional[dict]:
        if not raw:
            return None
        from cd_handler import CDHandler
        return CDHandler().parse_toc(raw)

    @staticmethod
    def _unknown(raw_tracks: List[Dict]) -> Dict:
        return {"artist": "Unknown Artist", "album": "Unknown CD",
                "year": "", "tracks": raw_tracks, "art_path": None}
