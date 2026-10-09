"""Post the sample events to a running instance and print the triage results.

    python scripts/send_sample.py [--url http://localhost:8000] [--secret S]
"""
import argparse
import hashlib
import hmac
import json
import time
from pathlib import Path

import httpx

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "events.json"


def post(client: httpx.Client, url: str, body: bytes, secret: str | None) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Signature-256"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(f"{url}/webhook/telemetry", content=body, headers=headers)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--secret", default=None)
    args = ap.parse_args()

    with httpx.Client(timeout=120) as client:
        ids = []
        for event in json.loads(SAMPLES.read_text(encoding="utf-8")):
            r = post(client, args.url, json.dumps(event).encode(), args.secret)
            print(f"POST -> {r.status_code} {r.json()}")
            if r.status_code == 202:
                ids.append(r.json()["event_id"])
        for event_id in ids:
            for _ in range(240):
                data = client.get(f"{args.url}/events/{event_id}").json()
                if data["status"] in ("done", "failed"):
                    break
                time.sleep(0.5)
            print("\n" + json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
