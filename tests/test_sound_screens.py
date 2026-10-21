"""Sound cues driven through the real screens, with a recording speaker and a fake clock."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import db
import elite
import sounds
from tests.test_elite_app import HAVE_FLET, STREAMS, ScreenTestCase, call, walk

if HAVE_FLET:
    import flet as ft
    import main as app


class Speaker:
    available = True
    reason = ""

    def __init__(self):
        self.played = []

    def play(self, cue, volume):
        self.played.append((cue, volume))
        return True

    @property
    def cues(self):
        return [c for c, _ in self.played]


class StopWatching(Exception):
    pass


@unittest.skipUnless(HAVE_FLET, "flet not installed")
class SoundScreenTests(ScreenTestCase):
    def setUp(self):
        super().setUp()
        self.speaker = Speaker()
        player = sounds.SoundPlayer(self.speaker, clock=self.clock.monotonic)
        patcher = patch.object(sounds, "create_player", lambda: player)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.player = player

    # ---- helpers ----
    def start_elite(self, rounds="2", round_len="0:10", with_sam=True):
        self.with_sam = with_sam

        async def go():
            await call(self.button("Scan for strap").on_click)
            await self.attach(self.ada, "AA:01")
            if with_sam:
                await self.attach(self.sam, "BB:02")
            self.pick(self.dropdown("Mode"), elite.MODE_ELITE)
            self.field("Rounds").value = rounds
            self.field("Round length (m:ss)").value = round_len
            self.field("Recovered at (% of HR reserve)").value = "70"
            self.field("Fight rest (m:ss)").value = "0:30"
            self.button("Start session").on_click(None)
        asyncio.run(go())

    def feed(self, plan):
        sam = 100 if self.with_sam else None        # Sam's strap only exists when he is attached
        for hr, seconds in plan:
            for _ in range(seconds):
                self.tick(hr, sam)

    def control(self, kind, test):
        return self.find(kind, test)

    def run_watchdog(self, iterations, keep_alive="sam", task_index=-1):
        """Runs the strap watchdog for N seconds of fake time; straps in keep_alive keep sending."""
        handler, args = self.page.tasks[task_index]
        if keep_alive == "sam":
            keep_alive = ("BB:02",) if self.with_sam else ()
        count = {"n": 0}

        async def fake_sleep(seconds):
            if count["n"] >= iterations:
                raise StopWatching
            count["n"] += 1
            self.clock.now += seconds
            for address in keep_alive:
                STREAMS[address].on_sample(120, [])

        async def run():
            with patch.object(asyncio, "sleep", fake_sleep):
                try:
                    await handler(*args)
                except StopWatching:
                    pass
        asyncio.run(run())
        return count["n"]

    # ---- elite cues ----
    def test_a_two_round_session_plays_the_right_cues_in_order(self):
        self.build()
        self.start_elite()
        self.feed(((100, 5), (185, 15), (100, 10), (100, 4), (185, 15)))
        self.assertEqual(self.speaker.cues,
                         ["session_start", "work_start", "round_end", "recovered", "work_start", "final_bell"])

    def test_work_clock_pause_and_resume_have_their_own_cues(self):
        self.build()
        self.start_elite(round_len="0:30", with_sam=False)
        self.feed(((100, 3), (185, 10)))
        self.assertEqual(self.speaker.cues, ["session_start", "work_start"])
        self.feed(((100, 9),))                                   # drop out of zone 5 past the grace period
        self.assertEqual(self.speaker.cues[-1], "work_pause")
        self.feed(((185, 6),))
        self.assertEqual(self.speaker.cues[-2:], ["work_pause", "work_resume"])
        self.assertEqual(self.speaker.cues.count("work_pause"), 1)           # once per drop, not every second

    def test_pressing_stop_does_not_ring_the_final_bell(self):
        self.build()
        self.start_elite()
        self.feed(((100, 3),))
        asyncio.run(call(self.button("Stop").on_click))
        self.assertNotIn("final_bell", self.speaker.cues)
        self.assertNotIn("round_end", self.speaker.cues)

    def test_normal_sessions_make_no_cues(self):
        self.build()

        async def go():
            await self.start_normal_session()
            for _ in range(6):
                self.tick(150)
        asyncio.run(go())
        self.assertEqual(self.speaker.cues, [])

    # ---- controls ----
    def test_mute_switch_silences_cues_and_is_remembered(self):
        self.build()
        switch = self.control(ft.Switch, lambda c: c.label == "Sounds")
        switch.value = False
        switch.on_change(SimpleNamespace(control=switch))
        self.start_elite(with_sam=False)
        self.feed(((100, 3), (185, 15)))
        self.assertEqual(self.speaker.cues, [])
        self.assertEqual(db.get_setting("sound_enabled"), "0")

    def test_volume_slider_sets_the_playback_volume(self):
        self.build()
        slider = self.control(ft.Slider, lambda c: True)
        slider.value = 0.3
        slider.on_change(SimpleNamespace(control=slider))
        self.start_elite(with_sam=False)
        self.assertEqual(self.speaker.played[0], ("session_start", 0.3))
        self.assertEqual(db.get_setting("sound_volume"), "0.30")

    def test_test_button_plays_a_cue_even_twice_in_a_row(self):
        self.build()
        self.button("Test sound").on_click(None)
        self.button("Test sound").on_click(None)
        self.assertEqual(self.speaker.cues, ["work_start", "work_start"])

    def test_live_mute_button_toggles_sound(self):
        self.build()
        mute = self.control(ft.IconButton, lambda c: c.tooltip == "Mute / unmute sounds")
        mute.on_click(None)
        self.assertFalse(self.player.enabled)
        self.assertEqual(mute.icon, ft.Icons.VOLUME_OFF)
        mute.on_click(None)
        self.assertTrue(self.player.enabled)
        self.assertEqual(mute.icon, ft.Icons.VOLUME_UP)

    def test_controls_are_disabled_with_an_explanation_when_there_is_no_sound_device(self):
        silent = sounds.SoundPlayer(sounds.NullBackend())
        silent.backend.reason = "Sound unavailable - run: python -m pip install -r requirements.txt"
        with patch.object(sounds, "create_player", lambda: silent):
            self.build()
        self.assertTrue(self.control(ft.Switch, lambda c: c.label == "Sounds").disabled)
        self.assertTrue(self.button("Test sound").disabled)
        self.assertIn("pip install -r requirements.txt", " ".join(self.texts()))
        self.start_elite(with_sam=False)                          # and a session still runs fine
        self.feed(((100, 2),))
        self.assertTrue(any(t.startswith("PRE-WORK") for t in self.texts()))

    # ---- strap silence ----
    def test_a_silent_strap_raises_the_alarm_and_a_banner_naming_who(self):
        self.build()
        self.start_elite()
        self.feed(((100, 2),))
        self.speaker.played.clear()
        self.run_watchdog(8)                                     # Ada's strap says nothing; Sam's keeps going
        self.assertEqual(self.speaker.cues, ["strap_lost"])
        self.assertTrue(any(t == "NO SIGNAL: Ada North" for t in self.texts()))

    def test_the_alarm_repeats_while_the_strap_stays_down_and_clears_when_it_returns(self):
        self.build()
        self.start_elite()
        self.feed(((100, 2),))
        self.speaker.played.clear()
        self.run_watchdog(18)
        self.assertEqual(self.speaker.cues, ["strap_lost", "strap_lost"])    # ~6s in, then ~10s later
        STREAMS["AA:01"].on_sample(120, [])                                  # the strap comes back
        self.run_watchdog(2, task_index=0)
        self.assertFalse(any(t.startswith("NO SIGNAL") for t in self.texts()))

    def test_a_strap_that_never_speaks_is_caught(self):
        self.build()
        self.start_elite(with_sam=False)
        self.speaker.played.clear()
        self.run_watchdog(7, keep_alive=())
        self.assertEqual(self.speaker.cues, ["strap_lost"])

    def test_healthy_straps_never_alarm(self):
        self.build()
        self.start_elite()
        self.speaker.played.clear()
        self.run_watchdog(20, keep_alive=("AA:01", "BB:02"))
        self.assertEqual(self.speaker.cues, [])

    def test_a_restarted_session_retires_the_old_watchdog(self):
        self.build()
        self.start_elite(with_sam=False)
        self.feed(((100, 2),))
        self.button("Restart").on_click(None)
        dialog = self.page.dialogs[-1]
        next(c for c in walk(dialog) if isinstance(c, ft.FilledButton) and c.content == "Restart").on_click(None)
        self.assertEqual(len(self.page.tasks), 2)
        self.speaker.played.clear()
        ran = self.run_watchdog(30, keep_alive=(), task_index=0)             # the OLD watchdog
        self.assertEqual(ran, 1)                                              # exits on its first tick
        self.assertEqual(self.speaker.cues, [])


if __name__ == "__main__":
    unittest.main()
