"""Elite mode: a heart-rate-gated round engine for fighters.

Each round is a three-phase cycle:

  PREWORK   the client works until their (smoothed) heart rate reaches zone 5.
  WORK      the round clock runs only while they are in zone 5. If they drop
            out of zone 5 the clock pauses until they return.
  RECOVERY  the client rests until their heart rate falls to a preset
            "recovered" level, then the next round's PREWORK begins.

The final round ends after WORK (no recovery). Total time runs from the start
of the session to the end of the last round; the goal over the weeks is to
shrink it towards the real fight length including rests.

This module is pure Python: no Flet, no Bluetooth, no database. The app feeds
it (timestamp, heart rate) samples and reads back phase events and round
results, which makes the logic easy to test with synthetic traces.

Design notes
  * HR is smoothed over a few seconds and every transition needs a short hold,
    so one noisy reading cannot start a round or pause the clock.
  * A short grace period below zone 5 still counts as work before the clock
    pauses.
  * Caps stop a client being stuck (no zone 5 reached, too long paused, never
    recovering). A capped round is recorded with a flag, not silently dropped.
  * Gaps longer than `max_gap_s` between samples (strap dropout) are never
    credited as work.
  * Zone 5 and "recovered" levels are frozen when the engine is created, so a
    session's numbers cannot shift mid-session if a client's max HR is updated.
"""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

PREWORK = "prework"
WORK = "work"
RECOVERY = "recovery"
DONE = "done"

MODE_NORMAL = "normal"
MODE_ELITE = "elite"


@dataclass(frozen=True)
class EliteParams:
    rounds: int
    round_s: float          # seconds of zone-5 work per round
    z5_low: float           # bpm: strictly above this counts as zone 5
    recovered_bpm: float    # bpm: at or below this counts as recovered
    smooth_s: float = 3.0
    start_hold_s: float = 2.0     # smoothed HR must stay in zone 5 this long to start the round
    pause_grace_s: float = 3.0    # time allowed below zone 5 before the clock pauses
    recover_hold_s: float = 2.0   # smoothed HR must stay recovered this long to end the rest
    max_prework_s: float = 180.0
    max_pause_s: float = 120.0    # total paused time allowed in one round
    max_recovery_s: float = 180.0
    max_gap_s: float = 5.0


@dataclass
class PhaseEvent:
    t: float
    round_no: int
    phase: str


@dataclass
class RoundResult:
    round_no: int
    started_t: float
    ended_t: float | None = None
    prework_s: float = 0.0              # time to reach zone 5
    work_s: float = 0.0                 # zone-5 time banked (== round length unless capped/stopped)
    paused_s: float = 0.0               # time below zone 5 after the clock started
    recovery_s: float | None = None     # time to reach the recovered level (None for the last round)
    hr_end_work: int | None = None
    hr_60s: int | None = None           # smoothed HR 60s into recovery (None if recovered sooner)
    flags: list[str] = field(default_factory=list)

    @property
    def hr_drop_60(self) -> int | None:
        if self.hr_end_work is None or self.hr_60s is None:
            return None
        return self.hr_end_work - self.hr_60s

    def as_dict(self) -> dict:
        return {
            "round_no": self.round_no,
            "started_t": self.started_t,
            "ended_t": self.ended_t,
            "prework_s": round(self.prework_s, 1),
            "work_s": round(self.work_s, 1),
            "paused_s": round(self.paused_s, 1),
            "recovery_s": None if self.recovery_s is None else round(self.recovery_s, 1),
            "hr_end_work": self.hr_end_work,
            "hr_60s": self.hr_60s,
            "flags": list(self.flags),
        }


class EliteEngine:
    def __init__(self, params: EliteParams, t0: float):
        self.p = params
        self.t_start = t0
        self.phase = PREWORK
        self.round_no = 1
        self.results: list[RoundResult] = []
        self.total_s: float | None = None
        self.smoothed: float | None = None

        self._last_t = t0
        self._win: deque[tuple[float, float]] = deque()
        self._phase_start = t0
        self._hold_since: float | None = None
        self._cur: RoundResult | None = RoundResult(1, t0)
        self._work_s = 0.0
        self._paused_s = 0.0
        self._running = False
        self._below_since: float | None = None
        self._events: list[PhaseEvent] = [PhaseEvent(t0, 1, PREWORK)]

    # ------------------------------------------------------------ public API

    def update(self, t: float, hr: float) -> list[PhaseEvent]:
        """Feed one sample. Returns phase events raised by this sample (usually none)."""
        if self.phase == DONE:
            return self.pop_events()
        dt = max(0.0, t - self._last_t)
        self._last_t = t

        self._win.append((t, float(hr)))
        while len(self._win) > 1 and t - self._win[0][0] >= self.p.smooth_s:
            self._win.popleft()
        self.smoothed = sum(h for _, h in self._win) / len(self._win)

        if self.phase == PREWORK:
            self._step_prework(t)
        elif self.phase == WORK:
            self._step_work(t, dt)
        elif self.phase == RECOVERY:
            self._step_recovery(t)
        return self.pop_events()

    def finish(self, t: float) -> list[PhaseEvent]:
        """Close the session early (e.g. the coach pressed End). Partial rounds are flagged."""
        if self.phase == DONE:
            return self.pop_events()
        if self.phase in (PREWORK, WORK):
            if self.phase == PREWORK:
                self._cur.prework_s = t - self._phase_start
            self._close_round(t, ["stopped"], in_work=self.phase == WORK)
        elif self.phase == RECOVERY:
            last = self.results[-1]
            last.recovery_s = None
            last.flags.append("stopped")
        self._finish(t)
        return self.pop_events()

    def pop_events(self) -> list[PhaseEvent]:
        events, self._events = self._events, []
        return events

    @property
    def done(self) -> bool:
        return self.phase == DONE

    def snapshot(self, t: float | None = None) -> dict:
        """Everything the UI needs to draw this client's phase line."""
        t = self._last_t if t is None else t
        if self.phase == WORK:
            remaining = max(0.0, self.p.round_s - self._work_s)
        elif self.phase == PREWORK:
            remaining = self.p.round_s
        else:
            remaining = 0.0
        total = self.total_s if self.total_s is not None else max(0.0, t - self.t_start)
        return {
            "phase": self.phase,
            "round_no": self.round_no,
            "rounds": self.p.rounds,
            "running": self._running if self.phase == WORK else False,
            "work_remaining_s": remaining,
            "phase_elapsed_s": max(0.0, t - self._phase_start),
            "total_elapsed_s": total,
            "smoothed": self.smoothed,
        }

    # ------------------------------------------------------------ phase steps

    def _step_prework(self, t: float) -> None:
        p = self.p
        if self.smoothed > p.z5_low:
            if self._hold_since is None:
                self._hold_since = t
            if t - self._hold_since >= p.start_hold_s - 1e-9:
                self._cur.prework_s = self._hold_since - self._phase_start
                # the hold itself was spent in zone 5, so it counts as work
                self._work_s = t - self._hold_since
                self._paused_s = 0.0
                self._running = True
                self._below_since = None
                self._enter(WORK, t)
                return
        else:
            self._hold_since = None
        if t - self._phase_start >= p.max_prework_s:
            self._cur.prework_s = t - self._phase_start
            self._end_round(t, ["prework_cap"], in_work=False)

    def _step_work(self, t: float, dt: float) -> None:
        p = self.p
        if dt > p.max_gap_s:
            # strap dropout: never credit the gap as work
            self._paused_s += dt
            self._running = False
            self._below_since = None
        elif self._running:
            self._work_s += dt
        else:
            self._paused_s += dt

        if self.smoothed > p.z5_low:
            self._running = True
            self._below_since = None
        elif self._running:
            if self._below_since is None:
                self._below_since = t
            elif t - self._below_since >= p.pause_grace_s - 1e-9:
                self._running = False

        if self._work_s >= p.round_s - 1e-9:
            self._work_s = p.round_s
            self._end_round(t, [], in_work=True)
        elif self._paused_s >= p.max_pause_s:
            self._end_round(t, ["pause_cap"], in_work=True)

    def _step_recovery(self, t: float) -> None:
        p = self.p
        last = self.results[-1]
        elapsed = t - self._phase_start
        if last.hr_60s is None and elapsed >= 60.0:
            last.hr_60s = round(self.smoothed)
        if self.smoothed <= p.recovered_bpm:
            if self._hold_since is None:
                self._hold_since = t
            if t - self._hold_since >= p.recover_hold_s - 1e-9:
                last.recovery_s = self._hold_since - self._phase_start
                self._next_round(t)
                return
        else:
            self._hold_since = None
        if elapsed >= p.max_recovery_s:
            last.recovery_s = elapsed
            last.flags.append("recovery_cap")
            self._next_round(t)

    # ------------------------------------------------------------ transitions

    def _enter(self, phase: str, t: float) -> None:
        self.phase = phase
        self._phase_start = t
        self._hold_since = None
        self._events.append(PhaseEvent(t, self.round_no, phase))

    def _close_round(self, t: float, flags: list[str], in_work: bool) -> None:
        cur = self._cur
        cur.ended_t = t
        cur.flags.extend(flags)
        if in_work:
            cur.work_s = min(self._work_s, self.p.round_s)
            cur.paused_s = self._paused_s
            if self.smoothed is not None:
                cur.hr_end_work = round(self.smoothed)
        self.results.append(cur)
        self._cur = None

    def _end_round(self, t: float, flags: list[str], in_work: bool) -> None:
        self._close_round(t, flags, in_work)
        if self.round_no >= self.p.rounds:
            self._finish(t)
        else:
            self._enter(RECOVERY, t)

    def _next_round(self, t: float) -> None:
        self.round_no += 1
        self._cur = RoundResult(self.round_no, t)
        self._work_s = 0.0
        self._paused_s = 0.0
        self._running = False
        self._below_since = None
        self._enter(PREWORK, t)

    def _finish(self, t: float) -> None:
        self.total_s = t - self.t_start
        self._running = False
        self._enter(DONE, t)


# ---------------------------------------------------------------- helpers

def recovered_bpm(thresholds: list[int], resting_hr: int | None, pct: float) -> float:
    """The 'recovered' heart rate as a fraction of heart rate reserve.

    thresholds is zones.zone_thresholds(...); its last entry is the max HR the
    zones were built from. With no resting HR it falls back to a fraction of max.
    """
    hr_max = thresholds[-1]
    base = resting_hr or 0
    return base + pct * (hr_max - base)


def params_for_client(thresholds: list[int], resting_hr: int | None, settings: dict) -> EliteParams:
    """Builds a client's frozen parameters from their zones and the session settings."""
    return EliteParams(
        rounds=settings["rounds"],
        round_s=settings["round_s"],
        z5_low=thresholds[4],
        recovered_bpm=recovered_bpm(thresholds, resting_hr, settings["recovered_pct"]),
    )


def parse_duration(text) -> float | None:
    """'3:00' -> 180.0, '90' -> 90.0, '1:30' -> 90.0. None if it isn't a duration."""
    s = str(text or "").strip()
    if not s:
        return None
    try:
        if ":" in s:
            minutes, seconds = s.split(":", 1)
            return int(minutes) * 60 + float(seconds)
        return float(s)
    except ValueError:
        return None


def parse_settings(rounds_text, round_text, recovered_text, rest_text) -> tuple[dict | None, str | None]:
    """Validates the elite form. Returns (settings, None) or (None, error message)."""
    try:
        rounds = int(str(rounds_text).strip())
    except ValueError:
        return None, "Rounds must be a whole number."
    if not 1 <= rounds <= 20:
        return None, "Rounds must be between 1 and 20."

    round_s = parse_duration(round_text)
    if round_s is None or not 10 <= round_s <= 600:
        return None, "Round length must be between 0:10 and 10:00 (m:ss or seconds)."

    try:
        recovered_pct = float(str(recovered_text).strip())
    except ValueError:
        return None, "Recovered level must be a number (percent of heart rate reserve)."
    if not 30 <= recovered_pct <= 95:
        return None, "Recovered level must be between 30 and 95 percent."

    rest_s = parse_duration(rest_text)
    if rest_s is None or not 0 <= rest_s <= 300:
        return None, "Fight rest must be between 0 and 5:00 (m:ss or seconds)."

    return {
        "rounds": rounds,
        "round_s": round_s,
        "recovered_pct": recovered_pct / 100.0,
        "target_rest_s": rest_s,
        "target_total_s": rounds * round_s + (rounds - 1) * rest_s,
    }, None


def format_clock(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    total = max(0, int(round(seconds)))
    return f"{total // 60}:{total % 60:02d}"


def total_seconds_from_events(events) -> float | None:
    """Total session time for one client from their stored phase events (dicts or rows with 'ts')."""
    events = list(events)
    if len(events) < 2:
        return None
    first = datetime.fromisoformat(events[0]["ts"])
    last = datetime.fromisoformat(events[-1]["ts"])
    return (last - first).total_seconds()
