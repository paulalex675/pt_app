# PT Studio — live heart rate app

A desktop app (macOS/Windows/Linux) for running PT sessions with multiple
clients and BLE heart-rate straps at the same time.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Pinned to `flet==1.0.2`. Flet has changed its API significantly (and more
than once) on the way to its 1.0 line, so **don't loosen this pin to `>=`**
— a later release could rename things again exactly like it did between
this app's first version and now. Bump the pin deliberately when you want
to upgrade, and re-check the app still opens afterwards.

On first Bluetooth connection, macOS will prompt for Bluetooth permission
for whichever app is running the script (Terminal, VS Code, etc.) — accept
this or the scan will silently find nothing.

## Using it

1. **Clients tab** — add a client (first/last name required; sex, DOB,
   height, weight and resting HR are all optional but recommended, since
   they drive the zone calculation).
2. **Session tab** — scan for straps, then select an available client and
  device and choose **Attach client to device**. Repeat to add participants;
  already attached clients and devices are removed from the selectors.
3. Choose **Start session** to record the participants. Each client's live
  heart rate appears beside a bar positioned by percent of estimated max HR;
  the fill color follows their current training zone. The dotted line marks
  100%, and bars can extend to 115%. **End session** closes it out.
4. Ending a session records its UTC end time and opens **Summary**. The
  summary shows the stored start/end times, duration, and each participant's
  maximum and average heart rate. Calories are estimates from average HR,
  duration, age, sex, and weight; they are unavailable when required client
  details or HR samples are missing.
5. After ending, enter any resting HR values the clients provide. Blank values
  leave the saved value unchanged. If an in-session reading exceeds a client's
  current estimated or recorded max HR, that new high is saved and used for
  their zone calculations in future sessions.

## Data

Everything is stored locally in `pt_studio.db` (SQLite), in tables including
`clients`, `sessions`, `session_participants`, and `hr_samples`. Samples retain
their client and strap identifiers. This is meant to be the source you
later export from and push into your S3 bronze layer — each session's
samples can be pulled with `db.session_samples(session_id)` and written out
as JSON/CSV for upload.

## Cloud pipeline and dashboard

```
End session -> Export to cloud -> S3 bronze -> Lambda -> S3 silver + gold -> API Lambda -> dashboard/index.html
```

- **bronze/** — one JSON record **per session per client**, exactly as recorded
  (`bronze/client_id=<id>/<session_id>.json`). A three-person session uploads three
  records. Records are pseudonymous: client id only (no name) and age at the session
  rather than date of birth.
- **silver/** — one computed summary per session per client (duration, avg/max/min HR,
  time in each zone). Zones use the client's recorded max HR when there is one.
  Gaps longer than 30s (strap dropouts) are not credited to any zone.
- **gold/** — one rolling summary per client, read by the dashboard API.

No Glue or Athena: those suit the Apple Health pipeline's multi-million-row XML, not
one small JSON record per client per session. One Lambda is simpler to run and debug.

### Deploy

Needs the AWS SAM CLI and credentials (`aws configure`).

```bash
cd infra
sam build
sam deploy --guided      # give BucketNameSuffix something unique (e.g. initials)
```

Then:

1. Set `PT_STUDIO_BUCKET` to the `BucketName` output (and optionally `PT_STUDIO_REGION`,
   default `eu-west-2`) before running `python main.py`.
2. In `dashboard/index.html`, set `API_BASE` to the `ApiUrl` output and `CLIENT_ID` to
   a client's id (`sqlite3 pt_studio.db "select id, first_name from clients;"`).
3. End a session, open the **Summary** tab, press **Export to cloud**, then open
   `dashboard/index.html`.

### Erasing a client (UK GDPR)

```bash
python -c "import export; print(export.delete_client_data('<client_id>'), 'objects deleted')"
```

This removes the client's bronze, silver and gold objects. S3 versioning is
deliberately **off** in the template — with versioning, a delete only adds a marker and
the data stays recoverable. Also delete the client from the local `pt_studio.db`.
Note that the local database still holds names and dates of birth.

### Before this goes public

The dashboard is unauthenticated and the API allows any origin. Fine for a private
screenshot; before a real client uses it, add a login and narrow
`Access-Control-Allow-Origin` in `lambda/api_summary.py` to your site's domain.

Tests: `pip install -r requirements-dev.txt` then `python -m unittest discover -s tests`.

## Known limits (v1)

- The app opens one BLE connection per attached participant. Bluetooth
  adapter and operating-system limits may constrain the number of simultaneous
  straps.
- `scan_for_straps()` lists every HR-capable device in range, so in a busy gym
  narrow the scan by matching `device.name` for "Polar".
- No auth/login yet — this is the private, in-studio tool. A client-facing
  login and dashboard is a separate, later build on top of the same data.
- Zone maths uses Tanaka max-HR (`208 - 0.7 * age`) and Karvonen when
  resting HR is known. Worth sanity-checking against Polar's own app for
  a client or two before trusting it fully.