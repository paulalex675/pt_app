"""API for the juniors trainer app (Paul's phone) and the parent consent page.

One Lambda, routed on the HTTP API route key. Trainer routes sit behind the
Cognito authoriser; the two /juniors/consent/... parent routes are public and
authorised by the single-use token instead.

Trainer (Cognito)
    GET  /juniors/payers                               bill payers (= guardians) from invoicing
    GET  /juniors/trainees                             everyone, with junior flag and consent summary
    POST /juniors/trainees                             create trainee (+ health profile)
    GET  /juniors/trainees/{id}                        detail: profile, consent, measurements
    PUT  /juniors/trainees/{id}                        edit trainee / health profile
    POST /juniors/trainees/{id}/consent-request        email a consent link to the payer
    POST /juniors/trainees/{id}/growth-summary         email the growth summary to the payer

Parent (public, token in body)
    POST /juniors/consent/lookup                       what the consent page needs to show

Sync with PT Studio: trainee and health items use the same attributes and the
same `updated_at` format PT Studio writes (naive UTC ISO, microseconds), so
last-edit-wins works in both directions. Edits use UpdateItem so fields this app
doesn't own (resting HR, max HR) are left alone.

Emails go through the invoicing mailer: an `email.requested` event on the
`pt-events` bus with template `junior_consent_request` or `junior_growth_summary`.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

try:
    from .consent import (CONSENT_TYPES, CURRENT_CONSENT_VERSION, active_consents,
                          hash_token, new_consent_request, status_by_type)
    from .maturity import Measurement, assess, decimal_age, is_junior
except ImportError:  # deployed: juniors/ is the Lambda root
    from consent import (CONSENT_TYPES, CURRENT_CONSENT_VERSION, active_consents,
                         hash_token, new_consent_request, status_by_type)
    from maturity import Measurement, assess, decimal_age, is_junior

_ddb = boto3.resource("dynamodb")
_events = boto3.client("events")

trainees = _ddb.Table(os.environ.get("TRAINEES_TABLE", "pt-trainees"))
health = _ddb.Table(os.environ.get("HEALTH_TABLE", "pt-health-profiles"))
consents = _ddb.Table(os.environ.get("CONSENTS_TABLE", "pt-junior-consents"))
requests = _ddb.Table(os.environ.get("CONSENT_REQUESTS_TABLE", "pt-junior-consent-requests"))
measurements = _ddb.Table(os.environ.get("MEASUREMENTS_TABLE", "pt-growth-measurements"))
INVOICING_TABLE = os.environ.get("INVOICING_TABLE", "")
EVENT_BUS = os.environ.get("EVENT_BUS", "pt-events")
CONSENT_PAGE_URL = os.environ.get("CONSENT_PAGE_URL", "")
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "")

PROFILE_SK = "PROFILE"
SEXES = ("male", "female", "other")
STAGE_WORDS = {
    "pre_phv": "Before the growth spurt",
    "circa_phv": "In or around the growth spurt",
    "post_phv": "Past the growth spurt",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- helpers

def _now() -> str:
    """Same format as PT Studio's db._now(), so updated_at strings compare correctly."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="microseconds")


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _ddb_value(value):
    return Decimal(str(value)) if isinstance(value, float) else value


def _resp(status: int, body) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(_plain(body), default=str)}


def _query_all(table, trainee_id: str, **extra) -> list[dict]:
    items, kwargs = [], {"KeyConditionExpression": Key("trainee_id").eq(trainee_id), **extra}
    while True:
        page = table.query(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _scan_all(table) -> list[dict]:
    items, kwargs = [], {}
    while True:
        page = table.scan(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _text(body: dict, field: str, label: str, max_len: int = 80, required: bool = True):
    value = (body.get(field) or "").strip()
    if required and not value:
        raise ApiError(400, f"{label} is required")
    if len(value) > max_len:
        raise ApiError(400, f"{label} is too long")
    return value or None


def _number(body: dict, field: str, label: str, low: float, high: float):
    raw = body.get(field)
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ApiError(400, f"{label} must be a number")
    if not low <= value <= high:
        raise ApiError(400, f"{label} looks wrong (expected {low:g}-{high:g})")
    return round(value, 1)


def _dob(body: dict):
    raw = body.get("dob")
    if raw in (None, ""):
        return None
    try:
        d = date.fromisoformat(raw)
    except (TypeError, ValueError):
        raise ApiError(400, "Date of birth must be YYYY-MM-DD")
    if d > _today() or d.year < 1920:
        raise ApiError(400, "Date of birth looks wrong")
    return d.isoformat()


# ------------------------------------------------------------- payers

def _payers() -> dict[str, dict]:
    """Invoicing CUSTOMER items, read-only. The payer email is the guardian contact."""
    if not INVOICING_TABLE:
        return {}
    table = _ddb.Table(INVOICING_TABLE)
    out, kwargs = {}, {"KeyConditionExpression": Key("pk").eq("CUSTOMER")}
    while True:
        page = table.query(**kwargs)
        for i in page["Items"]:
            out[i["sk"]] = {"id": i["sk"], "name": i.get("name", ""), "email": i.get("email", "")}
        if "LastEvaluatedKey" not in page:
            return out
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def list_payers(_event, _params):
    return 200, sorted(_payers().values(), key=lambda p: p["name"].lower())


# ------------------------------------------------------------- trainees

def _summary(t: dict, h: dict, payers: dict, consent_rows: list[dict] | None) -> dict:
    dob = h.get("dob")
    junior = bool(dob) and is_junior(date.fromisoformat(dob), _today())
    out = {
        "id": t["trainee_id"],
        "first_name": t.get("first_name", ""),
        "last_name": t.get("last_name", ""),
        "payer": payers.get(t.get("payer_id")) if t.get("payer_id") else None,
        "payer_id": t.get("payer_id"),
        "dob": dob,
        "sex": h.get("sex"),
        "height_cm": h.get("height_cm"),
        "weight_kg": h.get("weight_kg"),
        "age": round(decimal_age(date.fromisoformat(dob), _today()), 1) if dob else None,
        "is_junior": junior,
        "updated_at": t.get("updated_at"),
    }
    if junior and consent_rows is not None:
        out["consent"] = status_by_type(consent_rows, date.fromisoformat(dob), _today())
    return out


def list_trainees(_event, _params):
    payers = _payers()
    profiles = {h["trainee_id"]: h for h in _scan_all(health)}
    out = []
    for t in _scan_all(trainees):
        h = profiles.get(t["trainee_id"], {})
        rows = _query_all(consents, t["trainee_id"]) if h.get("dob") else None
        out.append(_summary(t, h, payers, rows))
    out.sort(key=lambda s: (not s["is_junior"], s["last_name"].lower(), s["first_name"].lower()))
    return 200, out


def _trainee_fields(body: dict, payers: dict) -> tuple[dict, dict]:
    """Validated (trainee attrs, health attrs). Only keys present in the body are returned."""
    t, h = {}, {}
    if "first_name" in body:
        t["first_name"] = _text(body, "first_name", "First name")
    if "last_name" in body:
        t["last_name"] = _text(body, "last_name", "Last name")
    if "payer_id" in body:
        pid = body.get("payer_id") or None
        if pid and payers and pid not in payers:
            raise ApiError(400, "That bill payer doesn't exist in invoicing")
        t["payer_id"] = pid
    if "dob" in body:
        h["dob"] = _dob(body)
    if "sex" in body:
        sex = body.get("sex") or None
        if sex is not None and sex not in SEXES:
            raise ApiError(400, "Sex must be male, female or other")
        h["sex"] = sex
    if "height_cm" in body:
        h["height_cm"] = _number(body, "height_cm", "Height", 50, 230)
    if "weight_kg" in body:
        h["weight_kg"] = _number(body, "weight_kg", "Weight", 10, 250)
    return t, h


def _update(table, trainee_id: str, attrs: dict, ts: str) -> None:
    """SET the given attrs (REMOVE the ones set to None) and stamp updated_at."""
    sets, removes, names, values = ["#u = :u"], [], {"#u": "updated_at"}, {":u": ts}
    for i, (k, v) in enumerate(attrs.items()):
        names[f"#a{i}"] = k
        if v is None:
            removes.append(f"#a{i}")
        else:
            sets.append(f"#a{i} = :v{i}")
            values[f":v{i}"] = _ddb_value(v)
    expr = "SET " + ", ".join(sets) + (" REMOVE " + ", ".join(removes) if removes else "")
    table.update_item(Key={"trainee_id": trainee_id}, UpdateExpression=expr,
                      ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def create_trainee(event, _params):
    body = _body(event)
    for f in ("first_name", "last_name"):
        body.setdefault(f, "")
    payers = _payers()
    t, h = _trainee_fields(body, payers)
    tid, ts = str(uuid.uuid4()), _now()
    trainees.put_item(Item={"trainee_id": tid, **{k: v for k, v in t.items() if v is not None},
                            "created_at": ts, "updated_at": ts},
                      ConditionExpression="attribute_not_exists(trainee_id)")
    health.put_item(Item={"trainee_id": tid, "updated_at": ts,
                          **{k: _ddb_value(v) for k, v in h.items() if v is not None}})
    return 201, _detail(tid, payers)


def update_trainee(event, params):
    tid = params["id"]
    if not trainees.get_item(Key={"trainee_id": tid}).get("Item"):
        raise ApiError(404, "Trainee not found")
    t, h = _trainee_fields(_body(event), _payers())
    ts = _now()
    # Both items get the same stamp: PT Studio pulls on the trainee's updated_at.
    _update(trainees, tid, t, ts)
    _update(health, tid, h, ts)
    return 200, _detail(tid)


def _detail(tid: str, payers: dict | None = None) -> dict:
    t = trainees.get_item(Key={"trainee_id": tid}).get("Item")
    if not t:
        raise ApiError(404, "Trainee not found")
    h = health.get_item(Key={"trainee_id": tid}).get("Item") or {}
    rows = _query_all(consents, tid)
    out = _summary(t, h, payers if payers is not None else _payers(), rows if h.get("dob") else None)
    out["consent_log"] = sorted(
        ({k: r.get(k) for k in ("consent_type", "given_by", "relationship", "method",
                                "consent_version", "granted_at", "withdrawn_at")} for r in rows),
        key=lambda r: r["granted_at"], reverse=True)
    growth = _query_all(measurements, tid)
    profile = next((g for g in growth if g["sk"] == PROFILE_SK), {})
    out["mid_parent_height_cm"] = profile.get("mid_parent_height_cm")
    out["measurements"] = [_measurement_view(g) for g in growth if g["sk"] != PROFILE_SK]
    out["measurements"].sort(key=lambda m: m["measured_on"], reverse=True)
    out["pending_requests"] = _pending_requests(tid)
    return out


def get_trainee(_event, params):
    return 200, _detail(params["id"])


def _measurement_view(g: dict) -> dict:
    keys = ("measurement_id", "measured_on", "height_cm", "sitting_height_cm", "weight_kg",
            "growth_cm_per_month", "stage", "moore_offset", "predicted_aphv", "flags", "measured_by")
    return {k: g.get(k) for k in keys}


# ------------------------------------------------------------- consent requests

def _pending_requests(tid: str) -> list[dict]:
    now = int(datetime.now(timezone.utc).timestamp())
    page = requests.query(IndexName="by_trainee", KeyConditionExpression=Key("trainee_id").eq(tid))
    return sorted(({"sent_at": r.get("sent_at"), "sent_to": r.get("sent_to"),
                    "expires_at": int(r["expires_at"])}
                   for r in page["Items"] if int(r["expires_at"]) > now),
                  key=lambda r: r["sent_at"] or "", reverse=True)


def _publish_email(template: str, ref: str, to: str, data: dict) -> bool:
    entry = {"template": template, "ref": ref, "to": [to], "data": data}
    if OWNER_EMAIL:
        entry["bcc"] = [OWNER_EMAIL]
    r = _events.put_events(Entries=[{
        "EventBusName": EVENT_BUS, "Source": "pt.juniors", "DetailType": "email.requested",
        "Detail": json.dumps(_plain(entry), default=str),
    }])
    return r.get("FailedEntryCount", 0) == 0


def _junior_with_payer(tid: str) -> tuple[dict, dict, dict]:
    t = trainees.get_item(Key={"trainee_id": tid}).get("Item")
    if not t:
        raise ApiError(404, "Trainee not found")
    h = health.get_item(Key={"trainee_id": tid}).get("Item") or {}
    if not h.get("dob"):
        raise ApiError(422, "Add a date of birth first")
    if not is_junior(date.fromisoformat(h["dob"]), _today()):
        raise ApiError(422, "This trainee is 18 or over")
    payer = _payers().get(t.get("payer_id") or "")
    if not payer or not payer.get("email"):
        raise ApiError(422, "Link a bill payer with an email address first - consent goes to them")
    return t, h, payer


def send_consent_request(event, params):
    tid = params["id"]
    t, h, payer = _junior_with_payer(tid)
    req = new_consent_request()
    sent_at = _now()
    requests.put_item(Item={"token_hash": req.token_hash, "trainee_id": tid,
                            "expires_at": req.expires_at, "sent_at": sent_at,
                            "sent_to": payer["email"]})
    link = f"{CONSENT_PAGE_URL}#t={req.token}" if CONSENT_PAGE_URL else None
    queued = False
    if link:
        queued = _publish_email("junior_consent_request", f"CONSENT-{tid[:8]}", payer["email"], {
            "child_first_name": t.get("first_name", ""),
            "guardian_name": payer["name"],
            "link": link,
            "expires_on": datetime.fromtimestamp(req.expires_at, timezone.utc).date().isoformat(),
            "consent_types": list(CONSENT_TYPES),
        })
    # Without a consent page URL configured, hand the link back so it can be tested.
    return 201, {"sent_to": payer["email"], "email_queued": queued,
                 "link": None if queued else (link or f"#t={req.token}"),
                 "expires_at": req.expires_at}


def lookup_consent(event, _params):
    """Public: the parent page calls this with the token from the link."""
    token = _body(event).get("token")
    if not token:
        raise ApiError(400, "Missing token")
    req = requests.get_item(Key={"token_hash": hash_token(token)}).get("Item")
    if not req or int(req["expires_at"]) < datetime.now(timezone.utc).timestamp():
        raise ApiError(410, "This consent link has expired or has already been used. Please ask for a new one.")
    t = trainees.get_item(Key={"trainee_id": req["trainee_id"]}).get("Item") or {}
    payer = _payers().get(t.get("payer_id") or "", {})
    return 200, {"child_first_name": t.get("first_name", ""),
                 "guardian_name": payer.get("name", ""),
                 "consent_types": list(CONSENT_TYPES),
                 "consent_version": CURRENT_CONSENT_VERSION,
                 "expires_at": int(req["expires_at"])}


# ------------------------------------------------------------- growth summary

def send_growth_summary(event, params):
    tid = params["id"]
    t, h, payer = _junior_with_payer(tid)
    dob = date.fromisoformat(h["dob"])
    if "growth_measurements" not in active_consents(_query_all(consents, tid), dob, _today()):
        raise ApiError(403, "No active growth measurement consent")
    rows = [g for g in _query_all(measurements, tid) if g["sk"] != PROFILE_SK]
    if not rows:
        raise ApiError(422, "No measurements to summarise yet")
    rows.sort(key=lambda g: g["measured_on"])
    latest = rows[-1]
    sex = h.get("sex")
    stage = None
    if sex in ("male", "female"):
        history = [Measurement(date.fromisoformat(g["measured_on"]), float(g["height_cm"]))
                   for g in rows[:-1]]
        current = Measurement(date.fromisoformat(latest["measured_on"]), float(latest["height_cm"]),
                              _float(latest.get("sitting_height_cm")), _float(latest.get("weight_kg")))
        stage = assess(sex, dob, current, history).stage
    data = {
        "child_first_name": t.get("first_name", ""),
        "guardian_name": payer["name"],
        "note": _text(_body(event), "note", "Note", max_len=1000, required=False),
        "stage": stage,
        "stage_text": STAGE_WORDS.get(stage),
        "rapid_growth": "rapid_growth" in (latest.get("flags") or []),
        "measurements": [{"measured_on": g["measured_on"],
                          "height_cm": _float(g.get("height_cm")),
                          "sitting_height_cm": _float(g.get("sitting_height_cm")),
                          "weight_kg": _float(g.get("weight_kg")),
                          "growth_cm_per_month": _float(g.get("growth_cm_per_month"))}
                         for g in rows[-12:]],
    }
    queued = _publish_email("junior_growth_summary", f"GROWTH-{tid[:8]}-{latest['measured_on']}",
                            payer["email"], data)
    return 200, {"sent_to": payer["email"], "email_queued": queued}


def _float(v):
    return None if v is None else float(v)


# ------------------------------------------------------------- router

ROUTES = {
    "GET /juniors/payers": list_payers,
    "GET /juniors/trainees": list_trainees,
    "POST /juniors/trainees": create_trainee,
    "GET /juniors/trainees/{id}": get_trainee,
    "PUT /juniors/trainees/{id}": update_trainee,
    "POST /juniors/trainees/{id}/consent-request": send_consent_request,
    "POST /juniors/trainees/{id}/growth-summary": send_growth_summary,
    "POST /juniors/consent/lookup": lookup_consent,
}


def _body(event: dict) -> dict:
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        raise ApiError(400, "Body must be JSON")
    if not isinstance(body, dict):
        raise ApiError(400, "Body must be a JSON object")
    return body


def handler(event, _context):
    fn = ROUTES.get(event.get("routeKey", ""))
    if not fn:
        return _resp(404, {"error": "Not found"})
    try:
        status, body = fn(event, event.get("pathParameters") or {})
        return _resp(status, body)
    except ApiError as exc:
        return _resp(exc.status, {"error": str(exc)})
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _resp(409, {"error": "That record changed - refresh and try again"})
        raise
