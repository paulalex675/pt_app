import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import db
import zones
from main import (
    can_start_session,
    chart_fill_fraction,
    chart_marker_fraction,
    estimate_calories,
)
from zones import zone_for_hr


class SessionLogicTests(unittest.TestCase):
    def test_can_start_session_requires_connected_client_and_no_active_session(self):
        self.assertTrue(can_start_session(True, "client-123", None))
        self.assertFalse(can_start_session(True, "client-123", "session-456"))
        self.assertFalse(can_start_session(False, "client-123", None))
        self.assertFalse(can_start_session(True, None, None))

    def test_zone_for_hr_handles_resting_hr_below_z1(self):
        thresholds = [100, 120, 140, 160, 180, 200]
        self.assertEqual(zone_for_hr(90, thresholds), 0)
        self.assertEqual(zone_for_hr(100, thresholds), 0)
        self.assertEqual(zone_for_hr(130, thresholds), 1)
        self.assertEqual(zone_for_hr(170, thresholds), 3)

    def test_stored_max_hr_overrides_estimated_zone_thresholds(self):
        thresholds = zones.zone_thresholds(30, 60, max_hr=205)
        self.assertEqual(thresholds[-1], 205)
        self.assertEqual(thresholds[0], round(60 + 0.5 * (205 - 60)))

    def test_chart_marker_is_at_max_hr_and_bar_allows_overrun(self):
        self.assertAlmostEqual(chart_marker_fraction(), 1 / 1.15)
        self.assertAlmostEqual(chart_fill_fraction(180, 180), 1 / 1.15)
        self.assertEqual(chart_fill_fraction(220, 180), 1.0)

    def test_calorie_estimate_requires_demographics(self):
        estimate = estimate_calories("female", 30, 65, 140, 1800)
        self.assertGreater(estimate, 0)
        self.assertIsNone(estimate_calories("other", 30, 65, 140, 1800))
        self.assertIsNone(estimate_calories("male", 30, None, 140, 1800))

    def test_session_records_multiple_participants_and_sample_owners(self):
        with TemporaryDirectory() as temp_dir, patch.object(db, "DB_PATH", Path(temp_dir) / "test.db"):
            db.init_db()
            client_a = db.create_client("Ada", "North")
            client_b = db.create_client("Sam", "South")
            self.assertIsNone(db.get_client(client_a)["max_hr"])
            db.update_client_resting_hr(client_a, 54)
            self.assertEqual(db.get_client(client_a)["resting_hr"], 54)
            self.assertEqual(db.update_client_max_hr(client_a, 198), 198)
            self.assertEqual(db.update_client_max_hr(client_a, 194), 198)
            self.assertEqual(db.update_client_max_hr(client_a, 202), 202)
            session_id = db.start_session(client_a, "strap-a")
            started = db.get_session(session_id)["started_at"]
            db.add_session_participant(session_id, client_a, "strap-a")
            db.add_session_participant(session_id, client_b, "strap-b")
            db.log_sample(session_id, 132, "[]", client_id=client_b, strap_device_id="strap-b")
            db.log_sample(session_id, 150, "[]", client_id=client_b, strap_device_id="strap-b")
            db.end_session(session_id)

            participants = db.list_session_participants(session_id)
            samples = db.session_samples(session_id)
            session, summaries = db.session_summary(session_id)

            self.assertTrue(started)
            self.assertTrue(session["ended_at"])
            self.assertEqual({row["client_id"] for row in participants}, {client_a, client_b})
            self.assertEqual({row["client_id"] for row in samples}, {client_b})
            self.assertEqual({row["strap_device_id"] for row in samples}, {"strap-b"})
            client_b_summary = next(row for row in summaries if row["client_id"] == client_b)
            self.assertEqual(client_b_summary["max_hr"], 150)
            self.assertEqual(client_b_summary["average_hr"], 141)
