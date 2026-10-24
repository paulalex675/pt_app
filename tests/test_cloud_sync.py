"""Sync between the local client list and the cloud tables, with two 'machines'."""
import itertools
import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import cloud_sync
import db

os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-2")

try:
    import boto3
    from botocore.exceptions import EndpointConnectionError, ClientError
    from moto import mock_aws
    HAVE_MOTO = True
except ImportError:
    HAVE_MOTO = False


def make_tables(ddb):
    ddb.create_table(
        TableName="pt-trainees", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "trainee_id", "AttributeType": "S"},
                              {"AttributeName": "payer_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "trainee_id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[{"IndexName": "by_payer",
                                 "KeySchema": [{"AttributeName": "payer_id", "KeyType": "HASH"},
                                               {"AttributeName": "trainee_id", "KeyType": "RANGE"}],
                                 "Projection": {"ProjectionType": "ALL"}}])
    ddb.create_table(
        TableName="pt-health-profiles", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "trainee_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "trainee_id", "KeyType": "HASH"}])
    ddb.create_table(
        TableName="pt-growth-measurements", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "trainee_id", "AttributeType": "S"},
                              {"AttributeName": "sk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "trainee_id", "KeyType": "HASH"},
                   {"AttributeName": "sk", "KeyType": "RANGE"}])
    inv = ddb.create_table(
        TableName="invoicing", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                              {"AttributeName": "sk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}])
    for cid, name in (("cust-jane", "Jane Smith"), ("cust-bob", "Bob Jones")):
        inv.put_item(Item={"pk": "CUSTOMER", "sk": cid, "name": name, "email": f"{cid}@x.com"})
    inv.put_item(Item={"pk": "INVOICE", "sk": "INV-1", "total": 40, "status": "sent"})
    inv.put_item(Item={"pk": "COUNTER", "sk": "INVOICE", "n": 1})


@unittest.skipUnless(HAVE_MOTO, "moto not installed")
class SyncTestCase(unittest.TestCase):
    def setUp(self):
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.ddb = boto3.resource("dynamodb", region_name="eu-west-2")
        make_tables(self.ddb)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ticks = itertools.count(1)
        clock = patch.object(db, "_now", lambda: f"2026-10-20T10:00:00.{next(self.ticks):06d}")
        clock.start()
        self.addCleanup(clock.stop)
        env = patch.dict(os.environ, {"PT_TRAINEES_TABLE": "pt-trainees", "PT_INVOICING_TABLE": "invoicing"})
        env.start()
        self.addCleanup(env.stop)

    @contextmanager
    def machine(self, name):
        with patch.object(db, "DB_PATH", Path(self.tmp.name) / f"{name}.db"):
            db.init_db()
            yield

    def sync(self):
        return cloud_sync.sync(self.ddb)

    def cloud(self, table, cid):
        return self.ddb.Table(table).get_item(Key={"trainee_id": cid}).get("Item")


class PushPullTests(SyncTestCase):
    def test_new_client_is_pushed_and_split_across_the_two_tables(self):
        with self.machine("a"):
            cid = db.create_client("Ada", "North", "female", "1990-06-15", 168.5, 62, 55, max_hr_manual=170)
            r = self.sync()
            self.assertTrue(r.ok, r.summary())
            self.assertEqual(r.pushed, 1)
            trainee, health = self.cloud("pt-trainees", cid), self.cloud("pt-health-profiles", cid)
            self.assertEqual((trainee["first_name"], trainee["last_name"]), ("Ada", "North"))
            self.assertNotIn("payer_id", trainee)                       # blank = pays for themselves
            for private in ("dob", "sex", "height_cm", "weight_kg", "resting_hr", "max_hr_manual"):
                self.assertNotIn(private, trainee)                       # health data only in its own table
            self.assertEqual((health["dob"], health["max_hr_manual"], health["resting_hr"]), ("1990-06-15", 170, 55))
            self.assertEqual(float(health["height_cm"]), 168.5)
            self.assertNotIn("first_name", health)
            self.assertEqual(self.sync().pushed, 0)                      # now clean: nothing re-sent

    def test_second_machine_receives_everything(self):
        with self.machine("a"):
            cid = db.create_client("Ada", "North", "female", "1990-06-15", 168.5, 62, 55, max_hr_manual=170)
            self.sync()
        with self.machine("b"):
            r = self.sync()
            self.assertEqual(r.pulled, 1)
            row = db.get_client(cid)
            self.assertEqual((row["first_name"], row["sex"], row["dob"], row["resting_hr"]), ("Ada", "female", "1990-06-15", 55))
            self.assertEqual((row["height_cm"], row["max_hr_manual"]), (168.5, 170))
            self.assertEqual(self.sync().pushed, 0)                      # pulled rows are not pushed back

    def test_newer_edit_on_another_machine_wins(self):
        with self.machine("a"):
            cid = db.create_client("Ada", "North", "female", "1990-06-15", 168, 62, 55)
            self.sync()
        with self.machine("b"):
            self.sync()
            db.update_client_resting_hr(cid, 48)
            self.sync()
        with self.machine("a"):
            r = self.sync()
            self.assertEqual(db.get_client(cid)["resting_hr"], 48)
            self.assertEqual((r.pulled, r.conflicts), (1, 0))

    def test_clearing_the_manual_max_reaches_the_cloud(self):
        with self.machine("a"):
            cid = db.create_client("Ada", "North", max_hr_manual=170)
            self.sync()
            db.set_client_max_hr_manual(cid, None)
            self.sync()
            self.assertNotIn("max_hr_manual", self.cloud("pt-health-profiles", cid))

    def test_a_losing_local_edit_is_counted_not_hidden(self):
        with self.machine("a"):
            cid = db.create_client("Ada", "North", "female", "1990-06-15", 168, 62, 55)
            self.sync()
        with self.machine("b"):
            self.sync()
            db.update_client_resting_hr(cid, 48)
            self.sync()                                                  # B's edit reaches the cloud
        with self.machine("a"):
            with patch.object(db, "_now", lambda: "2026-10-20T10:00:00.0000015"):   # after A's last sync, before B's edit
                db.update_client_resting_hr(cid, 60)
            r = self.sync()
            self.assertEqual(r.conflicts, 1)
            self.assertEqual(db.get_client(cid)["resting_hr"], 48)

    def test_push_never_overwrites_a_newer_cloud_record(self):
        with self.machine("a"):
            cid = db.create_client("Ada", "North", resting_hr=55)
            self.sync()
            db.update_client_resting_hr(cid, 60)                         # local edit...
            self.ddb.Table("pt-trainees").update_item(                   # ...but cloud moved on meanwhile
                Key={"trainee_id": cid}, UpdateExpression="SET updated_at = :u",
                ExpressionAttributeValues={":u": "2099-01-01T00:00:00.000000"})
            result = cloud_sync.SyncResult()
            cloud_sync._push(self.ddb.Table("pt-trainees"), self.ddb.Table("pt-health-profiles"), None, result)
            self.assertEqual((result.pushed, result.conflicts), (0, 1))
            self.assertEqual(int(self.cloud("pt-health-profiles", cid)["resting_hr"]), 55)
            self.assertEqual(len(db.dirty_clients()), 1)                  # still pending

    def test_learned_max_is_never_lowered_by_a_pull(self):
        with self.machine("a"):
            cid = db.create_client("Ada", "North", "female", "1990-06-15")
            self.sync()
            db.update_client_max_hr(cid, 196)                            # learned in a session, not yet synced
        with self.machine("b"):
            self.sync()
            db.update_client_resting_hr(cid, 50)                         # newer unrelated edit
            self.sync()
        with self.machine("a"):
            self.sync()
            self.assertEqual(db.get_client(cid)["max_hr"], 196)
            self.assertEqual(int(self.cloud("pt-health-profiles", cid)["max_hr_observed"]), 196)


class PayerTests(SyncTestCase):
    def test_payer_list_comes_from_customers_only(self):
        with self.machine("a"):
            r = self.sync()
            self.assertEqual(r.payers, 2)
            self.assertEqual([p["name"] for p in db.list_payers()], ["Bob Jones", "Jane Smith"])

    def test_one_payer_can_have_several_trainees_and_the_index_finds_them(self):
        with self.machine("a"):
            self.sync()
            t1 = db.create_client("Tom", "Smith", payer_id="cust-jane")
            t2 = db.create_client("Ana", "Smith", payer_id="cust-jane")
            db.create_client("Cash", "Client")
            r = self.sync()
            self.assertTrue(r.ok, r.summary())
            from boto3.dynamodb.conditions import Key
            found = self.ddb.Table("pt-trainees").query(
                IndexName="by_payer", KeyConditionExpression=Key("payer_id").eq("cust-jane"))["Items"]
            self.assertEqual({i["trainee_id"] for i in found}, {t1, t2})

    def test_a_payer_that_does_not_exist_is_refused_with_a_message(self):
        with self.machine("a"):
            cid = db.create_client("Tom", "Smith", payer_id="cust-ghost")
            r = self.sync()
            self.assertEqual(r.pushed, 0)
            self.assertIn("payer no longer exists", r.summary())
            self.assertIsNone(self.cloud("pt-trainees", cid))

    def test_sync_never_writes_to_the_invoicing_table(self):
        before = self.ddb.Table("invoicing").scan()["Items"]
        with self.machine("a"):
            db.create_client("Tom", "Smith", payer_id="cust-jane")
            self.sync()
            self.sync()
        key = lambda i: (i["pk"], i["sk"])
        self.assertEqual(sorted(self.ddb.Table("invoicing").scan()["Items"], key=key), sorted(before, key=key))


class FailureTests(SyncTestCase):
    def test_offline_leaves_local_data_alone_and_says_so(self):
        class Offline:
            def Table(self, name):
                raise EndpointConnectionError(endpoint_url="https://dynamodb.eu-west-2.amazonaws.com")
        with self.machine("a"):
            cid = db.create_client("Ada", "North")
            r = cloud_sync.sync(Offline())
            self.assertEqual(r.error, "No internet connection.")
            self.assertIn("working offline", r.summary())
            self.assertEqual(len(db.dirty_clients()), 1)                  # still queued for next time
            self.assertIsNotNone(db.get_client(cid))

    def test_expired_sso_gives_the_login_command(self):
        err = ClientError({"Error": {"Code": "ExpiredToken", "Message": "token expired"}}, "Scan")
        self.assertIn("aws sso login --profile pt", cloud_sync.friendly_error(err))

    def test_missing_table_is_explained(self):
        err = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, "Scan")
        self.assertIn("clients stack", cloud_sync.friendly_error(err))

    def test_sync_is_off_until_opted_in(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PT_TRAINEES_TABLE", None)
            r = cloud_sync.sync()
            self.assertFalse(r.enabled)
            self.assertIn("off", r.summary())


class ErasureTests(SyncTestCase):
    def test_delete_trainee_removes_both_tables(self):
        with self.machine("a"):
            cid = db.create_client("Ada", "North", "female", "1990-06-15")
            self.sync()
            cloud_sync.delete_trainee(cid, self.ddb)
            self.assertIsNone(self.cloud("pt-trainees", cid))
            self.assertIsNone(self.cloud("pt-health-profiles", cid))

    def test_delete_trainee_removes_junior_growth_data(self):
        with self.machine("a"):
            cid = db.create_client("Sam", "Young", "male", "2013-05-01")
            self.sync()
            growth = self.ddb.Table("pt-growth-measurements")
            for sk in ("PROFILE", "2026-09-20#a", "2026-10-20#b"):
                growth.put_item(Item={"trainee_id": cid, "sk": sk})
            growth.put_item(Item={"trainee_id": "someone-else", "sk": "PROFILE"})
            cloud_sync.delete_trainee(cid, self.ddb)
            from boto3.dynamodb.conditions import Key
            self.assertEqual(growth.query(KeyConditionExpression=Key("trainee_id").eq(cid))["Count"], 0)
            self.assertEqual(growth.scan()["Count"], 1)


if __name__ == "__main__":
    unittest.main()
