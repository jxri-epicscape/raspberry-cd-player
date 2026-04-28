#!/usr/bin/env python3
"""
ui.py - Pygame UI for 480×800 portrait display (DRM/KMS).

Layout (playback screen)
------------------------
┌──────────────────────────────┐  ← 480 wide
│  Artist (big)                │  0–44 px
│  Album  (big)                │  44–88 px
│  Year   (small)              │  88–100 px
│    Album Art  400×400        │  100–500 px
│  ── Now Playing title ──     │  508–545 px
│  Scrollable tracklist        │  548–800 px
└──────────────────────────────┘
"""

import logging
import os
import io
import time
from typing import List, Dict, Optional

import pygame

log = logging.getLogger("ui")

WIDTH  = 480
HEIGHT = 800

# Colour palette
C_BG        = (15,  15,  20)
C_ACCENT    = (255, 165,  0)   # amber
C_TEXT      = (230, 230, 230)
C_SUBTEXT   = (150, 150, 160)
C_HIGHLIGHT = (40,  40,  55)
C_PLAYING   = (255, 165,  0)
C_WHITE     = (255, 255, 255)
C_PROGRESS  = (80,  80,  90)

FONT_TITLE_SIZE    = 40   # artist name
FONT_SUBTITLE_SIZE = 32   # album name
FONT_BOLD_SIZE     = 22
FONT_NORMAL_SIZE   = 18
FONT_SMALL_SIZE    = 15

ART_SIZE = (400, 400)
ART_Y    = 92             # two-line header: artist + album(year)

TRACK_LIST_Y       = 548
TRACK_LIST_HEIGHT  = HEIGHT - TRACK_LIST_Y
VISIBLE_TRACKS     = 8


class UI:
    def __init__(self):
        # Alustetaan pygame display-moduuli erikseen
        if not pygame.display.get_init():
            pygame.display.init()
            
        # Trixie/Python 3.13 saattaa vaatia, että SDL tietää mikä DRM-kortti on käytössä
        # Jos kmsdrm ei toimi, kokeillaan poistaa FULLSCREEN ja käyttää tavallista modea
        log.info("Yritetään avata näyttö...")
        
        try:
            # Kokeillaan ensin ilman FULLSCREEN-lippua, jos se aiheuttaa ongelmia DRM:n kanssa
            self.screen = pygame.display.set_mode(
                (WIDTH, HEIGHT),
                pygame.NOFRAME
            )
        except pygame.error as e:
            log.error(f"Näytön avaaminen epäonnistui: {e}")
            # Viimeinen yritys täysin perusasetuksilla
            self.screen = pygame.display.set_mode((WIDTH, HEIGHT))

        pygame.display.set_caption("HiFiBerry Player")
        pygame.mouse.set_visible(False) # Piilotetaan kursori kerralla
        self._load_fonts()
        # ... loput ennallaan ...
        self._art_cache: Dict[str, pygame.Surface] = {}
        self._placeholder = self._make_placeholder()
        self._message_until = 0.0
        self._message_text  = ""
        self._loading_text  = ""
        self._is_loading    = False

        # tracklist scroll offset
        self._track_scroll  = 0

    # ── Fonts ──────────────────────────────────────────────────────────────────
    def _load_fonts(self):
        try:
            self.font_title    = pygame.font.Font(None, FONT_TITLE_SIZE)
            self.font_subtitle = pygame.font.Font(None, FONT_SUBTITLE_SIZE)
            self.font_bold     = pygame.font.Font(None, FONT_BOLD_SIZE + 10)
            self.font_normal   = pygame.font.Font(None, FONT_NORMAL_SIZE + 8)
            self.font_small    = pygame.font.Font(None, FONT_SMALL_SIZE + 6)
        except Exception:
            self.font_title = self.font_subtitle = self.font_bold = \
                self.font_normal = self.font_small = pygame.font.Font(None, 24)

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _make_placeholder(self) -> pygame.Surface:
        surf = pygame.Surface(ART_SIZE)
        surf.fill((40, 40, 50))
        txt = self.font_bold.render("♪", True, C_ACCENT)
        rect = txt.get_rect(center=(ART_SIZE[0] // 2, ART_SIZE[1] // 2))
        surf.blit(txt, rect)
        return surf

    def _render_text(self, text: str, font, color=C_TEXT, max_width=WIDTH - 20) -> pygame.Surface:
        """Render text, truncating with ellipsis if too wide."""
        surf = font.render(text, True, color)
        if surf.get_width() > max_width:
            while text and font.render(text + "…", True, color).get_width() > max_width:
                text = text[:-1]
            surf = font.render(text + "…", True, color)
        return surf

    def _draw_text_centered(self, text: str, font, y: int, color=C_TEXT):
        surf = self._render_text(text, font, color)
        rect = surf.get_rect(centerx=WIDTH // 2, y=y)
        self.screen.blit(surf, rect)

    def _draw_text_left(self, text: str, font, x: int, y: int, color=C_TEXT, max_width: int = WIDTH - 20):
        surf = self._render_text(text, font, color, max_width)
        self.screen.blit(surf, (x, y))

    def _draw_progress_bar(self, x, y, w, h, fraction: float, bg=C_PROGRESS, fg=C_ACCENT):
        pygame.draw.rect(self.screen, bg, (x, y, w, h), border_radius=4)
        if fraction > 0:
            pygame.draw.rect(self.screen, fg, (x, y, int(w * fraction), h), border_radius=4)

    def _load_art(self, path: Optional[str]) -> pygame.Surface:
        if not path:
            return self._placeholder
        if path in self._art_cache:
            return self._art_cache[path]
        try:
            surf = pygame.image.load(path).convert()
            surf = pygame.transform.smoothscale(surf, ART_SIZE)
            self._art_cache[path] = surf
            return surf
        except Exception:
            log.debug("Could not load art: %s", path)
            return self._placeholder

    def _format_seconds(self, secs: float) -> str:
        secs = max(0, int(secs))
        return f"{secs // 60}:{secs % 60:02d}"

    # ── Loading / message overlays ────────────────────────────────────────────
    def show_loading(self, text: str = "Loading…"):
        self._is_loading    = True
        self._loading_text  = text
        self.screen.fill(C_BG)
        self._draw_text_centered(text, self.font_bold, HEIGHT // 2 - 20, C_ACCENT)
        pygame.display.flip()

    def show_message(self, text: str, duration: float = 2.0):
        self._message_text  = text
        self._message_until = time.time() + duration

    def _draw_message_overlay(self):
        if time.time() < self._message_until:
            overlay = pygame.Surface((WIDTH, 60), pygame.SRCALPHA)
            overlay.fill((0, 0, 0, 180))
            self.screen.blit(overlay, (0, HEIGHT // 2 - 30))
            self._draw_text_centered(self._message_text, self.font_bold, HEIGHT // 2 - 12, C_WHITE)

    # ── Screens ───────────────────────────────────────────────────────────────
    def draw_main_menu(self, items: List[str], selected: int):
        self.screen.fill(C_BG)
        self._draw_text_centered("🎵  HiFiBerry Player", self.font_bold, 40, C_ACCENT)
        pygame.draw.line(self.screen, C_ACCENT, (20, 80), (WIDTH - 20, 80), 1)

        item_h = 70
        start_y = 130
        for i, item in enumerate(items):
            y = start_y + i * item_h
            if i == selected:
                pygame.draw.rect(self.screen, C_HIGHLIGHT, (10, y - 8, WIDTH - 20, item_h - 10), border_radius=8)
                color = C_ACCENT
            else:
                color = C_TEXT
            self._draw_text_centered(item, self.font_bold, y + 14, color)

        self._draw_message_overlay()

    def draw_library(self, albums: List[Dict], selected: int):
        self.screen.fill(C_BG)
        self._draw_text_centered("Library", self.font_bold, 15, C_ACCENT)
        pygame.draw.line(self.screen, C_ACCENT, (20, 50), (WIDTH - 20, 50), 1)

        if not albums:
            self._draw_text_centered("No albums found", self.font_normal, HEIGHT // 2, C_SUBTEXT)
            return

        item_h  = 60
        vis     = (HEIGHT - 60) // item_h
        start_i = max(0, selected - vis // 2)
        y       = 60

        for i in range(start_i, min(start_i + vis, len(albums))):
            album = albums[i]
            is_sel = (i == selected)
            if is_sel:
                pygame.draw.rect(self.screen, C_HIGHLIGHT, (5, y, WIDTH - 10, item_h - 4), border_radius=6)
            label  = f"{album.get('artist','?')} – {album.get('title','?')}"
            color  = C_ACCENT if is_sel else C_TEXT
            self._draw_text_left(label, self.font_normal, 14, y + 10, color, WIDTH - 28)
            year = album.get("year", "")
            if year:
                yr_surf = self.font_small.render(str(year), True, C_SUBTEXT)
                self.screen.blit(yr_surf, (WIDTH - yr_surf.get_width() - 14, y + 10))
            y += item_h

        self._draw_message_overlay()

    def draw_playback(
        self,
        album_info:    Dict,
        tracklist:     List[Dict],
        current_track: int,
        paused:        bool,
        position:      float = 0,
        duration:      float = 0,
    ):
        self.screen.fill(C_BG)

        # ── Header ────────────────────────────────────────────────────────────
        artist = album_info.get("artist", "Unknown Artist")
        album  = album_info.get("album",  "Unknown Album")
        year   = str(album_info.get("year", ""))
        album_line = f"{album} ({year})" if year else album
        self._draw_text_centered(artist,     self.font_title,    6,  C_WHITE)
        self._draw_text_centered(album_line, self.font_subtitle, 52, C_SUBTEXT)

        # ── Album art ─────────────────────────────────────────────────────────
        art_path = album_info.get("art_path")
        art = self._load_art(art_path)
        art_x = (WIDTH - ART_SIZE[0]) // 2
        self.screen.blit(art, (art_x, ART_Y))

        # ── Now playing ───────────────────────────────────────────────────────
        now_y = ART_Y + ART_SIZE[1] + 8
        if tracklist and 0 <= current_track < len(tracklist):
            track = tracklist[current_track]
            title = track.get("title", f"Track {current_track + 1}")
            self._draw_text_centered(title, self.font_bold, now_y, C_ACCENT)

        # ── Progress bar (thin, no timestamps) ───────────────────────────────
        bar_y = now_y + 28
        frac  = (position / duration) if duration > 0 else 0
        pygame.draw.rect(self.screen, C_PROGRESS, (20, bar_y, WIDTH - 40, 3), border_radius=2)
        if frac > 0:
            pygame.draw.rect(self.screen, C_ACCENT,
                             (20, bar_y, int((WIDTH - 40) * frac), 3), border_radius=2)

        # ── Tracklist ─────────────────────────────────────────────────────────
        tl_y   = TRACK_LIST_Y
        pygame.draw.line(self.screen, C_ACCENT, (20, tl_y - 5), (WIDTH - 20, tl_y - 5), 1)

        # Auto-scroll to keep current track visible
        if current_track < self._track_scroll:
            self._track_scroll = current_track
        elif current_track >= self._track_scroll + VISIBLE_TRACKS:
            self._track_scroll = current_track - VISIBLE_TRACKS + 1

        row_h = (HEIGHT - tl_y) // VISIBLE_TRACKS
        for row, i in enumerate(range(self._track_scroll, self._track_scroll + VISIBLE_TRACKS)):
            if i >= len(tracklist):
                break
            t   = tracklist[i]
            y   = tl_y + row * row_h
            is_cur = (i == current_track)
            if is_cur:
                pygame.draw.rect(self.screen, C_HIGHLIGHT, (5, y, WIDTH - 10, row_h - 2), border_radius=5)
            num   = f"{t.get('num', i+1):02d}"
            title = t.get("title", f"Track {i+1}")
            color = C_PLAYING if is_cur else C_TEXT
            num_s = self.font_small.render(num, True, C_SUBTEXT)
            self.screen.blit(num_s, (12, y + (row_h - num_s.get_height()) // 2))
            self._draw_text_left(title, self.font_small, 40, y + (row_h - 15) // 2,
                                 color, WIDTH - 55)

        self._draw_message_overlay()

    def draw_wrapped(self, summary: Dict):
        self.screen.fill(C_BG)
        self._draw_text_centered("🎵  Music Wrapped", self.font_bold, 20, C_ACCENT)
        pygame.draw.line(self.screen, C_ACCENT, (20, 65), (WIDTH - 20, 65), 1)

        y = 85
        dy = 28

        def row(label, value, color=C_TEXT):
            nonlocal y
            self._draw_text_left(label, self.font_small, 20, y, C_SUBTEXT)
            val_s = self.font_small.render(str(value), True, color)
            self.screen.blit(val_s, (WIDTH - val_s.get_width() - 20, y))
            y += dy

        row("Total plays", summary.get("total_plays", 0), C_WHITE)
        row("Top artist",  summary.get("top_artist",  "—"), C_ACCENT)
        row("Top album",   summary.get("top_album",   "—"), C_WHITE)
        row("Top track",   summary.get("top_track",   "—"), C_ACCENT)

        y += 10
        self._draw_text_centered("Recent plays", self.font_bold, y, C_WHITE)
        y += dy + 5
        for entry in summary.get("recent", [])[:8]:
            self._draw_text_left(
                f"  {entry.get('artist','')} – {entry.get('title','')}",
                self.font_small, 20, y, C_TEXT
            )
            y += dy
            if y > HEIGHT - 30:
                break

        self._draw_text_centered("↑↓ BACK", self.font_small, HEIGHT - 24, C_SUBTEXT)
