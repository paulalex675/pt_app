import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import db
import export
import zones

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lambda"))
# The Lambda reads these at import time.
os.environ.setdefault("BUCKET_NAME", "test-bucket")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-2")

try:
    import boto3
    from moto import mock_aws
    HAVE_MOTO = True
except ImportError:  # moto is a dev-only dependency
    HAVE_MOTO = False


class MaxHrGuardTests(unittest.TestCase):
    def test_single_spike_is_ignored(self):
        streak = {}
        self.assertIsNone(zones.track_new_max(streak, 250, 190))  # implausible
        self.assertIsNone(zones.track_new_max(streak, 195, 190))  # one reading only
        self.assertIsNone(zones.track_new_max(streak, 150, 190))  # streak broken

    def test_sustained_readings_confirm_lowest_of_streak(self):
        streak = {}
        self.assertIsNone(zones.track_new_max(streak, 196, 190))
        self.assertIsNone(zones.track_new_max(streak, 199, 190))
        self.assertEqual(zones.track_new_max(streak, 197, 190), 196)

    def test_streak_resets_after_confirmation(self):
        streak = {}
        for hr in (196, 197, 198):
            zones.track_new_max(streak, hr, 190)
        self.assertIsNone(zones.track_new_max(streak, 199, 196))


def _make_session(temp_dir):
    """Two clients share a session, each with their own strap and samples."""
    patcher = patch.object(db, "DB_PATH", Path(temp_dir) / "t.db")
    patcher.start()
    db.init_db()
    a = db.create_client("Ada", "North", "female", "1990-06-15", 168, 62, 55)
    b = db.create_client("Sam", "South", "male", "1985-01-01", 180, 82, 60)
    sid = db.start_session(a, "strap-a")
    db.add_session_participant(sid, a, "strap-a")
    db.add_session_participant(sid, b, "strap-b")
    for i in range(20):
        db.log_sample(sid, 100 + i, "[]", client_id=a, strap_device_id="strap-a")
        db.log_sample(sid, 120 + i, "[]", client_id=b, strap_device_id="strap-b")
    db.end_session(sid)
    return patcher, sid, a, b


class ExportTests(unittest.TestCase):
    def test_one_record_per_client_with_only_their_samples(self):
        with TemporaryDirectory() as d:
            patcher, sid, a, b = _make_session(d)
            try:
                exports = export.build_client_exports(sid)
                self.assertEqual({e["client_id"] for e in exports}, {a, b})
                by_client = {e["client_id"]: e for e in exports}
                self.assertEqual(len(by_client[a]["samples"]), 20)
                self.assertTrue(all(100 <= s["hr"] < 120 for s in by_client[a]["samples"]))
                self.assertTrue(all(120 <= s["hr"] < 140 for s in by_client[b]["samples"]))
                self.assertEqual(by_client[a]["strap_device_id"], "strap-a")
            finally:
                patcher.stop()

    def test_records_are_pseudonymous(self):
        with TemporaryDirectory() as d:
            patcher, sid, a, b = _make_session(d)
            try:
                blob = json.dumps(export.build_client_exports(sid))
                for secret in ("Ada", "North", "Sam", "South", "1990-06-15", "1985-01-01"):
                    self.assertNotIn(secret, blob)
                first = export.build_client_exports(sid)[0]["client"]
                self.assertIn("age_at_session", first)
                self.assertNotIn("dob", first)
            finally:
                patcher.stop()

    def test_unfinished_session_is_refused(self):
        with TemporaryDirectory() as d:
            patcher = patch.object(db, "DB_PATH", Path(d) / "t.db")
            patcher.start()
            try:
                db.init_db()
                a = db.create_client("A", "B")
                sid = db.start_session(a, "strap-a")
                with self.assertRaises(ValueError):
                    export.build_client_exports(sid)
            finally:
                patcher.stop()


@unittest.skipUnless(HAVE_MOTO, "moto not installed (pip install -r requirements-dev.txt)")
class CloudPipelineTests(unittest.TestCase):
    def test_multi_client_session_through_to_gold_and_erasure(self):
        with TemporaryDirectory() as d, mock_aws():
            os.environ["BUCKET_NAME"] = "test-bucket"
            os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-2")
            s3 = boto3.client("s3", region_name="eu-west-2")
            s3.create_bucket(Bucket="test-bucket",
                             CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})

            import silver_transform
            silver_transform.s3 = s3

            patcher, sid, a, b = _make_session(d)
            try:
                keys = export.upload_session(sid, bucket="test-bucket", s3_client=s3)
                self.assertEqual(len(keys), 2)

                # Re-export is idempotent (same keys, no duplicates)
                self.assertEqual(sorted(export.upload_session(sid, bucket="test-bucket", s3_client=s3)),
                                 sorted(keys))

                for key in keys:
                    silver_transform.handler(
                        {"Records": [{"s3": {"bucket": {"name": "test-bucket"},
                                             "object": {"key": key}}}]}, None)
                    silver_transform.handler(  # trigger twice: gold must not duplicate
                        {"Records": [{"s3": {"bucket": {"name": "test-bucket"},
                                             "object": {"key": key}}}]}, None)

                for cid in (a, b):
                    gold = json.loads(s3.get_object(
                        Bucket="test-bucket", Key=f"gold/client_id={cid}/summary.json")["Body"].read())
                    self.assertEqual(gold["session_count"], 1)
                    self.assertEqual(gold["client_id"], cid)

                # GDPR erasure removes one client and leaves the other untouched
                deleted = export.delete_client_data(a, bucket="test-bucket", s3_client=s3)
                self.assertEqual(deleted, 3)  # bronze + silver + gold
                remaining = [o["Key"] for o in s3.list_objects_v2(Bucket="test-bucket")["Contents"]]
                self.assertTrue(all(f"client_id={a}" not in k for k in remaining))
                self.assertTrue(any(f"client_id={b}" in k for k in remaining))
            finally:
                patcher.stop()

    def test_dropout_gap_is_not_credited_to_a_zone(self):
        import silver_transform
        start = datetime(2026, 9, 20, 9, 0, 0)
        ts = [start, start + timedelta(seconds=10), start + timedelta(seconds=300)]
        bronze = {
            "session_id": "s", "client_id": "c", "started_at": start.isoformat(),
            "ended_at": ts[-1].isoformat(),
            "client": {"age_at_session": 30, "resting_hr": 55, "max_hr": None},
            "samples": [{"ts": t.isoformat(), "hr": 140, "rr_intervals": []} for t in ts],
        }
        summary = silver_transform._summarise(bronze)
        self.assertEqual(sum(summary["zone_seconds"]), 10.0)  # the 290s gap is excluded


if __name__ == "__main__":
    unittest.main()
