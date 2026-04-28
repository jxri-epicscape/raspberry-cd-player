#!/usr/bin/env python3
"""
input_handler.py — e4you Retro Fun (TAC-2) USB joystick driver.

Physical layout
───────────────
  BTN_SOUTH (button 0)  — main action button
  BTN_EAST  (button 1)  — secondary / back button
  ABS_X / ABS_Y         — joystick axes, range -127..127, flat ≈ 15

Event mapping
─────────────
  Joystick  UP    → InputEvent.UP
  Joystick  DOWN  → InputEvent.DOWN
  Joystick  LEFT  → InputEvent.LEFT
  Joystick  RIGHT → InputEvent.RIGHT

  BTN_SOUTH short press  → InputEvent.FIRE
  BTN_SOUTH long  press  → InputEvent.BACK
  BTN_EAST  press        → InputEvent.BACK   (shortcut, no long-press needed)

Hat-motion (D-pad style) is also supported as a fallback.
"""

import logging
import time
from enum import Enum, auto
from typing import Optional

import pygame

log = logging.getLogger("input")

# ── Tuning ────────────────────────────────────────────────────────────────────
LONG_PRESS_THRESHOLD = 0.8   # seconds to trigger BACK from BTN_SOUTH
AXIS_THRESHOLD       = 0.35  # normalised, device range is -1.0..1.0
AXIS_RELEASE_ZONE    = 0.15  # below this the axis is considered centred
AXIS_DEBOUNCE        = 0.25  # minimum seconds between two events on the same axis


class InputEvent(Enum):
    UP    = auto()
    DOWN  = auto()
    LEFT  = auto()
    RIGHT = auto()
    FIRE  = auto()
    BACK  = auto()


class InputHandler:
    def __init__(self):
        pygame.joystick.init()
        self._joysticks: list = []
        for i in range(pygame.joystick.get_count()):
            j = pygame.joystick.Joystick(i)
            j.init()
            self._joysticks.append(j)
            log.info(
                "Joystick %d: %s  (axes=%d  buttons=%d  hats=%d)",
                i, j.get_name(), j.get_numaxes(), j.get_numbuttons(), j.get_numhats(),
            )

        # Per-axis state: locked prevents re-fire until stick returns to centre
        # last_time prevents multiple events within the debounce window
        self._axis_locked:    dict[int, bool]  = {}
        self._axis_last_time: dict[int, float] = {}

        # Button 0 long-press tracking
        self._btn0_down_at: Optional[float] = None

    # ── Public interface ──────────────────────────────────────────────────────
    def process(self, event: pygame.event.Event) -> Optional[InputEvent]:
        """
        Call once per pygame event.
        Returns an InputEvent or None if the event should be ignored.
        """
        et = event.type

        # ── Buttons ───────────────────────────────────────────────────────────
        if et == pygame.JOYBUTTONDOWN:
            if event.button == 0:
                self._btn0_down_at = time.monotonic()
            elif event.button == 1:
                # BTN_EAST fires BACK immediately on press
                return InputEvent.BACK
            return None

        if et == pygame.JOYBUTTONUP:
            if event.button == 0:
                return self._resolve_btn0()
            return None

        # ── Analogue axes ─────────────────────────────────────────────────────
        if et == pygame.JOYAXISMOTION:
            return self._handle_axis(event.axis, event.value)

        # ── Hat / D-pad (fallback for devices that use hat instead of axis) ──
        if et == pygame.JOYHATMOTION:
            return self._handle_hat(event.value)

        return None

    # ── Private helpers ───────────────────────────────────────────────────────
    def _resolve_btn0(self) -> InputEvent:
        held = 0.0
        if self._btn0_down_at is not None:
            held = time.monotonic() - self._btn0_down_at
        self._btn0_down_at = None
        return InputEvent.BACK if held >= LONG_PRESS_THRESHOLD else InputEvent.FIRE

    def _handle_axis(self, axis: int, value: float) -> Optional[InputEvent]:
        now = time.monotonic()

        # Stick returned to centre → release the lock for this axis
        if abs(value) < AXIS_RELEASE_ZONE:
            self._axis_locked[axis] = False
            return None

        # Locked (waiting for stick to return to centre)
        if self._axis_locked.get(axis, False):
            return None

        # Debounce: ignore if we fired this axis too recently
        if now - self._axis_last_time.get(axis, 0.0) < AXIS_DEBOUNCE:
            return None

        # Map to event
        result: Optional[InputEvent] = None
        if axis == 1:           # Y-axis
            if value < -AXIS_THRESHOLD:
                result = InputEvent.UP
            elif value > AXIS_THRESHOLD:
                result = InputEvent.DOWN
        elif axis == 0:         # X-axis
            if value < -AXIS_THRESHOLD:
                result = InputEvent.LEFT
            elif value > AXIS_THRESHOLD:
                result = InputEvent.RIGHT

        if result is not None:
            self._axis_locked[axis]    = True
            self._axis_last_time[axis] = now
            log.debug("Axis %d → %s (value=%.2f)", axis, result.name, value)

        return result

    def _handle_hat(self, value: tuple) -> Optional[InputEvent]:
        """Convert a hat (x, y) tuple to an InputEvent."""
        x, y = value
        if y ==  1: return InputEvent.UP
        if y == -1: return InputEvent.DOWN
        if x == -1: return InputEvent.LEFT
        if x ==  1: return InputEvent.RIGHT
        return None
