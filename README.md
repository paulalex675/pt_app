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
3. Choose **Start session**. The setup controls disappear (pickers, other buttons and the tab
   strip) and the heart rate bars **fill the window**, resizing with it. Each client's live heart
   rate sits beside a bar positioned by percent of estimated max HR, coloured by their current
   zone; the dotted line marks 100% and bars can extend to 115%. Only three buttons remain, with
   the elapsed time beside them:
   - **Pause** freezes recording and every clock (including the elite round clocks). The bars keep
     showing live heart rate, and the paused time is left out of the session duration. The button
     becomes **Resume**.
   - **Stop** ends and saves the session.
   - **Restart** asks first, then discards everything recorded so far and starts a fresh session
     straight away with the same clients and settings. Use it for a false start.
4. **Stop** goes **straight to the Summary tab** (the Session tab behind it is already reset to a
   clean setup screen). The summary shows the start/end times, duration (minus any paused time) and
   each participant's maximum and average heart rate. Calories are estimates from average HR,
   duration, age, sex, and weight; they are unavailable when required client details or HR samples
   are missing.
5. A prompt for resting HR values appears over the summary. Blank values leave the saved value
   unchanged. If an in-session reading exceeds a client's current estimated or recorded max HR, that
   new high is saved and used for their zone calculations in future sessions.
6. **Previous sessions** - the Summary tab lists every finished session, newest first, with the
   date, mode, who took part and how long it lasted. Click one to look back at it. The app opens on
   the most recent session. **Export to cloud** exports whichever session you are looking at, so a
   session can still be uploaded after the app has been closed and reopened.

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

## Elite mode

A second session mode for fighters or people aiming to fight. Pick **Elite** in the Mode box on the
Session tab, add everyone first, then press Start.

Each client runs their own cycle, repeated for the number of rounds you set:

1. **Pre-work** - the client works until their heart rate reaches zone 5.
2. **Work** - the round clock runs **only while they are in zone 5**. If they drop out it pauses
   until they return.
3. **Recovery** - they rest until their heart rate falls to the "recovered" level, then the next
   round's pre-work begins. The last round ends after its work.

Total time is recorded and compared with the real fight length (rounds + fight rests). The aim is to
shrink it towards that over the weeks.

| Setting | Meaning |
|---|---|
| Rounds / Round length | how many rounds, and how many seconds of zone-5 work make a round |
| Recovered at (% of HR reserve) | the rest level; default 70 means resting HR + 70% of (max - resting) |
| Fight rest | the rest in a real fight, used only to work out the target total time |

Rules built in (see `elite.py`):

- Heart rate is smoothed over 3 seconds and each transition needs a 2-second hold, so one noisy
  reading cannot start a round or pause the clock.
- A 3-second grace below zone 5 still counts as work before the clock pauses.
- Caps stop anyone being stuck: 3 minutes of pre-work, 2 minutes paused in a round and 3 minutes of
  recovery. A capped round is recorded with a flag (`prework_cap`, `pause_cap`, `recovery_cap`); a
  round cut short by Stop is flagged `stopped`.
- A gap of more than 5 seconds between readings (strap dropout) is never credited as work.
- Zone 5 and the recovered level are **frozen when the session starts**, so a client's numbers cannot
  shift mid-session if their max HR is updated. The frozen values are stored with the session.
- Pausing the session pauses these clocks too; paused time is not part of the total.

What is recorded per round: pre-work time, zone-5 work, time paused, recovery time, HR at the end of
work, and HR 60 seconds into recovery (blank if they recovered sooner). Keep the settings the same
from week to week and compare round 1 with round 1.

**Be honest about what this proves.** Fights really do sit near maximum heart rate, but no study has
tested this particular zone-5-gated protocol against ordinary intervals. Treat it as a measurable
conditioning tool and a repeatable benchmark, run it as a pilot, and avoid claiming it works better
than other methods. Maximal-effort work also needs health screening and a check with your insurer.
There are no audio cues yet; the screen is the signal.

In the cloud, elite sessions export with `schema_version` 2 (a `mode` field and an `elite` block
holding only that client's own rounds, events and frozen levels). Silver adds a per-round breakdown,
gold keeps an `elite_sessions` history, and the dashboard shows total time per session against the
fight length. Records from before elite mode still work.

## Where things go

Keep this layout. Files copied into the wrong folder (for example a Lambda in the repo root) are not
what gets deployed or tested.

```
main.py  elite.py  db.py  export.py  zones.py  ble_client.py       the desktop app
lambda/silver_transform.py  lambda/api_summary.py                    what `sam deploy` ships
infra/template.yaml                                                  AWS stack (samconfig.toml stays local)
dashboard/index.html                                                 the client dashboard
tests/                                                               all tests live here
```

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