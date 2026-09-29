# PT Studio — live heart rate app

A desktop app (macOS/Windows/Linux) for running PT sessions with a single
Polar H10 pod, shared between clients with personal straps.

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
2. **Session tab** — pick the client, put the H10 pod on their strap, tap
   **Scan for strap** (the strap needs skin contact to wake up and start
   advertising), then **Start session** once connected.
3. Live heart rate, current zone and time-in-zone are shown while the
   session runs. **End session** closes it out in the database.

## Data

Everything is stored locally in `pt_studio.db` (SQLite), in three tables:
`clients`, `sessions`, `hr_samples`. This is meant to be the source you
later export from and push into your S3 bronze layer — each session's
samples can be pulled with `db.session_samples(session_id)` and written out
as JSON/CSV for upload.

## Known limits (v1)

- One BLE connection at a time — fine for a single H10 pod, but it means
  no two simultaneous live sessions. A second pod just needs a second
  `HeartRateStream` instance wired into the UI.
- `scan_for_straps()` connects to the first HR-capable device it finds.
  If other BLE heart rate devices are ever in range at the same time,
  narrow the scan by matching `device.name` for "Polar".
- No auth/login yet — this is the private, in-studio tool. A client-facing
  login and dashboard is a separate, later build on top of the same data.
- Zone maths uses Tanaka max-HR (`208 - 0.7 * age`) and Karvonen when
  resting HR is known. Worth sanity-checking against Polar's own app for
  a client or two before trusting it fully.