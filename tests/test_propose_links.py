import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-2")

import propose_links as pl

try:
    import boto3
    from moto import mock_aws
    HAVE_MOTO = True
except ImportError:
    HAVE_MOTO = False

PAYERS = [{"id": "c1", "name": "Jane Smith"}, {"id": "c2", "name": "Bob Jones"},
          {"id": "c3", "name": "Amy Jones"}, {"id": "c4", "name": "Nobody Linked"}]


def trainee(i, first, last):
    return {"id": i, "first_name": first, "last_name": last}


class ProposeTests(unittest.TestCase):
    def test_unique_surname_is_suggested_but_never_pre_approved(self):
        r = pl.propose_links([trainee("t1", "Tom", "Smith")], PAYERS)["links"][0]
        self.assertEqual(r["proposed_payer_id"], "c1")
        self.assertFalse(r["approved"])

    def test_two_children_can_share_one_payer(self):
        rows = pl.propose_links([trainee("t1", "Tom", "Smith"), trainee("t2", "Ana", "Smith")], PAYERS)["links"]
        self.assertEqual({r["proposed_payer_id"] for r in rows}, {"c1"})

    def test_ambiguous_surname_is_not_guessed(self):
        r = pl.propose_links([trainee("t1", "Sam", "Jones")], PAYERS)["links"][0]
        self.assertIsNone(r["proposed_payer_id"])
        self.assertEqual(len(r["candidates"]), 2)

    def test_no_match_and_unlinked_payers_are_reported(self):
        out = pl.propose_links([trainee("t1", "Sam", "Wright")], PAYERS)
        self.assertIsNone(out["links"][0]["proposed_payer_id"])
        self.assertEqual({p["id"] for p in out["payers_without_trainees"]}, {"c1", "c2", "c3", "c4"})

    def test_blank_surname_never_matches(self):
        r = pl.propose_links([trainee("t1", "Cher", "")], [{"id": "x", "name": ""}])["links"][0]
        self.assertIsNone(r["proposed_payer_id"])


@unittest.skipUnless(HAVE_MOTO, "moto not installed")
class ReadPayersTests(unittest.TestCase):
    def test_reads_only_customer_items_and_pages_through_all(self):
        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="eu-west-2")
            ddb.create_table(TableName="inv", BillingMode="PAY_PER_REQUEST",
                             AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                                   {"AttributeName": "sk", "AttributeType": "S"}],
                             KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                                        {"AttributeName": "sk", "KeyType": "RANGE"}])
            for i in range(3):
                ddb.put_item(TableName="inv", Item={"pk": {"S": "CUSTOMER"}, "sk": {"S": f"c{i}"},
                                                    "name": {"S": f"Name {i}"}})
            ddb.put_item(TableName="inv", Item={"pk": {"S": "INVOICE"}, "sk": {"S": "INV-1"}})
            payers = pl.read_payers("inv", dynamodb=ddb)
            self.assertEqual(sorted(p["id"] for p in payers), ["c0", "c1", "c2"])


if __name__ == "__main__":
    unittest.main()
