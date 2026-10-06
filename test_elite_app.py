"""Drives the real PT Studio screens in elite mode with fake straps and a fake clock.

No window, no Bluetooth: a stand-in page object receives the controls, and the
test clicks the same handlers a person would.
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
class EliteSessionThroughTheScreens(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", Path(self.tmp.name) / "t.db")
        self.db_patch.start()
        db.init_db()
        self.ada = db.create_client("Ada", "North", "female", "1996-01-01", 168, 62, 55)
        self.sam = db.create_client("Sam", "South", "male", "1996-01-01", 180, 82, 55)
        STREAMS.clear()

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def find(self, kind, test):
        return next(c for c in walk(self.page) if isinstance(c, kind) and test(c))

    def button(self, label):
        return self.find((ft.FilledButton, ft.FilledTonalButton, ft.OutlinedButton),
                         lambda c: c.content == label)

    def dropdown(self, label):
        return self.find(ft.Dropdown, lambda c: c.label == label)

    def field(self, label):
        return self.find(ft.TextField, lambda c: c.label == label)

    def texts(self):
        return [c.value for c in walk(self.page) if isinstance(c, ft.Text) and c.value]

    def pick(self, dropdown, value):
        dropdown.value = value
        dropdown.on_select(SimpleNamespace(control=dropdown))

    async def attach(self, client_id, address):
        self.pick(self.dropdown("Select client for session"), client_id)
        self.pick(self.dropdown("Select device for session"), address)
        await call(self.button("Attach client to device").on_click)

    def tick(self, ada_hr, sam_hr):
        self.clock.now += 1.0
        STREAMS["AA:01"].on_sample(ada_hr, [])
        STREAMS["BB:02"].on_sample(sam_hr, [])

    def test_two_clients_one_finishes_one_is_stopped(self):
        self.clock = FakeClock()
        self.page = FakePage()
        with patch.object(app, "time", self.clock), \
                patch.object(app, "scan_for_straps", fake_scan), \
                patch.object(app, "HeartRateStream", FakeStream):
            app.main(self.page)

            async def scenario():
                await call(self.button("Scan for strap").on_click)
                self.pick(self.dropdown("Select client for session"), self.ada)
                self.pick(self.dropdown("Select device for session"), "AA:01")
                await call(self.button("Attach client to device").on_click)
                await self.attach(self.sam, "BB:02")

                # bad input is rejected with a message and no session starts
                self.pick(self.dropdown("Mode"), elite.MODE_ELITE)
                self.field("Rounds").value = "0"
                self.button("Start session").on_click(None)
                self.assertTrue(any("Rounds must be" in t for t in self.texts()))
                self.assertEqual(db.list_clients("") and self._session_count(), 0)

                self.field("Rounds").value = "2"
                self.field("Round length (m:ss)").value = "0:10"
                self.field("Recovered at (% of HR reserve)").value = "70"
                self.field("Fight rest (m:ss)").value = "0:30"
                self.button("Start session").on_click(None)

                # Ada: warm up, work two rounds with a recovery between. Sam stays low.
                for _ in range(5):
                    self.tick(100, 100)
                for _ in range(15):
                    self.tick(185, 100)       # round 1 work
                for _ in range(10):
                    self.tick(100, 100)       # recovery
                for _ in range(4):
                    self.tick(100, 100)       # round 2 pre-work
                for _ in range(15):
                    self.tick(185, 100)       # round 2 work
                self.assertTrue(any(t.startswith("DONE") for t in self.texts()))

                await call(self.button("End session").on_click)

            asyncio.run(scenario())

        session = self._only_session()
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
        self.assertGreater(elite.total_seconds_from_events(
            [dict(e) for e in db.list_phase_events(session["id"], self.ada)]), 30)

        sam_rounds = db.list_elite_rounds(session["id"], self.sam)
        self.assertEqual(len(sam_rounds), 1)
        self.assertIn("stopped", json.loads(sam_rounds[0]["flags"]))
        self.assertEqual(sam_rounds[0]["work_s"], 0.0)

        # the summary screen shows the elite breakdown
        texts = self.texts()
        self.assertTrue(any(t.startswith("Elite session: 2 x 0:10") for t in texts))
        self.assertTrue(any(t.startswith("R1  pre-work") for t in texts))

    def test_normal_mode_is_unchanged(self):
        self.clock = FakeClock()
        self.page = FakePage()
        with patch.object(app, "time", self.clock), \
                patch.object(app, "scan_for_straps", fake_scan), \
                patch.object(app, "HeartRateStream", FakeStream):
            app.main(self.page)

            async def scenario():
                await call(self.button("Scan for strap").on_click)
                await self.attach(self.ada, "AA:01")
                self.button("Start session").on_click(None)
                for _ in range(5):
                    self.clock.now += 1.0
                    STREAMS["AA:01"].on_sample(150, [])
                await call(self.button("End session").on_click)

            asyncio.run(scenario())

        session = self._only_session()
        self.assertEqual(session["mode"], "normal")
        self.assertEqual(db.list_elite_rounds(session["id"]), [])
        self.assertEqual(db.list_phase_events(session["id"]), [])
        self.assertFalse(any(t.startswith("Elite session") for t in self.texts()))

    def _session_count(self):
        with db.get_conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def _only_session(self):
        with db.get_conn() as conn:
            rows = conn.execute("SELECT * FROM sessions").fetchall()
        self.assertEqual(len(rows), 1)
        return rows[0]


if __name__ == "__main__":
    unittest.main()
