"""Drives the real PT Studio screens with fake straps and a fake clock.

No window, no Bluetooth: a stand-in page object receives the controls, and the
tests click the same handlers a person would.
"""
import asyncio
import inspect
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import db
import elite

try:
    import flet as ft
    import main as app
    HAVE_FLET = True
except ImportError:  # pragma: no cover
    HAVE_FLET = False


def walk(node, seen=None):
    seen = seen if seen is not None else set()
    if node is None or isinstance(node, (str, int, float, bool)) or id(node) in seen:
        return
    seen.add(id(node))
    yield node
    for name in ("controls", "content", "tabs", "actions", "leading", "title", "trailing", "subtitle"):
        value = getattr(node, name, None)
        if isinstance(value, (list, tuple)):
            for item in value:
                yield from walk(item, seen)
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            yield from walk(value, seen)


class FakeWindow:
    pass


class FakePage:
    def __init__(self):
        self.window = FakeWindow()
        self.controls = []
        self.dialogs = []
        self.width = 1400
        self.height = 900

    def add(self, *controls):
        self.controls.extend(controls)

    def update(self):
        pass

    def show_dialog(self, dialog):
        self.dialogs.append(dialog)

    def pop_dialog(self):
        pass


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


STREAMS = {}


class FakeStream:
    def __init__(self, address, on_sample):
        self.address = address
        self.on_sample = on_sample
        STREAMS[address] = self

    async def connect(self):
        return None

    async def disconnect(self):
        return None


async def fake_scan():
    return [SimpleNamespace(name="Polar A", address="AA:01"), SimpleNamespace(name="Polar B", address="BB:02")]


async def call(handler):
    result = handler(None)
    if inspect.isawaitable(result):
        await result


@unittest.skipUnless(HAVE_FLET, "flet not installed")
class ScreenTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", Path(self.tmp.name) / "t.db")
        self.db_patch.start()
        db.init_db()
        self.ada = db.create_client("Ada", "North", "female", "1996-01-01", 168, 62, 55)
        self.sam = db.create_client("Sam", "South", "male", "1996-01-01", 180, 82, 55)
        STREAMS.clear()
        self.clock = FakeClock()
        self.page = FakePage()

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def build(self):
        """Builds the app with fake straps and a fake clock."""
        patcher = patch.multiple(app, time=self.clock, scan_for_straps=fake_scan, HeartRateStream=FakeStream)
        patcher.start()
        self.addCleanup(patcher.stop)
        app.main(self.page)

    # ---- finding controls ----
    def find(self, kind, test):
        return next(c for c in walk(self.page) if isinstance(c, kind) and test(c))

    def keyed(self, key):
        return next(c for c in walk(self.page) if getattr(c, "key", None) == key)

    def button(self, label):
        return self.find((ft.FilledButton, ft.FilledTonalButton, ft.OutlinedButton), lambda c: c.content == label)

    def dropdown(self, label):
        return self.find(ft.Dropdown, lambda c: c.label == label)

    def field(self, label):
        return self.find(ft.TextField, lambda c: c.label == label)

    def tabs(self):
        return self.find(ft.Tabs, lambda c: True)

    def tab_bar(self):
        return self.find(ft.TabBar, lambda c: True)

    def texts(self):
        return [c.value for c in walk(self.page) if isinstance(c, ft.Text) and c.value]

    def clock_text(self):
        return self.find(ft.Text, lambda c: c.value and c.value[:2].isdigit() and ":" in c.value).value

    def history_tiles(self):
        return [c for c in walk(self.page) if isinstance(c, ft.ListTile) and c.on_click
                and isinstance(c.subtitle, ft.Text) and c.subtitle.value.startswith(("Normal", "Elite"))]

    # ---- actions ----
    def pick(self, dropdown, value):
        dropdown.value = value
        dropdown.on_select(SimpleNamespace(control=dropdown))

    async def attach(self, client_id, address):
        self.pick(self.dropdown("Select client for session"), client_id)
        self.pick(self.dropdown("Select device for session"), address)
        await call(self.button("Attach client to device").on_click)

    def tick(self, ada_hr, sam_hr=None):
        self.clock.now += 1.0
        STREAMS["AA:01"].on_sample(ada_hr, [])
        if sam_hr is not None:
            STREAMS["BB:02"].on_sample(sam_hr, [])

    async def start_normal_session(self, client_id=None, address="AA:01"):
        await call(self.button("Scan for strap").on_click)
        await self.attach(client_id or self.ada, address)
        self.button("Start session").on_click(None)

    def sample_count(self, session_id):
        with db.get_conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM hr_samples WHERE session_id = ?", (session_id,)).fetchone()[0]

    def sessions(self):
        with db.get_conn() as conn:
            return conn.execute("SELECT * FROM sessions ORDER BY started_at").fetchall()

    def only_session(self):
        rows = self.sessions()
        self.assertEqual(len(rows), 1)
        return rows[0]


class EliteSessionThroughTheScreens(ScreenTestCase):
    def test_two_clients_one_finishes_one_is_stopped(self):
        self.build()

        async def scenario():
            await call(self.button("Scan for strap").on_click)
            await self.attach(self.ada, "AA:01")
            await self.attach(self.sam, "BB:02")

            # bad input is rejected with a message and no session starts
            self.pick(self.dropdown("Mode"), elite.MODE_ELITE)
            self.field("Rounds").value = "0"
            self.button("Start session").on_click(None)
            self.assertTrue(any("Rounds must be" in t for t in self.texts()))
            self.assertEqual(len(self.sessions()), 0)

            self.field("Rounds").value = "2"
            self.field("Round length (m:ss)").value = "0:10"
            self.field("Recovered at (% of HR reserve)").value = "70"
            self.field("Fight rest (m:ss)").value = "0:30"
            self.button("Start session").on_click(None)

            # Ada: warm up, work two rounds with a recovery between. Sam stays low.
            for hr, seconds in ((100, 5), (185, 15), (100, 10), (100, 4), (185, 15)):
                for _ in range(seconds):
                    self.tick(hr, 100)
            self.assertTrue(any(t.startswith("DONE") for t in self.texts()))

            await call(self.button("Stop").on_click)

        asyncio.run(scenario())

        session = self.only_session()
        self.assertEqual(session["mode"], "elite")
        params = db.session_mode_params(session)
        self.assertEqual(params["rounds"], 2)
        self.assertEqual(params["target_total_s"], 2 * 10 + 1 * 30)
        self.assertIn(self.ada, params["clients"])
        self.assertGreater(params["clients"][self.ada]["z5_low"], 150)

        ada_rounds = db.list_elite_rounds(session["id"], self.ada)
        self.assertEqual([r["round_no"] for r in ada_rounds], [1, 2])
        self.assertEqual(ada_rounds[0]["work_s"], 10.0)
        self.assertIsNotNone(ada_rounds[0]["recovery_s"])
        self.assertIsNone(ada_rounds[1]["recovery_s"])
        self.assertEqual(json.loads(ada_rounds[0]["flags"]), [])
        ada_phases = [e["phase"] for e in db.list_phase_events(session["id"], self.ada)]
        self.assertEqual(ada_phases, ["prework", "work", "recovery", "prework", "work", "done"])

        sam_rounds = db.list_elite_rounds(session["id"], self.sam)
        self.assertEqual(len(sam_rounds), 1)
        self.assertIn("stopped", json.loads(sam_rounds[0]["flags"]))
        self.assertEqual(sam_rounds[0]["work_s"], 0.0)

        texts = self.texts()
        self.assertTrue(any(t.startswith("Elite session: 2 x 0:10") for t in texts))
        self.assertTrue(any(t.startswith("R1  pre-work") for t in texts))

    def test_normal_mode_is_unchanged(self):
        self.build()

        async def scenario():
            await self.start_normal_session()
            for _ in range(5):
                self.tick(150)
            await call(self.button("Stop").on_click)

        asyncio.run(scenario())
        session = self.only_session()
        self.assertEqual(session["mode"], "normal")
        self.assertEqual(db.list_elite_rounds(session["id"]), [])
        self.assertEqual(db.list_phase_events(session["id"]), [])
        self.assertFalse(any(t.startswith("Elite session") for t in self.texts()))


class LiveSessionScreens(ScreenTestCase):
    def test_live_view_shows_only_the_graph_and_buttons_then_restores(self):
        self.build()
        setup, header = self.keyed("session-setup"), self.keyed("chart-header")
        setup_buttons, live = self.keyed("setup-buttons"), self.keyed("live-controls")
        self.assertTrue(setup.visible and header.visible and setup_buttons.visible)
        self.assertFalse(live.visible)
        self.assertTrue(self.tab_bar().visible)

        async def scenario():
            await self.start_normal_session()
            self.tick(150)
            self.assertFalse(setup.visible)
            self.assertFalse(header.visible)
            self.assertFalse(setup_buttons.visible)
            self.assertTrue(live.visible)
            self.assertFalse(self.tab_bar().visible)               # the tab strip is hidden too
            # the bar now fills the width of the window: 1400 - name - heart rate - gaps - margins
            self.assertTrue(any(isinstance(c, ft.Stack) and c.width == 908 for c in walk(self.page)))

            self.page.width = 1000                                  # resizing the window rescales the bar
            self.page.on_resize(None)
            self.assertTrue(any(isinstance(c, ft.Stack) and c.width == 508 for c in walk(self.page)))

            await call(self.button("Stop").on_click)

        asyncio.run(scenario())
        self.assertTrue(setup.visible and header.visible and setup_buttons.visible)
        self.assertFalse(live.visible)
        self.assertTrue(self.tab_bar().visible)

    def test_stop_goes_straight_to_the_summary_with_a_reset_session_screen(self):
        self.build()

        async def scenario():
            await self.start_normal_session()
            for _ in range(4):
                self.tick(150)
            await call(self.button("Stop").on_click)

        asyncio.run(scenario())
        self.assertEqual(self.tabs().selected_index, 2)                       # Summary tab
        self.assertIn("Session Summary", self.texts())
        self.assertEqual(self.page.dialogs[-1].title.value, "Record resting heart rate")
        # the Session tab is a fresh setup screen: nobody attached, Start disabled
        self.assertTrue(self.button("Start session").disabled)
        self.assertTrue(any(t == "No participants added" for t in self.texts()))

    def test_pause_stops_recording_and_resume_continues(self):
        self.build()

        async def scenario():
            await self.start_normal_session()
            for _ in range(5):
                self.tick(150)
            session_id = self.only_session()["id"]
            self.assertEqual(self.sample_count(session_id), 5)

            self.button("Pause").on_click(None)
            self.assertTrue(any(t.startswith("PAUSED") for t in self.texts()))
            elapsed_before = self.clock_text()
            for _ in range(5):
                self.tick(160)
            self.assertEqual(self.sample_count(session_id), 5)                # nothing recorded while paused
            self.assertEqual(self.clock_text(), elapsed_before)               # the clock is frozen too

            self.button("Resume").on_click(None)
            self.assertFalse(any(t.startswith("PAUSED") for t in self.texts()))
            for _ in range(3):
                self.tick(150)
            self.assertEqual(self.sample_count(session_id), 8)

            await call(self.button("Stop").on_click)

        asyncio.run(scenario())
        self.assertEqual(self.only_session()["paused_s"], 5.0)               # excluded from the duration

    def test_pause_freezes_the_elite_clocks(self):
        self.build()

        async def scenario():
            await call(self.button("Scan for strap").on_click)
            await self.attach(self.ada, "AA:01")
            self.pick(self.dropdown("Mode"), elite.MODE_ELITE)
            self.field("Rounds").value = "1"
            self.field("Round length (m:ss)").value = "0:10"
            self.button("Start session").on_click(None)

            for _ in range(4):
                self.tick(185)                                               # into the work phase
            self.button("Pause").on_click(None)
            for _ in range(60):
                self.tick(185)                                               # a minute of nothing counted
            self.button("Resume").on_click(None)
            for _ in range(10):
                self.tick(185)
            await call(self.button("Stop").on_click)

        asyncio.run(scenario())
        session = self.only_session()
        events = [dict(e) for e in db.list_phase_events(session["id"], self.ada)]
        self.assertEqual(events[-1]["phase"], "done")
        total = elite.total_seconds_from_events(events)
        self.assertLess(total, 20)                                           # the 60 paused seconds are not in it
        self.assertEqual(db.list_elite_rounds(session["id"], self.ada)[0]["work_s"], 10.0)

    def test_restart_discards_the_run_and_starts_a_fresh_session(self):
        self.build()

        async def scenario():
            await self.start_normal_session()
            for _ in range(5):
                self.tick(150)
            first = self.only_session()["id"]

            self.button("Restart").on_click(None)                            # asks first
            dialog = self.page.dialogs[-1]
            self.assertEqual(dialog.title.value, "Restart this session?")
            self.assertEqual(len(self.sessions()), 1)
            next(a for a in dialog.actions if a.content == "Cancel").on_click(None)
            self.assertEqual(self.only_session()["id"], first)               # cancel changes nothing

            self.button("Restart").on_click(None)
            next(a for a in self.page.dialogs[-1].actions if a.content == "Restart").on_click(None)
            second = self.only_session()["id"]                                # exactly one session, a new one
            self.assertNotEqual(second, first)
            self.assertEqual(self.sample_count(first), 0)                    # the discarded run is gone
            self.assertTrue(self.keyed("live-controls").visible)             # still on the live screen
            self.tick(150)
            self.assertEqual(self.sample_count(second), 1)                   # and recording again
            await call(self.button("Stop").on_click)

        asyncio.run(scenario())

    def test_previous_sessions_list_lets_you_look_back_and_export_the_one_you_view(self):
        self.build()

        async def scenario():
            await self.start_normal_session(self.ada, "AA:01")
            for _ in range(3):
                self.tick(150)
            await call(self.button("Stop").on_click)
            self.clock.now += 10
            await self.start_normal_session(self.sam, "BB:02")
            for _ in range(3):
                STREAMS["BB:02"].on_sample(140, [])
                self.clock.now += 1
            await call(self.button("Stop").on_click)

        asyncio.run(scenario())
        tiles = self.history_tiles()
        self.assertEqual(len(tiles), 2)
        self.assertIn("Sam South", tiles[0].subtitle.value)                  # newest first
        self.assertIn("Ada North", tiles[1].subtitle.value)
        self.assertTrue(tiles[0].selected)                                    # the one just finished is open
        self.assertFalse(tiles[1].selected)

        older = next(r for r in self.sessions() if r["client_id"] == self.ada)["id"]
        tiles[1].on_click(None)                                               # look back at the older session
        self.assertTrue(self.history_tiles()[1].selected)
        uploaded = []

        async def exporting():
            with patch("export.upload_session", lambda session_id, *a, **k: uploaded.append(session_id) or ["key"]):
                await call(self.button("Export to cloud").on_click)

        asyncio.run(exporting())
        self.assertEqual(uploaded, [older])                                   # exports what you are looking at

    def test_summary_opens_on_the_most_recent_session_at_startup(self):
        earlier = db.start_session(self.ada, "AA:01")
        db.add_session_participant(earlier, self.ada, "AA:01")
        db.log_sample(earlier, 140, "[]", client_id=self.ada, strap_device_id="AA:01")
        db.end_session(earlier)
        self.build()
        self.assertIn("Session Summary", self.texts())
        tiles = self.history_tiles()
        self.assertEqual(len(tiles), 1)
        self.assertTrue(tiles[0].selected)
        self.assertFalse(self.button("Export to cloud").disabled)


if __name__ == "__main__":
    unittest.main()
