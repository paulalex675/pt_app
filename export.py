"""Export a finished session to S3 as bronze records.

Grain: one record per session per client. A session with three participants
uploads three objects:

    bronze/client_id=<client>/<session_id>.json

Why per client:
  * Erasing a client (UK GDPR) is deleting one prefix, not rewriting shared files.
  * Re-uploading a session overwrites the same keys, so exports are idempotent.

Data minimisation: records are pseudonymous. They carry the client_id only (no
name) and the client's age at the session rather than their date of birth.

Config: set PT_STUDIO_BUCKET (and optionally PT_STUDIO_REGION, default
eu-west-2) and configure AWS credentials (`aws configure`).
"""
import json
import os
from datetime import date, datetime, timezone

import db

SCHEMA_VERSION = 1
DEFAULT_REGION = "eu-west-2"  # London — keep client health data in the UK


def _age_on(dob_iso: str | None, on: date) -> int | None:
    if not dob_iso:
        return None
    dob = date.fromisoformat(dob_iso)
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def build_client_exports(session_id: str) -> list[dict]:
    """Returns one bronze payload per participant in the session."""
    session = db.get_session(session_id)
    if session is None:
        raise ValueError(f"No session found with id {session_id}")
    if not session["ended_at"]:
        raise ValueError("Session has not ended yet; end it before exporting.")

    started = datetime.fromisoformat(session["started_at"])
    samples = db.session_samples(session_id)

    # Participants come from session_participants. Sessions recorded before that
    # table existed fall back to the session's own client/strap.
    participants = [
        (p["client_id"], p["strap_device_id"]) for p in db.list_session_participants(session_id)
    ] or [(session["client_id"], session["strap_device_id"])]

    exports = []
    for client_id, strap_id in participants:
        client = db.get_client(client_id)
        own = [
            s for s in samples
            if s["client_id"] == client_id
            or (s["client_id"] is None and client_id == session["client_id"])  # legacy rows
        ]
        exports.append({
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "client_id": client_id,
            "strap_device_id": strap_id,
            "started_at": session["started_at"],
            "ended_at": session["ended_at"],
            "client": {
                "sex": client["sex"],
                "age_at_session": _age_on(client["dob"], started.date()),
                "height_cm": client["height_cm"],
                "weight_kg": client["weight_kg"],
                "resting_hr": client["resting_hr"],
                "max_hr": client["max_hr"],
            },
            "samples": [
                {
                    "ts": s["ts"],
                    "hr": s["hr"],
                    "rr_intervals": json.loads(s["rr_intervals"]) if s["rr_intervals"] else [],
                }
                for s in own
            ],
            "exported_at": datetime.now(timezone.utc).isoformat(),
        })
    return exports


def bronze_key(client_id: str, session_id: str) -> str:
    return f"bronze/client_id={client_id}/{session_id}.json"


def upload_session(session_id: str, bucket: str | None = None, region: str | None = None,
                   s3_client=None) -> list[str]:
    """Uploads one bronze object per participant. Returns the S3 keys written."""
    bucket = bucket or os.environ.get("PT_STUDIO_BUCKET")
    if not bucket:
        raise RuntimeError("Set the PT_STUDIO_BUCKET environment variable to your S3 bucket name.")
    if s3_client is None:
        import boto3
        s3_client = boto3.client("s3", region_name=region or os.environ.get("PT_STUDIO_REGION", DEFAULT_REGION))

    keys = []
    for payload in build_client_exports(session_id):
        key = bronze_key(payload["client_id"], session_id)
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(payload).encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
        keys.append(key)
    return keys


def delete_client_data(client_id: str, bucket: str | None = None, s3_client=None) -> int:
    """GDPR erasure: removes every bronze/silver/gold object for one client."""
    bucket = bucket or os.environ.get("PT_STUDIO_BUCKET")
    if not bucket:
        raise RuntimeError("Set the PT_STUDIO_BUCKET environment variable to your S3 bucket name.")
    if s3_client is None:
        import boto3
        s3_client = boto3.client("s3", region_name=os.environ.get("PT_STUDIO_REGION", DEFAULT_REGION))

    deleted = 0
    for layer in ("bronze", "silver", "gold"):
        prefix = f"{layer}/client_id={client_id}/"
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objs:
                s3_client.delete_objects(Bucket=bucket, Delete={"Objects": objs})
                deleted += len(objs)
    return deleted


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python export.py <session_id>")
        sys.exit(1)
    for k in upload_session(sys.argv[1]):
        print("Uploaded", k)
