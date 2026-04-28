#!/usr/bin/env python3
"""
library_manager.py - Manage the JSON-based HD music library.

Expected library structure:
    LIBRARY_ROOT/
        library.json           ← master index (auto-generated)
        Artist Name/
            Album Title (Year)/
                01 - Track.flac
                02 - Track.flac
                album.json     ← per-album metadata cache (optional)
                cover.jpg      ← local cover art (optional)

library.json schema:
[
  {
    "artist": "Pink Floyd",
    "title":  "The Dark Side of the Moon",
    "year":   "1973",
    "path":   "/music/Pink Floyd/The Dark Side of the Moon (1973)",
    "tracks": [
      {"num": 1, "title": "Speak to Me", "duration": "1:30",
       "path": "/music/.../01 - Speak to Me.flac"}
    ]
  }
]
"""

import json
import logging
import os
import re
from typing import List, Dict, Optional

log = logging.getLogger("library")

LIBRARY_ROOT = os.environ.get("MUSIC_LIBRARY", os.path.expanduser("~/music"))
LIBRARY_JSON = os.path.join(LIBRARY_ROOT, "library.json")

AUDIO_EXTENSIONS = {".flac", ".wav", ".aiff", ".aif", ".alac", ".mp3", ".ogg", ".m4a"}


class LibraryManager:
    def __init__(self, root: str = LIBRARY_ROOT):
        self.root = root
        self._albums: Optional[List[Dict]] = None
        self._json_mtime: float = 0

    # ── Public API ────────────────────────────────────────────────────────────
    def get_albums(self) -> List[Dict]:
        """Return album list, reloading if library.json was changed on disk."""
        try:
            mtime = os.path.getmtime(LIBRARY_JSON)
        except OSError:
            mtime = 0
        if self._albums is None or mtime != self._json_mtime:
            self._albums = self._load_or_scan()
            self._json_mtime = mtime
        return self._albums

    def refresh(self):
        """Force a full rescan."""
        self._albums = self._scan()
        self._save_json(self._albums)
        return self._albums

    def find_album(self, artist: str, title: str) -> Optional[Dict]:
        for a in self.get_albums():
            if a.get("artist", "").lower() == artist.lower() and \
               a.get("title",  "").lower() == title.lower():
                return a
        return None

    # ── Persistence ───────────────────────────────────────────────────────────
    def _load_or_scan(self) -> List[Dict]:
        if os.path.exists(LIBRARY_JSON):
            try:
                with open(LIBRARY_JSON, encoding="utf-8") as f:
                    data = json.load(f)
                # Always refresh art paths — cover.jpg may have been added after
                # the initial scan without regenerating library.json.
                for album in data:
                    path = album.get("path", "")
                    if path:
                        fresh = self._find_local_art(path)
                        if fresh:
                            album["art_path"] = fresh
                log.info("Loaded library.json (%d albums)", len(data))
                return data
            except Exception as exc:
                log.warning("library.json invalid, rescanning: %s", exc)

        albums = self._scan()
        self._save_json(albums)
        return albums

    def _save_json(self, albums: List[Dict]):
        try:
            with open(LIBRARY_JSON, "w", encoding="utf-8") as f:
                json.dump(albums, f, indent=2, ensure_ascii=False)
            log.info("Saved library.json (%d albums)", len(albums))
        except Exception as exc:
            log.error("Could not save library.json: %s", exc)

    # ── Scanner ───────────────────────────────────────────────────────────────
    def _scan(self) -> List[Dict]:
        """Walk LIBRARY_ROOT and build album list."""
        albums = []
        if not os.path.isdir(self.root):
            log.warning("Library root not found: %s", self.root)
            return albums

        for artist_dir in sorted(os.scandir(self.root), key=lambda e: e.name.lower()):
            if not artist_dir.is_dir() or artist_dir.name.startswith("."):
                continue
            for album_dir in sorted(os.scandir(artist_dir.path), key=lambda e: e.name.lower()):
                if not album_dir.is_dir() or album_dir.name.startswith("."):
                    continue
                album = self._parse_album_dir(artist_dir.name, album_dir)
                if album:
                    albums.append(album)

        log.info("Scan complete: %d albums found", len(albums))
        return albums

    def _parse_album_dir(self, artist: str, album_entry: os.DirEntry) -> Optional[Dict]:
        """Extract album metadata from a directory."""
        # Check for per-album JSON
        json_path = os.path.join(album_entry.path, "album.json")
        if os.path.exists(json_path):
            try:
                with open(json_path, encoding="utf-8") as f:
                    meta = json.load(f)
                meta.setdefault("path", album_entry.path)
                meta.setdefault("artist", artist)
                if "tracks" not in meta:
                    meta["tracks"] = self._scan_tracks(album_entry.path)
                # local art
                meta.setdefault("art_path", self._find_local_art(album_entry.path))
                return meta
            except Exception as exc:
                log.debug("album.json parse error %s: %s", album_entry.path, exc)

        # Parse title & year from directory name
        title, year = self._parse_dir_name(album_entry.name)

        tracks = self._scan_tracks(album_entry.path)
        if not tracks:
            return None   # skip empty dirs

        return {
            "artist":   artist,
            "title":    title,
            "year":     year,
            "path":     album_entry.path,
            "tracks":   tracks,
            "art_path": self._find_local_art(album_entry.path),
        }

    def _parse_dir_name(self, name: str):
        """Try to extract (title, year) from 'Title (Year)' or 'Year - Title'."""
        # Pattern: "Title (YYYY)"
        m = re.match(r"^(.+?)\s*\((\d{4})\)\s*$", name)
        if m:
            return m.group(1).strip(), m.group(2)
        # Pattern: "YYYY - Title"
        m = re.match(r"^(\d{4})\s*[-–]\s*(.+)$", name)
        if m:
            return m.group(2).strip(), m.group(1)
        return name, ""

    def _scan_tracks(self, album_path: str) -> List[Dict]:
        tracks = []
        try:
            entries = sorted(os.scandir(album_path), key=lambda e: e.name.lower())
        except PermissionError:
            return tracks

        for entry in entries:
            if not entry.is_file():
                continue
            _, ext = os.path.splitext(entry.name.lower())
            if ext not in AUDIO_EXTENSIONS:
                continue

            title, num = self._parse_track_filename(entry.name)
            tracks.append({
                "num":      num or len(tracks) + 1,
                "title":    title,
                "duration": "",   # could be read with mutagen if installed
                "path":     entry.path,
            })

        # Sort by track number
        tracks.sort(key=lambda t: t["num"])
        return tracks

    def _parse_track_filename(self, filename: str):
        """Extract (title, track_number) from '01 - Title.flac' etc."""
        base = os.path.splitext(filename)[0]
        m = re.match(r"^(\d+)\s*[-–.]\s*(.+)$", base)
        if m:
            return m.group(2).strip(), int(m.group(1))
        return base, None

    def _find_local_art(self, album_path: str) -> Optional[str]:
        for name in ("cover.jpg", "cover.png", "folder.jpg", "folder.png",
                     "front.jpg", "front.png", "AlbumArt.jpg"):
            p = os.path.join(album_path, name)
            if os.path.exists(p):
                return p
        return None
