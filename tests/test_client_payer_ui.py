"""Bill payer dropdown, edit dialog and Sync button, driven through the real screens."""
import asyncio
import os
import unittest
from unittest.mock import patch

import cloud_sync
import db
from tests.test_elite_app import HAVE_FLET, ScreenTestCase, call, walk

if HAVE_FLET:
    import flet as ft


@unittest.skipUnless(HAVE_FLET, "flet not installed")
class PayerUiTests(ScreenTestCase):
    def setUp(self):
        super().setUp()
        self.queued = []
        self.page.run_task = lambda handler, *a, **k: self.queued.append(handler)
        self.sync_calls = []

        def fake_sync():
            self.sync_calls.append(1)
            return cloud_sync.SyncResult(pulled=1, pushed=2)
        patcher = patch.object(cloud_sync, "sync", fake_sync)
        patcher.start()
        self.addCleanup(patcher.stop)
        db.replace_payers([{"id": "cust-jane", "name": "Jane Smith"}, {"id": "cust-bob", "name": "Bob Jones"}])

    def enable_sync(self):
        env = patch.dict(os.environ, {"PT_TRAINEES_TABLE": "pt-trainees"})
        env.start()
        self.addCleanup(env.stop)

    # ---- helpers ----
    def new_client_dialog(self):
        self.button("New client").on_click(None)
        dialog = self.page.dialogs[-1]
        fields = {c.label: c for c in walk(dialog) if isinstance(c, ft.TextField)}
        payer = next(c for c in walk(dialog) if isinstance(c, ft.Dropdown) and c.label == "Bill payer")
        save = next(c for c in walk(dialog) if isinstance(c, ft.FilledButton))
        return fields, payer, save

    def add_client(self, first, last, payer_key=None):
        fields, payer, save = self.new_client_dialog()
        fields["First name"].value, fields["Last name"].value = first, last
        if payer_key:
            payer.value = payer_key
        save.on_click(None)
        return next(c["id"] for c in db.list_clients() if c["first_name"] == first)

    def edit_dialog(self, client_id):
        self.pick(self.dropdown("Select client for session"), client_id)
        self.button("Edit client").on_click(None)
        dialog = self.page.dialogs[-1]
        payer = next(c for c in walk(dialog) if isinstance(c, ft.Dropdown))
        save = next(c for c in walk(dialog) if isinstance(c, ft.FilledButton))
        return payer, save

    def list_subtitle(self, name):
        tile = next(c for c in walk(self.page) if isinstance(c, ft.ListTile)
                    and isinstance(c.title, ft.Text) and c.title.value == name)
        return tile.subtitle.value

    # ---- add form ----
    def test_dropdown_offers_self_pay_then_the_synced_payers(self):
        self.build()
        _, payer, _ = self.new_client_dialog()
        self.assertEqual([(o.key, o.text) for o in payer.options][1:], [("cust-bob", "Bob Jones"), ("cust-jane", "Jane Smith")])
        self.assertEqual(payer.options[0].text, "Pays for themselves")
        self.assertEqual(payer.value, payer.options[0].key)           # default: pays for themselves

    def test_default_is_no_payer_and_a_chosen_payer_is_saved(self):
        self.build()
        cash = self.add_client("Cash", "Client")
        kid = self.add_client("Tom", "Smith", "cust-jane")
        self.assertIsNone(db.get_client(cash)["payer_id"])
        self.assertEqual(db.get_client(kid)["payer_id"], "cust-jane")
        self.assertIn("Bill payer: Jane Smith", self.list_subtitle("Tom Smith"))
        self.assertNotIn("Bill payer", self.list_subtitle("Cash Client"))

    def test_two_children_can_share_one_payer(self):
        self.build()
        a, b = self.add_client("Tom", "Smith", "cust-jane"), self.add_client("Ana", "Smith", "cust-jane")
        self.assertEqual({db.get_client(a)["payer_id"], db.get_client(b)["payer_id"]}, {"cust-jane"})

    def test_form_resets_to_self_pay_after_saving(self):
        self.build()
        self.add_client("Tom", "Smith", "cust-jane")
        _, payer, _ = self.new_client_dialog()
        self.assertEqual(payer.value, payer.options[0].key)

    # ---- edit dialog ----
    def test_edit_links_relinks_and_unlinks(self):
        self.build()
        payer, save = self.edit_dialog(self.ada)
        self.assertEqual(payer.value, payer.options[0].key)           # starts as self-pay
        payer.value = "cust-bob"
        save.on_click(None)
        self.assertEqual(db.get_client(self.ada)["payer_id"], "cust-bob")

        payer, save = self.edit_dialog(self.ada)
        self.assertEqual(payer.value, "cust-bob")                     # pre-filled with the current payer
        payer.value = payer.options[0].key
        save.on_click(None)
        self.assertIsNone(db.get_client(self.ada)["payer_id"])

    # ---- sync wiring ----
    def test_sync_button_is_hidden_until_cloud_sync_is_enabled(self):
        self.build()
        self.assertFalse(self.button("Sync").visible)

    def test_sync_button_runs_a_sync_and_reports(self):
        self.enable_sync()
        self.build()
        self.assertTrue(self.button("Sync").visible)

        async def scenario():
            await call(self.button("Sync").on_click)
        asyncio.run(scenario())
        self.assertEqual(len(self.sync_calls), 1)
        self.assertIn("Synced: 1 down, 2 up.", self.texts())

    def test_startup_edit_and_add_each_queue_a_sync_when_enabled(self):
        self.enable_sync()
        self.build()
        self.assertEqual(len(self.queued), 1)                          # on startup
        self.add_client("Tom", "Smith")
        self.assertEqual(len(self.queued), 2)                          # after adding
        payer, save = self.edit_dialog(self.ada)
        save.on_click(None)
        self.assertEqual(len(self.queued), 3)                          # after editing

    def test_nothing_is_queued_when_sync_is_off(self):
        self.build()
        self.add_client("Tom", "Smith")
        self.assertEqual(self.queued, [])
        self.assertEqual(self.sync_calls, [])

    def test_a_crashing_sync_never_breaks_the_app(self):
        self.enable_sync()
        self.build()

        def boom():
            raise RuntimeError("kaboom")
        with patch.object(cloud_sync, "sync", boom):
            asyncio.run(call(self.button("Sync").on_click))
        self.assertTrue(any(t.startswith("Sync failed: kaboom") for t in self.texts()))
        asyncio.run(call(self.button("Sync").on_click))               # and it can be retried
        self.assertIn("Synced: 1 down, 2 up.", self.texts())


if __name__ == "__main__":
    unittest.main()
