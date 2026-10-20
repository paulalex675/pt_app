"""Dry run: propose payer <-> trainee links. READ-ONLY - writes nothing to AWS.

Reads the trainees already in PT Studio (local SQLite) and the payers already
in the invoicing table (CUSTOMER items), then writes link_proposal.json for you
to review and edit. A later step will load only what you approve.

    python tools/propose_links.py --table "$INVOICING_TABLE" --profile pt

Matching is deliberately cautious: a shared surname is a *suggestion*, never a
decision, and anything ambiguous is left for you to assign by hand.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _surname(name: str) -> str:
    parts = re.findall(r"[a-z'\-]+", (name or "").lower())
    return parts[-1] if parts else ""


def propose_links(trainees: list[dict], payers: list[dict]) -> dict:
    """trainees: [{id, first_name, last_name}], payers: [{id, name}] -> proposal dict."""
    by_surname: dict[str, list[dict]] = {}
    for p in payers:
        by_surname.setdefault(_surname(p["name"]), []).append(p)

    rows = []
    for t in trainees:
        candidates = by_surname.get(_surname(t["last_name"]), []) if t["last_name"] else []
        if len(candidates) == 1:
            payer, confidence = candidates[0], "same surname - please confirm"
        elif candidates:
            payer, confidence = None, "several payers share this surname - choose one"
        else:
            payer, confidence = None, "no match - assign by hand (or add the payer first)"
        rows.append({
            "trainee_id": t["id"],
            "trainee_name": f'{t["first_name"]} {t["last_name"]}'.strip(),
            "proposed_payer_id": payer["id"] if payer else None,
            "proposed_payer_name": payer["name"] if payer else None,
            "candidates": [{"id": c["id"], "name": c["name"]} for c in candidates],
            "note": confidence,
            "approved": False,   # you flip this to true; nothing is loaded without it
        })
    linked = {r["proposed_payer_id"] for r in rows if r["proposed_payer_id"]}
    return {
        "links": rows,
        "payers_without_trainees": [p for p in payers if p["id"] not in linked],
    }


def read_payers(table_name: str, profile=None, region="eu-west-2", dynamodb=None) -> list[dict]:
    """Query (not scan) the CUSTOMER partition. Only reads."""
    if dynamodb is None:
        import boto3
        dynamodb = boto3.Session(profile_name=profile, region_name=region).client("dynamodb")
    payers, kwargs = [], dict(
        TableName=table_name,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": {"S": "CUSTOMER"}},
    )
    while True:
        page = dynamodb.query(**kwargs)
        for item in page["Items"]:
            payers.append({"id": item["sk"]["S"], "name": item.get("name", {}).get("S", "")})
        if "LastEvaluatedKey" not in page:
            return payers
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def read_trainees() -> list[dict]:
    import db
    return [{"id": c["id"], "first_name": c["first_name"], "last_name": c["last_name"]}
            for c in db.list_clients()]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--table", required=True, help="invoicing table name")
    ap.add_argument("--profile", default=None, help="AWS profile (e.g. pt)")
    ap.add_argument("--region", default="eu-west-2")
    ap.add_argument("--out", default="link_proposal.json")
    args = ap.parse_args(argv)

    proposal = propose_links(read_trainees(), read_payers(args.table, args.profile, args.region))
    Path(args.out).write_text(json.dumps(proposal, indent=2))
    for r in proposal["links"]:
        print(f'{r["trainee_name"]:<28} -> {r["proposed_payer_name"] or "?":<24} {r["note"]}')
    if proposal["payers_without_trainees"]:
        print("\nPayers with no trainee yet:", ", ".join(p["name"] for p in proposal["payers_without_trainees"]))
    print(f"\nWrote {args.out}. Nothing was written to AWS.")


if __name__ == "__main__":
    main()
