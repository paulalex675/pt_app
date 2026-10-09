"""PT Studio — desktop app for live heart-rate coaching sessions.

Run with: python main.py
Requires a Polar H10 (or other standard BLE HRM) paired/discoverable, and
Bluetooth permission granted to the terminal/IDE running this (macOS will
prompt on first connect).
"""
import asyncio
import os
import time
from datetime import datetime, timedelta

try:
    import certifi
except ImportError:  # pragma: no cover - dependency is normally installed with the app
    certifi = None


def configure_ssl_environment():
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return os.environ.get("SSL_CERT_FILE")

    candidate_paths = []
    if certifi is not None:
        candidate_paths.append(certifi.where())
    candidate_paths.extend(
        [
            "/etc/ssl/cert.pem",
            "/System/Library/OpenSSL/cert.pem",
            "/Library/Frameworks/Python.framework/Versions/3.13/etc/openssl/cert.pem",
        ]
    )

    for cert_path in candidate_paths:
        if cert_path and os.path.exists(cert_path):
            os.environ["SSL_CERT_FILE"] = cert_path
            return cert_path

    return None


configure_ssl_environment()

import flet as ft

import db
import elite
import zones
from ble_client import HeartRateStream, rr_to_json, scan_for_straps


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
        "session_wall_start": None,
        "client_device_bindings": {},
        "available_devices": [],
    }

    def refresh_session_controls():
        has_active_session = bool(state["session_id"])
        start_btn.disabled = not (state["participants"] and not has_active_session)
        stop_btn.disabled = not has_active_session
        mode_picker.disabled = has_active_session
        elite_fields_disabled = has_active_session or mode_picker.value != elite.MODE_ELITE
        for field in (
            elite_rounds_field,
            elite_round_length_field,
            elite_recovered_field,
            elite_rest_field,
        ):
            field.disabled = elite_fields_disabled
        client_picker.disabled = False
        device_picker.disabled = False
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
    stop_btn = ft.FilledButton("End session", icon=ft.Icons.STOP, disabled=True, bgcolor=ft.Colors.RED_700)
    mode_picker = ft.Dropdown(
        label="Mode",
        width=220,
        value=elite.MODE_NORMAL,
        options=[
            ft.dropdown.Option(key=elite.MODE_NORMAL, text="Normal"),
            ft.dropdown.Option(key=elite.MODE_ELITE, text="Elite"),
        ],
    )
    elite_rounds_field = ft.TextField(label="Rounds", value="5", width=120)
    elite_round_length_field = ft.TextField(label="Round length (m:ss)", value="3:00", width=180)
    elite_recovered_field = ft.TextField(
        label="Recovered at (% of HR reserve)", value="70", width=250
    )
    elite_rest_field = ft.TextField(label="Fight rest (m:ss)", value="1:00", width=180)
    elite_settings = ft.Row(
        [elite_rounds_field, elite_round_length_field, elite_recovered_field, elite_rest_field],
        wrap=True,
        visible=False,
    )
    elite_status = ft.Column(spacing=4)
    summary_session_title = ft.Text("No completed session", size=20, weight=ft.FontWeight.BOLD)
    summary_session_times = ft.Text("Start: --    End: --    Duration: --", color=ft.Colors.GREY_400)
    summary_participant_rows = ft.Column(spacing=4)
    summary_elite_details = ft.Column(spacing=4)
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
        bar = ft.Stack(
            controls=[
                ft.Container(left=0, top=5, width=CHART_WIDTH, height=24, bgcolor="#30343b", border_radius=3),
                bar_fill,
                marker_overlay,
                ft.Container(left=0, top=7, width=CHART_WIDTH, height=20, content=value_control),
            ],
            width=CHART_WIDTH,
            height=34,
        )
        row = ft.Row([name_control, bar, hr_control], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        return {
            "client_id": client_id,
            "device_address": device["address"],
            "device_name": device["name"],
            "name": client_name,
            "max_hr": max_hr,
            "thresholds": load_client_thresholds(client_id),
            "last_sample_time": None,
            "max_streak": {},
            "elite_engine": None,
            "elite_saved_rounds": 0,
            "bar_fill": bar_fill,
            "value_control": value_control,
            "hr_control": hr_control,
            "row": row,
            "stream": None,
        }

    def refresh_participant_chart():
        participant_chart.controls = [participant["row"] for participant in state["participants"].values()]
        participant_chart.visible = bool(state["participants"])
        empty_chart_message.visible = not state["participants"]

    def apply_participant_sample(client_id, hr: int, rr_intervals: list[float]):
        participant = state["participants"].get(client_id)
        if not participant:
            return
        if state["session_id"]:
            confirmed_max = zones.track_new_max(participant["max_streak"], hr, participant["max_hr"])
            if confirmed_max:
                participant["max_hr"] = db.update_client_max_hr(client_id, confirmed_max) or confirmed_max
                participant["thresholds"] = load_client_thresholds(client_id)
        zone_index = zones.zone_for_hr(hr, participant["thresholds"])
        participant["bar_fill"].width = CHART_WIDTH * chart_fill_fraction(hr, participant["max_hr"])
        participant["bar_fill"].bgcolor = zones.ZONE_COLORS[zone_index]
        participant["hr_control"].value = f"{hr} bpm"
        percent = hr / participant["max_hr"] * 100 if participant["max_hr"] else 0
        participant["value_control"].value = f"{percent:.0f}%"
        participant["value_control"].text_align = ft.TextAlign.RIGHT
        participant["value_control"].width = max(42, min(CHART_WIDTH, CHART_WIDTH * chart_fill_fraction(hr, participant["max_hr"])))

        now = time.monotonic()
        if state["session_id"]:
            db.log_sample(
                state["session_id"], hr, rr_to_json(rr_intervals),
                client_id=client_id, strap_device_id=participant["device_address"],
            )
            engine = participant["elite_engine"]
            if engine is not None:
                events = engine.update(now, hr)
                _record_elite_updates(participant, events)
            participant["last_sample_time"] = now
            started = state["session_start"]
            secs = int(now - started) if started else 0
            elapsed_text.value = f"{secs // 60:02d}:{secs % 60:02d}"
            _refresh_elite_status()
        page.update()

    def _elite_timestamp(monotonic_time):
        offset = monotonic_time - state["session_start"]
        return (state["session_wall_start"] + timedelta(seconds=offset)).isoformat()

    def _record_elite_updates(participant, events):
        session_id = state["session_id"]
        if not session_id:
            return
        client_id = participant["client_id"]
        engine = participant["elite_engine"]
        for event in events:
            db.save_phase_event(
                session_id, client_id, event.round_no, event.phase, _elite_timestamp(event.t)
            )
        while participant["elite_saved_rounds"] < len(engine.results):
            result = engine.results[participant["elite_saved_rounds"]]
            db.save_elite_round(
                session_id,
                client_id,
                result.round_no,
                _elite_timestamp(result.started_t),
                _elite_timestamp(result.ended_t) if result.ended_t is not None else None,
                result.prework_s,
                result.work_s,
                result.paused_s,
                result.recovery_s,
                result.hr_end_work,
                result.hr_60s,
                result.flags,
            )
            participant["elite_saved_rounds"] += 1
        if engine.results:
            result = engine.results[-1]
            db.save_elite_round(
                session_id,
                client_id,
                result.round_no,
                _elite_timestamp(result.started_t),
                _elite_timestamp(result.ended_t) if result.ended_t is not None else None,
                result.prework_s,
                result.work_s,
                result.paused_s,
                result.recovery_s,
                result.hr_end_work,
                result.hr_60s,
                result.flags,
            )

    def _refresh_elite_status():
        elite_status.controls.clear()
        if not state["session_id"] or mode_picker.value != elite.MODE_ELITE:
            return
        for participant in state["participants"].values():
            engine = participant["elite_engine"]
            if engine is None:
                continue
            snapshot = engine.snapshot(time.monotonic())
            if engine.done:
                text = f"DONE — {participant['name']}"
            else:
                phase = snapshot["phase"].replace("prework", "pre-work")
                text = (
                    f"{participant['name']}: Round {snapshot['round_no']}/{snapshot['rounds']} "
                    f"{phase} — {elite.format_clock(snapshot['work_remaining_s'])} work remaining"
                )
            elite_status.controls.append(ft.Text(text, color=ft.Colors.GREY_300))

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

    def on_mode_pick(e):
        elite_settings.visible = e.control.value == elite.MODE_ELITE
        refresh_session_controls()
        page.update()

    mode_picker.on_select = on_mode_pick

    def start_session(e):
        if not state["participants"]:
            start_session_error.value = "Add at least one client and device before starting."
            start_session_error.visible = True
            page.update()
            return
        settings = None
        mode_params = None
        if mode_picker.value == elite.MODE_ELITE:
            settings, error = elite.parse_settings(
                elite_rounds_field.value,
                elite_round_length_field.value,
                elite_recovered_field.value,
                elite_rest_field.value,
            )
            if error:
                start_session_error.value = error
                start_session_error.visible = True
                page.update()
                return
            mode_params = dict(settings)
            mode_params["clients"] = {}
            for participant in state["participants"].values():
                client = db.get_client(participant["client_id"])
                thresholds = participant["thresholds"]
                mode_params["clients"][participant["client_id"]] = {
                    "z5_low": thresholds[4],
                    "recovered_bpm": elite.recovered_bpm(
                        thresholds, client["resting_hr"], settings["recovered_pct"]
                    ),
                    "max_hr": thresholds[-1],
                    "resting_hr": client["resting_hr"],
                }
        first_participant = next(iter(state["participants"].values()))
        state["session_id"] = db.start_session(
            first_participant["client_id"],
            first_participant["device_address"],
            mode=mode_picker.value or elite.MODE_NORMAL,
            mode_params=mode_params,
        )
        state["session_start"] = time.monotonic()
        session = db.get_session(state["session_id"])
        state["session_wall_start"] = datetime.fromisoformat(session["started_at"])
        for participant in state["participants"].values():
            db.add_session_participant(
                state["session_id"], participant["client_id"], participant["device_address"]
            )
            if settings is not None:
                client = db.get_client(participant["client_id"])
                params = elite.params_for_client(
                    participant["thresholds"], client["resting_hr"], settings
                )
                engine = elite.EliteEngine(params, state["session_start"])
                participant["elite_engine"] = engine
                participant["elite_saved_rounds"] = 0
                _record_elite_updates(participant, engine.pop_events())
        start_session_error.visible = False
        _refresh_elite_status()
        refresh_session_controls()
        page.update()

    def refresh_summary_screen(session_id):
        session, participant_summaries = db.session_summary(session_id)
        summary_participant_rows.controls.clear()
        summary_elite_details.controls.clear()
        if not session:
            summary_session_title.value = "No completed session"
            summary_session_times.value = "Start: --    End: --    Duration: --"
            summary_empty_message.visible = True
            return

        started_at = datetime.fromisoformat(session["started_at"])
        ended_at = datetime.fromisoformat(session["ended_at"]) if session["ended_at"] else None
        duration_seconds = (ended_at - started_at).total_seconds() if ended_at else 0
        summary_session_title.value = "Session Summary"
        summary_session_times.value = (
            f"Start: {format_session_timestamp(session['started_at'])}    "
            f"End: {format_session_timestamp(session['ended_at'])}    "
            f"Duration: {format_session_duration(duration_seconds)}"
        )
        summary_empty_message.visible = not participant_summaries
        if session["mode"] == elite.MODE_ELITE:
            params = db.session_mode_params(session)
            summary_elite_details.controls.append(
                ft.Text(
                    f"Elite session: {params['rounds']} x {elite.format_clock(params['round_s'])} "
                    f"rounds; target rest {elite.format_clock(params['target_rest_s'])}"
                )
            )
            for participant in db.list_session_participants(session_id):
                for result in db.list_elite_rounds(session_id, participant["client_id"]):
                    summary_elite_details.controls.append(
                        ft.Text(
                            f"R{result['round_no']}  pre-work "
                            f"{elite.format_clock(result['prework_s'])} | work "
                            f"{elite.format_clock(result['work_s'])} | recovery "
                            f"{elite.format_clock(result['recovery_s'])}"
                        )
                    )
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
        if state["session_id"]:
            if mode_picker.value == elite.MODE_ELITE:
                stop_time = time.monotonic()
                for participant in state["participants"].values():
                    engine = participant["elite_engine"]
                    if engine is not None:
                        _record_elite_updates(participant, engine.finish(stop_time))
            completed_session_id = state["session_id"]
            db.end_session(completed_session_id)
            state["last_session_id"] = completed_session_id
            refresh_summary_screen(completed_session_id)
            export_btn.disabled = False
            export_status.value = ""
        state["session_id"] = None
        state["session_start"] = None
        state["session_wall_start"] = None
        elite_status.controls.clear()
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
        if state["last_session_id"] and completed_client_ids:
            prompt_for_resting_hr(completed_client_ids)
        else:
            page.update()

    start_btn.on_click = start_session
    stop_btn.on_click = stop_session

    session_tab = ft.Column(
        [
            ft.Text("Add participants", weight=ft.FontWeight.BOLD),
            ft.Row([client_picker, device_picker], alignment=ft.MainAxisAlignment.START),
            ft.Row([scan_btn], alignment=ft.MainAxisAlignment.START),
            ft.Row([attach_btn], alignment=ft.MainAxisAlignment.START),
            ft.Row([strap_status], alignment=ft.MainAxisAlignment.START),
            start_session_error,
            ft.Row([mode_picker, elite_settings], alignment=ft.MainAxisAlignment.START, wrap=True),
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
            participant_chart,
            empty_chart_message,
            ft.Row([start_btn, stop_btn], alignment=ft.MainAxisAlignment.CENTER, spacing=20),
            elapsed_text,
            elite_status,
            zone_legend,
        ],
        expand=True,
    )

    export_status = ft.Text("", size=12, color=ft.Colors.GREY_400)

    async def export_last_session(e):
        session_id = state["last_session_id"]
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

    summary_tab = ft.Column(
        [
            summary_session_title,
            summary_session_times,
            summary_elite_details,
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

    # ==================================================================
    # LAYOUT
    # ==================================================================
    tabs = ft.Tabs(
        length=3,
        selected_index=0,
        expand=True,
        content=ft.Column(
            expand=True,
            controls=[
                ft.TabBar(
                    tabs=[
                        ft.Tab(label="Clients", icon=ft.Icons.PEOPLE),
                        ft.Tab(label="Session", icon=ft.Icons.FAVORITE),
                        ft.Tab(label="Summary", icon=ft.Icons.QUERY_STATS),
                    ]
                ),
                ft.TabBarView(
                    expand=True,
                    controls=[
                        ft.Container(clients_tab, padding=20),
                        ft.Container(session_tab, padding=20),
                        ft.Container(summary_tab, padding=20),
                    ],
                ),
            ],
        ),
    )
    page.add(tabs)
    refresh_client_list()
    refresh_session_controls()


if __name__ == "__main__":
    ft.run(main)