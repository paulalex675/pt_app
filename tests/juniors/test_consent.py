from datetime import date, datetime, timezone

import pytest

from juniors.consent import (
    CURRENT_CONSENT_VERSION,
    ConsentError,
    active_consents,
    build_consent_rows,
    hash_token,
    new_consent_request,
    require,
    status_by_type,
)

DOB_CHILD = date(2013, 5, 1)      # 13 in Oct 2026
DOB_TURNING_18 = date(2008, 9, 1)  # turned 18 in Sept 2026
TODAY = date(2026, 10, 22)


def row(ctype, granted_at, relationship="mother", version=CURRENT_CONSENT_VERSION,
        withdrawn_at=None):
    r = {"consent_type": ctype, "granted_at": granted_at,
         "relationship": relationship, "consent_version": version}
    if withdrawn_at:
        r["withdrawn_at"] = withdrawn_at
    return r


def test_parent_consent_active_for_child():
    rows = [row("growth_measurements", "2026-10-01T10:00:00+00:00")]
    assert active_consents(rows, DOB_CHILD, TODAY) == {"growth_measurements"}


def test_withdrawn_consent_not_active():
    rows = [row("growth_measurements", "2026-10-01T10:00:00+00:00",
                withdrawn_at="2026-10-10T10:00:00+00:00")]
    assert active_consents(rows, DOB_CHILD, TODAY) == set()
    with pytest.raises(ConsentError):
        require("growth_measurements", rows, DOB_CHILD, TODAY)


def test_latest_row_wins_regrant_after_withdrawal():
    rows = [
        row("hr_data", "2026-09-01T10:00:00+00:00", withdrawn_at="2026-09-05T10:00:00+00:00"),
        row("hr_data", "2026-10-01T10:00:00+00:00"),
    ]
    assert "hr_data" in active_consents(rows, DOB_CHILD, TODAY)


def test_old_wording_needs_reconsent():
    rows = [row("training", "2026-01-01T10:00:00+00:00", version="2025-old")]
    assert status_by_type(rows, DOB_CHILD, TODAY)["training"] == "expired"


def test_parent_consent_lapses_at_18_and_self_consent_takes_over():
    parent = [row("training", "2026-01-01T10:00:00+00:00")]
    assert active_consents(parent, DOB_TURNING_18, TODAY) == set()
    own = parent + [row("training", "2026-10-01T10:00:00+00:00", relationship="self")]
    assert active_consents(own, DOB_TURNING_18, TODAY) == {"training"}


def test_child_cannot_self_consent():
    rows = [row("training", "2026-10-01T10:00:00+00:00", relationship="self")]
    assert active_consents(rows, DOB_CHILD, TODAY) == set()


def test_status_panel_shows_every_type():
    status = status_by_type([row("training", "2026-10-01T10:00:00+00:00")], DOB_CHILD, TODAY)
    assert status == {"training": "granted", "growth_measurements": "missing",
                      "hr_data": "missing", "photos": "missing"}


def test_build_rows_only_for_ticked_types():
    now = datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc)
    rows = build_consent_rows("t1", "Jane Smith", "mother",
                              ["training", "growth_measurements"], "online_form", now)
    assert [r["consent_type"] for r in rows] == ["training", "growth_measurements"]
    assert rows[0]["sk"] == "training#2026-10-22T18:00:00+00:00"
    assert all(r["consent_version"] == CURRENT_CONSENT_VERSION for r in rows)


@pytest.mark.parametrize("kwargs", [
    {"relationship": "uncle"},
    {"granted_types": ["marketing_emails"]},
    {"given_by": "   "},
])
def test_build_rows_rejects_bad_input(kwargs):
    args = {"trainee_id": "t1", "given_by": "Jane", "relationship": "mother",
            "granted_types": ["training"], "method": "online_form"}
    args.update(kwargs)
    with pytest.raises(ValueError):
        build_consent_rows(**args)


def test_request_stores_only_hash():
    req = new_consent_request(datetime(2026, 10, 22, tzinfo=timezone.utc))
    assert req.token_hash == hash_token(req.token)
    assert req.token not in req.token_hash
    assert req.expires_at == int(datetime(2026, 11, 5, tzinfo=timezone.utc).timestamp())
