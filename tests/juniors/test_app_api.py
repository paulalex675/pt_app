"""Trainer app API tests against mocked DynamoDB and EventBridge (moto)."""

import importlib
import json
import os
from datetime import date, datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

TRAINEES, HEALTH, CONSENTS = "pt-trainees", "pt-health-profiles", "pt-junior-consents"
REQUESTS, GROWTH, INVOICING = "pt-junior-consent-requests", "pt-growth-measurements", "invoicing"


def _table(ddb, name, pk, sk=None, gsi=None):
    keys = [{"AttributeName": pk, "KeyType": "HASH"}]
    attrs = {pk}
    if sk:
        keys.append({"AttributeName": sk, "KeyType": "RANGE"})
        attrs.add(sk)
    extra = {}
    if gsi:
        attrs.add(gsi)
        extra["GlobalSecondaryIndexes"] = [{
            "IndexName": "by_trainee", "KeySchema": [{"AttributeName": gsi, "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"}}]
    ddb.create_table(TableName=name, KeySchema=keys, BillingMode="PAY_PER_REQUEST",
                     AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in attrs],
                     **extra)


@pytest.fixture
def env():
    os.environ.update({
        "AWS_DEFAULT_REGION": "eu-west-2", "AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x",
        "TRAINEES_TABLE": TRAINEES, "HEALTH_TABLE": HEALTH, "CONSENTS_TABLE": CONSENTS,
        "CONSENT_REQUESTS_TABLE": REQUESTS, "MEASUREMENTS_TABLE": GROWTH,
        "INVOICING_TABLE": INVOICING, "EVENT_BUS": "pt-events",
        "CONSENT_PAGE_URL": "https://consent.example/", "OWNER_EMAIL": "paul@example.com",
    })
    with mock_aws():
        ddb = boto3.resource("dynamodb")
        _table(ddb, TRAINEES, "trainee_id")
        _table(ddb, HEALTH, "trainee_id")
        _table(ddb, CONSENTS, "trainee_id", "sk")
        _table(ddb, REQUESTS, "token_hash", gsi="trainee_id")
        _table(ddb, GROWTH, "trainee_id", "sk")
        _table(ddb, INVOICING, "pk", "sk")
        ddb.Table(INVOICING).put_item(Item={"pk": "CUSTOMER", "sk": "cust-jane",
                                            "name": "Jane Smith", "email": "jane@example.com"})
        events = boto3.client("events")
        events.create_event_bus(Name="pt-events")
        # Capture what the mailer would receive.
        sqs = boto3.client("sqs")
        q = sqs.create_queue(QueueName="mail")["QueueUrl"]
        arn = sqs.get_queue_attributes(QueueUrl=q, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        events.put_rule(Name="r", EventBusName="pt-events",
                        EventPattern=json.dumps({"detail-type": ["email.requested"]}))
        events.put_targets(Rule="r", EventBusName="pt-events", Targets=[{"Id": "q", "Arn": arn}])

        import juniors.app_api as api
        import juniors.handler as handler
        api, handler = importlib.reload(api), importlib.reload(handler)

        def mail():
            msgs = sqs.receive_message(QueueUrl=q, MaxNumberOfMessages=10).get("Messages", [])
            return [json.loads(m["Body"])["detail"] for m in msgs]

        yield api, handler, ddb, mail


def call(api, route, body=None, **params):
    r = api.handler({"routeKey": route, "pathParameters": params or None,
                     "body": json.dumps(body) if body is not None else None}, None)
    return r["statusCode"], json.loads(r["body"])


JUNIOR = {"first_name": "Sam", "last_name": "Smith", "payer_id": "cust-jane",
          "dob": "2013-05-01", "sex": "male", "height_cm": 150, "weight_kg": 40}


def test_payers_come_from_invoicing(env):
    api, *_ = env
    status, body = call(api, "GET /juniors/payers")
    assert status == 200 and body == [{"id": "cust-jane", "name": "Jane Smith", "email": "jane@example.com"}]


def test_create_writes_pt_studio_shaped_items(env):
    api, _, ddb, _ = env
    status, body = call(api, "POST /juniors/trainees", JUNIOR)
    assert status == 201 and body["is_junior"] and body["payer"]["email"] == "jane@example.com"
    t = ddb.Table(TRAINEES).get_item(Key={"trainee_id": body["id"]})["Item"]
    h = ddb.Table(HEALTH).get_item(Key={"trainee_id": body["id"]})["Item"]
    assert t["first_name"] == "Sam" and t["payer_id"] == "cust-jane"
    assert h["dob"] == "2013-05-01" and h["sex"] == "male"
    # Same naive-UTC microsecond format PT Studio uses, so string comparison works.
    datetime.fromisoformat(t["updated_at"])
    assert "+" not in t["updated_at"] and len(t["updated_at"].split(".")[1]) == 6
    assert body["consent"] == {c: "missing" for c in ("training", "growth_measurements", "hr_data", "photos")}


def test_create_validates(env):
    api, *_ = env
    assert call(api, "POST /juniors/trainees", {**JUNIOR, "first_name": ""})[0] == 400
    assert call(api, "POST /juniors/trainees", {**JUNIOR, "payer_id": "nope"})[0] == 400
    assert call(api, "POST /juniors/trainees", {**JUNIOR, "dob": "2099-01-01"})[0] == 400
    assert call(api, "POST /juniors/trainees", {**JUNIOR, "height_cm": 900})[0] == 400


def test_edit_keeps_fields_this_app_does_not_own(env):
    api, _, ddb, _ = env
    tid = call(api, "POST /juniors/trainees", JUNIOR)[1]["id"]
    ddb.Table(HEALTH).update_item(Key={"trainee_id": tid}, UpdateExpression="SET max_hr_manual = :m",
                                  ExpressionAttributeValues={":m": 195})
    before = ddb.Table(TRAINEES).get_item(Key={"trainee_id": tid})["Item"]["updated_at"]
    status, body = call(api, "PUT /juniors/trainees/{id}", {"height_cm": 152.5, "payer_id": ""}, id=tid)
    assert status == 200 and body["height_cm"] == 152.5 and body["payer"] is None
    h = ddb.Table(HEALTH).get_item(Key={"trainee_id": tid})["Item"]
    t = ddb.Table(TRAINEES).get_item(Key={"trainee_id": tid})["Item"]
    assert int(h["max_hr_manual"]) == 195 and "payer_id" not in t
    assert t["updated_at"] > before and t["updated_at"] == h["updated_at"]


def test_list_puts_juniors_first(env):
    api, *_ = env
    call(api, "POST /juniors/trainees", {**JUNIOR, "first_name": "Adult", "last_name": "Aaron", "dob": "1990-01-01"})
    call(api, "POST /juniors/trainees", JUNIOR)
    status, body = call(api, "GET /juniors/trainees")
    assert [b["first_name"] for b in body] == ["Sam", "Adult"]
    assert "consent" not in body[1]


def test_consent_request_emails_payer_and_full_flow(env):
    api, handler, ddb, mail = env
    tid = call(api, "POST /juniors/trainees", JUNIOR)[1]["id"]

    status, body = call(api, "POST /juniors/trainees/{id}/consent-request", id=tid)
    assert status == 201 and body["email_queued"] and body["sent_to"] == "jane@example.com"
    [msg] = mail()
    assert msg["template"] == "junior_consent_request" and msg["to"] == ["jane@example.com"]
    assert msg["bcc"] == ["paul@example.com"] and msg["data"]["child_first_name"] == "Sam"
    token = msg["data"]["link"].split("#t=")[1]

    assert len(call(api, "GET /juniors/trainees/{id}", id=tid)[1]["pending_requests"]) == 1

    status, page = call(api, "POST /juniors/consent/lookup", {"token": token})
    assert status == 200 and page["child_first_name"] == "Sam" and page["guardian_name"] == "Jane Smith"

    resp = handler.consent_submit_handler({"body": json.dumps({
        "token": token, "given_by": "Jane Smith", "relationship": "mother",
        "granted": ["training", "growth_measurements"]})}, None)
    assert resp["statusCode"] == 200

    detail = call(api, "GET /juniors/trainees/{id}", id=tid)[1]
    assert detail["consent"]["growth_measurements"] == "granted"
    assert detail["consent"]["photos"] == "missing"
    assert detail["pending_requests"] == []
    assert call(api, "POST /juniors/consent/lookup", {"token": token})[0] == 410


def test_consent_request_needs_payer_email(env):
    api, *_ = env
    tid = call(api, "POST /juniors/trainees", {**JUNIOR, "payer_id": ""})[1]["id"]
    status, body = call(api, "POST /juniors/trainees/{id}/consent-request", id=tid)
    assert status == 422 and "bill payer" in body["error"]


def test_consent_request_not_for_adults(env):
    api, *_ = env
    tid = call(api, "POST /juniors/trainees", {**JUNIOR, "dob": "1990-01-01"})[1]["id"]
    assert call(api, "POST /juniors/trainees/{id}/consent-request", id=tid)[0] == 422


def test_without_consent_page_url_link_is_returned(env, monkeypatch):
    api, _, _, mail = env
    monkeypatch.setattr(api, "CONSENT_PAGE_URL", "")
    tid = call(api, "POST /juniors/trainees", JUNIOR)[1]["id"]
    status, body = call(api, "POST /juniors/trainees/{id}/consent-request", id=tid)
    assert status == 201 and not body["email_queued"] and body["link"].startswith("#t=")
    assert mail() == []


def _grant(api, handler, tid, mail):
    call(api, "POST /juniors/trainees/{id}/consent-request", id=tid)
    token = mail()[-1]["data"]["link"].split("#t=")[1]
    handler.consent_submit_handler({"body": json.dumps({
        "token": token, "given_by": "Jane Smith", "relationship": "mother",
        "granted": ["growth_measurements"]})}, None)


def test_growth_summary(env):
    api, handler, _, mail = env
    tid = call(api, "POST /juniors/trainees", JUNIOR)[1]["id"]
    assert call(api, "POST /juniors/trainees/{id}/growth-summary", id=tid)[0] == 403

    _grant(api, handler, tid, mail)
    assert call(api, "POST /juniors/trainees/{id}/growth-summary", id=tid)[0] == 422  # nothing measured

    for days_ago, height in ((60, 148.0), (30, 149.0), (0, 150.2)):
        on = (date.today() - timedelta(days=days_ago)).isoformat()
        r = handler.measurement_handler({"body": json.dumps(
            {"trainee_id": tid, "measured_on": on, "height_cm": height,
             "sitting_height_cm": 78.0, "weight_kg": 40.0})}, None)
        assert r["statusCode"] == 201

    detail = call(api, "GET /juniors/trainees/{id}", id=tid)[1]
    assert [m["height_cm"] for m in detail["measurements"]] == [150.2, 149.0, 148.0]

    status, body = call(api, "POST /juniors/trainees/{id}/growth-summary", {"note": "Great progress"}, id=tid)
    assert status == 200 and body["email_queued"]
    [msg] = mail()
    d = msg["data"]
    assert msg["template"] == "junior_growth_summary" and msg["to"] == ["jane@example.com"]
    assert len(d["measurements"]) == 3 and d["note"] == "Great progress"
    assert d["rapid_growth"] is True and d["stage"] == "circa_phv"
    assert d["stage_text"]


def test_unknown_route_and_trainee(env):
    api, *_ = env
    assert call(api, "DELETE /juniors/trainees/{id}", id="x")[0] == 404
    assert call(api, "GET /juniors/trainees/{id}", id="nobody")[0] == 404
