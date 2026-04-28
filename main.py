#!/usr/bin/env python3
"""
main.py - Core state machine for the HiFiBerry Music Player
"""

import re
import sys
import time
import threading
import logging
import os

CD_VAULT = os.path.expanduser("~/cd_vault")
VAULT_AUDIO_EXTS = {".flac", ".wav", ".mp3", ".ogg", ".m4a"}

import pygame

from player import Player
from ui import UI
from metadata_manager import MetadataManager
from stats_manager import StatsManager
from cd_handler import CDHandler
from input_handler import InputHandler, InputEvent
from library_manager import LibraryManager

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("main")

# ── States ────────────────────────────────────────────────────────────────────
class State:
    MAIN_MENU       = "main_menu"
    LIBRARY_BROWSE  = "library_browse"
    CD_LOADING      = "cd_loading"
    PLAYBACK        = "playback"
    WRAPPED_SUMMARY = "wrapped_summary"


class MusicPlayer:
    def __init__(self):
        pygame.init()
        pygame.display.set_caption("HiFiBerry Player")

        self.player   = Player()
        self.ui       = UI()
        self.metadata = MetadataManager()
        self.stats    = StatsManager()
        self.cd       = CDHandler()
        self.library  = LibraryManager()
        self.input    = InputHandler()

        self.state          = State.MAIN_MENU
        self.prev_state     = None
        self.album_info     = {}
        self.tracklist      = []
        self.current_track  = 0
        self.paused         = False
        self.source         = None

        self.main_menu_items = ["Play CD", "Library", "Music Wrapped", "Quit"]
        self.main_menu_sel   = 0
        self.library_albums  = []
        self.library_sel     = 0

        self._running        = True
        self._last_play_time = 0
        self._last_chapter_check = 0
        self._disc_id_str    = None   # stored so hot-reload can re-fetch
        self._override_mtime = 0      # mtime of disc_overrides.json at last load

        self._cd_monitor = threading.Thread(target=self._monitor_cd, daemon=True)
        self._cd_monitor.start()

    def _monitor_cd(self):
        was_present = False
        while self._running:
            present = self.cd.is_disc_present()
            if present and not was_present:
                log.info("CD inserted – triggering load")
                self._schedule(self._load_cd)
                was_present = True
            elif not present and was_present:
                if self.state != State.CD_LOADING:
                    log.info("CD removed")
                    if self.state == State.PLAYBACK and self.source in ("cd", "data_cd", "vault"):
                        self.player.stop()
                        if self.source == "data_cd":
                            self.cd.unmount()
                        self.state = State.MAIN_MENU
                    was_present = False
            was_present = present
            time.sleep(2)

    def _schedule(self, fn, *args):
        ev = pygame.event.Event(pygame.USEREVENT, {"fn": fn, "args": args})
        pygame.event.post(ev)

    def _load_cd(self):
        self.state = State.CD_LOADING
        self.ui.show_loading("Reading CD…")

        # Set drive speed so disc is ready before any detection
        self.cd.set_speed()

        # Try data disc first (blkid check) — fallback is tried again below
        # in case blkid requires root and silently fails.
        if self.cd.is_data_disc():
            data_files = self.cd.get_data_files()
            if data_files:
                self._load_data_cd(data_files)
                return
            self.ui.show_message("Data disc – no audio files found", duration=3)
            self.state = State.MAIN_MENU
            return

        tracks = self.cd.get_tracks()

        if not tracks:
            # blkid may have failed (permission) — try mounting as data disc anyway
            log.info("No audio tracks — attempting data-disc fallback")
            data_files = self.cd.get_data_files()
            if data_files:
                self._load_data_cd(data_files)
                return
            log.warning("No tracks found on CD")
            self.ui.show_message("Read error or empty CD", duration=3)
            self.state = State.MAIN_MENU
            return

        disc_id = self.cd.get_disc_id()
        self._disc_id_str = disc_id

        # Write cddb_id + vault_key to temp files for meta_editor
        toc       = self.cd.parse_toc(disc_id) if disc_id else None
        vault_key = toc["mb_disc_id"] if toc else ((disc_id or "").split()[0])
        try:
            cddb_id = (disc_id or "").split()[0] if disc_id else ""
            with open("/tmp/musicplayer_current_disc", "w") as _f:
                _f.write(cddb_id)
            with open("/tmp/musicplayer_vault_key", "w") as _f:
                _f.write(vault_key)
        except Exception:
            pass

        # ── Vault check: play ripped copy if available ─────────────────────
        if vault_key:
            vault_dir = os.path.join(CD_VAULT, vault_key)
            if os.path.isdir(vault_dir):
                vault_files = sorted(
                    os.path.join(vault_dir, fn)
                    for fn in os.listdir(vault_dir)
                    if os.path.splitext(fn)[1].lower() in VAULT_AUDIO_EXTS
                )
                if vault_files:
                    log.info("Vault hit for %s — playing ripped copy", vault_key)
                    self.ui.show_loading("Loading from vault…")
                    meta = self.metadata.fetch_cd_metadata(disc_id, tracks)
                    meta_tracks = meta.get("tracks", [])
                    tracklist = []
                    for i, path in enumerate(vault_files):
                        mt = meta_tracks[i] if i < len(meta_tracks) else {}
                        d  = mt.get("duration", 0)
                        if isinstance(d, (int, float)):
                            d = f"{int(d // 60):02d}:{int(d % 60):02d}"
                        tracklist.append({
                            "num":      i + 1,
                            "title":    mt.get("title", f"Track {i+1:02d}"),
                            "duration": d,
                            "path":     path,
                        })
                    self.album_info    = {**meta, "tracks": tracklist}
                    self.tracklist     = tracklist
                    self.source        = "vault"
                    self.current_track = 0
                    self._start_playback()
                    return

        meta    = self.metadata.fetch_cd_metadata(disc_id, tracks)
        try:
            self._override_mtime = os.path.getmtime(
                "/opt/musicplayer/disc_overrides.json")
        except OSError:
            self._override_mtime = 0

        self.album_info = meta
        self.tracklist  = meta.get("tracks", tracks)

        # Normalise durations to MM:SS strings
        for t in self.tracklist:
            d = t.get("duration", 0)
            if isinstance(d, (int, float)):
                t["duration"] = f"{int(d // 60):02d}:{int(d % 60):02d}"
            else:
                t["duration"] = str(d)

        self.source        = "cd"
        self.current_track = 0
        self._start_playback()

    def _load_data_cd(self, files: list):
        """Load an MP3/data disc — read ID3 tags, fetch art, start playback."""
        self.ui.show_loading("Reading MP3 disc…")
        try:
            from mutagen import File as MutagenFile
            use_tags = True
        except ImportError:
            use_tags = False

        tracklist = []
        disc_artist = disc_album = disc_year = ""

        for i, path in enumerate(files):
            title = artist = album = year = ""
            if use_tags:
                try:
                    audio = MutagenFile(path, easy=True)
                    if audio:
                        title  = (audio.get("title")  or [""])[0]
                        artist = (audio.get("artist") or [""])[0]
                        album  = (audio.get("album")  or [""])[0]
                        year   = str((audio.get("date") or [""])[0])[:4]
                        if not disc_artist and artist:
                            disc_artist = artist
                        if not disc_album and album:
                            disc_album  = album
                        if not disc_year and year:
                            disc_year   = year
                except Exception:
                    pass
            if not title:
                # Fall back to filename, strip leading track number
                name  = os.path.splitext(os.path.basename(path))[0]
                m     = re.match(r"^\d+[\s.\-_]+(.+)$", name)
                title = m.group(1).strip() if m else name
            tracklist.append({"num": i + 1, "title": title, "path": path, "duration": ""})

        # Infer album from mount-point / disc label if tags gave nothing
        if not disc_album:
            disc_album = os.path.basename(files[0].rsplit("/", 2)[0]) if files else "MP3 Disc"
        if not disc_artist:
            disc_artist = "Unknown Artist"

        log.info("MP3 disc: %s – %s (%d tracks)", disc_artist, disc_album, len(tracklist))

        # Fetch cover art via normal metadata pipeline
        meta = self.metadata.fetch_album_metadata(disc_artist, disc_album, disc_year or None)

        self.album_info = {
            "artist":   disc_artist,
            "album":    disc_album,
            "year":     disc_year,
            "art_path": meta.get("art_path"),
            "tracks":   tracklist,
        }
        self.tracklist     = tracklist
        self.source        = "data_cd"   # file-path playback from physical disc
        self.current_track = 0
        self._start_playback()

    def _load_library_album(self, album):
        self.ui.show_loading(f"Loading {album.get('title', '?')}…")
        local_tracks = album.get("tracks", [])
        meta = self.metadata.fetch_album_metadata(
            album.get("artist", ""), album.get("title", ""), album.get("year")
        )
        # MusicBrainz tracks have no file paths — always use the local tracks
        # which come from the library scanner and contain the actual file paths.
        meta["tracks"] = local_tracks
        # Local cover.jpg always wins — the user placed it intentionally.
        # Fall back to MusicBrainz art only when no local file exists.
        meta["art_path"] = album.get("art_path") or meta.get("art_path")
        self.album_info    = meta
        self.tracklist     = local_tracks
        self.source        = "library"
        self.current_track = 0
        self._start_playback()

    def _start_playback(self):
        self.state  = State.PLAYBACK
        self.paused = False
        self._play_track(self.current_track)

    def _play_track(self, idx: int):
        if not self.tracklist:
            return
        idx = max(0, min(idx, len(self.tracklist) - 1))
        self.current_track = idx
        track = self.tracklist[idx]

        uri = f"cdda://{idx + 1}" if self.source == "cd" else track.get("path", "")

        self._last_play_time = time.monotonic()
        self.player.play(uri)
        self.paused = False
        self.stats.log_play(
            artist=self.album_info.get("artist", "Unknown"),
            title=track.get("title", f"Track {idx + 1}"),
            album=self.album_info.get("album", "Unknown"),
        )
        log.info("Playing track %d: %s", idx + 1, track.get("title"))

    def _handle_input(self, event: InputEvent):
        s = self.state

        # ── Main menu ─────────────────────────────────────────────────────────
        if s == State.MAIN_MENU:
            if event == InputEvent.UP:
                self.main_menu_sel = (self.main_menu_sel - 1) % len(self.main_menu_items)
            elif event == InputEvent.DOWN:
                self.main_menu_sel = (self.main_menu_sel + 1) % len(self.main_menu_items)
            elif event == InputEvent.FIRE:
                self._main_menu_select()
            elif event == InputEvent.BACK:
                pass   # nothing to go back to from main menu

        # ── Library browser ───────────────────────────────────────────────────
        elif s == State.LIBRARY_BROWSE:
            if event == InputEvent.UP:
                self.library_sel = max(0, self.library_sel - 1)
            elif event == InputEvent.DOWN:
                self.library_sel = min(len(self.library_albums) - 1, self.library_sel + 1)
            elif event == InputEvent.FIRE:
                if self.library_albums:
                    self._load_library_album(self.library_albums[self.library_sel])
            elif event == InputEvent.BACK:
                self.state = State.MAIN_MENU

        # ── Playback ──────────────────────────────────────────────────────────
        elif s == State.PLAYBACK:
            if event == InputEvent.FIRE:
                # Toggle pause / resume
                self.paused = not self.paused
                if self.paused:
                    self.player.pause()
                else:
                    self.player.resume()

            elif event == InputEvent.UP:
                self._play_track(self.current_track - 1)

            elif event == InputEvent.DOWN:
                self._play_track(self.current_track + 1)

            elif event == InputEvent.LEFT:
                self.player.volume_down()
                vol = self.player.get_volume()
                self.ui.show_message(f"Volume  {vol}%", duration=1.5)

            elif event == InputEvent.RIGHT:
                self.player.volume_up()
                vol = self.player.get_volume()
                self.ui.show_message(f"Volume  {vol}%", duration=1.5)

            elif event == InputEvent.BACK:
                self.player.stop()
                self.state = self.prev_state or State.MAIN_MENU

        # ── Wrapped summary ───────────────────────────────────────────────────
        elif s == State.WRAPPED_SUMMARY:
            if event in (InputEvent.BACK, InputEvent.FIRE):
                self.state = State.MAIN_MENU

    def _main_menu_select(self):
        item = self.main_menu_items[self.main_menu_sel]
        if item == "Play CD":
            if self.cd.is_disc_present():
                self._load_cd()
            else:
                self.ui.show_message("No CD detected", duration=2)
        elif item == "Library":
            self.library_albums = self.library.get_albums()
            self.library_sel    = 0
            self.prev_state     = State.MAIN_MENU
            self.state          = State.LIBRARY_BROWSE
        elif item == "Music Wrapped":
            self.state = State.WRAPPED_SUMMARY
        elif item == "Quit":
            self._running = False

    def _check_track_end(self):
        if self.state != State.PLAYBACK:
            return
        if self.paused:
            return
        if time.monotonic() - self._last_play_time < 3.0:
            return

        # For CD: poll MPV chapter to detect auto-advance between tracks.
        # Only poll every 2 seconds to avoid flooding the IPC socket.
        if self.source == "cd":
            now = time.monotonic()
            if now - self._last_chapter_check >= 2.0:
                self._last_chapter_check = now
                chap = self.player.get_current_chapter()
                if chap >= 0 and chap != self.current_track:
                    # MPV has moved to the next chapter (next track)
                    self.current_track = chap
                    if 0 <= chap < len(self.tracklist):
                        track = self.tracklist[chap]
                        self.stats.log_play(
                            artist=self.album_info.get("artist", "Unknown"),
                            title=track.get("title", f"Track {chap + 1}"),
                            album=self.album_info.get("album", "Unknown"),
                        )
                        log.info("CD auto-advanced to track %d: %s",
                                 chap + 1, track.get("title"))
                    self._last_play_time = now

        # File finished when MPV goes idle
        if self.player.is_idle():
            if self.source in ("library", "data_cd", "vault"):
                # Auto-advance to next track; end album when all done
                next_idx = self.current_track + 1
                if next_idx < len(self.tracklist):
                    log.info("Auto-advancing to track %d", next_idx + 1)
                    self._play_track(next_idx)
                    return
                log.info("Album finished")
                if self.source == "data_cd":
                    self.cd.unmount()
            else:
                log.info("Album finished")
            self.player.stop()
            self.state = self.prev_state or State.MAIN_MENU

    def _check_override_change(self):
        """Hot-reload metadata if disc_overrides.json was saved while playing."""
        if self.state != State.PLAYBACK or self.source != "cd":
            return
        if not self._disc_id_str:
            return
        try:
            mtime = os.path.getmtime("/opt/musicplayer/disc_overrides.json")
        except OSError:
            return
        if mtime <= self._override_mtime:
            return
        # File changed — reload
        self._override_mtime = mtime
        log.info("disc_overrides.json changed — reloading metadata")
        raw_tracks = self.tracklist  # keep current tracklist as fallback
        meta = self.metadata.fetch_cd_metadata(self._disc_id_str, raw_tracks)
        self.album_info = meta
        self.tracklist  = meta.get("tracks") or raw_tracks
        self.ui._art_cache.clear()   # flush cached artwork surface

    def run(self):
        clock = pygame.time.Clock()
        while self._running:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    self._running = False
                elif ev.type == pygame.USEREVENT and hasattr(ev, "fn"):
                    try:
                        ev.fn(*ev.args)
                    except Exception as exc:
                        log.exception("Scheduled call failed: %s", exc)
                else:
                    inp = self.input.process(ev)
                    if inp:
                        self._handle_input(inp)

            self._check_track_end()
            self._check_override_change()

            s = self.state
            if s == State.MAIN_MENU:
                self.ui.draw_main_menu(self.main_menu_items, self.main_menu_sel)
            elif s == State.LIBRARY_BROWSE:
                self.ui.draw_library(self.library_albums, self.library_sel)
            elif s == State.CD_LOADING:
                pass   # ui.show_loading() already painted
            elif s == State.PLAYBACK:
                self.ui.draw_playback(
                    album_info    = self.album_info,
                    tracklist     = self.tracklist,
                    current_track = self.current_track,
                    paused        = self.paused,
                )
            elif s == State.WRAPPED_SUMMARY:
                summary = self.stats.get_wrapped()
                self.ui.draw_wrapped(summary)

            pygame.display.flip()
            clock.tick(30)

        self.player.stop()
        self.player.quit()
        pygame.quit()
        log.info("Goodbye.")


def main():
    app = MusicPlayer()
    app.run()


if __name__ == "__main__":
    main()
