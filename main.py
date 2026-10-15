"""PT Studio — desktop app for live heart-rate coaching sessions.

Run with: python main.py
Requires a Polar H10 (or other standard BLE HRM) paired/discoverable, and
Bluetooth permission granted to the terminal/IDE running this (macOS will
prompt on first connect).
"""
import asyncio
import json
import os
import time
from datetime import datetime, timedelta

import flet as ft

import db
import elite
import zones
from ble_client import HeartRateStream, rr_to_json, scan_for_straps


def configure_ssl_environment() -> None:
    """Point Python's SSL at certifi's CA bundle when nothing else has.

    A python.org install on macOS ships without root certificates, which makes
    uploads fail with CERTIFICATE_VERIFY_FAILED. setup_env.sh does the same for
    the shell; this covers launching the app any other way.
    """
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi
    except ImportError:
        return
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ.setdefault("SSL_CERT_DIR", "")


def can_start_session(connected: bool, selected_client_id, session_id) -> bool:
    return bool(connected and selected_client_id and not session_id)


def load_client_thresholds(selected_client_id):
    if not selected_client_id:
        return None
    client = db.get_client(selected_client_id)
    if not client:
        return None
    age = db.client_age(client["dob"])
    return zones.zone_thresholds(age, client["resting_hr"], client["max_hr"])


CHART_MAX_PERCENT = 1.15
CHART_WIDTH = 560


def chart_fill_fraction(hr: int, max_hr: int) -> float:
    if max_hr <= 0:
        return 0.0
    return min(max(hr / max_hr, 0.0), CHART_MAX_PERCENT) / CHART_MAX_PERCENT


def chart_marker_fraction() -> float:
    return 1.0 / CHART_MAX_PERCENT


def estimate_calories(sex, age, weight_kg, average_hr, duration_seconds):
    if (
        sex not in ("male", "female")
        or age is None
        or weight_kg is None
        or average_hr is None
        or duration_seconds <= 0
    ):
        return None
    if sex == "male":
        kcal_per_minute = (
            -55.0969 + 0.6309 * average_hr + 0.1988 * weight_kg + 0.2017 * age
        ) / 4.184
    else:
        kcal_per_minute = (
            -20.4022 + 0.4472 * average_hr - 0.1263 * weight_kg + 0.074 * age
        ) / 4.184
    return round(max(0.0, kcal_per_minute) * duration_seconds / 60, 1)


def format_session_timestamp(value):
    if not value:
        return "Not recorded"
    timestamp = datetime.fromisoformat(value)
    return f"{timestamp:%Y-%m-%d %H:%M:%S} UTC"


def format_session_duration(seconds):
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def main(page: ft.Page):
    page.title = "PT Studio"
    page.theme_mode = ft.ThemeMode.DARK
    page.window.maximized = True
    #page.window.width = 900
    #page.window.height = 700
    page.padding = 0
    db.init_db()

    # ---------------- shared state ----------------
    state = {
        "selected_client_id": None,
        "selected_device_address": None,
        "participants": {},
        "session_id": None,
        "last_session_id": None,
        "session_start": None,
        "client_device_bindings": {},
        "available_devices": [],
        "mode": elite.MODE_NORMAL,
        "elite": None,              # settings of the running/last elite session
        "session_start_wall": None,
        "paused": False,
        "pause_started": None,
        "paused_total": 0.0,
        "summary_session_id": None,   # the session shown on the Summary tab
    }

    def clock():
        """Session time: monotonic time minus everything spent paused (frozen while paused)."""
        if state["paused"] and state["pause_started"] is not None:
            return state["pause_started"] - state["paused_total"]
        return time.monotonic() - state["paused_total"]

    def refresh_session_controls():
        has_active_session = bool(state["session_id"])
        start_btn.disabled = not (state["participants"] and not has_active_session)
        stop_btn.disabled = not has_active_session
        client_picker.disabled = False
        device_picker.disabled = False
        for control in (mode_picker, elite_rounds_field, elite_round_field,
                        elite_recovered_field, elite_rest_field):
            control.disabled = has_active_session
        attached_client_ids = set(state["participants"])
        attached_device_addresses = {
            participant["device_address"] for participant in state["participants"].values()
        }
        eligible_clients = [
            client for client in db.list_clients(client_search.value or "")
            if client["id"] not in attached_client_ids
        ]
        eligible_devices = [
            device for device in state["available_devices"]
            if device["address"] not in attached_device_addresses
        ]
        client_picker.options = [
            ft.dropdown.Option(key=client["id"], text=f"{client['first_name']} {client['last_name']}")
            for client in eligible_clients
        ]
        device_picker.options = [
            ft.dropdown.Option(
                key=device["address"],
                text=f"{device['name'] or device['address']} ({device['address']})",
            )
            for device in eligible_devices
        ]
        if state["selected_client_id"] not in {c["id"] for c in eligible_clients}:
            state["selected_client_id"] = None
            client_picker.value = None
        else:
            client_picker.value = state["selected_client_id"]
        if state["selected_device_address"] not in {d["address"] for d in eligible_devices}:
            state["selected_device_address"] = None
            device_picker.value = None
        else:
            device_picker.value = state["selected_device_address"]
        attach_btn.disabled = not (
            state["selected_client_id"]
            and state["selected_device_address"]
            and not any(p["client_id"] == state["selected_client_id"] for p in state["participants"].values())
            and not any(p["device_address"] == state["selected_device_address"] for p in state["participants"].values())
        )

    def set_selected_client(value):
        state["selected_client_id"] = value
        refresh_session_controls()
        page.update()

    def set_selected_device(value):
        state["selected_device_address"] = value
        refresh_session_controls()
        page.update()

    # ==================================================================
    # CLIENTS VIEW
    # ==================================================================
    client_search = ft.TextField(label="Search clients", width=300, on_change=lambda e: refresh_client_list())
    client_list_view = ft.ListView(expand=True, spacing=4)
    client_picker = ft.Dropdown(label="Select client for session", width=340, options=[])
    device_picker = ft.Dropdown(label="Select device for session", width=340, options=[])
    attach_btn = ft.FilledTonalButton("Attach client to device", icon=ft.Icons.LINK, disabled=True)
    participant_chart = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO, expand=True, visible=False)

    def refresh_client_list():
        client_list_view.controls.clear()
        for c in db.list_clients(client_search.value or ""):
            age = db.client_age(c["dob"])
            subtitle_bits = []
            if age is not None:
                subtitle_bits.append(f"{age}y")
            if c["sex"]:
                subtitle_bits.append(c["sex"])
            if c["resting_hr"]:
                subtitle_bits.append(f"RHR {c['resting_hr']}")
            if c["max_hr"]:
                subtitle_bits.append(f"Max HR {c['max_hr']}")
            is_selected = c["id"] == state["selected_client_id"]
            client_list_view.controls.append(
                ft.ListTile(
                    title=ft.Text(f"{c['first_name']} {c['last_name']}"),
                    subtitle=ft.Text(", ".join(subtitle_bits) or "No details yet"),
                    leading=ft.Icon(ft.Icons.PERSON),
                    selected=is_selected,
                    on_click=lambda e, cid=c["id"]: set_selected_client(cid),
                )
            )
        refresh_session_controls()
        page.update()

    def refresh_device_list():
        refresh_session_controls()
        page.update()

    # ---- add client dialog ----
    f_first = ft.TextField(label="First name", width=250)
    f_last = ft.TextField(label="Last name", width=250)
    f_sex = ft.Dropdown(
        label="Sex", width=250,
        options=[ft.dropdown.Option("male"), ft.dropdown.Option("female"), ft.dropdown.Option("other")],
    )
    f_dob = ft.TextField(label="DOB (YYYY-MM-DD)", width=250, hint_text="1990-05-14")
    f_height = ft.TextField(label="Height (cm)", width=250, keyboard_type=ft.KeyboardType.NUMBER)
    f_weight = ft.TextField(label="Weight (kg)", width=250, keyboard_type=ft.KeyboardType.NUMBER)
    f_resting_hr = ft.TextField(label="Resting HR (bpm, optional)", width=250, keyboard_type=ft.KeyboardType.NUMBER)
    f_error = ft.Text(color=ft.Colors.RED_300, visible=False)

    def close_dialog(e=None):
        page.pop_dialog()

    def save_client(e):
        if not f_first.value or not f_last.value:
            f_error.value = "First and last name are required."
            f_error.visible = True
            page.update()
            return
        try:
            height = float(f_height.value) if f_height.value else None
            weight = float(f_weight.value) if f_weight.value else None
            resting_hr = int(f_resting_hr.value) if f_resting_hr.value else None
            dob = f_dob.value.strip() if f_dob.value else None
            if dob:
                from datetime import date
                date.fromisoformat(dob)  # validates format
        except ValueError:
            f_error.value = "Check DOB is YYYY-MM-DD and numbers are valid."
            f_error.visible = True
            page.update()
            return

        db.create_client(f_first.value, f_last.value, f_sex.value, dob, height, weight, resting_hr)
        for f in (f_first, f_last, f_sex, f_dob, f_height, f_weight, f_resting_hr):
            f.value = None
        f_error.visible = False
        close_dialog()
        refresh_client_list()

    add_client_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("New client"),
        content=ft.Column(
            [f_first, f_last, f_sex, f_dob, f_height, f_weight, f_resting_hr, f_error],
            tight=True, spacing=10, scroll=ft.ScrollMode.AUTO, height=420,
        ),
        actions=[ft.TextButton("Cancel", on_click=close_dialog), ft.FilledButton("Save", on_click=save_client)],
    )

    def open_add_client(e):
        page.show_dialog(add_client_dialog)

    clients_tab = ft.Column(
        [
            ft.Row([client_search, ft.FilledButton("New client", icon=ft.Icons.PERSON_ADD, on_click=open_add_client)],
                   alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ft.Divider(),
            client_list_view,
        ],
        expand=True,
    )

    # ==================================================================
    # SESSION VIEW
    # ==================================================================
    strap_status = ft.Text("Strap: not connected", color=ft.Colors.GREY_400)
    scan_btn = ft.FilledTonalButton("Scan for strap", icon=ft.Icons.BLUETOOTH_SEARCHING)
    elapsed_text = ft.Text("00:00", size=16, color=ft.Colors.GREY_400)
    start_btn = ft.FilledButton("Start session", icon=ft.Icons.PLAY_ARROW, disabled=True)
    stop_btn = ft.FilledButton("Stop", icon=ft.Icons.STOP, disabled=True, bgcolor=ft.Colors.RED_700)
    summary_session_title = ft.Text("No completed session", size=20, weight=ft.FontWeight.BOLD)
    summary_session_times = ft.Text("Start: --    End: --    Duration: --", color=ft.Colors.GREY_400)
    summary_participant_rows = ft.Column(spacing=4)
    summary_elite_rows = ft.Column(spacing=2)
    summary_empty_message = ft.Text("End a session to view its summary.", color=ft.Colors.GREY_400)
    resting_hr_fields = {}
    resting_hr_dialog_content = ft.Column(spacing=8, tight=True, scroll=ft.ScrollMode.AUTO, height=360)
    resting_hr_error = ft.Text(color=ft.Colors.RED_300, visible=False)
    zone_legend = ft.Row(
        [ft.Row([ft.Container(width=10, height=10, bgcolor=zones.ZONE_COLORS[i], border_radius=2),
                 ft.Text(zones.ZONE_NAMES[i], size=11)], spacing=4) for i in range(5)],
        spacing=14, wrap=True,
    )

    start_session_error = ft.Text(color=ft.Colors.RED_300, visible=False)
    empty_chart_message = ft.Text("No participants added", color=ft.Colors.GREY_400)

    # ---- session mode: normal or elite ----
    mode_picker = ft.Dropdown(
        label="Mode", width=180, value=elite.MODE_NORMAL,
        options=[
            ft.dropdown.Option(key=elite.MODE_NORMAL, text="Normal"),
            ft.dropdown.Option(key=elite.MODE_ELITE, text="Elite"),
        ],
    )
    elite_rounds_field = ft.TextField(label="Rounds", value="3", width=90, keyboard_type=ft.KeyboardType.NUMBER)
    elite_round_field = ft.TextField(label="Round length (m:ss)", value="3:00", width=160)
    elite_recovered_field = ft.TextField(
        label="Recovered at (% of HR reserve)", value="70", width=230, keyboard_type=ft.KeyboardType.NUMBER
    )
    elite_rest_field = ft.TextField(label="Fight rest (m:ss)", value="1:00", width=140)
    elite_settings_row = ft.Row(
        [elite_rounds_field, elite_round_field, elite_recovered_field, elite_rest_field],
        spacing=12, wrap=True, visible=False,
    )
    elite_hint = ft.Text(
        "Elite: work until zone 5 (red line) -> the round clock runs only while in zone 5 -> "
        "rest until the recovered level (teal line). Total time is compared with the real fight length "
        "(rounds + fight rests). Add everyone before starting.",
        size=11, color=ft.Colors.GREY_400, visible=False,
    )

    def on_mode_pick(e):
        state["mode"] = mode_picker.value or elite.MODE_NORMAL
        is_elite = state["mode"] == elite.MODE_ELITE
        elite_settings_row.visible = is_elite
        elite_hint.visible = is_elite
        page.update()

    mode_picker.on_select = on_mode_pick

    def make_participant_row(client_id, device):
        client = db.get_client(client_id)
        client_name = f"{client['first_name']} {client['last_name']}"
        age = db.client_age(client["dob"])
        max_hr = client["max_hr"] or zones.estimate_max_hr(age)
        name_control = ft.Text(client_name, width=150, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        hr_control = ft.Text("-- bpm", width=64, text_align=ft.TextAlign.RIGHT)
        bar_fill = ft.Container(left=0, top=5, width=0, height=24, bgcolor=zones.ZONE_COLORS[0], border_radius=3)
        marker = ft.Column(
            [ft.Container(width=2, height=4, bgcolor=ft.Colors.WHITE) for _ in range(6)],
            spacing=2,
            tight=True,
        )
        marker_overlay = ft.Container(
            left=CHART_WIDTH * chart_marker_fraction(), top=0, width=2, height=34,
            content=marker,
        )
        value_control = ft.Text("", size=11, color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD)
        bar_track = ft.Container(left=0, top=5, width=CHART_WIDTH, height=24, bgcolor="#30343b", border_radius=3)
        value_holder = ft.Container(left=0, top=7, width=CHART_WIDTH, height=20, content=value_control)
        bar = ft.Stack(
            controls=[bar_track, bar_fill, marker_overlay, value_holder],
            width=CHART_WIDTH,
            height=34,
        )
        row = ft.Row([name_control, bar, hr_control], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        phase_control = ft.Text("", size=13, weight=ft.FontWeight.BOLD, visible=False)
        row_with_phase = ft.Column([row, phase_control], spacing=2)
        return {
            "client_id": client_id,
            "device_address": device["address"],
            "device_name": device["name"],
            "name": client_name,
            "max_hr": max_hr,
            "thresholds": load_client_thresholds(client_id),
            "last_sample_time": None,
            "max_streak": {},
            "bar_fill": bar_fill,
            "bar_track": bar_track,
            "marker": marker,
            "marker_overlay": marker_overlay,
            "value_holder": value_holder,
            "name_control": name_control,
            "elite_markers": [],          # (control, bpm) lines drawn on the bar in elite mode
            "bar_width": CHART_WIDTH,
            "value_control": value_control,
            "hr_control": hr_control,
            "row": row_with_phase,
            "phase_control": phase_control,
            "bar_stack": bar,
            "engine": None,
            "stream": None,
        }

    def size_participant(participant, width, height, big):
        """Resizes one client's bar row: small for setup, large for the live full-screen view."""
        track_h = max(10, height - 10)
        participant["bar_width"] = width
        participant["bar_track"].width = width
        participant["bar_track"].height = track_h
        participant["bar_fill"].height = track_h
        participant["marker_overlay"].left = width * chart_marker_fraction()
        participant["marker_overlay"].height = height
        participant["marker"].controls = [
            ft.Container(width=2, height=4, bgcolor=ft.Colors.WHITE) for _ in range(max(6, height // 6))
        ]
        holder_h = min(40, track_h)
        participant["value_holder"].width = width
        participant["value_holder"].height = holder_h
        participant["value_holder"].top = 5 + (track_h - holder_h) / 2
        participant["value_control"].size = 20 if big else 11
        participant["bar_stack"].width = width
        participant["bar_stack"].height = height
        for control, bpm in participant["elite_markers"]:
            control.left = width * chart_fill_fraction(int(bpm), participant["max_hr"])
            control.height = height
        participant["name_control"].width = 230 if big else 150
        participant["name_control"].size = 26 if big else None
        participant["hr_control"].width = 190 if big else 64
        participant["hr_control"].size = 44 if big else None
        participant["phase_control"].size = 20 if big else 13

    def live_dimensions():
        width = getattr(page, "width", None) or 1280
        height = getattr(page, "height", None) or 800
        count = max(1, len(state["participants"]))
        bar_w = int(max(300, width - 230 - 190 - 2 * 12 - 48))      # name + heart rate columns, gaps, margins
        room = (height - 110) / count                               # leave space for the button row
        bar_h = int(max(60, min(240, room - (56 if state["mode"] == elite.MODE_ELITE else 24))))
        return bar_w, bar_h

    def apply_live_layout():
        bar_w, bar_h = live_dimensions()
        for participant in state["participants"].values():
            size_participant(participant, bar_w, bar_h, big=True)

    def apply_setup_layout():
        for participant in state["participants"].values():
            size_participant(participant, CHART_WIDTH, 34, big=False)

    def refresh_participant_chart():
        participant_chart.controls = [participant["row"] for participant in state["participants"].values()]
        participant_chart.visible = bool(state["participants"])
        empty_chart_message.visible = not state["participants"]

    def wall_iso(t_mono):
        """Converts a monotonic timestamp to a UTC ISO time anchored to the session's start."""
        start = state["session_start_wall"]
        if start is None:
            return datetime.utcnow().isoformat()
        return (start + timedelta(seconds=t_mono - state["session_start"])).isoformat()

    def persist_elite(client_id, participant, events):
        """After any phase change, store the events and the latest state of every round."""
        if not events or not state["session_id"]:
            return
        for event in events:
            db.save_phase_event(state["session_id"], client_id, event.round_no, event.phase, wall_iso(event.t))
        for r in participant["engine"].results:
            db.save_elite_round(
                state["session_id"], client_id, r.round_no,
                wall_iso(r.started_t), wall_iso(r.ended_t) if r.ended_t is not None else None,
                round(r.prework_s, 1), round(r.work_s, 1), round(r.paused_s, 1),
                None if r.recovery_s is None else round(r.recovery_s, 1),
                r.hr_end_work, r.hr_60s, r.flags,
            )

    def render_elite_status(participant):
        engine = participant["engine"]
        snap = engine.snapshot(clock())
        params = engine.p
        control = participant["phase_control"]
        label = f"Round {snap['round_no']}/{snap['rounds']}"
        if snap["phase"] == elite.PREWORK:
            control.value = f"PRE-WORK  {label}  get above {params.z5_low:.0f} bpm"
            control.color = zones.ZONE_COLORS[3]
        elif snap["phase"] == elite.WORK:
            left = elite.format_clock(snap["work_remaining_s"])
            if snap["running"]:
                control.value = f"WORK  {label}  {left} left"
                control.color = zones.ZONE_COLORS[4]
            else:
                control.value = f"PAUSED  {label}  {left} left  get back above {params.z5_low:.0f} bpm"
                control.color = zones.ZONE_COLORS[3]
        elif snap["phase"] == elite.RECOVERY:
            control.value = f"RECOVERY  {label}  rest to {params.recovered_bpm:.0f} bpm or below"
            control.color = zones.ZONE_COLORS[1]
        else:
            target = (state["elite"] or {}).get("target_total_s")
            control.value = (
                f"DONE  total {elite.format_clock(snap['total_elapsed_s'])}"
                f"  vs fight {elite.format_clock(target)}"
            )
            control.color = zones.ZONE_COLORS[2]
        control.visible = True

    def handle_elite_sample(client_id, participant, hr, now):
        events = participant["engine"].update(now, hr)
        persist_elite(client_id, participant, events)
        render_elite_status(participant)
        if events and all(p["engine"].done for p in state["participants"].values() if p.get("engine")):
            elapsed_text.value = "All rounds complete. Press End session to save."

    def apply_participant_sample(client_id, hr: int, rr_intervals: list[float]):
        participant = state["participants"].get(client_id)
        if not participant:
            return
        recording = bool(state["session_id"]) and not state["paused"]
        if recording:
            confirmed_max = zones.track_new_max(participant["max_streak"], hr, participant["max_hr"])
            if confirmed_max:
                participant["max_hr"] = db.update_client_max_hr(client_id, confirmed_max) or confirmed_max
                participant["thresholds"] = load_client_thresholds(client_id)
        zone_index = zones.zone_for_hr(hr, participant["thresholds"])
        bar_w = participant["bar_width"]
        participant["bar_fill"].width = bar_w * chart_fill_fraction(hr, participant["max_hr"])
        participant["bar_fill"].bgcolor = zones.ZONE_COLORS[zone_index]
        participant["hr_control"].value = f"{hr} bpm"
        percent = hr / participant["max_hr"] * 100 if participant["max_hr"] else 0
        participant["value_control"].value = f"{percent:.0f}%"
        participant["value_control"].text_align = ft.TextAlign.RIGHT
        participant["value_control"].width = max(42, min(bar_w, bar_w * chart_fill_fraction(hr, participant["max_hr"])))

        now = clock()
        if recording:
            db.log_sample(
                state["session_id"], hr, rr_to_json(rr_intervals),
                client_id=client_id, strap_device_id=participant["device_address"],
            )
            participant["last_sample_time"] = now
            started = state["session_start"]
            secs = int(now - started) if started else 0
            elapsed_text.value = f"{secs // 60:02d}:{secs % 60:02d}"
            if participant.get("engine"):
                handle_elite_sample(client_id, participant, hr, now)
        page.update()

    def on_hr_sample(client_id, hr: int, rr_intervals: list[float]):
        apply_participant_sample(client_id, hr, rr_intervals)

    async def do_scan(e):
        scan_btn.disabled = True
        strap_status.value = "Scanning..."
        page.update()
        found = await scan_for_straps()
        scan_btn.disabled = False
        if not found:
            strap_status.value = "No strap found. Make sure it's worn (skin contact wakes it) and try again."
            page.update()
            return

        state["available_devices"] = [
            {"name": device.name, "address": device.address}
            for device in found
        ]
        refresh_device_list()
        strap_status.value = f"Found {len(found)} device(s). Select a client and device to add them."
        page.update()

    async def attach_selected_device(e):
        if state["session_id"] and state["mode"] == elite.MODE_ELITE:
            strap_status.value = "Add everyone before starting an elite session."
            page.update()
            return
        if not state["selected_client_id"]:
            strap_status.value = "Choose a client before attaching a strap."
            page.update()
            return
        if not state["selected_device_address"]:
            strap_status.value = "Choose a device before attaching it."
            page.update()
            return

        device = next((d for d in state["available_devices"] if d["address"] == state["selected_device_address"]), None)
        if not device:
            strap_status.value = "That device is no longer available. Scan again."
            page.update()
            return

        client_id = state["selected_client_id"]
        if client_id in state["participants"]:
            strap_status.value = "That client is already in this session."
            page.update()
            return
        if any(p["device_address"] == device["address"] for p in state["participants"].values()):
            strap_status.value = "That device is already attached to a client."
            page.update()
            return

        strap_status.value = f"Connecting to {device['name'] or device['address']}..."
        page.update()

        participant = make_participant_row(client_id, device)
        stream = HeartRateStream(
            device["address"],
            lambda hr, rr, cid=client_id: on_hr_sample(cid, hr, rr),
        )
        try:
            await stream.connect()
        except Exception as exc:
            strap_status.value = f"Could not connect to {device['name'] or device['address']}: {exc}"
            page.update()
            return

        participant["stream"] = stream
        state["participants"][client_id] = participant
        state["client_device_bindings"][device["address"]] = client_id
        db.add_client_device_binding(client_id, device["address"], device["name"])
        if state["session_id"]:
            db.add_session_participant(state["session_id"], client_id, device["address"])
        strap_status.value = f"Added {participant['name']} with {device['name'] or device['address']}"
        refresh_participant_chart()
        refresh_session_controls()
        page.update()

    scan_btn.on_click = do_scan
    attach_btn.on_click = attach_selected_device

    def on_client_pick(e):
        set_selected_client(e.control.value)

    client_picker.on_select = on_client_pick

    def on_device_pick(e):
        set_selected_device(e.control.value)

    device_picker.on_select = on_device_pick

    def launch_session(mode, settings):
        """Creates the session and its engines, then switches to the full-screen live view."""
        state["paused"] = False
        state["pause_started"] = None
        state["paused_total"] = 0.0
        now = clock()
        mode_params = None
        if settings:
            # Zone 5 and "recovered" are frozen for the whole session, so the numbers cannot move mid-session.
            frozen = {}
            for participant in state["participants"].values():
                client = db.get_client(participant["client_id"])
                thresholds = list(participant["thresholds"])
                params = elite.params_for_client(thresholds, client["resting_hr"], settings)
                participant["engine"] = elite.EliteEngine(params, now)
                frozen[participant["client_id"]] = {
                    "z5_low": params.z5_low,
                    "recovered_bpm": round(params.recovered_bpm, 1),
                    "max_hr": thresholds[-1],
                    "resting_hr": client["resting_hr"],
                }
            mode_params = {**settings, "clients": frozen}
        first_participant = next(iter(state["participants"].values()))
        state["session_id"] = db.start_session(
            first_participant["client_id"], first_participant["device_address"],
            mode=mode, mode_params=mode_params,
        )
        state["session_start_wall"] = datetime.fromisoformat(
            db.get_session(state["session_id"])["started_at"]
        )
        for participant in state["participants"].values():
            db.add_session_participant(
                state["session_id"], participant["client_id"], participant["device_address"]
            )
        state["session_start"] = now
        state["elite"] = settings
        for participant in state["participants"].values():
            for control, _ in participant["elite_markers"]:          # clear lines left by a restarted session
                if control in participant["bar_stack"].controls:
                    participant["bar_stack"].controls.remove(control)
            participant["elite_markers"] = []
            engine = participant.get("engine")
            if engine:
                for bpm, colour in ((engine.p.z5_low, zones.ZONE_COLORS[4]), (engine.p.recovered_bpm, zones.ZONE_COLORS[1])):
                    line = ft.Container(top=0, width=2, height=34, bgcolor=colour)
                    participant["bar_stack"].controls.append(line)
                    participant["elite_markers"].append((line, bpm))
                persist_elite(participant["client_id"], participant, engine.pop_events() or [])
                render_elite_status(participant)
        start_session_error.visible = False
        pause_btn.content = "Pause"
        pause_btn.icon = ft.Icons.PAUSE
        live_status.value = ""
        elapsed_text.value = "00:00"
        refresh_session_controls()
        set_live_view(True)
        page.update()

    def start_session(e):
        if not state["participants"]:
            start_session_error.value = "Add at least one client and device before starting."
            start_session_error.visible = True
            page.update()
            return
        mode = state["mode"]
        settings = None
        if mode == elite.MODE_ELITE:
            settings, error = elite.parse_settings(
                elite_rounds_field.value, elite_round_field.value,
                elite_recovered_field.value, elite_rest_field.value,
            )
            if error:
                start_session_error.value = error
                start_session_error.visible = True
                page.update()
                return
        launch_session(mode, settings)

    def toggle_pause(e):
        """Pause freezes recording and every clock; the bars keep showing live heart rate."""
        if not state["session_id"]:
            return
        now = time.monotonic()
        if not state["paused"]:
            state["paused"] = True
            state["pause_started"] = now
            pause_btn.content = "Resume"
            pause_btn.icon = ft.Icons.PLAY_ARROW
            live_status.value = "PAUSED - not recording"
        else:
            state["paused_total"] += now - state["pause_started"]
            state["paused"] = False
            state["pause_started"] = None
            pause_btn.content = "Pause"
            pause_btn.icon = ft.Icons.PAUSE
            live_status.value = ""
        page.update()

    def discard_and_restart(e=None):
        page.pop_dialog()
        old_session = state["session_id"]
        if not old_session:
            return
        db.delete_session(old_session)
        state["session_id"] = None
        for participant in state["participants"].values():
            participant["engine"] = None
            participant["max_streak"] = {}
            participant["last_sample_time"] = None
            participant["bar_fill"].width = 0
            participant["hr_control"].value = "-- bpm"
            participant["value_control"].value = ""
            participant["phase_control"].value = ""
            participant["phase_control"].visible = False
        launch_session(state["mode"], state["elite"])

    restart_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Restart this session?"),
        content=ft.Text(
            "Everything recorded so far will be discarded, and a new session starts straight away "
            "with the same clients and settings."
        ),
        actions=[
            ft.TextButton("Cancel", on_click=lambda e: page.pop_dialog()),
            ft.FilledButton("Restart", on_click=discard_and_restart),
        ],
    )

    def confirm_restart(e):
        if state["session_id"]:
            page.show_dialog(restart_dialog)

    def populate_elite_summary(session, session_id):
        params = db.session_mode_params(session)
        target = params.get("target_total_s")
        summary_elite_rows.controls.append(ft.Divider())
        summary_elite_rows.controls.append(ft.Text(
            f"Elite session: {params.get('rounds')} x {elite.format_clock(params.get('round_s'))} rounds, "
            f"fight length {elite.format_clock(target)} (rests {elite.format_clock(params.get('target_rest_s'))})",
            weight=ft.FontWeight.BOLD,
        ))
        for member in db.list_session_participants(session_id):
            client = db.get_client(member["client_id"])
            events = [dict(ev) for ev in db.list_phase_events(session_id, member["client_id"])]
            total = elite.total_seconds_from_events(events)
            summary_elite_rows.controls.append(ft.Text(
                f"{client['first_name']} {client['last_name']}: total {elite.format_clock(total)} "
                f"(fight {elite.format_clock(target)})",
                weight=ft.FontWeight.BOLD, size=14,
            ))
            for r in db.list_elite_rounds(session_id, member["client_id"]):
                flags = json.loads(r["flags"] or "[]")
                drop = (r["hr_end_work"] - r["hr_60s"]) if r["hr_end_work"] is not None and r["hr_60s"] is not None else None
                summary_elite_rows.controls.append(ft.Text(
                    f"R{r['round_no']}  pre-work {elite.format_clock(r['prework_s'])}  "
                    f"work {elite.format_clock(r['work_s'])}  paused {elite.format_clock(r['paused_s'])}  "
                    f"recovery {elite.format_clock(r['recovery_s'])}  "
                    f"HR drop at 60s {'--' if drop is None else str(drop) + ' bpm'}"
                    f"{'  [' + ', '.join(flags) + ']' if flags else ''}",
                    size=12, color=ft.Colors.GREY_300,
                ))

    def refresh_summary_screen(session_id):
        session, participant_summaries = db.session_summary(session_id)
        summary_participant_rows.controls.clear()
        summary_elite_rows.controls.clear()
        if not session:
            summary_session_title.value = "No completed session"
            summary_session_times.value = "Start: --    End: --    Duration: --"
            summary_empty_message.visible = True
            return

        started_at = datetime.fromisoformat(session["started_at"])
        ended_at = datetime.fromisoformat(session["ended_at"]) if session["ended_at"] else None
        paused_seconds = (session["paused_s"] or 0) if "paused_s" in session.keys() else 0
        duration_seconds = max(0, (ended_at - started_at).total_seconds() - paused_seconds) if ended_at else 0
        summary_session_title.value = "Session Summary"
        summary_session_times.value = (
            f"Start: {format_session_timestamp(session['started_at'])}    "
            f"End: {format_session_timestamp(session['ended_at'])}    "
            f"Duration: {format_session_duration(duration_seconds)}"
        )
        summary_empty_message.visible = not participant_summaries
        for participant in participant_summaries:
            age = db.client_age(participant["dob"])
            calories = estimate_calories(
                participant["sex"], age, participant["weight_kg"],
                participant["average_hr"], duration_seconds,
            )
            summary_participant_rows.controls.append(
                ft.Row(
                    [
                        ft.Text(f"{participant['first_name']} {participant['last_name']}", width=220),
                        ft.Text(f"{participant['max_hr']} bpm" if participant["max_hr"] is not None else "--", width=100),
                        ft.Text(f"{participant['average_hr']:.1f} bpm" if participant["average_hr"] is not None else "--", width=120),
                        ft.Text(f"{calories:.1f} kcal" if calories is not None else "Unavailable", width=180),
                    ],
                    spacing=12,
                )
            )
        summary_empty_message.value = "No participant heart-rate samples were recorded." if participant_summaries else "No participant data recorded."
        if session["mode"] == elite.MODE_ELITE:
            populate_elite_summary(session, session_id)

    def finish_resting_hr_entry(e=None):
        page.pop_dialog()
        tabs.selected_index = 2
        page.update()

    def save_resting_hr_entries(e):
        updates = []
        try:
            for client_id, field in resting_hr_fields.items():
                raw_value = (field.value or "").strip()
                if not raw_value:
                    continue
                resting_hr = int(raw_value)
                if not 30 <= resting_hr <= 220:
                    raise ValueError
                updates.append((client_id, resting_hr))
        except ValueError:
            resting_hr_error.value = "Enter a resting heart rate from 30 to 220 bpm, or leave it blank."
            resting_hr_error.visible = True
            page.update()
            return

        for client_id, resting_hr in updates:
            db.update_client_resting_hr(client_id, resting_hr)
        refresh_client_list()
        finish_resting_hr_entry()

    resting_hr_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Record resting heart rate"),
        content=ft.Column(
            [
                ft.Text("Enter any resting HR values provided. Leave others blank to keep their current values."),
                resting_hr_dialog_content,
                resting_hr_error,
            ],
            tight=True,
            spacing=10,
        ),
        actions=[
            ft.TextButton("Skip", on_click=finish_resting_hr_entry),
            ft.FilledButton("Save", on_click=save_resting_hr_entries),
        ],
    )

    def prompt_for_resting_hr(client_ids):
        resting_hr_fields.clear()
        resting_hr_dialog_content.controls.clear()
        resting_hr_error.visible = False
        for client_id in client_ids:
            client = db.get_client(client_id)
            if not client:
                continue
            client_name = f"{client['first_name']} {client['last_name']}"
            field = ft.TextField(
                label=f"{client_name} resting HR (bpm)",
                value=str(client["resting_hr"]) if client["resting_hr"] else "",
                keyboard_type=ft.KeyboardType.NUMBER,
                width=320,
            )
            resting_hr_fields[client_id] = field
            resting_hr_dialog_content.controls.append(field)
        page.show_dialog(resting_hr_dialog)

    async def stop_session(e):
        completed_client_ids = list(state["participants"])
        completed_session_id = None
        if state["session_id"]:
            completed_session_id = state["session_id"]
            stop_now = clock()
            paused_s = state["paused_total"]
            if state["paused"] and state["pause_started"] is not None:
                paused_s += time.monotonic() - state["pause_started"]
            for cid, participant in state["participants"].items():
                engine = participant.get("engine")
                if engine and not engine.done:
                    persist_elite(cid, participant, engine.finish(stop_now))
            db.end_session(completed_session_id, paused_s=paused_s)
            state["last_session_id"] = completed_session_id
        state["paused"] = False
        state["pause_started"] = None
        state["paused_total"] = 0.0
        state["session_id"] = None
        state["session_start"] = None
        for participant in state["participants"].values():
            stream = participant["stream"]
            if stream:
                await stream.disconnect()
        state["participants"].clear()
        state["available_devices"].clear()
        state["selected_client_id"] = None
        state["selected_device_address"] = None
        client_picker.value = None
        device_picker.value = None
        refresh_participant_chart()
        refresh_session_controls()
        set_live_view(False)                       # the Session tab is back to a fresh setup screen
        if completed_session_id:
            show_session_in_summary(completed_session_id)
            tabs.selected_index = 2                # straight to the summary
            if completed_client_ids:
                prompt_for_resting_hr(completed_client_ids)
                return
        page.update()

    start_btn.on_click = start_session
    stop_btn.on_click = stop_session

    # ---- live controls (shown instead of the setup screen while a session runs) ----
    pause_btn = ft.FilledTonalButton("Pause", icon=ft.Icons.PAUSE, on_click=toggle_pause)
    restart_btn = ft.OutlinedButton("Restart", icon=ft.Icons.RESTART_ALT, on_click=confirm_restart)
    live_status = ft.Text("", size=18, weight=ft.FontWeight.BOLD, color=zones.ZONE_COLORS[3])
    live_controls = ft.Row(
        [pause_btn, stop_btn, restart_btn, elapsed_text, live_status],
        alignment=ft.MainAxisAlignment.CENTER, spacing=20, visible=False, key="live-controls",
    )
    setup_buttons_row = ft.Row([start_btn], alignment=ft.MainAxisAlignment.CENTER, key="setup-buttons")

    session_setup_group = ft.Column(
        [
            ft.Row([mode_picker]),
            elite_settings_row,
            elite_hint,
            ft.Text("Add participants", weight=ft.FontWeight.BOLD),
            ft.Row([client_picker, device_picker], alignment=ft.MainAxisAlignment.START),
            ft.Row([scan_btn], alignment=ft.MainAxisAlignment.START),
            ft.Row([attach_btn], alignment=ft.MainAxisAlignment.START),
            ft.Row([strap_status], alignment=ft.MainAxisAlignment.START),
            start_session_error,
        ],
        key="session-setup",
    )
    chart_header_group = ft.Column(
        [
            ft.Divider(),
            ft.Row([ft.Text("Client", width=150), ft.Text("Heart rate vs. max HR"), ft.Text("Heart rate")], spacing=12),
            ft.Stack(
                controls=[
                    ft.Text("0%", left=162, top=0, size=10),
                    ft.Text("50%", left=162 + CHART_WIDTH * (0.5 / CHART_MAX_PERCENT) - 12, top=0, size=10),
                    ft.Text("100%", left=162 + CHART_WIDTH * chart_marker_fraction() - 16, top=0, size=10),
                    ft.Text("115%", left=162 + CHART_WIDTH - 28, top=0, size=10),
                ],
                width=CHART_WIDTH + 216,
                height=18,
            ),
            ft.Text("The dotted line marks 100% of estimated max HR. Bars may extend to 115%.", size=11, color=ft.Colors.GREY_400),
        ],
        key="chart-header",
    )

    session_tab = ft.Column(
        [
            session_setup_group,
            chart_header_group,
            participant_chart,
            empty_chart_message,
            setup_buttons_row,
            live_controls,
            zone_legend,
        ],
        expand=True,
    )
    session_container = ft.Container(session_tab, padding=20)

    def set_live_view(is_live):
        """Live: only the bars and Pause / Stop / Restart. Not live: the normal setup screen."""
        for group in (session_setup_group, chart_header_group, setup_buttons_row, zone_legend):
            group.visible = not is_live
        live_controls.visible = is_live
        tab_bar.visible = not is_live
        session_container.padding = 8 if is_live else 20
        elapsed_text.size = 28 if is_live else 16
        if is_live:
            apply_live_layout()
        else:
            apply_setup_layout()

    def on_page_resize(e):
        if state["session_id"]:
            apply_live_layout()
            page.update()

    page.on_resize = on_page_resize

    export_status = ft.Text("", size=12, color=ft.Colors.GREY_400)

    async def export_last_session(e):
        session_id = state["summary_session_id"] or state["last_session_id"]
        if not session_id:
            return
        export_btn.disabled = True
        export_status.value = "Uploading..."
        export_status.color = ft.Colors.GREY_400
        page.update()
        try:
            import export as export_module
            keys = await asyncio.to_thread(export_module.upload_session, session_id)
            export_status.value = f"Uploaded {len(keys)} client record(s)."
            export_status.color = ft.Colors.GREEN_400
        except Exception as ex:
            export_status.value = f"Upload failed: {ex}"
            export_status.color = ft.Colors.RED_300
            export_btn.disabled = False
        page.update()

    export_btn = ft.OutlinedButton(
        "Export to cloud", icon=ft.Icons.CLOUD_UPLOAD, disabled=True, on_click=export_last_session
    )

    summary_detail = ft.Column(
        [
            summary_session_title,
            summary_session_times,
            ft.Divider(),
            ft.Row(
                [
                    ft.Text("Client", width=220, weight=ft.FontWeight.BOLD),
                    ft.Text("Max HR", width=100, weight=ft.FontWeight.BOLD),
                    ft.Text("Average HR", width=120, weight=ft.FontWeight.BOLD),
                    ft.Text("Estimated calories", width=180, weight=ft.FontWeight.BOLD),
                ],
                spacing=12,
            ),
            summary_participant_rows,
            summary_elite_rows,
            summary_empty_message,
            ft.Row([export_btn, export_status], spacing=12),
            ft.Text(
                "Calories are estimates based on average heart rate, session duration, age, sex, and weight.",
                size=11,
                color=ft.Colors.GREY_400,
            ),
        ],
        expand=True,
        scroll=ft.ScrollMode.AUTO,
    )

    # ---- previous sessions (newest first); click one to look back at it ----
    session_history_list = ft.ListView(spacing=2, expand=True)

    def refresh_session_history():
        tiles = []
        for row in db.list_sessions():
            ended = datetime.fromisoformat(row["ended_at"])
            started = datetime.fromisoformat(row["started_at"])
            length = max(0, (ended - started).total_seconds() - (row["paused_s"] or 0))
            mode_label = "Elite" if row["mode"] == elite.MODE_ELITE else "Normal"
            tiles.append(ft.ListTile(
                title=ft.Text(format_session_timestamp(row["started_at"]), size=14),
                subtitle=ft.Text(
                    f"{mode_label} - {row['names'] or 'no participants'} - {format_session_duration(length)}",
                    size=12, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
                ),
                dense=True,
                selected=row["id"] == state["summary_session_id"],
                on_click=lambda e, sid=row["id"]: show_session_in_summary(sid),
            ))
        session_history_list.controls = tiles or [ft.Text("No sessions yet.", color=ft.Colors.GREY_400)]

    def show_session_in_summary(session_id):
        state["summary_session_id"] = session_id
        refresh_summary_screen(session_id)
        export_btn.disabled = False
        export_status.value = ""
        refresh_session_history()
        page.update()

    summary_tab = ft.Row(
        [
            ft.Container(
                ft.Column([ft.Text("Previous sessions", weight=ft.FontWeight.BOLD), session_history_list], expand=True),
                width=340,
            ),
            ft.VerticalDivider(width=1),
            summary_detail,
        ],
        expand=True,
        vertical_alignment=ft.CrossAxisAlignment.START,
    )

    # ==================================================================
    # LAYOUT
    # ==================================================================
    tab_bar = ft.TabBar(
        tabs=[
            ft.Tab(label="Clients", icon=ft.Icons.PEOPLE),
            ft.Tab(label="Session", icon=ft.Icons.FAVORITE),
            ft.Tab(label="Summary", icon=ft.Icons.QUERY_STATS),
        ]
    )
    tabs = ft.Tabs(
        length=3,
        selected_index=0,
        expand=True,
        content=ft.Column(
            expand=True,
            controls=[
                tab_bar,
                ft.TabBarView(
                    expand=True,
                    controls=[
                        ft.Container(clients_tab, padding=20),
                        session_container,
                        ft.Container(summary_tab, padding=20),
                    ],
                ),
            ],
        ),
    )
    page.add(tabs)
    refresh_client_list()
    refresh_session_controls()
    latest_sessions = db.list_sessions(1)
    if latest_sessions:
        show_session_in_summary(latest_sessions[0]["id"])      # open on the most recent session
    else:
        refresh_session_history()


if __name__ == "__main__":
    configure_ssl_environment()
    ft.run(main)
