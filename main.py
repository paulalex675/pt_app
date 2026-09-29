"""PT Studio — desktop app for live heart-rate coaching sessions.

Run with: python main.py
Requires a Polar H10 (or other standard BLE HRM) paired/discoverable, and
Bluetooth permission granted to the terminal/IDE running this (macOS will
prompt on first connect).
"""
import asyncio
import time

import flet as ft

import db
import zones
from ble_client import HeartRateStream, rr_to_json, scan_for_straps


def main(page: ft.Page):
    page.title = "PT Studio"
    page.theme_mode = ft.ThemeMode.DARK
    page.window.width = 900
    page.window.height = 700
    page.padding = 0
    db.init_db()

    # ---------------- shared state ----------------
    state = {
        "selected_client_id": None,
        "session_id": None,
        "strap_address": None,
        "stream": None,
        "connected": False,
        "session_start": None,
        "zone_seconds": [0, 0, 0, 0, 0],
        "last_sample_time": None,
        "thresholds": None,
    }

    # ==================================================================
    # CLIENTS VIEW
    # ==================================================================
    client_search = ft.TextField(label="Search clients", width=300, on_change=lambda e: refresh_client_list())
    client_list_view = ft.ListView(expand=True, spacing=4)
    client_picker = ft.Dropdown(label="Select client for session", width=340, options=[])

    def refresh_client_list():
        client_list_view.controls.clear()
        client_picker.options.clear()
        for c in db.list_clients(client_search.value or ""):
            age = db.client_age(c["dob"])
            subtitle_bits = []
            if age is not None:
                subtitle_bits.append(f"{age}y")
            if c["sex"]:
                subtitle_bits.append(c["sex"])
            if c["resting_hr"]:
                subtitle_bits.append(f"RHR {c['resting_hr']}")
            client_list_view.controls.append(
                ft.ListTile(
                    title=ft.Text(f"{c['first_name']} {c['last_name']}"),
                    subtitle=ft.Text(", ".join(subtitle_bits) or "No details yet"),
                    leading=ft.Icon(ft.Icons.PERSON),
                )
            )
            client_picker.options.append(
                ft.dropdown.Option(key=c["id"], text=f"{c['first_name']} {c['last_name']}")
            )
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
        add_client_dialog.open = False
        page.update()

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
        page.open(add_client_dialog)

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
    hr_display = ft.Text("--", size=72, weight=ft.FontWeight.BOLD)
    zone_label = ft.Text("No signal", size=18)
    elapsed_text = ft.Text("00:00", size=16, color=ft.Colors.GREY_400)
    start_btn = ft.FilledButton("Start session", icon=ft.Icons.PLAY_ARROW, disabled=True)
    stop_btn = ft.FilledButton("End session", icon=ft.Icons.STOP, disabled=True, bgcolor=ft.Colors.RED_700)

    zone_bars = [ft.Container(width=0, height=14, bgcolor=zones.ZONE_COLORS[i], border_radius=3) for i in range(5)]
    zone_bar_row = ft.Row(zone_bars, spacing=2)
    zone_legend = ft.Row(
        [ft.Row([ft.Container(width=10, height=10, bgcolor=zones.ZONE_COLORS[i], border_radius=2),
                 ft.Text(zones.ZONE_NAMES[i], size=11)], spacing=4) for i in range(5)],
        spacing=14, wrap=True,
    )

    def update_zone_bar():
        total = sum(state["zone_seconds"]) or 1
        for i, bar in enumerate(zone_bars):
            bar.width = max(2, 500 * state["zone_seconds"][i] / total)

    def on_hr_sample(hr: int, rr_intervals: list[float]):
        # bleak's notification callback fires on the same asyncio loop Flet
        # is running on (both are asyncio-based), so this can update the UI
        # directly without hopping threads.
        _apply_sample(hr, rr_intervals)

    def _apply_sample(hr: int, rr_intervals: list[float]):
        now = time.monotonic()
        if state["last_sample_time"] is not None and state["session_id"]:
            elapsed = now - state["last_sample_time"]
            z = zones.zone_for_hr(hr, state["thresholds"])
            state["zone_seconds"][z] += elapsed
        state["last_sample_time"] = now

        hr_display.value = str(hr)
        if state["thresholds"]:
            z = zones.zone_for_hr(hr, state["thresholds"])
            zone_label.value = zones.ZONE_NAMES[z]
            zone_label.color = zones.ZONE_COLORS[z]
            hr_display.color = zones.ZONE_COLORS[z]

        if state["session_id"]:
            db.log_sample(state["session_id"], hr, rr_to_json(rr_intervals))
            started = state["session_start"]
            secs = int(time.monotonic() - started) if started else 0
            elapsed_text.value = f"{secs // 60:02d}:{secs % 60:02d}"
            update_zone_bar()

        page.update()

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

        device = found[0]  # single-pod setup: just take the first HR device seen
        strap_status.value = f"Connecting to {device.name or device.address}..."
        page.update()

        stream = HeartRateStream(device.address, on_hr_sample)
        await stream.connect()
        state["stream"] = stream
        state["strap_address"] = device.address
        state["connected"] = True
        strap_status.value = f"Strap connected: {device.name or device.address}"
        start_btn.disabled = state["selected_client_id"] is None
        page.update()

    scan_btn.on_click = do_scan

    def on_client_pick(e):
        state["selected_client_id"] = client_picker.value
        start_btn.disabled = not (state["connected"] and state["selected_client_id"])
        page.update()

    client_picker.on_change = on_client_pick

    def start_session(e):
        client = db.get_client(state["selected_client_id"])
        age = db.client_age(client["dob"])
        state["thresholds"] = zones.zone_thresholds(age, client["resting_hr"])
        state["session_id"] = db.start_session(client["id"], state["strap_address"])
        state["session_start"] = time.monotonic()
        state["last_sample_time"] = None
        state["zone_seconds"] = [0, 0, 0, 0, 0]
        start_btn.disabled = True
        stop_btn.disabled = False
        client_picker.disabled = True
        page.update()

    def stop_session(e):
        db.end_session(state["session_id"])
        state["session_id"] = None
        state["session_start"] = None
        start_btn.disabled = False
        stop_btn.disabled = True
        client_picker.disabled = False
        page.update()

    start_btn.on_click = start_session
    stop_btn.on_click = stop_session

    session_tab = ft.Column(
        [
            ft.Row([client_picker], alignment=ft.MainAxisAlignment.START),
            ft.Row([scan_btn, strap_status], alignment=ft.MainAxisAlignment.START, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Divider(),
            ft.Container(
                content=ft.Column(
                    [hr_display, zone_label, elapsed_text],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=4,
                ),
                alignment=ft.alignment.center, padding=30,
            ),
            ft.Row([start_btn, stop_btn], alignment=ft.MainAxisAlignment.CENTER, spacing=20),
            ft.Divider(),
            ft.Text("Time in zone", size=13, color=ft.Colors.GREY_400),
            zone_bar_row,
            zone_legend,
        ],
        expand=True,
    )

    # ==================================================================
    # LAYOUT
    # ==================================================================
    tabs = ft.Tabs(
        selected_index=0,
        tabs=[
            ft.Tab(text="Clients", icon=ft.Icons.PEOPLE, content=ft.Container(clients_tab, padding=20)),
            ft.Tab(text="Session", icon=ft.Icons.FAVORITE, content=ft.Container(session_tab, padding=20)),
        ],
        expand=True,
    )
    page.add(tabs)
    refresh_client_list()


if __name__ == "__main__":
    ft.app(target=main)
