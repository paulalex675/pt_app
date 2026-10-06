import copy
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lambda"))
os.environ.setdefault("BUCKET_NAME", "test-bucket")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-2")

try:
    import boto3
    from moto import mock_aws
    HAVE_MOTO = True
except ImportError:  # moto is a dev-only dependency
    HAVE_MOTO = False

BASE = datetime(2026, 10, 6, 9, 0, 0)


def iso(seconds):
    return (BASE + timedelta(seconds=seconds)).isoformat()


def make_elite_session(temp_dir):
    """Ada finishes both rounds; Sam is stopped during his first pre-work."""
    patcher = patch.object(db, "DB_PATH", Path(temp_dir) / "t.db")
    patcher.start()
    db.init_db()
    ada = db.create_client("Ada", "North", "female", "1990-06-15", 168, 62, 55)
    sam = db.create_client("Sam", "South", "male", "1985-01-01", 180, 82, 60)
    params = {
        "rounds": 2, "round_s": 10.0, "recovered_pct": 0.7, "target_rest_s": 30.0, "target_total_s": 50.0,
        "clients": {
            ada: {"z5_low": 170, "recovered_bpm": 140, "max_hr": 187, "resting_hr": 55},
            sam: {"z5_low": 165, "recovered_bpm": 135, "max_hr": 180, "resting_hr": 60},
        },
    }
    sid = db.start_session(ada, "strap-a", mode="elite", mode_params=params)
    db.add_session_participant(sid, ada, "strap-a")
    db.add_session_participant(sid, sam, "strap-b")
    for i in range(10):
        db.log_sample(sid, 150 + i, "[]", client_id=ada, strap_device_id="strap-a")
        db.log_sample(sid, 120 + i, "[]", client_id=sam, strap_device_id="strap-b")

    for phase, rnd, t in [("prework", 1, 0), ("work", 1, 20), ("recovery", 1, 30),
                          ("prework", 2, 60), ("work", 2, 75), ("done", 2, 85)]:
        db.save_phase_event(sid, ada, rnd, phase, iso(t))
    db.save_elite_round(sid, ada, 1, iso(0), iso(30), 20.0, 10.0, 0.0, 30.0, 182, 160, [])
    db.save_elite_round(sid, ada, 2, iso(60), iso(85), 15.0, 10.0, 0.0, None, 184, None, [])

    db.save_phase_event(sid, sam, 1, "prework", iso(0))
    db.save_phase_event(sid, sam, 1, "done", iso(25))
    db.save_elite_round(sid, sam, 1, iso(0), iso(25), 25.0, 0.0, 0.0, None, None, None, ["stopped"])
    db.end_session(sid)
    return patcher, sid, ada, sam


class EliteExportTests(unittest.TestCase):
    def test_each_client_gets_their_own_elite_block(self):
        with TemporaryDirectory() as d:
            patcher, sid, ada, sam = make_elite_session(d)
            try:
                records = {r["client_id"]: r for r in export.build_client_exports(sid)}
                self.assertEqual(records[ada]["mode"], "elite")
                self.assertEqual(records[ada]["schema_version"], 2)
                self.assertEqual(len(records[ada]["elite"]["rounds"]), 2)
                self.assertEqual(len(records[sam]["elite"]["rounds"]), 1)
                self.assertEqual([e["phase"] for e in records[ada]["elite"]["events"]][-1], "done")
                self.assertEqual(records[ada]["elite"]["frozen"]["z5_low"], 170)
                self.assertEqual(records[sam]["elite"]["frozen"]["z5_low"], 165)
            finally:
                patcher.stop()

    def test_one_clients_record_never_contains_another_clients_data(self):
        with TemporaryDirectory() as d:
            patcher, sid, ada, sam = make_elite_session(d)
            try:
                records = {r["client_id"]: r for r in export.build_client_exports(sid)}
                blob = json.dumps(records[ada])
                self.assertNotIn(sam, blob)
                self.assertNotIn("clients", records[ada]["elite"]["params"])
                for secret in ("Ada", "North", "Sam", "South", "1990-06-15"):
                    self.assertNotIn(secret, blob)
            finally:
                patcher.stop()

    def test_normal_sessions_have_no_elite_block(self):
        with TemporaryDirectory() as d:
            patcher = patch.object(db, "DB_PATH", Path(d) / "t.db")
            patcher.start()
            try:
                db.init_db()
                a = db.create_client("A", "B", "male", "1990-01-01", 180, 80, 55)
                sid = db.start_session(a, "strap-a")
                db.add_session_participant(sid, a, "strap-a")
                db.log_sample(sid, 140, "[]", client_id=a, strap_device_id="strap-a")
                db.end_session(sid)
                record = export.build_client_exports(sid)[0]
                self.assertEqual(record["mode"], "normal")
                self.assertNotIn("elite", record)
            finally:
                patcher.stop()


@unittest.skipUnless(HAVE_MOTO, "moto not installed (pip install -r requirements-dev.txt)")
class EliteCloudTests(unittest.TestCase):
    def run_handler(self, silver_transform, key):
        silver_transform.handler(
            {"Records": [{"s3": {"bucket": {"name": "test-bucket"}, "object": {"key": key}}}]}, None)

    def test_elite_summary_reaches_gold_and_reuploads_do_not_duplicate(self):
        with TemporaryDirectory() as d, mock_aws():
            s3 = boto3.client("s3", region_name="eu-west-2")
            s3.create_bucket(Bucket="test-bucket",
                             CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
            import silver_transform
            silver_transform.s3 = s3

            patcher, sid, ada, sam = make_elite_session(d)
            try:
                keys = export.upload_session(sid, bucket="test-bucket", s3_client=s3)
                for key in keys + keys:                      # uploads processed twice
                    self.run_handler(silver_transform, key)

                gold_ada = json.loads(s3.get_object(
                    Bucket="test-bucket", Key=f"gold/client_id={ada}/summary.json")["Body"].read())
                self.assertEqual(len(gold_ada["elite_sessions"]), 1)
                e = gold_ada["elite_sessions"][0]
                self.assertEqual(e["total_s"], 85.0)
                self.assertEqual(e["target_total_s"], 50.0)
                self.assertTrue(e["completed"])
                self.assertEqual(e["capped_rounds"], 0)
                self.assertEqual(e["round1_prework_s"], 20.0)
                self.assertEqual(e["avg_prework_s"], 17.5)
                self.assertEqual(e["avg_recovery_s"], 30.0)       # last round has no recovery: ignored
                self.assertEqual(e["avg_hr_drop_60"], 22.0)
                self.assertEqual(len(e["rounds"]), 2)

                gold_sam = json.loads(s3.get_object(
                    Bucket="test-bucket", Key=f"gold/client_id={sam}/summary.json")["Body"].read())
                self.assertFalse(gold_sam["elite_sessions"][0]["completed"])
            finally:
                patcher.stop()

    def test_elite_history_accumulates_in_date_order(self):
        with TemporaryDirectory() as d, mock_aws():
            s3 = boto3.client("s3", region_name="eu-west-2")
            s3.create_bucket(Bucket="test-bucket",
                             CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
            import silver_transform
            silver_transform.s3 = s3

            patcher, sid, ada, sam = make_elite_session(d)
            try:
                first = next(r for r in export.build_client_exports(sid) if r["client_id"] == ada)
                later = copy.deepcopy(first)
                later["session_id"] = "later-session"
                later["started_at"] = (BASE + timedelta(days=7)).isoformat()
                later["ended_at"] = (BASE + timedelta(days=7, seconds=70)).isoformat()
                later["elite"]["events"] = [
                    {"ts": (BASE + timedelta(days=7)).isoformat(), "round_no": 1, "phase": "prework"},
                    {"ts": (BASE + timedelta(days=7, seconds=70)).isoformat(), "round_no": 2, "phase": "done"},
                ]
                for record in (later, first):                 # uploaded out of order on purpose
                    key = f"bronze/client_id={ada}/{record['session_id']}.json"
                    s3.put_object(Bucket="test-bucket", Key=key, Body=json.dumps(record))
                    self.run_handler(silver_transform, key)

                gold = json.loads(s3.get_object(
                    Bucket="test-bucket", Key=f"gold/client_id={ada}/summary.json")["Body"].read())
                totals = [e["total_s"] for e in gold["elite_sessions"]]
                self.assertEqual(totals, [85.0, 70.0])        # earlier session first, later one improved
            finally:
                patcher.stop()


class LegacyRecordTests(unittest.TestCase):
    def test_records_without_a_mode_still_summarise(self):
        import silver_transform
        bronze = {
            "session_id": "old", "client_id": "c", "started_at": BASE.isoformat(),
            "ended_at": (BASE + timedelta(seconds=30)).isoformat(),
            "client": {"age_at_session": 30, "resting_hr": 55, "max_hr": None},
            "samples": [{"ts": iso(i * 10), "hr": 140, "rr_intervals": []} for i in range(4)],
        }
        summary = silver_transform._summarise(bronze)
        self.assertNotIn("elite", summary)
        self.assertEqual(summary["duration_sec"], 30)


if __name__ == "__main__":
    unittest.main()
