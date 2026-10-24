"""Keeps the local client list and the cloud tables in step. Offline-first.

PT Studio always works from its local SQLite file, so sessions never wait on
the internet. This module is only called at a few moments: app start, after a
client is added or edited, after a session upload, and from the Sync button.

  pull  -> cloud trainees newer than the local copy overwrite it (last edit wins)
  push  -> local edits not yet in the cloud are written (conditionally, so a
           newer edit from your other machine is never overwritten)
  payers-> the invoicing CUSTOMER items are READ into a local list for the
           Bill payer dropdown. This module never writes to the invoicing table.

Opt in by setting PT_TRAINEES_TABLE (e.g. pt-trainees) once the clients stack is
deployed. Optional: PT_HEALTH_TABLE (default pt-health-profiles),
PT_INVOICING_TABLE (enables the payer list), PT_STUDIO_REGION (default eu-west-2).
"""
import os
from dataclasses import dataclass, field
from decimal import Decimal

import db

DEFAULT_REGION = "eu-west-2"
DEFAULT_HEALTH_TABLE = "pt-health-profiles"
DEFAULT_GROWTH_TABLE = "pt-growth-measurements"


@dataclass
class SyncResult:
    enabled: bool = True
    pulled: int = 0
    pushed: int = 0
    conflicts: int = 0          # edits that lost to a newer edit elsewhere
    payers: int | None = None   # None = payer list not refreshed
    problems: list[str] = field(default_factory=list)
    error: str | None = None    # set when the cloud couldn't be reached at all

    @property
    def ok(self) -> bool:
        return self.enabled and not self.error and not self.problems

    def summary(self) -> str:
        if not self.enabled:
            return "Cloud sync is off (PT_TRAINEES_TABLE is not set)."
        if self.error:
            return f"Cloud sync unavailable - working offline. {self.error}"
        bits = [f"Synced: {self.pulled} down, {self.pushed} up"]
        if self.conflicts:
            bits.append(f"{self.conflicts} edit(s) replaced by newer changes")
        if self.problems:
            bits.append("; ".join(self.problems))
        return ". ".join(bits) + "."


def is_enabled() -> bool:
    return bool(os.environ.get("PT_TRAINEES_TABLE"))


def _resource():
    import boto3
    from botocore.config import Config
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("PT_STUDIO_REGION", DEFAULT_REGION),
        # fail fast when offline instead of freezing the app
        config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2}),
    )


def _to_ddb(value):
    return Decimal(str(value)) if isinstance(value, float) else value


def _from_ddb(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _clean(item: dict) -> dict:
    return {k: _to_ddb(v) for k, v in item.items() if v not in (None, "")}


def friendly_error(exc: Exception) -> str:
    text = f"{type(exc).__name__} {exc}"
    if any(w in text for w in ("SSOToken", "TokenRetrieval", "ExpiredToken", "UnauthorizedSSO",
                               "NoCredentials", "InvalidClientTokenId")):
        return "Your AWS login has expired - run: aws sso login --profile pt"
    if "AccessDenied" in text:
        return "AWS refused access - the sync permission may be missing."
    if "ResourceNotFound" in text:
        return "A client table was not found - has the clients stack been deployed?"
    if any(w in text for w in ("EndpointConnection", "ConnectTimeout", "ReadTimeout", "ConnectionError")):
        return "No internet connection."
    return str(exc)


def _scan_all(table) -> list[dict]:
    items, kwargs = [], {}
    while True:
        page = table.scan(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def fetch_payers(dynamodb, invoicing_table: str) -> list[dict]:
    """Query (not scan) the CUSTOMER partition. Read-only."""
    from boto3.dynamodb.conditions import Key
    table, payers, kwargs = dynamodb.Table(invoicing_table), [], {"KeyConditionExpression": Key("pk").eq("CUSTOMER")}
    while True:
        page = table.query(**kwargs)
        payers.extend({"id": i["sk"], "name": i.get("name", "")} for i in page["Items"])
        if "LastEvaluatedKey" not in page:
            return payers
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _newer(a: str | None, b: str | None) -> bool:
    return (a or "") > (b or "")


def _pull(trainees_table, health_table, result: SyncResult) -> None:
    health = {h["trainee_id"]: h for h in _scan_all(health_table)}
    for t in _scan_all(trainees_table):
        cid, h = t["trainee_id"], health.get(t["trainee_id"], {})
        cloud = {
            "id": cid,
            "first_name": t.get("first_name", ""), "last_name": t.get("last_name", ""),
            "payer_id": t.get("payer_id"), "created_at": t.get("created_at"),
            "updated_at": t.get("updated_at"),
            "sex": h.get("sex"), "dob": h.get("dob"),
            **{k: _from_ddb(h.get(k)) for k in ("height_cm", "weight_kg", "resting_hr", "max_hr_manual")},
            "max_hr": _from_ddb(h.get("max_hr_observed")),
        }
        local = db.get_client(cid)
        if local is None:
            db.apply_cloud_client({**cloud, "synced_at": cloud["updated_at"]})
            result.pulled += 1
        elif _newer(cloud["updated_at"], local["updated_at"]):
            was_dirty = local["synced_at"] is None or local["synced_at"] < local["updated_at"]
            merged_max = max(local["max_hr"] or 0, cloud["max_hr"] or 0) or None
            # a learned max is only ever raised; if ours is higher the cloud still needs it
            needs_push = bool(merged_max and merged_max != cloud["max_hr"])
            db.apply_cloud_client({**cloud, "max_hr": merged_max,
                                   "synced_at": None if needs_push else cloud["updated_at"]})
            result.pulled += 1
            result.conflicts += 1 if was_dirty else 0
        else:
            db.raise_observed_max_hr(cid, cloud["max_hr"])


def _push(trainees_table, health_table, known_payers: set | None, result: SyncResult) -> None:
    from botocore.exceptions import ClientError
    for c in db.dirty_clients():
        name = f'{c["first_name"]} {c["last_name"]}'.strip()
        if c["payer_id"] and known_payers is not None and c["payer_id"] not in known_payers:
            result.problems.append(f"{name}: payer no longer exists in invoicing - pick another")
            continue
        ts = c["updated_at"]
        guard = {
            "ConditionExpression": "attribute_not_exists(updated_at) OR updated_at <= :u",
            "ExpressionAttributeValues": {":u": ts},
        }
        try:
            trainees_table.put_item(Item=_clean({
                "trainee_id": c["id"], "first_name": c["first_name"], "last_name": c["last_name"],
                "payer_id": c["payer_id"], "created_at": c["created_at"], "updated_at": ts}), **guard)
            health_table.put_item(Item=_clean({
                "trainee_id": c["id"], "sex": c["sex"], "dob": c["dob"],
                "height_cm": c["height_cm"], "weight_kg": c["weight_kg"], "resting_hr": c["resting_hr"],
                "max_hr_manual": c["max_hr_manual"], "max_hr_observed": c["max_hr"],
                "updated_at": ts}), **guard)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                result.conflicts += 1      # a newer edit exists in the cloud; next sync pulls it
                continue
            raise
        db.mark_synced(c["id"], ts)
        result.pushed += 1


def sync(dynamodb=None) -> SyncResult:
    """Pull, refresh payers, push. Never raises for cloud problems - they land in the result."""
    result = SyncResult(enabled=is_enabled() or dynamodb is not None)
    if not result.enabled:
        return result
    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:                                  # pragma: no cover
        result.error = "boto3 is not installed."
        return result
    try:
        dynamodb = dynamodb or _resource()
        trainees = dynamodb.Table(os.environ.get("PT_TRAINEES_TABLE", "pt-trainees"))
        health = dynamodb.Table(os.environ.get("PT_HEALTH_TABLE", DEFAULT_HEALTH_TABLE))
        known_payers = None
        invoicing = os.environ.get("PT_INVOICING_TABLE")
        if invoicing:
            payers = fetch_payers(dynamodb, invoicing)
            db.replace_payers(payers)
            result.payers, known_payers = len(payers), {p["id"] for p in payers}
        _pull(trainees, health, result)
        _push(trainees, health, known_payers, result)
    except (BotoCoreError, ClientError) as exc:
        result.error = friendly_error(exc)
    return result


def delete_trainee(client_id: str, dynamodb=None) -> None:
    """GDPR erasure for the cloud tables (export.delete_client_data covers the S3 data).

    Also removes a junior's growth measurements and mid-parent height. The junior
    consent log (pt-junior-consents) is kept as evidence of what was agreed; how
    long to keep it is still an open retention decision.
    """
    dynamodb = dynamodb or _resource()
    dynamodb.Table(os.environ.get("PT_TRAINEES_TABLE", "pt-trainees")).delete_item(Key={"trainee_id": client_id})
    dynamodb.Table(os.environ.get("PT_HEALTH_TABLE", DEFAULT_HEALTH_TABLE)).delete_item(Key={"trainee_id": client_id})
    _delete_partition(dynamodb.Table(os.environ.get("PT_GROWTH_TABLE", DEFAULT_GROWTH_TABLE)), client_id)


def _delete_partition(table, client_id: str) -> None:
    """Delete every item for one trainee. A missing table (juniors not deployed) is fine."""
    from boto3.dynamodb.conditions import Key
    from botocore.exceptions import ClientError
    kwargs = {"KeyConditionExpression": Key("trainee_id").eq(client_id),
              "ProjectionExpression": "trainee_id, sk"}
    try:
        with table.batch_writer() as batch:
            while True:
                page = table.query(**kwargs)
                for item in page["Items"]:
                    batch.delete_item(Key={"trainee_id": item["trainee_id"], "sk": item["sk"]})
                if "LastEvaluatedKey" not in page:
                    return
                kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
