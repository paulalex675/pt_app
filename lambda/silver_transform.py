"""Triggered whenever a bronze/*.json session lands in S3.

Computes a per-session summary (silver) and folds it into a running
per-client summary (gold) that the dashboard API reads.

Zone thresholds are recomputed here from age_at_session, resting_hr and
max_hr (all carried in the bronze record), mirroring zones.py in the desktop app, so silver/gold stay
correct even if the app's zone logic changes later — this Lambda is the
single source of truth for anything computed from raw samples.
"""
import json
import os
import urllib.parse
from datetime import datetime

import boto3

s3 = boto3.client("s3")
BUCKET = os.environ["BUCKET_NAME"]

ZONE_BOUNDS = [0.50, 0.60, 0.70, 0.80, 0.90, 1.00]
MAX_GAP_SEC = 30  # ignore sample gaps longer than this (BLE dropouts)
ZONE_NAMES = ["Z1 Recovery", "Z2 Aerobic", "Z3 Tempo", "Z4 Threshold", "Z5 Max"]


def _estimate_max_hr(age):
    return round(208 - 0.7 * age) if age is not None else 190


def _zone_thresholds(age, resting_hr, max_hr=None):
    """Mirrors zones.py in the desktop app: a recorded max HR wins over the estimate."""
    hr_max = max_hr or _estimate_max_hr(age)
    if resting_hr:
        hrr = hr_max - resting_hr
        return [round(resting_hr + b * hrr) for b in ZONE_BOUNDS]
    return [round(b * hr_max) for b in ZONE_BOUNDS]


def _zone_index(hr, thresholds):
    for i in range(5):
        if hr <= thresholds[i + 1]:
            return i
    return 4


def _summarise(bronze: dict) -> dict:
    samples = bronze["samples"]
    client = bronze["client"]
    thresholds = _zone_thresholds(
        client.get("age_at_session"), client.get("resting_hr"), client.get("max_hr")
    )

    zone_seconds = [0.0] * 5
    hrs = [s["hr"] for s in samples]
    prev_ts = None
    for s in samples:
        ts = datetime.fromisoformat(s["ts"])
        if prev_ts is not None:
            elapsed = (ts - prev_ts).total_seconds()
            # A strap dropout leaves a gap; don't credit that time to any zone.
            if elapsed <= MAX_GAP_SEC:
                zone_seconds[_zone_index(s["hr"], thresholds)] += elapsed
        prev_ts = ts

    total = sum(zone_seconds) or 1
    duration_sec = int(sum(zone_seconds))

    started = datetime.fromisoformat(bronze["started_at"])
    ended = datetime.fromisoformat(bronze["ended_at"]) if bronze.get("ended_at") else started

    return {
        "session_id": bronze["session_id"],
        "client_id": bronze["client_id"],
        "date": started.date().isoformat(),
        "started_at": bronze["started_at"],
        "ended_at": bronze.get("ended_at"),
        "duration_sec": duration_sec,
        "avg_hr": round(sum(hrs) / len(hrs)) if hrs else None,
        "max_hr": max(hrs) if hrs else None,
        "min_hr": min(hrs) if hrs else None,
        "zone_seconds": [round(z, 1) for z in zone_seconds],
        "zone_pct": [round(100 * z / total, 1) for z in zone_seconds],
        "zone_names": ZONE_NAMES,
    }


def _update_gold(client_id: str, session_summary: dict):
    gold_key = f"gold/client_id={client_id}/summary.json"
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=gold_key)
        gold = json.loads(obj["Body"].read())
    except s3.exceptions.NoSuchKey:
        gold = {"client_id": client_id, "sessions": []}

    # Replace an existing entry for this session (idempotent on re-upload), else append.
    gold["sessions"] = [s for s in gold["sessions"] if s["session_id"] != session_summary["session_id"]]
    gold["sessions"].append(session_summary)
    gold["sessions"].sort(key=lambda s: s["started_at"])

    gold["session_count"] = len(gold["sessions"])
    gold["avg_duration_sec"] = round(sum(s["duration_sec"] for s in gold["sessions"]) / gold["session_count"])
    # Average zone split across all sessions, for the "typical week" style summary.
    zone_totals = [0.0] * 5
    for s in gold["sessions"]:
        for i, pct in enumerate(s["zone_pct"]):
            zone_totals[i] += pct
    gold["avg_zone_pct"] = [round(z / gold["session_count"], 1) for z in zone_totals]
    gold["last_session_at"] = gold["sessions"][-1]["started_at"]

    s3.put_object(
        Bucket=BUCKET, Key=gold_key,
        Body=json.dumps(gold).encode("utf-8"),
        ContentType="application/json", ServerSideEncryption="AES256",
    )


def handler(event, context):
    for record in event["Records"]:
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])

        obj = s3.get_object(Bucket=bucket, Key=key)
        bronze = json.loads(obj["Body"].read())

        summary = _summarise(bronze)

        silver_key = f"silver/client_id={summary['client_id']}/{summary['session_id']}.json"
        s3.put_object(
            Bucket=BUCKET, Key=silver_key,
            Body=json.dumps(summary).encode("utf-8"),
            ContentType="application/json", ServerSideEncryption="AES256",
        )

        _update_gold(summary["client_id"], summary)

    return {"statusCode": 200}
