"""End-to-end handler tests against mocked DynamoDB (moto)."""

import importlib
import json
import os
from datetime import date, datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

TABLES = {
    "TRAINEES_TABLE": ("pt-trainees", "trainee_id", None),
    "HEALTH_TABLE": ("pt-health-profiles", "trainee_id", None),
    "CONSENTS_TABLE": ("pt-junior-consents", "trainee_id", "sk"),
    "CONSENT_REQUESTS_TABLE": ("pt-junior-consent-requests", "token_hash", None),
    "MEASUREMENTS_TABLE": ("pt-growth-measurements", "trainee_id", "sk"),
}


@pytest.fixture
def h():
    os.environ.update({"AWS_DEFAULT_REGION": "eu-west-2",
                       "AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x"})
    with mock_aws():
        ddb = boto3.resource("dynamodb")
        for env, (name, pk, sk) in TABLES.items():
            os.environ[env] = name
            keys = [{"AttributeName": pk, "KeyType": "HASH"}]
            attrs = [{"AttributeName": pk, "AttributeType": "S"}]
            if sk:
                keys.append({"AttributeName": sk, "KeyType": "RANGE"})
                attrs.append({"AttributeName": sk, "AttributeType": "S"})
            ddb.create_table(TableName=name, KeySchema=keys,
                             AttributeDefinitions=attrs, BillingMode="PAY_PER_REQUEST")
        # Same shape cloud_sync._push writes.
        ddb.Table("pt-trainees").put_item(Item={
            "trainee_id": "t1", "first_name": "Test", "last_name": "Junior"})
        ddb.Table("pt-health-profiles").put_item(Item={
            "trainee_id": "t1", "dob": "2013-05-01", "sex": "male"})
        import juniors.handler as handler
        yield importlib.reload(handler), ddb


def call(fn, body):
    resp = fn({"body": json.dumps(body)}, None)
    return resp["statusCode"], json.loads(resp["body"])


def send_link(ddb, days=14):
    from juniors.consent import new_consent_request
    req = new_consent_request()
    expires = int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp())
    ddb.Table("pt-junior-consent-requests").put_item(
        Item={"token_hash": req.token_hash, "trainee_id": "t1", "expires_at": expires})
    return req.token


MEASURE = {"trainee_id": "t1", "measured_on": date.today().isoformat(),
           "height_cm": 160.0, "sitting_height_cm": 82.0, "weight_kg": 48.0}


def test_measurement_blocked_without_consent(h):
    handler, _ = h
    status, body = call(handler.measurement_handler, MEASURE)
    assert status == 403


def test_full_flow_consent_then_measure_then_withdraw(h):
    handler, ddb = h
    token = send_link(ddb)
    status, body = call(handler.consent_submit_handler, {
        "token": token, "given_by": "Jane Smith", "relationship": "mother",
        "granted": ["training", "growth_measurements"],
        "parent_a_height_cm": 180, "parent_b_height_cm": 165})
    assert status == 200 and set(body["recorded"]) == {"training", "growth_measurements"}

    profile = ddb.Table("pt-growth-measurements").get_item(
        Key={"trainee_id": "t1", "sk": "PROFILE"})["Item"]
    assert float(profile["mid_parent_height_cm"]) == 172.5
    assert "parent_a_height_cm" not in profile
    # The health profile PT Studio overwrites on sync is left alone.
    assert "mid_parent_height_cm" not in ddb.Table("pt-health-profiles").get_item(
        Key={"trainee_id": "t1"})["Item"]

    # Link is single use.
    status, _ = call(handler.consent_submit_handler, {
        "token": token, "given_by": "Jane", "relationship": "mother", "granted": ["photos"]})
    assert status == 410

    status, body = call(handler.measurement_handler, MEASURE)
    assert status == 201
    assert body["stage"] in ("pre_phv", "circa_phv", "post_phv")
    assert body["algorithm_version"].startswith("maturity-v1")

    status, _ = call(handler.consent_withdraw_handler,
                     {"trainee_id": "t1", "consent_type": "growth_measurements"})
    assert status == 200
    status, _ = call(handler.measurement_handler, MEASURE)
    assert status == 403


def test_expired_link_rejected(h):
    handler, ddb = h
    token = send_link(ddb, days=-1)
    status, _ = call(handler.consent_submit_handler, {
        "token": token, "given_by": "Jane", "relationship": "mother", "granted": ["training"]})
    assert status == 410


def test_bad_sitting_height_rejected(h):
    handler, ddb = h
    token = send_link(ddb)
    call(handler.consent_submit_handler, {
        "token": token, "given_by": "Jane", "relationship": "mother",
        "granted": ["growth_measurements"]})
    status, body = call(handler.measurement_handler, {**MEASURE, "sitting_height_cm": 16.0})
    assert status == 400


def test_profile_item_is_not_treated_as_a_measurement(h):
    handler, ddb = h
    token = send_link(ddb)
    call(handler.consent_submit_handler, {
        "token": token, "given_by": "Jane", "relationship": "mother",
        "granted": ["growth_measurements"],
        "parent_a_height_cm": 180, "parent_b_height_cm": 165})
    status, body = call(handler.measurement_handler, MEASURE)
    assert status == 201
    assert body["growth_cm_per_month"] is None   # first real measurement, no history


def test_monthly_growth_rate_from_previous_visit(h):
    handler, ddb = h
    token = send_link(ddb)
    call(handler.consent_submit_handler, {
        "token": token, "given_by": "Jane", "relationship": "mother",
        "granted": ["growth_measurements"]})
    last_month = (date.today() - timedelta(days=30)).isoformat()
    assert call(handler.measurement_handler,
                {**MEASURE, "measured_on": last_month, "height_cm": 159.0})[0] == 201
    status, body = call(handler.measurement_handler, MEASURE)
    assert status == 201
    assert body["growth_cm_per_month"] == pytest.approx(1.01, abs=0.02)
    assert "rapid_growth" in body["flags"]


def test_unknown_trainee_404(h):
    handler, _ = h
    status, _ = call(handler.measurement_handler, {**MEASURE, "trainee_id": "nobody"})
    assert status == 404


def test_missing_dob_or_other_sex_422(h):
    handler, ddb = h
    ddb.Table("pt-trainees").put_item(Item={"trainee_id": "t2", "first_name": "A"})
    status, body = call(handler.measurement_handler, {**MEASURE, "trainee_id": "t2"})
    assert status == 422 and "date of birth" in body["error"]
    ddb.Table("pt-health-profiles").put_item(
        Item={"trainee_id": "t2", "dob": "2013-05-01", "sex": "other"})
    status, body = call(handler.measurement_handler, {**MEASURE, "trainee_id": "t2"})
    assert status == 422 and "male or female" in body["error"]


def test_same_day_measurement_replaces_earlier_entry(h):
    handler, ddb = h
    token = send_link(ddb)
    call(handler.consent_submit_handler, {
        "token": token, "given_by": "Jane", "relationship": "mother",
        "granted": ["growth_measurements"]})
    assert call(handler.measurement_handler, {**MEASURE, "height_cm": 106.0, "sitting_height_cm": None})[0] == 201  # typo
    status, _ = call(handler.measurement_handler, MEASURE)
    assert status == 201
    from boto3.dynamodb.conditions import Key
    items = ddb.Table("pt-growth-measurements").query(
        KeyConditionExpression=Key("trainee_id").eq("t1"))["Items"]
    rows = [i for i in items if i["sk"] != "PROFILE"]
    assert len(rows) == 1 and float(rows[0]["height_cm"]) == 160.0
