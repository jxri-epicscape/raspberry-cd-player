#!/usr/bin/env python3
"""
player.py — MPV IPC controller for HiFiBerry.
Optimised for Debian Trixie + MPV 0.40.
"""

import json
import logging
import os
import socket
import time
import threading
from typing import Optional

log = logging.getLogger("player")

IPC_SOCKET = "/tmp/mpv_ipc.sock"

VOLUME_STEP = 5   # percent per button press
VOLUME_MIN  = 0
VOLUME_MAX  = 100


class Player:
    def __init__(self):
        self._sock: Optional[socket.socket] = None
        self._lock  = threading.Lock()
        self._req_id = 0
        self._volume = 85   # default; synced from MPV on first get
        self._cd_loaded = False  # True once cdda:///dev/sr0 is loaded

        log.info("Initialising player (waiting for MPV IPC socket)…")
        self._connect_ipc()

    # ── IPC connection ────────────────────────────────────────────────────────
    def _connect_ipc(self):
        if not os.path.exists(IPC_SOCKET):
            log.warning("Socket %s not found — is MPV running?", IPC_SOCKET)
            self._sock = None
            return
        try:
            if self._sock:
                self._sock.close()
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.connect(IPC_SOCKET)
            self._sock.settimeout(0.5)
            log.info("Connected to MPV IPC.")
        except Exception as e:
            log.error("IPC connection error: %s", e)
            self._sock = None

    def _send(self, command: list) -> Optional[dict]:
        with self._lock:
            if not self._sock:
                self._connect_ipc()
                if not self._sock:
                    return None

            self._req_id += 1
            request = {"command": command, "request_id": self._req_id}
            try:
                self._sock.sendall((json.dumps(request) + "\n").encode())
                for _ in range(10):
                    data = self._sock.recv(4096)
                    if not data:
                        break
                    for line in data.split(b"\n"):
                        if not line:
                            continue
                        resp = json.loads(line)
                        if resp.get("request_id") == self._req_id:
                            return resp
                return None
            except Exception as e:
                log.debug("IPC send error: %s", e)
                self._sock = None
                return None

    # ── Playback ──────────────────────────────────────────────────────────────
    def play(self, uri: str):
        log.info("Play: %s", uri)
        if uri.startswith("cdda://"):
            try:
                track_num = int(uri.replace("cdda://", ""))
            except ValueError:
                log.error("Invalid CD track URI: %s", uri)
                return
            # MPV does NOT support cdda:///dev/sr0:N (track suffix) in v0.40.
            # Load the whole disc once; each track is exposed as a chapter.
            # Navigate with set_property chapter (0-based index).
            if not self._cd_loaded:
                log.info("Loading CD disc (cdda:///dev/sr0)")
                self._send(["loadfile", "cdda:///dev/sr0", "replace"])
                self._send(["set_property", "pause", False])
                self._cd_loaded = True
                # If starting from a track other than 1, wait for disc open
                # then seek to the correct chapter.
                if track_num > 1:
                    time.sleep(3.0)
                    log.info("Seeking to chapter %d (track %d)", track_num - 1, track_num)
                    self._send(["set_property", "chapter", track_num - 1])
            else:
                chapter = track_num - 1
                log.info("Seeking to chapter %d (track %d)", chapter, track_num)
                self._send(["set_property", "chapter", chapter])
                self._send(["set_property", "pause", False])
        else:
            self._cd_loaded = False
            self._send(["loadfile", uri, "replace"])
            self._send(["set_property", "pause", False])

    def pause(self):
        self._send(["set_property", "pause", True])

    def resume(self):
        self._send(["set_property", "pause", False])

    def toggle_pause(self):
        """Toggle between paused and playing."""
        self._send(["cycle", "pause"])

    def stop(self):
        self._cd_loaded = False
        self._send(["stop"])

    def get_current_chapter(self) -> int:
        """Return current MPV chapter index (0-based), or -1 on error."""
        res = self._send(["get_property", "chapter"])
        if res and res.get("error") == "success":
            data = res.get("data")
            if data is not None:
                return int(data)
        return -1

    def get_position(self) -> float:
        res = self._send(["get_property", "time-pos"])
        if res and res.get("error") == "success":
            return float(res.get("data") or 0)
        return 0.0

    def is_idle(self) -> bool:
        res = self._send(["get_property", "idle-active"])
        if res and res.get("error") == "success":
            return res.get("data") is True
        return True

    # ── Volume ────────────────────────────────────────────────────────────────
    def get_volume(self) -> int:
        res = self._send(["get_property", "volume"])
        if res and res.get("error") == "success":
            self._volume = int(res.get("data") or self._volume)
        return self._volume

    def set_volume(self, level: int):
        level = max(VOLUME_MIN, min(VOLUME_MAX, level))
        self._volume = level
        self._send(["set_property", "volume", level])
        log.debug("Volume → %d", level)

    def volume_up(self):
        self.set_volume(self.get_volume() + VOLUME_STEP)

    def volume_down(self):
        self.set_volume(self.get_volume() - VOLUME_STEP)

    # ── Cleanup ───────────────────────────────────────────────────────────────
    def quit(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
