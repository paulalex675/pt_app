# Junior growth tracking

Consent log, monthly growth measurements and maturity estimates for under-18 trainees.
This folder is the whole Lambda package; its tests live in `tests/juniors/`.

## What's here

| File | Purpose |
| --- | --- |
| `maturity.py` | Pure maths: decimal age, junior check, Moore 2015 and Mirwald 2002 maturity offset, mid-parent height, growth velocity, PHV stage |
| `consent.py` | Consent rules: active consent per type, status panel, building rows, single-use link tokens |
| `handler.py` | Lambdas: record a measurement, parent consent submit, withdraw consent |
| `app_api.py` | Trainer app API: trainees, payers, consent requests, growth summaries; public consent-page lookup |

Infrastructure is in the existing templates:

- **Tables** → `infra/clients-stack.yaml`, next to `pt-trainees` and `pt-health-profiles`.
- **Lambdas + API** → `infra/template.yaml`, on their own `JuniorsApi` so the dashboard API is untouched.

## Tables

| Table | Keys | Holds |
| --- | --- | --- |
| `pt-junior-consents` | `trainee_id` / `sk` = `<type>#<granted_at>` | Append-only consent log; withdrawal stamps `withdrawn_at` |
| `pt-junior-consent-requests` | `token_hash` | Single-use emailed links, SHA-256 hashed, TTL 14 days |
| `pt-growth-measurements` | `trainee_id` / `sk` = `<date>#<id>` | Measurements plus calculated results and `algorithm_version`; one `sk = PROFILE` item per junior holds mid-parent height |

Date of birth and sex come from `pt-health-profiles` (`dob`, `sex`), which PT Studio
already syncs, so there's nothing extra to enter. Mid-parent height is kept in the
growth table rather than the health profile because PT Studio's sync replaces the
whole health item on each push. The two raw parent heights are never stored.

PT Studio's `PtStudioSync` policy has no access to the junior tables.

## Rules built in

- Junior status is calculated from date of birth, never stored.
- No measurement is saved without an active `growth_measurements` consent.
- Consent counts only if it's on the current wording (`CURRENT_CONSENT_VERSION`).
  Bump that string when the form text changes and everyone shows "expired".
- Parent consent stops counting at 18; the athlete must then consent as `self`.
- Growth rate is only calculated against a visit at least 28 days earlier (suits monthly measuring).
- Growth over 0.6 cm/month flags `rapid_growth` and moves a pre-PHV child to circa-PHV.
- Maturity offset is flagged unreliable for boys outside 11–16 and girls outside 9–15,
  and needs sex set to male or female in PT Studio.
- `cloud_sync.delete_trainee()` now also erases the junior's growth data. The consent
  log is kept as evidence until a retention period is decided.

## API

Base URL: the `JuniorsApiUrl` stack output.

| Route | Who | What |
| --- | --- | --- |
| `GET /juniors/payers` | trainer | Bill payers from invoicing (their email is the guardian contact) |
| `GET /juniors/trainees` | trainer | Everyone, juniors first, with consent status |
| `POST /juniors/trainees` | trainer | Create a trainee + health profile |
| `GET /juniors/trainees/{id}` | trainer | Profile, consent status and log, open consent links, measurements |
| `PUT /juniors/trainees/{id}` | trainer | Edit any of: names, payer, dob, sex, height, weight |
| `POST /juniors/trainees/{id}/consent-request` | trainer | Email a consent link to the payer |
| `POST /juniors/trainees/{id}/growth-summary` | trainer | Email the growth table to the payer (optional `note`) |
| `POST /juniors/measurements` | trainer | Record a measurement (needs growth consent) |
| `POST /juniors/consent/withdraw` | trainer | Withdraw one consent type |
| `POST /juniors/consent/lookup` | parent | What the consent page shows (token in body) |
| `POST /juniors/consent` | parent | Submit consent (token in body) |

Trainer routes need the invoicing Cognito login. Parent routes are public; the
single-use token from the emailed link is the authorisation. The link carries the
token after `#`, so it never reaches server logs.

**Same data as PT Studio.** Trainees and health profiles are the `pt-trainees` /
`pt-health-profiles` items PT Studio syncs, with the same `updated_at` format, so an
edit on the phone shows up in PT Studio at its next sync and vice versa (newest edit
wins). Edits only touch the fields sent, so max HR and resting HR are left alone.

**Emails** go through the invoicing mailer (`email.requested` on the `pt-events` bus)
using the `junior_consent_request` and `junior_growth_summary` templates in the
invoicing repo, BCC'd to `OwnerEmail`. Until `ConsentPageUrl` is set, a consent
request isn't emailed: the API returns the link so you can test it yourself.

## Deploy (Git Bash)

1. Tables (same command as before; it adds the three new tables to the existing stack):

```bash
cd infra
aws cloudformation deploy --template-file clients-stack.yaml --stack-name pt-studio-clients \
  --parameter-overrides InvoicingTableName="$INVOICING_TABLE" \
  --capabilities CAPABILITY_NAMED_IAM --profile pt
```

2. Find the invoicing app's Cognito user pool and app client IDs:

```bash
aws cognito-idp list-user-pools --max-results 10 --profile pt --region eu-west-2
aws cognito-idp list-user-pool-clients --user-pool-id <pool id> --profile pt --region eu-west-2
```

3. Lambdas and API. `--guided` asks for any new parameters (Cognito IDs,
   `InvoicingTableName`, `OwnerEmail`, `ConsentPageUrl`) and saves them to
   `samconfig.toml`; accept the other saved answers:

```bash
sam build
sam deploy --guided --profile pt
```

   The new `JuniorsApiUrl` output is the base URL for the consent/measure app.

## Run the tests

```bash
python -m pytest -q tests/juniors
```

## Still to do

- **Send consent request**: create a `pt-junior-consent-requests` row with
  `new_consent_request()` and email the link via the invoicing mailer.
- **Front end**: parent consent page, trainer status panel, phone measure screen.
- **Khamis-Roche %PAH**: transcribe the coefficient tables from Khamis & Roche
  (1994), verify them, and pass `percent_pah` into `assess()`. Staging already
  prefers %PAH when supplied.
- **Retention policy**: how long the consent log and measurements are kept after a
  junior leaves or consent is withdrawn.
- **Restrict CORS**: set `JuniorsAllowedOrigin` to the app's URL once it's hosted.
