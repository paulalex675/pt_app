# Junior growth tracking

Consent log, growth measurements and maturity estimates for under-18 trainees.
Lives in the same repo and CDK stack as the shared trainee table, so no new repo.

## What's here

| File | Purpose |
| --- | --- |
| `maturity.py` | Pure maths: decimal age, junior check, Moore 2015 and Mirwald 2002 maturity offset, mid-parent height, growth velocity, PHV stage |
| `consent.py` | Consent rules: active consent per type, status panel, building rows, single-use link tokens |
| `handler.py` | Lambdas: record a measurement, parent consent submit, withdraw consent |
| `juniors_construct.py` | CDK construct: 3 DynamoDB tables + 3 Lambdas with least-privilege grants |
| `tests/` | 32 tests, including a full consent → measure → withdraw flow on mocked DynamoDB |

## Tables

| Table | Keys | Holds |
| --- | --- | --- |
| `JuniorConsents` | `trainee_id` / `sk` = `<type>#<granted_at>` | Append-only consent log; withdrawal stamps `withdrawn_at` |
| `JuniorConsentRequests` | `token_hash` | Single-use emailed links, SHA-256 hashed, TTL 14 days |
| `GrowthMeasurements` | `trainee_id` / `sk` = `<date>#<id>` | Raw measurements plus the calculated results and `algorithm_version` |

Existing trainee records need two new attributes: `date_of_birth` (`YYYY-MM-DD`)
and `sex` (`male` / `female`, used only by the equations). The consent handler
adds `mid_parent_height_cm`; the two raw parent heights are never stored.

## Rules built in

- Junior status is calculated from date of birth, never stored.
- No measurement is saved without an active `growth_measurements` consent.
- Consent counts only if it's on the current wording (`CURRENT_CONSENT_VERSION`).
  Bump that string when the form text changes and everyone shows "expired".
- Parent consent stops counting at 18; the athlete must then consent as `self`.
- Growth over 0.6 cm/month flags `rapid_growth` and moves a pre-PHV child to circa-PHV.
- Maturity offset is flagged unreliable for boys outside 11–16 and girls outside 9–15.

## Run the tests (Git Bash)

```bash
python -m venv .venv
source .venv/Scripts/activate
pip install pytest boto3 moto aws-cdk-lib constructs
python -m pytest -q juniors/tests
```

## Wire into the existing stack

```python
from juniors.juniors_construct import JuniorsConstruct

juniors = JuniorsConstruct(self, "Juniors", trainees_table=trainees_table)
```

Then add routes to the existing HTTP API:

- `POST /juniors/measurements` → `juniors.measurement_fn` (Cognito authoriser)
- `POST /juniors/consent/withdraw` → `juniors.consent_withdraw_fn` (Cognito authoriser)
- `POST /juniors/consent` → `juniors.consent_submit_fn` (public; the token is the auth)

Adjust `TRAINEES_TABLE`'s key in `handler.py` if your trainee table's partition
key isn't `trainee_id`.

## Still to do

- **Send consent request**: create a `JuniorConsentRequests` row with
  `new_consent_request()` and email the link via the existing mailer.
- **Front end**: parent consent page, trainer status panel, phone measure screen.
- **Khamis-Roche %PAH**: transcribe the coefficient tables from Khamis & Roche
  (1994), verify them, and pass `percent_pah` into `assess()`. Staging already
  prefers %PAH when supplied.
- **Retention policy**: decide how long measurements are kept after a junior
  leaves or consent is withdrawn.
