"""Lambda handlers for junior consent and growth measurements.

Environment variables
    TRAINEES_TABLE          existing shared trainee table (partition key: trainee_id)
    CONSENTS_TABLE          JuniorConsents
    CONSENT_REQUESTS_TABLE  JuniorConsentRequests
    MEASUREMENTS_TABLE      GrowthMeasurements

Trainee records need two new attributes: date_of_birth ("YYYY-MM-DD") and
sex ("male" | "female", used only by the maturity equations). Mid-parent height
is written to the trainee record by the consent handler.

Routes (API Gateway HTTP API, Lambda proxy):
    POST /juniors/measurements        measurement_handler     (trainer, Cognito)
    POST /juniors/consent             consent_submit_handler  (parent, token link)
    POST /juniors/consent/withdraw    consent_withdraw_handler (trainer, Cognito)
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

from .consent import (
    ConsentError,
    build_consent_rows,
    hash_token,
    require,
)
from .maturity import Measurement, assess, is_junior, mid_parent_height

_ddb = boto3.resource("dynamodb")
trainees = _ddb.Table(os.environ.get("TRAINEES_TABLE", "Trainees"))
consents = _ddb.Table(os.environ.get("CONSENTS_TABLE", "JuniorConsents"))
requests = _ddb.Table(os.environ.get("CONSENT_REQUESTS_TABLE", "JuniorConsentRequests"))
measurements = _ddb.Table(os.environ.get("MEASUREMENTS_TABLE", "GrowthMeasurements"))


# ---------- helpers ----------

def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _body(event: dict) -> dict:
    return json.loads(event.get("body") or "{}")


def _to_ddb(value):
    """DynamoDB needs Decimal, not float."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_ddb(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_to_ddb(v) for v in value]
    return value


def _float(value):
    return None if value is None else float(value)


def _query_all(table, trainee_id: str) -> list[dict]:
    items, kwargs = [], {"KeyConditionExpression": Key("trainee_id").eq(trainee_id)}
    while True:
        page = table.query(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _trainer(event: dict) -> str:
    claims = (event.get("requestContext", {}).get("authorizer", {})
              .get("jwt", {}).get("claims", {}))
    return claims.get("email") or claims.get("sub") or "unknown"


# ---------- measurements ----------

def measurement_handler(event, context):
    """Record a measurement for a junior and return the maturity assessment."""
    b = _body(event)
    trainee_id = b.get("trainee_id")
    if not trainee_id:
        return _resp(400, {"error": "trainee_id is required"})

    trainee = trainees.get_item(Key={"trainee_id": trainee_id}).get("Item")
    if not trainee:
        return _resp(404, {"error": "trainee not found"})
    if not trainee.get("date_of_birth") or trainee.get("sex") not in ("male", "female"):
        return _resp(422, {"error": "trainee needs date_of_birth and sex first"})

    dob = date.fromisoformat(trainee["date_of_birth"])
    measured_on = date.fromisoformat(b.get("measured_on") or date.today().isoformat())
    if not is_junior(dob, measured_on):
        return _resp(422, {"error": "growth tracking is for under-18s only"})

    try:
        require("growth_measurements", _query_all(consents, trainee_id), dob, date.today())
    except ConsentError as exc:
        return _resp(403, {"error": str(exc)})

    try:
        current = Measurement(
            measured_on=measured_on,
            height_cm=float(b["height_cm"]),
            sitting_height_cm=_float(b.get("sitting_height_cm")),
            weight_kg=_float(b.get("weight_kg")),
        )
    except (KeyError, TypeError, ValueError):
        return _resp(400, {"error": "height_cm is required and must be a number"})

    if not 80 <= current.height_cm <= 220:
        return _resp(400, {"error": "height_cm looks wrong (expected 80-220 cm)"})
    if current.sitting_height_cm is not None and not (
            0.4 * current.height_cm <= current.sitting_height_cm <= 0.6 * current.height_cm):
        return _resp(400, {"error": "sitting_height_cm looks wrong for this height"})

    history = [
        Measurement(date.fromisoformat(m["measured_on"]), float(m["height_cm"]),
                    _float(m.get("sitting_height_cm")), _float(m.get("weight_kg")))
        for m in _query_all(measurements, trainee_id)
    ]
    # Khamis-Roche %PAH plugs in here once implemented (needs mid_parent_height_cm).
    result = assess(trainee["sex"], dob, current, history, percent_pah=None)

    measurement_id = str(uuid.uuid4())
    item = {
        "trainee_id": trainee_id,
        "sk": f"{measured_on.isoformat()}#{measurement_id}",
        "measurement_id": measurement_id,
        "measured_on": measured_on.isoformat(),
        "height_cm": current.height_cm,
        "sitting_height_cm": current.sitting_height_cm,
        "weight_kg": current.weight_kg,
        "measured_by": _trainer(event),
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **asdict(result),
    }
    measurements.put_item(Item=_to_ddb(item))
    return _resp(201, {"measurement_id": measurement_id, **asdict(result)})


# ---------- consent ----------

def consent_submit_handler(event, context):
    """Parent submits the consent page opened from their emailed link."""
    b = _body(event)
    token = b.get("token")
    if not token:
        return _resp(400, {"error": "missing token"})

    req = requests.get_item(Key={"token_hash": hash_token(token)}).get("Item")
    now = datetime.now(timezone.utc)
    # DynamoDB TTL deletes lazily, so check expiry ourselves as well.
    if not req or int(req["expires_at"]) < now.timestamp():
        return _resp(410, {"error": "this consent link has expired; ask for a new one"})

    trainee_id = req["trainee_id"]
    try:
        rows = build_consent_rows(
            trainee_id=trainee_id,
            given_by=b.get("given_by", ""),
            relationship=b.get("relationship", ""),
            granted_types=b.get("granted", []),
            method="online_form",
            now=now,
        )
        mph = mid_parent_height(_float(b.get("parent_a_height_cm")),
                                _float(b.get("parent_b_height_cm")))
    except ValueError as exc:
        return _resp(400, {"error": str(exc)})

    with consents.batch_writer() as batch:
        for row in rows:
            batch.put_item(Item=row)

    # Store only the mid-parent average; the two raw heights are never saved.
    if mph is not None and "growth_measurements" in b.get("granted", []):
        trainees.update_item(
            Key={"trainee_id": trainee_id},
            UpdateExpression="SET mid_parent_height_cm = :m",
            ExpressionAttributeValues={":m": Decimal(str(mph))},
        )

    # Single use: remove the link once submitted.
    requests.delete_item(Key={"token_hash": req["token_hash"]})
    return _resp(200, {"recorded": [r["consent_type"] for r in rows]})


def consent_withdraw_handler(event, context):
    """Withdraw a consent type. Stamps withdrawn_at on the latest row; the row
    itself is kept as the audit trail."""
    b = _body(event)
    trainee_id, ctype = b.get("trainee_id"), b.get("consent_type")
    if not trainee_id or not ctype:
        return _resp(400, {"error": "trainee_id and consent_type are required"})

    page = consents.query(
        KeyConditionExpression=Key("trainee_id").eq(trainee_id)
        & Key("sk").begins_with(f"{ctype}#"),
        ScanIndexForward=False,
        Limit=1,
    )
    if not page["Items"]:
        return _resp(404, {"error": "no consent of that type on record"})
    latest = page["Items"][0]
    if latest.get("withdrawn_at"):
        return _resp(200, {"status": "already withdrawn"})

    consents.update_item(
        Key={"trainee_id": trainee_id, "sk": latest["sk"]},
        UpdateExpression="SET withdrawn_at = :w, withdrawn_recorded_by = :by",
        ExpressionAttributeValues={
            ":w": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ":by": _trainer(event),
        },
    )
    return _resp(200, {"status": "withdrawn", "consent_type": ctype})
