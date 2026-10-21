import os
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import db
import sounds

try:
    import numpy as np
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False


class FakeBackend:
    available = True
    reason = ""

    def __init__(self, succeed=True):
        self.played, self.succeed = [], succeed

    def play(self, cue, volume):
        self.played.append((cue, volume))
        return self.succeed


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def make_player(**kw):
    clock = Clock()
    backend = FakeBackend()
    return sounds.SoundPlayer(backend, clock=clock, **kw), backend, clock


class EventMappingTests(unittest.TestCase):
    def test_every_phase_has_the_right_cue(self):
        self.assertEqual(sounds.cue_for_event("prework", 1), sounds.SESSION_START)
        self.assertEqual(sounds.cue_for_event("prework", 2), sounds.RECOVERED)   # next round = recovered
        self.assertEqual(sounds.cue_for_event("work", 1), sounds.WORK_START)
        self.assertEqual(sounds.cue_for_event("recovery", 3), sounds.ROUND_END)
        self.assertEqual(sounds.cue_for_event("done", 4), sounds.FINAL_BELL)
        self.assertIsNone(sounds.cue_for_event("mystery", 1))


class PlayerTests(unittest.TestCase):
    def test_plays_at_the_set_volume(self):
        player, backend, _ = make_player(volume=0.5)
        self.assertTrue(player.play(sounds.WORK_START))
        self.assertEqual(backend.played, [(sounds.WORK_START, 0.5)])

    def test_muted_zero_volume_unknown_cue_and_missing_backend_stay_silent(self):
        player, backend, _ = make_player(enabled=False)
        self.assertFalse(player.play(sounds.WORK_START))
        player, backend, _ = make_player(volume=0.0)
        self.assertFalse(player.play(sounds.WORK_START))
        player, backend, _ = make_player()
        self.assertFalse(player.play("not-a-cue"))
        self.assertFalse(player.play(None))
        self.assertEqual(backend.played, [])
        silent = sounds.SoundPlayer(sounds.NullBackend())
        self.assertFalse(silent.play(sounds.WORK_START))
        self.assertFalse(silent.available)

    def test_the_same_cue_is_not_repeated_inside_the_debounce_window(self):
        player, backend, clock = make_player(debounce_s=1.5)
        self.assertTrue(player.play(sounds.WORK_START))
        clock.now += 0.4
        self.assertFalse(player.play(sounds.WORK_START))      # five fighters hit zone 5 together: one "go"
        self.assertTrue(player.play(sounds.ROUND_END))        # a different cue is unaffected
        clock.now += 1.5
        self.assertTrue(player.play(sounds.WORK_START))

    def test_safety_cues_are_never_held_back(self):
        player, backend, clock = make_player()
        for _ in range(3):
            self.assertTrue(player.play(sounds.STRAP_LOST))
        self.assertEqual(len(backend.played), 3)

    def test_a_failed_play_does_not_start_the_debounce(self):
        player, backend, clock = make_player()
        backend.succeed = False
        self.assertFalse(player.play(sounds.WORK_START))
        backend.succeed = True
        self.assertTrue(player.play(sounds.WORK_START))

    def test_force_bypasses_debounce_but_not_mute(self):
        player, backend, clock = make_player()
        player.play(sounds.WORK_START)
        self.assertTrue(player.play(sounds.WORK_START, force=True))
        player.enabled = False
        self.assertFalse(player.play(sounds.WORK_START, force=True))

    def test_status_text(self):
        player, _, _ = make_player()
        self.assertEqual(player.status, "On")
        player.enabled = False
        self.assertEqual(player.status, "Muted")

    def test_missing_pygame_gives_a_helpful_message_not_a_crash(self):
        with TemporaryDirectory() as d, patch.object(db, "DB_PATH", Path(d) / "t.db"):
            db.init_db()
            with patch.dict(os.environ, {"PT_STUDIO_SOUND": "on"}), \
                    patch.object(sounds, "PygameBackend", side_effect=ImportError("no pygame")):
                player = sounds.create_player()
            self.assertFalse(player.available)
            self.assertFalse(player.play(sounds.WORK_START))
            self.assertIn("pip install -r requirements.txt", player.status)

    def test_no_audio_device_gives_a_message_not_a_crash(self):
        with TemporaryDirectory() as d, patch.object(db, "DB_PATH", Path(d) / "t.db"):
            db.init_db()
            with patch.dict(os.environ, {"PT_STUDIO_SOUND": "on"}), \
                    patch.object(sounds, "PygameBackend", side_effect=RuntimeError("no device")):
                player = sounds.create_player()
            self.assertFalse(player.available)
            self.assertIn("no device", player.status)


class SettingsTests(unittest.TestCase):
    def test_mute_and_volume_survive_a_restart(self):
        with TemporaryDirectory() as d, patch.object(db, "DB_PATH", Path(d) / "t.db"):
            db.init_db()
            player, _, _ = make_player()
            player.set_enabled(False)
            player.set_volume(0.3)
            again, _, _ = make_player()
            again.load_settings()
            self.assertEqual((again.enabled, again.volume), (False, 0.3))

    def test_bad_stored_values_fall_back_to_defaults(self):
        with TemporaryDirectory() as d, patch.object(db, "DB_PATH", Path(d) / "t.db"):
            db.init_db()
            db.set_setting("sound_volume", "loud")
            player, _, _ = make_player()
            player.load_settings()
            self.assertEqual(player.volume, sounds.DEFAULT_VOLUME)
            db.set_setting("sound_volume", "7")
            player.load_settings()
            self.assertEqual(player.volume, 1.0)

    def test_create_player_is_silent_when_switched_off(self):
        with TemporaryDirectory() as d, patch.object(db, "DB_PATH", Path(d) / "t.db"):
            db.init_db()
            with patch.dict(os.environ, {"PT_STUDIO_SOUND": "off"}):
                self.assertFalse(sounds.create_player().available)


class StrapMonitorTests(unittest.TestCase):
    def test_alerts_once_when_a_strap_goes_quiet_then_every_repeat(self):
        m = sounds.StrapMonitor(timeout_s=5, repeat_s=10)
        heard = {"a": 98.0}
        self.assertEqual(m.check(100.0, heard), [])            # quiet for 2s: fine
        self.assertEqual(m.check(102.9, heard), [])            # 4.9s: still fine
        self.assertEqual(m.check(103.1, heard), ["a"])         # 5.1s: alert
        self.assertEqual(m.check(110.0, heard), [])            # still quiet, too soon to repeat
        self.assertEqual(m.check(113.2, heard), ["a"])         # 10s after the first alert: repeat

    def test_recovery_rearms_the_alert(self):
        m = sounds.StrapMonitor(timeout_s=5, repeat_s=10)
        self.assertEqual(m.check(110, {"a": 100.0}), ["a"])
        self.assertEqual(m.check(111, {"a": 110.9}), [])       # samples are flowing again
        self.assertEqual(m.check(120, {"a": 110.9}), ["a"])    # next dropout alerts immediately

    def test_only_the_silent_client_is_reported(self):
        m = sounds.StrapMonitor()
        self.assertEqual(m.check(100, {"a": 99.0, "b": 90.0}), ["b"])

    def test_clients_with_no_clock_yet_or_removed_are_ignored(self):
        m = sounds.StrapMonitor()
        self.assertEqual(m.check(100, {"a": None}), [])
        self.assertEqual(m.check(100, {"b": 50.0}), ["b"])
        self.assertEqual(m.check(101, {}), [])                  # b removed
        self.assertEqual(m.check(102, {"b": 50.0}), ["b"])      # and would alert again if re-added


class CueFileTests(unittest.TestCase):
    def load(self, cue):
        with wave.open(str(sounds.SOUND_DIR / f"{cue}.wav")) as w:
            params = w.getparams()
            data = w.readframes(w.getnframes())
        return params, data

    def test_every_cue_has_a_clean_wav(self):
        for cue in sounds.CUES:
            with self.subTest(cue=cue):
                params, data = self.load(cue)
                self.assertEqual((params.nchannels, params.sampwidth, params.framerate), (1, 2, 44100))
                seconds = params.nframes / params.framerate
                self.assertTrue(0.3 < seconds <= 3.0, seconds)
                if HAVE_NUMPY:
                    x = np.frombuffer(data, "<i2") / 32768
                    self.assertLess(np.max(np.abs(x)), 0.9)                  # under -0.9 dBFS: no clipping
                    self.assertGreater(np.max(np.abs(x)), 0.1)               # and not silent
                    self.assertLess(max(abs(x[0]), abs(x[-1])), 0.002)       # starts/ends at zero: no click

    def test_loudness_is_matched_and_alerts_stand_out(self):
        if not HAVE_NUMPY:
            self.skipTest("numpy not installed")
        rms = {}
        for cue in sounds.CUES:
            x = np.frombuffer(self.load(cue)[1], "<i2") / 32768
            active = np.abs(x) > 0.02 * np.max(np.abs(x))
            rms[cue] = 20 * np.log10(np.sqrt(np.mean(x[active] ** 2)))
        normal = [v for k, v in rms.items() if k not in sounds.SAFETY_CUES]
        self.assertLess(max(normal) - min(normal), 3.5)                      # one product, not eight volumes
        self.assertGreater(rms[sounds.STRAP_LOST], max(normal) + 4)          # the alert is clearly louder

    def test_cues_are_distinguishable_by_length_or_pitch(self):
        if not HAVE_NUMPY:
            self.skipTest("numpy not installed")
        profile = {}
        for cue in sounds.CUES:
            params, data = self.load(cue)
            x = np.frombuffer(data, "<i2") / 32768
            spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
            centroid = np.sum(np.fft.rfftfreq(len(x), 1 / 44100) * spec) / np.sum(spec)
            profile[cue] = (params.nframes / 44100, centroid)
        cues = list(profile)
        for i, a in enumerate(cues):
            for b in cues[i + 1:]:
                (da, ca), (db_, cb) = profile[a], profile[b]
                self.assertTrue(abs(da - db_) > 0.1 or abs(ca - cb) > 40, f"{a} vs {b} too alike")


class PygameBackendTests(unittest.TestCase):
    def test_every_cue_loads_and_plays_through_the_real_mixer(self):
        try:
            import pygame
        except ImportError:
            self.skipTest("pygame not installed")
        with patch.dict(os.environ, {"SDL_AUDIODRIVER": "dummy"}):
            try:
                backend = sounds.PygameBackend()
            except Exception as exc:
                self.skipTest(f"no mixer available: {exc}")
            try:
                for cue in sounds.CUES:
                    self.assertTrue(backend.play(cue, 0.7), cue)
                self.assertFalse(backend.play("nope", 0.7))                  # missing file: False, no crash
            finally:
                pygame.mixer.quit()


if __name__ == "__main__":
    unittest.main()
