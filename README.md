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

## Data

Everything is stored locally in `pt_studio.db` (SQLite), in tables including
`clients`, `sessions`, `session_participants`, and `hr_samples`. Samples retain
their client and strap identifiers. This is meant to be the source you
later export from and push into your S3 bronze layer — each session's
samples can be pulled with `db.session_samples(session_id)` and written out
as JSON/CSV for upload.

## Known limits (v1)

- The app opens one BLE connection per attached participant. Bluetooth
  adapter and operating-system limits may constrain the number of simultaneous
  straps.
- `scan_for_straps()` connects to the first HR-capable device it finds.
  If other BLE heart rate devices are ever in range at the same time,
  narrow the scan by matching `device.name` for "Polar".
- No auth/login yet — this is the private, in-studio tool. A client-facing
  login and dashboard is a separate, later build on top of the same data.
- Zone maths uses Tanaka max-HR (`208 - 0.7 * age`) and Karvonen when
  resting HR is known. Worth sanity-checking against Polar's own app for
  a client or two before trusting it fully.