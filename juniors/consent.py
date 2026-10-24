"""Consent rules for junior trainees.

Consent is an append-only log: one row per consent type each time it is
granted, and withdrawal sets `withdrawn_at` on that row rather than deleting
it. The current state of each type is the latest row for that type.

DynamoDB layout (JuniorConsents table)
    PK  trainee_id
    SK  "<consent_type>#<granted_at ISO timestamp>"
    given_by, relationship, consent_version, method, granted_at,
    withdrawn_at (optional), evidence_key (S3 key of a signed paper form, optional)
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Mapping

try:
    from .maturity import is_junior
except ImportError:  # deployed: juniors/ is the Lambda root, so modules sit flat
    from maturity import is_junior

# Bump this whenever the consent wording changes. Older versions stop counting
# as active, which forces a re-consent request.
CURRENT_CONSENT_VERSION = "2026-10-v1"

CONSENT_TYPES = (
    "training",             # taking part in coached sessions
    "growth_measurements",  # height, sitting height, weight, maturity estimates
    "hr_data",              # heart-rate recording in PT Studio
    "photos",               # photos or video for marketing
)

RELATIONSHIPS = ("mother", "father", "guardian", "self")
METHODS = ("online_form", "signed_paper")

REQUEST_LINK_LIFETIME = timedelta(days=14)


class ConsentError(Exception):
    """Raised when an action needs consent that isn't active."""


def _latest_per_type(rows: Iterable[Mapping]) -> dict[str, Mapping]:
    latest: dict[str, Mapping] = {}
    for row in rows:
        ctype = row["consent_type"]
        if ctype not in latest or row["granted_at"] > latest[ctype]["granted_at"]:
            latest[ctype] = row
    return latest


def active_consents(rows: Iterable[Mapping], date_of_birth: date,
                    today: date) -> set[str]:
    """Consent types currently in force for this trainee.

    A type is active when its latest row:
      - has not been withdrawn,
      - uses the current consent wording, and
      - was given by the right person: a parent/guardian while the trainee is
        under 18, the trainee themselves ("self") once they are 18.
    """
    adult = not is_junior(date_of_birth, today)
    active = set()
    for ctype, row in _latest_per_type(rows).items():
        if row.get("withdrawn_at"):
            continue
        if row.get("consent_version") != CURRENT_CONSENT_VERSION:
            continue
        given_by_self = row.get("relationship") == "self"
        if adult != given_by_self:
            continue
        active.add(ctype)
    return active


def require(consent_type: str, rows: Iterable[Mapping], date_of_birth: date,
            today: date) -> None:
    """Raise ConsentError unless consent_type is active."""
    if consent_type not in active_consents(rows, date_of_birth, today):
        raise ConsentError(f"No active '{consent_type}' consent for this trainee")


def status_by_type(rows: Iterable[Mapping], date_of_birth: date,
                   today: date) -> dict[str, str]:
    """Traffic-light status per type for the trainer panel:
    'granted', 'withdrawn', 'expired' (old wording or turned 18) or 'missing'."""
    latest = _latest_per_type(rows)
    active = active_consents(latest.values(), date_of_birth, today)
    out = {}
    for ctype in CONSENT_TYPES:
        if ctype in active:
            out[ctype] = "granted"
        elif ctype not in latest:
            out[ctype] = "missing"
        elif latest[ctype].get("withdrawn_at"):
            out[ctype] = "withdrawn"
        else:
            out[ctype] = "expired"
    return out


def build_consent_rows(trainee_id: str, given_by: str, relationship: str,
                       granted_types: Iterable[str], method: str,
                       now: datetime | None = None) -> list[dict]:
    """Rows to write when a consent form is submitted. Only ticked types
    produce rows; an unticked box simply leaves that type missing."""
    if relationship not in RELATIONSHIPS:
        raise ValueError(f"unknown relationship '{relationship}'")
    if method not in METHODS:
        raise ValueError(f"unknown method '{method}'")
    if not given_by.strip():
        raise ValueError("given_by is required")
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    rows = []
    for ctype in granted_types:
        if ctype not in CONSENT_TYPES:
            raise ValueError(f"unknown consent type '{ctype}'")
        rows.append({
            "trainee_id": trainee_id,
            "sk": f"{ctype}#{stamp}",
            "consent_type": ctype,
            "given_by": given_by.strip(),
            "relationship": relationship,
            "consent_version": CURRENT_CONSENT_VERSION,
            "method": method,
            "granted_at": stamp,
        })
    return rows


@dataclass(frozen=True)
class ConsentRequest:
    token: str          # goes in the emailed link, never stored
    token_hash: str     # stored as the ConsentRequests partition key
    expires_at: int     # epoch seconds, used as the DynamoDB TTL attribute


def new_consent_request(now: datetime | None = None) -> ConsentRequest:
    """Single-use link token for the parent consent page. Only the hash is
    stored, so a leaked table can't be used to open consent links."""
    now = now or datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    return ConsentRequest(
        token=token,
        token_hash=hash_token(token),
        expires_at=int((now + REQUEST_LINK_LIFETIME).timestamp()),
    )


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
