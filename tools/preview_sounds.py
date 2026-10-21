"""Plays every cue in turn through this machine's speakers: python tools/preview_sounds.py"""
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sounds


def main():
    try:
        backend = sounds.PygameBackend()
    except Exception as exc:
        print(f"Cannot play sound here: {exc}\nTry: python -m pip install -r requirements.txt")
        return 1
    descriptions = {
        "session_start": "session starts", "work_start": "zone 5 reached - GO", "work_pause": "dropped out of zone 5",
        "work_resume": "back in zone 5", "round_end": "round complete", "recovered": "recovered - next round",
        "final_bell": "all rounds done", "strap_lost": "ALERT - strap lost",
    }
    for cue in sounds.CUES:
        with wave.open(str(sounds.SOUND_DIR / f"{cue}.wav")) as w:
            seconds = w.getnframes() / w.getframerate()
        print(f"{cue:<14} {descriptions[cue]}")
        backend.play(cue, 0.8)
        time.sleep(seconds + 0.7)
    return 0


if __name__ == "__main__":
    sys.exit(main())
