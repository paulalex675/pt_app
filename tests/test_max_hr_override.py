"""Manual max HR: medical caps and people whose true max is well below 220 - age."""
import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import db
import export
import zones
from tests.test_elite_app import HAVE_FLET, ScreenTestCase, call, walk

if HAVE_FLET:
    import flet as ft


class ResolveTests(unittest.TestCase):
    def test_manual_beats_observed_beats_estimate(self):
        self.assertEqual(zones.resolve_max_hr(30, 160, 195), (160, "manual"))
        self.assertEqual(zones.resolve_max_hr(30, None, 195), (195, "observed"))
        self.assertEqual(zones.resolve_max_hr(30, None, None), (zones.estimate_max_hr(30), "estimated"))

    def test_validation_range(self):
        for bad in (None, 99, 221, "170", 170.5):
            self.assertFalse(zones.valid_manual_max_hr(bad), bad)
        self.assertTrue(zones.valid_manual_max_hr(100))
        self.assertTrue(zones.valid_manual_max_hr(220))

    def test_zones_scale_to_the_manual_value(self):
        low = zones.zone_thresholds(30, 60, 160)
        self.assertEqual(low[-1], 160)
        self.assertLess(low[-1], zones.zone_thresholds(30, 60)[-1])


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        patcher = patch.object(db, "DB_PATH", Path(self.tmp.name) / "t.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        db.init_db()

    def test_old_databases_are_migrated(self):
        with db.get_conn() as conn:
            conn.execute("ALTER TABLE clients DROP COLUMN max_hr_manual")
        db.init_db()
        cid = db.create_client("A", "B", max_hr_manual=170)
        self.assertEqual(db.get_client(cid)["max_hr_manual"], 170)

    def test_learning_never_touches_the_manual_value(self):
        cid = db.create_client("A", "B", "male", "1990-01-01", max_hr_manual=165)
        db.update_client_max_hr(cid, 190)
        row = db.get_client(cid)
        self.assertEqual(row["max_hr_manual"], 165)
        self.assertEqual(db.client_max_hr(row), (165, "manual"))

    def test_clearing_returns_to_learned_value(self):
        cid = db.create_client("A", "B", "male", "1990-01-01", max_hr_manual=165)
        db.update_client_max_hr(cid, 190)
        db.set_client_max_hr_manual(cid, None)
        self.assertEqual(db.client_max_hr(db.get_client(cid)), (190, "observed"))

    def test_export_carries_effective_value_and_its_source(self):
        cid = db.create_client("A", "B", "male", "1990-01-01", 180, 80, 55, max_hr_manual=165)
        sid = db.start_session(cid, "s")
        db.add_session_participant(sid, cid, "s")
        db.log_sample(sid, 120, "[]", client_id=cid, strap_device_id="s")
        db.end_session(sid)
        client = export.build_client_exports(sid)[0]["client"]
        self.assertEqual((client["max_hr"], client["max_hr_source"]), (165, "manual"))


@unittest.skipUnless(HAVE_FLET, "flet not installed")
class ScreenTests(ScreenTestCase):
    def test_readings_above_a_manual_cap_do_not_raise_it(self):
        db.set_client_max_hr_manual(self.ada, 170)
        self.build()

        async def scenario():
            await self.start_normal_session(self.ada)
            for _ in range(6):
                self.tick(190)
        asyncio.run(scenario())
        row = db.get_client(self.ada)
        self.assertEqual(row["max_hr_manual"], 170)
        self.assertIsNone(row["max_hr"])                      # learner stayed out of it

    def test_without_a_manual_value_the_app_still_learns(self):
        self.build()

        async def scenario():
            await self.start_normal_session(self.ada)
            for _ in range(6):
                self.tick(200)
        asyncio.run(scenario())
        self.assertEqual(db.get_client(self.ada)["max_hr"], 200)

    def _open_edit(self):
        self.pick(self.dropdown("Select client for session"), self.ada)
        self.button("Set max HR").on_click(None)
        dialog = self.page.dialogs[-1]
        field = next(c for c in walk(dialog) if isinstance(c, ft.TextField))
        save = next(c for c in walk(dialog) if isinstance(c, ft.FilledButton))
        error = next(c for c in walk(dialog) if isinstance(c, ft.Text) and c.color == ft.Colors.RED_300)
        return field, save, error

    def test_edit_dialog_sets_rejects_and_clears(self):
        self.build()
        field, save, error = self._open_edit()
        field.value = "172"
        save.on_click(None)
        self.assertEqual(db.get_client(self.ada)["max_hr_manual"], 172)

        field, save, error = self._open_edit()
        self.assertEqual(field.value, "172")                  # pre-filled
        for bad in ("99", "250", "abc"):
            field.value = bad
            save.on_click(None)
            self.assertTrue(error.visible, bad)
            self.assertEqual(db.get_client(self.ada)["max_hr_manual"], 172, bad)

        field.value = ""
        save.on_click(None)
        self.assertIsNone(db.get_client(self.ada)["max_hr_manual"])

    def test_edit_button_needs_a_selected_client(self):
        self.build()
        self.assertTrue(self.button("Set max HR").disabled)

    def test_new_client_form_accepts_and_validates_the_override(self):
        self.build()
        self.button("New client").on_click(None)
        dialog = self.page.dialogs[-1]
        fields = {c.label: c for c in walk(dialog) if isinstance(c, ft.TextField)}
        save = next(c for c in walk(dialog) if isinstance(c, ft.FilledButton))
        fields["First name"].value, fields["Last name"].value = "Cap", "Tain"
        fields["Max HR override (bpm, optional)"].value = "300"
        save.on_click(None)
        self.assertFalse([c for c in db.list_clients() if c["first_name"] == "Cap"])   # rejected
        fields["Max HR override (bpm, optional)"].value = "168"
        save.on_click(None)
        made = [c for c in db.list_clients() if c["first_name"] == "Cap"]
        self.assertEqual(made[0]["max_hr_manual"], 168)


if __name__ == "__main__":
    unittest.main()
