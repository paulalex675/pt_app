"""Audio cues for sessions. Silent and harmless if there is no sound device.

The cue files in sounds/ are made by tools/make_sounds.py. Playback uses pygame's mixer
(listed in requirements.txt); if pygame is missing or the machine has no audio output the
app simply stays silent and says so - a session never depends on sound.

How a group is handled is deliberately one small, replaceable piece (see SoundPlayer.play):
the default is shared cues, with the same cue never repeated inside `debounce_s`, so six
fighters reaching zone 5 together give one "go" rather than six. Safety cues are never held back.
"""
import os
import time
from pathlib import Path

import db

SOUND_DIR = Path(__file__).resolve().parent / "sounds"

SESSION_START = "session_start"
WORK_START = "work_start"
WORK_PAUSE = "work_pause"
WORK_RESUME = "work_resume"
ROUND_END = "round_end"
RECOVERED = "recovered"
FINAL_BELL = "final_bell"
STRAP_LOST = "strap_lost"

CUES = (SESSION_START, WORK_START, WORK_PAUSE, WORK_RESUME, ROUND_END, RECOVERED, FINAL_BELL, STRAP_LOST)
SAFETY_CUES = frozenset({STRAP_LOST})

DEFAULT_VOLUME = 0.8


def cue_for_event(phase: str, round_no: int) -> str | None:
    """Which cue an elite phase event should make (see elite.py for the phases)."""
    if phase == "prework":
        return SESSION_START if round_no == 1 else RECOVERED     # later rounds start when recovered
    return {"work": WORK_START, "recovery": ROUND_END, "done": FINAL_BELL}.get(phase)


class NullBackend:
    available = False
    reason = "Sound is switched off."

    def play(self, cue: str, volume: float) -> bool:
        return False


class PygameBackend:
    def __init__(self):
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        import pygame                                   # ImportError -> caller falls back to silence
        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
        self._pygame = pygame
        self._sounds = {}
        self.available = True
        self.reason = ""

    def _sound(self, cue):
        if cue not in self._sounds:
            self._sounds[cue] = self._pygame.mixer.Sound(str(SOUND_DIR / f"{cue}.wav"))
        return self._sounds[cue]

    def play(self, cue: str, volume: float) -> bool:
        try:
            sound = self._sound(cue)
            sound.set_volume(max(0.0, min(1.0, volume)))
            sound.play()
            return True
        except Exception:                                # a missing file or a vanished device must not stop a session
            return False


class SoundPlayer:
    def __init__(self, backend=None, enabled=True, volume=DEFAULT_VOLUME, debounce_s=1.5, clock=time.monotonic):
        self.backend = backend or NullBackend()
        self.enabled = enabled
        self.volume = volume
        self.debounce_s = debounce_s
        self._clock = clock
        self._last_played: dict[str, float] = {}

    @property
    def available(self) -> bool:
        return bool(self.backend.available)

    @property
    def status(self) -> str:
        if not self.available:
            return getattr(self.backend, "reason", "") or "Sound unavailable."
        return "On" if self.enabled and self.volume > 0 else "Muted"

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        db.set_setting("sound_enabled", "1" if self.enabled else "0")

    def set_volume(self, volume: float) -> None:
        self.volume = max(0.0, min(1.0, float(volume)))
        db.set_setting("sound_volume", f"{self.volume:.2f}")

    def load_settings(self) -> None:
        self.enabled = db.get_setting("sound_enabled", "1") != "0"
        try:
            self.volume = max(0.0, min(1.0, float(db.get_setting("sound_volume", str(DEFAULT_VOLUME)))))
        except ValueError:
            self.volume = DEFAULT_VOLUME

    def play(self, cue: str | None, force: bool = False) -> bool:
        """Play a cue. Returns True if it was sent to the speaker.

        `force` is for the Test button: it ignores the debounce but still respects mute.
        """
        if cue not in CUES or not self.available or not self.enabled or self.volume <= 0:
            return False
        now = self._clock()
        if not force and cue not in SAFETY_CUES:
            last = self._last_played.get(cue)
            if last is not None and now - last < self.debounce_s:
                return False
        if self.backend.play(cue, self.volume):
            self._last_played[cue] = now
            return True
        return False


class StrapMonitor:
    """Notices a strap that has gone quiet in the middle of a session.

    Call check() about once a second. It returns the clients whose strap just went silent,
    and again every `repeat_s` while it stays silent. A strap that starts sending again
    is forgotten, so the next dropout alerts immediately.
    """

    def __init__(self, timeout_s=5.0, repeat_s=10.0):
        self.timeout_s = timeout_s
        self.repeat_s = repeat_s
        self._alerted_at: dict[str, float] = {}

    def check(self, now: float, heard_at: dict[str, float | None]) -> list[str]:
        alerts = []
        for client_id, last in heard_at.items():
            if last is None or now - last <= self.timeout_s:
                self._alerted_at.pop(client_id, None)
                continue
            previous = self._alerted_at.get(client_id)
            if previous is None or now - previous >= self.repeat_s:
                self._alerted_at[client_id] = now
                alerts.append(client_id)
        for gone in set(self._alerted_at) - set(heard_at):
            del self._alerted_at[gone]
        return alerts

    def reset(self) -> None:
        self._alerted_at.clear()


def create_player() -> SoundPlayer:
    """The player the app uses. PT_STUDIO_SOUND=off forces silence (the tests do this)."""
    if os.environ.get("PT_STUDIO_SOUND", "").lower() == "off":
        player = SoundPlayer(NullBackend())
    else:
        try:
            player = SoundPlayer(PygameBackend())
        except Exception as exc:                         # not installed, or no audio device
            backend = NullBackend()
            backend.reason = ("Sound unavailable - run: python -m pip install -r requirements.txt"
                              if isinstance(exc, ImportError) else f"Sound unavailable: {exc}")
            player = SoundPlayer(backend)
    player.load_settings()
    return player
