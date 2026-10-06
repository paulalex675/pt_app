import unittest

import elite
import zones
from elite import DONE, PREWORK, RECOVERY, WORK, EliteEngine, EliteParams


class Run:
    """Drives an engine with a 1 Hz heart rate trace and records every phase event."""

    def __init__(self, **overrides):
        base = dict(rounds=2, round_s=10.0, z5_low=170.0, recovered_bpm=130.0)
        base.update(overrides)
        self.engine = EliteEngine(EliteParams(**base), t0=0.0)
        self.t = 0.0
        self.events = list(self.engine.pop_events())  # includes the initial PREWORK event

    def hold(self, seconds, hr):
        for _ in range(int(seconds)):
            self.t += 1
            self.events.extend(self.engine.update(self.t, hr))
        return self

    def jump(self, seconds, hr):
        """Skip ahead without samples (strap dropout), then deliver one sample."""
        self.t += seconds
        self.events.extend(self.engine.update(self.t, hr))
        return self

    @property
    def phases(self):
        return [e.phase for e in self.events]


class FullCycleTests(unittest.TestCase):
    def test_two_rounds_run_prework_work_recovery_then_done(self):
        r = Run()
        r.hold(20, 100)       # round 1 pre-work: warming up
        r.hold(15, 180)       # zone 5: round clock runs, round completes
        r.hold(30, 100)       # recovery
        r.hold(20, 100)       # round 2 pre-work
        r.hold(15, 180)       # round 2 work
        self.assertEqual(r.phases, [PREWORK, WORK, RECOVERY, PREWORK, WORK, DONE])
        res = r.engine.results
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0].work_s, 10.0)
        self.assertEqual(res[0].paused_s, 0.0)
        self.assertTrue(20 <= res[0].prework_s <= 25, res[0].prework_s)
        self.assertIsNotNone(res[0].recovery_s)
        self.assertIsNone(res[1].recovery_s)          # last round has no recovery
        self.assertTrue(r.engine.done)
        self.assertEqual(r.engine.total_s, r.engine.results[-1].ended_t)

    def test_total_time_includes_every_phase(self):
        r = Run(rounds=1)
        r.hold(10, 100).hold(20, 180)
        res = r.engine.results[0]
        # pre-work + work (+ any pause) accounts for the whole of the single round
        self.assertAlmostEqual(res.prework_s + res.work_s + res.paused_s, r.engine.total_s, delta=3.0)


class GatingTests(unittest.TestCase):
    def test_single_spike_does_not_start_the_round(self):
        r = Run()
        r.hold(5, 100).hold(1, 200).hold(5, 100)
        self.assertEqual(r.engine.phase, PREWORK)

    def test_one_sample_dip_is_absorbed_by_the_grace_period(self):
        r = Run(round_s=20.0, rounds=1)
        r.hold(8, 180)        # reach zone 5 and start the clock
        r.hold(1, 120)        # a single low reading
        r.hold(30, 180)
        self.assertEqual(r.engine.results[0].paused_s, 0.0)
        self.assertEqual(r.engine.results[0].work_s, 20.0)

    def test_sustained_drop_pauses_the_clock_and_work_resumes(self):
        r = Run(round_s=20.0, rounds=1)
        r.hold(8, 180)
        banked = r.engine.snapshot()["work_remaining_s"]
        r.hold(12, 120)       # clearly out of zone 5
        self.assertFalse(r.engine.snapshot()["running"])
        paused_remaining = r.engine.snapshot()["work_remaining_s"]
        r.hold(10, 120)       # still out: nothing more is banked
        self.assertEqual(r.engine.snapshot()["work_remaining_s"], paused_remaining)
        self.assertLess(paused_remaining, banked)
        r.hold(40, 180)       # back in zone 5: finishes
        res = r.engine.results[0]
        self.assertEqual(res.work_s, 20.0)
        self.assertGreater(res.paused_s, 15.0)

    def test_recovery_waits_for_the_recovered_level(self):
        r = Run()
        r.hold(12, 180)
        self.assertEqual(r.engine.phase, RECOVERY)
        r.hold(30, 150)       # resting but still above the recovered level
        self.assertEqual(r.engine.phase, RECOVERY)
        r.hold(10, 100)
        self.assertEqual(r.engine.phase, PREWORK)
        self.assertEqual(r.engine.round_no, 2)


class MeasurementTests(unittest.TestCase):
    def test_hr_drop_after_60s_of_recovery(self):
        r = Run(max_recovery_s=300.0)
        r.hold(12, 180)
        r.hold(70, 160)       # slow recovery: still high at 60s
        r.hold(10, 100)
        first = r.engine.results[0]
        self.assertIsNotNone(first.hr_end_work)
        self.assertIsNotNone(first.hr_60s)
        self.assertEqual(first.hr_drop_60, first.hr_end_work - first.hr_60s)
        self.assertGreater(first.hr_drop_60, 0)

    def test_fast_recovery_has_no_60s_figure(self):
        r = Run()
        r.hold(12, 180).hold(15, 100)
        self.assertIsNone(r.engine.results[0].hr_60s)
        self.assertIsNone(r.engine.results[0].hr_drop_60)
        self.assertLess(r.engine.results[0].recovery_s, 20)

    def test_gap_in_samples_is_never_credited_as_work(self):
        r = Run(round_s=30.0, rounds=1)
        r.hold(8, 180)
        before = r.engine.snapshot()["work_remaining_s"]
        r.jump(20, 180)       # strap dropped out for 20s
        after = r.engine.snapshot()["work_remaining_s"]
        self.assertGreaterEqual(after, before - 1.5)   # at most one normal second banked
        r.hold(60, 180)
        res = r.engine.results[0]
        self.assertGreaterEqual(res.paused_s, 20.0)


class CapTests(unittest.TestCase):
    def test_prework_cap_flags_the_round_and_moves_on(self):
        r = Run(max_prework_s=20.0)
        r.hold(25, 100)
        first = r.engine.results[0]
        self.assertIn("prework_cap", first.flags)
        self.assertEqual(first.work_s, 0.0)

    def test_pause_cap_ends_the_round_early(self):
        r = Run(max_pause_s=10.0, round_s=60.0)
        r.hold(8, 180)
        r.hold(30, 100)
        first = r.engine.results[0]
        self.assertIn("pause_cap", first.flags)
        self.assertLess(first.work_s, 60.0)

    def test_recovery_cap_moves_to_the_next_round(self):
        r = Run(max_recovery_s=30.0)
        r.hold(12, 180)
        r.hold(40, 150)       # never gets to the recovered level
        first = r.engine.results[0]
        self.assertIn("recovery_cap", first.flags)
        self.assertEqual(r.engine.round_no, 2)


class StopTests(unittest.TestCase):
    def test_finish_mid_work_flags_the_round_as_stopped(self):
        r = Run(round_s=60.0)
        r.hold(15, 180)
        r.events.extend(r.engine.finish(r.t))
        res = r.engine.results[0]
        self.assertIn("stopped", res.flags)
        self.assertLess(res.work_s, 60.0)
        self.assertTrue(r.engine.done)
        self.assertEqual(r.phases[-1], DONE)

    def test_finish_in_recovery_flags_the_finished_round(self):
        r = Run()
        r.hold(12, 180).hold(5, 150)
        self.assertEqual(r.engine.phase, RECOVERY)
        r.engine.finish(r.t)
        self.assertIn("stopped", r.engine.results[0].flags)

    def test_updates_after_done_are_ignored(self):
        r = Run(rounds=1)
        r.hold(12, 180)
        self.assertTrue(r.engine.done)
        count = len(r.engine.results)
        r.hold(10, 190)
        self.assertEqual(len(r.engine.results), count)


class HelperTests(unittest.TestCase):
    def test_recovered_level_uses_heart_rate_reserve(self):
        thresholds = zones.zone_thresholds(age=30, resting_hr=60, max_hr=190)
        self.assertEqual(thresholds[-1], 190)
        self.assertEqual(elite.recovered_bpm(thresholds, 60, 0.70), 60 + 0.70 * 130)

    def test_recovered_level_without_resting_hr_uses_percent_of_max(self):
        thresholds = zones.zone_thresholds(age=30, resting_hr=None, max_hr=190)
        self.assertEqual(elite.recovered_bpm(thresholds, None, 0.70), 0.70 * 190)

    def test_params_take_zone_five_from_the_clients_thresholds(self):
        thresholds = zones.zone_thresholds(age=30, resting_hr=60, max_hr=190)
        settings = {"rounds": 3, "round_s": 180.0, "recovered_pct": 0.7}
        params = elite.params_for_client(thresholds, 60, settings)
        self.assertEqual(params.z5_low, thresholds[4])
        self.assertEqual(params.rounds, 3)

    def test_parse_duration(self):
        self.assertEqual(elite.parse_duration("3:00"), 180.0)
        self.assertEqual(elite.parse_duration("1:30"), 90.0)
        self.assertEqual(elite.parse_duration("90"), 90.0)
        self.assertIsNone(elite.parse_duration(""))
        self.assertIsNone(elite.parse_duration("abc"))

    def test_parse_settings_computes_the_fight_length_target(self):
        settings, error = elite.parse_settings("5", "3:00", "70", "1:00")
        self.assertIsNone(error)
        self.assertEqual(settings["rounds"], 5)
        self.assertEqual(settings["round_s"], 180.0)
        self.assertEqual(settings["recovered_pct"], 0.70)
        self.assertEqual(settings["target_total_s"], 5 * 180 + 4 * 60)

    def test_parse_settings_rejects_bad_input(self):
        for args in [("0", "3:00", "70", "1:00"), ("x", "3:00", "70", "1:00"),
                     ("3", "0:05", "70", "1:00"), ("3", "3:00", "20", "1:00"),
                     ("3", "3:00", "70", "9:00"), ("3", "", "70", "1:00")]:
            settings, error = elite.parse_settings(*args)
            self.assertIsNone(settings, args)
            self.assertTrue(error, args)

    def test_format_clock(self):
        self.assertEqual(elite.format_clock(125), "2:05")
        self.assertEqual(elite.format_clock(None), "--:--")


if __name__ == "__main__":
    unittest.main()
