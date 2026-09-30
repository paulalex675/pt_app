"""GET /clients/{client_id}/summary — returns the gold-layer summary JSON
for the dashboard to render. Read-only, no auth yet (see README for the
staged plan on adding client logins).
"""
import json
import os

import boto3

s3 = boto3.client("s3")
BUCKET = os.environ["BUCKET_NAME"]

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",  # tighten to your site's domain once it's live
    "Access-Control-Allow-Methods": "GET,OPTIONS",
    "Content-Type": "application/json",
}


def handler(event, context):
    client_id = event.get("pathParameters", {}).get("client_id")
    if not client_id:
        return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": "client_id required"})}

    key = f"gold/client_id={client_id}/summary.json"
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=key)
        body = obj["Body"].read()
    except s3.exceptions.NoSuchKey:
        return {"statusCode": 404, "headers": CORS_HEADERS, "body": json.dumps({"error": "no sessions yet for this client"})}

    return {"statusCode": 200, "headers": CORS_HEADERS, "body": body.decode("utf-8")}
