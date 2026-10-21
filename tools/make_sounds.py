"""Generates the PT Studio cue sounds (sounds/*.wav). Run: python tools/make_sounds.py

Everything is synthesised from maths, so there is nothing to license and the cues are
reproducible (fixed random seed). Needs numpy (generation only - the app does not).

Design rules - deliberately NOT musical instruments (tuned notes that ring out sound like a piano):
  * timer-style gated beeps: instant on, flat, instant off - the sound of an interval timer
  * synth sweeps (rising = go / back on, falling = paused) rather than notes
  * a punchy low thump plus a tiny click on the important moments, for weight on laptop speakers
  * bells are metallic and inharmonic (no clear pitch), like a ring bell rather than a chime
  * each cue differs in rhythm and contour so they can be told apart without looking
  * loudness-matched; the strap alert is clearly louder; a soft limiter keeps peaks under -1 dBFS
"""
import sys
import wave
from pathlib import Path

import numpy as np

SR = 44100
OUT = Path(__file__).resolve().parent.parent / "sounds"
RNG = np.random.default_rng(20261021)


def _t(duration):
    return np.arange(int(duration * SR)) / SR


def _fade(x, ms=4.0):
    n = min(int(SR * ms / 1000), len(x) // 2)
    if n:
        ramp = np.sin(np.linspace(0, np.pi / 2, n)) ** 2
        x[:n] *= ramp
        x[-n:] *= ramp[::-1]
    return x


# ---------------------------------------------------------------- building blocks

def sweep(f0, f1, duration):
    """Per-sample frequency curve moving exponentially from f0 to f1."""
    t = _t(duration)
    return f0 * (f1 / f0) ** (t / duration)


def osc(freq, duration, kind="saw"):
    """Band-limited oscillator. freq is a number or a per-sample array (for sweeps)."""
    n = int(duration * SR)
    f = np.full(n, float(freq)) if np.isscalar(freq) else np.asarray(freq, dtype=float)[:n]
    phase = 2 * np.pi * np.cumsum(f) / SR
    kmax = max(1, int(min(40, 18000 / f.max())))
    if kind == "sine":
        return np.sin(phase)
    out = np.zeros(n)
    for k in range(1, kmax + 1):
        if kind == "saw":
            out += np.sin(k * phase) / k
        elif kind == "square" and k % 2 == 1:
            out += np.sin(k * phase) / k
        elif kind == "tri" and k % 2 == 1:
            out += ((-1) ** ((k - 1) // 2)) * np.sin(k * phase) / (k * k)
    return out * {"saw": 2 / np.pi, "square": 4 / np.pi, "tri": 8 / np.pi ** 2}[kind]


def lowpass(x, cutoff):
    """Two cascaded one-pole filters (12 dB/oct). cutoff may be a number or a per-sample array."""
    c = np.full(len(x), float(cutoff)) if np.isscalar(cutoff) else np.asarray(cutoff, dtype=float)[:len(x)]
    a = 1 - np.exp(-2 * np.pi * c / SR)
    y1 = y2 = 0.0
    out = np.empty(len(x))
    for i in range(len(x)):
        y1 += a[i] * (x[i] - y1)
        y2 += a[i] * (y1 - y2)
        out[i] = y2
    return out


def gate(n, attack=0.003, release=0.02):
    """Flat envelope: on quickly, hold, off quickly."""
    env = np.ones(n)
    a, r = max(int(attack * SR), 1), max(int(release * SR), 1)
    env[:a] = np.linspace(0, 1, a)
    env[-r:] = np.minimum(env[-r:], np.linspace(1, 0, r))
    return env


def beep(freq, duration, kind="square", cutoff=3800, attack=0.003, release=0.02):
    """Timer-style beep."""
    return lowpass(osc(freq, duration, kind), cutoff) * gate(int(duration * SR), attack, release)


def click(duration=0.012, level=1.0):
    """Tiny bright transient that makes a hit feel physical."""
    t = _t(duration)
    noise = np.diff(RNG.standard_normal(len(t) + 1))
    return level * noise * np.exp(-t / 0.003)


def thump(f0=190, f1=58, duration=0.20):
    """Punchy low 'hit': a fast downward sine sweep with a quick decay."""
    t = _t(duration)
    return np.sin(2 * np.pi * np.cumsum(sweep(f0, f1, duration)) / SR) * np.exp(-t / 0.06)


def stab(freq, duration, open_hz=3600, closed_hz=500):
    """Short synth stab: a saw whose filter snaps shut - instrument-less, like a sport-watch alert."""
    t = _t(duration)
    cutoff = closed_hz + (open_hz - closed_hz) * np.exp(-t / (duration * 0.35))
    return lowpass(osc(freq, duration, "saw"), cutoff) * gate(len(t), 0.002, 0.03)


def release_tail(x, frac=0.45):
    """Let a sound die away smoothly over its last stretch, so it never ends on a click."""
    n = max(int(len(x) * frac), 1)
    x[-n:] *= 0.5 * (1 + np.cos(np.linspace(0, np.pi, n)))
    return x


def clang(freq=760.0, duration=1.5):
    """Struck metal: inharmonic partials with slight detune (shimmer) and a bright strike."""
    t = _t(duration)
    ratios = (1.0, 1.52, 2.14, 2.63, 3.31, 4.07, 5.18)
    amps = (1.0, 0.85, 0.70, 0.55, 0.45, 0.30, 0.20)
    out = np.zeros_like(t)
    for i, (r, amp) in enumerate(zip(ratios, amps)):
        tau = 0.6 / (0.55 + 0.5 * i)
        for detune in (0.998, 1.002):
            out += amp * 0.5 * np.sin(2 * np.pi * freq * r * detune * t) * np.exp(-t / tau)
    out[:int(0.010 * SR)] += click(0.010, 0.55)
    return release_tail(out, 0.5)


def alarm(freq, duration):
    """Urgent, slightly hard tone for alerts (soft-clipped sine => odd harmonics)."""
    t = _t(duration)
    return _fade(np.tanh(2.6 * np.sin(2 * np.pi * freq * t)) * 0.8, 6.0)


def place(*events, tail=0.0):
    """events: (start_seconds, samples, gain)."""
    length = max(int(s * SR) + len(x) for s, x, _ in events) + int(tail * SR)   # size in samples, not seconds
    buf = np.zeros(length)
    for start, x, gain in events:
        i = int(start * SR)
        buf[i:i + len(x)] += gain * x
    return buf


# ---------------------------------------------------------------- finishing

def _impulse_response(length_s=0.30, tau=0.07):
    t = _t(length_s)
    ir = RNG.standard_normal(len(t)) * np.exp(-t / tau)
    for i in range(1, len(ir)):                     # one-pole low-pass: warm, not hissy
        ir[i] = 0.4 * ir[i] + 0.6 * ir[i - 1]
    ir[0] = 0.0
    return ir / np.sqrt(np.sum(ir ** 2))


IR = _impulse_response()


def reverb(x, wet):
    if wet <= 0:
        return x
    n = len(x) + len(IR)
    tail = np.fft.irfft(np.fft.rfft(x, n) * np.fft.rfft(IR, n), n)
    out = np.zeros(n)
    out[:len(x)] = x * (1 - wet * 0.5)
    out += wet * 0.9 * tail
    return out


def finish(x, rms_dbfs, wet):
    """Light room, match loudness over the audible part, soft-limit under -1 dBFS, round the ends."""
    x = reverb(x, wet)
    x = x[: int(np.max(np.nonzero(np.abs(x) > 5e-3 * np.max(np.abs(x)))[0]) + 1)]
    active = np.abs(x) > 0.02 * np.max(np.abs(x))
    rms = np.sqrt(np.mean(x[active] ** 2))
    x = x * (10 ** (rms_dbfs / 20) / rms)
    knee, ceiling = 0.55, 0.891                      # -1 dBFS
    a = np.abs(x)
    over = a > knee
    x = np.where(over, np.sign(x) * (knee + (ceiling - knee) * np.tanh((a - knee) / (ceiling - knee))), x)
    return _fade(x, 8.0)


NORMAL, ALERT = -21.0, -15.0

CUES = {
    # ready: three short, flat, low beeps - the sound of an interval timer counting in
    "session_start": lambda: finish(place(
        (0.00, beep(760, 0.09), 1.0), (0.18, beep(760, 0.09), 1.0), (0.36, beep(760, 0.09), 1.0)), NORMAL, 0.06),
    # GO: low thump + click + a synth riser that lands on a bright hold
    "work_start": lambda: finish(place(
        (0.00, thump(), 1.0), (0.00, click(), 0.35),
        (0.00, release_tail(lowpass(osc(sweep(300, 1500, 0.20), 0.20, "saw"), sweep(800, 4200, 0.20)) * gate(int(0.20 * SR), 0.004, 0.002), 0.0), 0.8),
        (0.20, release_tail(beep(1500, 0.34, "saw", 4200, 0.002, 0.30), 0.7), 0.9)), NORMAL, 0.06),
    # clock paused: a soft falling sweep, like powering down
    "work_pause": lambda: finish(place(
        (0.00, release_tail(lowpass(osc(sweep(850, 280, 0.34), 0.34, "tri"), 2200) * gate(int(0.34 * SR), 0.01, 0.1), 0.5), 1.0)),
        NORMAL - 2.0, 0.04),
    # back in the zone: a very short rising chirp
    "work_resume": lambda: finish(place(
        (0.00, click(0.008, 0.25), 1.0),
        (0.00, lowpass(osc(sweep(420, 1100, 0.13), 0.13, "square"), 3000) * gate(int(0.13 * SR), 0.003, 0.04), 0.9)),
        NORMAL - 1.5, 0.04),
    # round complete: two quick strikes of a metallic ring bell
    "round_end": lambda: finish(place(
        (0.00, clang(760, 1.5), 1.0), (0.24, clang(760, 1.5), 0.9)), NORMAL, 0.08),
    # recovered, next round: two short synth stabs, a fifth apart
    "recovered": lambda: finish(place(
        (0.00, stab(620, 0.13), 0.9), (0.17, stab(930, 0.34), 1.0)), NORMAL, 0.05),
    # final bell: three quick strikes then a long ring
    "final_bell": lambda: finish(place(
        (0.00, clang(760, 1.2), 0.9), (0.22, clang(760, 1.2), 0.95), (0.44, clang(760, 1.9), 1.0)), NORMAL + 0.5, 0.08),
    # strap lost: three urgent beeps, dry (no reverb) for clarity
    "strap_lost": lambda: finish(place(
        (0.00, alarm(988, 0.13), 1.0), (0.22, alarm(988, 0.13), 1.0), (0.44, alarm(988, 0.13), 1.0)), ALERT, 0.0),
}


WHEN = {
    "session_start": "The session has started - get above your zone 5 line.",
    "work_start": "Zone 5 reached - your round clock is running. GO.",
    "work_pause": "You dropped out of zone 5 - the clock has paused.",
    "work_resume": "Back in zone 5 - the clock is running again.",
    "round_end": "Round complete - rest until you reach your recovered heart rate.",
    "recovered": "Recovered - the next round starts. Get back above zone 5.",
    "final_bell": "All rounds complete.",
    "strap_lost": "ALERT: a heart rate strap has stopped sending data.",
}

PREVIEW_HTML = """<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PT Studio sounds</title>
<style>
:root{--bg:#101a22;--panel:#182631;--line:#25384a;--text:#eaefec;--mute:#9fb0ba;--accent:#3fbfad;--alert:#e5484d}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,sans-serif;padding:20px;max-width:680px;margin-inline:auto}
h1{font-size:1.5rem;margin:.2em 0}p.sub{color:var(--mute);margin:0 0 1.2rem}
.row{display:flex;gap:14px;align-items:center;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:10px}
.row b{display:block}.row span{color:var(--mute);font-size:.9rem}
button{background:var(--accent);color:#06201c;border:0;border-radius:8px;font:600 1rem system-ui;padding:.6em 1em;cursor:pointer;min-width:5.5em}
.alert button{background:var(--alert);color:#fff}
.seq{background:transparent;color:var(--accent);border:1px solid var(--accent);margin:.6rem 0 1.2rem}
</style></head><body>
<h1>PT Studio sounds</h1>
<p class="sub">Tap to hear each cue. Use the volume you would have in the gym.</p>
<button class="seq" id="seq">Play a whole two-round fight in order</button>
%ROWS%
<script>
const clips = %CLIPS%;
const play = n => { const a = new Audio(clips[n]); a.play(); return a; };
document.querySelectorAll("[data-cue]").forEach(b => b.onclick = () => play(b.dataset.cue));
const order = [["session_start",0],["work_start",1.2],["work_pause",5],["work_resume",7],["round_end",10],
               ["recovered",13],["work_start",16],["final_bell",20],["strap_lost",25]];
document.getElementById("seq").onclick = () => order.forEach(([n,t]) => setTimeout(() => play(n), t*1000));
</script></body></html>"""


def write_preview(names):
    import base64
    rows, clips = [], {}
    for n in names:
        clips[n] = "data:audio/wav;base64," + base64.b64encode((OUT / f"{n}.wav").read_bytes()).decode()
        cls = " alert" if n == "strap_lost" else ""
        rows.append(f'<div class="row{cls}"><button data-cue="{n}">Play</button>'
                    f'<div><b>{n.replace("_", " ")}</b><span>{WHEN[n]}</span></div></div>')
    import json
    html = PREVIEW_HTML.replace("%ROWS%", "\n".join(rows)).replace("%CLIPS%", json.dumps(clips))
    (OUT / "preview.html").write_text(html, encoding="utf-8")


def write_wav(path, x):
    pcm = np.clip(x, -1, 1)
    pcm = (pcm * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def analyse(x):
    pcm = np.round(np.clip(x, -1, 1) * 32767) / 32767
    spec = np.abs(np.fft.rfft(pcm * np.hanning(len(pcm))))
    freqs = np.fft.rfftfreq(len(pcm), 1 / SR)
    active = np.abs(pcm) > 0.02 * np.max(np.abs(pcm))
    return {
        "dur": len(pcm) / SR,
        "peak_db": 20 * np.log10(np.max(np.abs(pcm))),
        "rms_db": 20 * np.log10(np.sqrt(np.mean(pcm[active] ** 2))),
        "edge": max(abs(pcm[0]), abs(pcm[-1])),
        "dc": abs(np.mean(pcm)),
        "centroid": float(np.sum(freqs * spec) / np.sum(spec)),
    }


def main():
    OUT.mkdir(exist_ok=True)
    print(f"{'cue':<14}{'secs':>6}{'peak dB':>9}{'rms dB':>8}{'centroid Hz':>13}{'end/start':>11}{'dc':>8}")
    for name, make in CUES.items():
        x = make()
        write_wav(OUT / f"{name}.wav", x)
        a = analyse(x)
        print(f"{name:<14}{a['dur']:>6.2f}{a['peak_db']:>9.1f}{a['rms_db']:>8.1f}{a['centroid']:>13.0f}{a['edge']:>11.4f}{a['dc']:>8.4f}")

    write_preview(list(CUES))
    print("\nWrote sounds/preview.html - open it in any browser to listen.")


if __name__ == "__main__":
    sys.exit(main())
