"""One-shot payment diagnostics for the VPS.

Run with the venv python from the repo root:

    /home/ubuntu/bondom_account/.venv/bin/python \
        /home/ubuntu/bondom_account/scripts/vps_payment_diagnose.py

Prints, in order:
  1. Effective payment config (.env) with the token decoded (issued/expiry).
  2. This server's public IP and country (Bakong blocks non-Cambodia IPs).
  3. The latest rows from the payments / wallet_topups tables.
  4. Raw Bakong API responses for the latest md5 hashes.
  5. A one-line VERDICT naming the root cause.
"""

import base64
import http.client
import json
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared.config import settings  # noqa: E402  (needs sys.path above)

BAKONG_HOST = "api-bakong.nbc.gov.kh"


def section(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


def decode_jwt(token: str) -> dict | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


def raw_check(md5: str) -> tuple[int, dict | str]:
    conn = http.client.HTTPSConnection(BAKONG_HOST, timeout=15)
    try:
        conn.request(
            "POST",
            "/v1/check_transaction_by_md5",
            body=json.dumps({"md5": md5}),
            headers={
                "Authorization": f"Bearer {settings.bakong_token}",
                "Content-Type": "application/json",
            },
        )
        resp = conn.getresponse()
        body = resp.read().decode()
    finally:
        conn.close()
    try:
        return resp.status, json.loads(body)
    except json.JSONDecodeError:
        return resp.status, body[:300]


def main() -> None:
    now = datetime.now(timezone.utc)
    verdicts: list[str] = []

    section("1. Config")
    print(f"PAYMENT_DEV_MODE  = {settings.payment_dev_mode}")
    print(f"BAKONG_ACCOUNT_ID = {settings.bakong_account_id}")
    token = settings.bakong_token or ""
    print(f"BAKONG_TOKEN      = {token[:12]}...{token[-6:]}" if token else "BAKONG_TOKEN      = (MISSING)")
    if settings.payment_dev_mode:
        verdicts.append("PAYMENT_DEV_MODE=true — real Bakong is never called!")
    if not token:
        verdicts.append("BAKONG_TOKEN missing from .env")
    claims = decode_jwt(token) if token else None
    if claims:
        iat = datetime.fromtimestamp(claims.get("iat", 0), timezone.utc)
        exp = datetime.fromtimestamp(claims.get("exp", 0), timezone.utc)
        print(f"token issued      = {iat}   token expires = {exp}")
        print(f"now (UTC)         = {now}")
        if exp < now:
            verdicts.append(
                f"TOKEN EXPIRED on {exp:%Y-%m-%d} — renew at Bakong Developer "
                "and update BAKONG_TOKEN in .env"
            )
    elif token:
        print("token is not a decodable JWT (unexpected format)")

    section("2. Server public IP")
    try:
        with urllib.request.urlopen("https://ipinfo.io/json", timeout=10) as r:
            info = json.loads(r.read().decode())
        print(f"ip={info.get('ip')} country={info.get('country')} org={info.get('org')}")
        if info.get("country") and info["country"] != "KH":
            print("NOTE: server is outside Cambodia — Bakong may block it (HTTP 403).")
    except Exception as exc:
        print(f"could not determine public IP: {exc}")

    section("3. Latest DB rows (store.db)")
    db_path = REPO_ROOT / "store.db"
    md5s: list[tuple[str, str, str]] = []  # (source, md5, status)
    if not db_path.exists():
        print(f"store.db not found at {db_path}")
    else:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        for table in ("payments", "wallet_topups"):
            try:
                rows = con.execute(
                    f"SELECT * FROM {table} ORDER BY id DESC LIMIT 5"
                ).fetchall()
            except sqlite3.Error as exc:
                print(f"{table}: {exc}")
                continue
            print(f"-- {table} --")
            for row in rows:
                d = dict(row)
                d.pop("qr_string", None)
                print(f"  {d}")
                md5s.append((f"{table}#{d['id']}", d["md5"], d["status"]))
        con.close()

    section("4. Raw Bakong checks")
    checked_verdict_done = False
    for label, md5, db_status in md5s[:4]:
        try:
            status, body = raw_check(md5)
        except Exception as exc:
            print(f"{label} md5={md5}: REQUEST FAILED: {exc}")
            verdicts.append(f"Cannot reach Bakong API from this server: {exc}")
            break
        print(f"{label} db_status={db_status} md5={md5}")
        print(f"  HTTP {status}: {body}")
        if checked_verdict_done:
            continue
        checked_verdict_done = True  # verdict from the newest md5 only
        if status == 401 or (isinstance(body, dict) and body.get("errorCode") == 6):
            verdicts.append(
                "TOKEN REJECTED (unauthorized) — BAKONG_TOKEN on this server "
                "is stale/expired. Copy the freshly renewed token into .env."
            )
        elif status == 403:
            verdicts.append(
                "IP BLOCKED (HTTP 403) — Bakong only accepts Cambodia IPs; "
                "this VPS cannot call the Bakong API directly."
            )
        elif isinstance(body, dict) and body.get("responseCode") == 0:
            verdicts.append(
                "Bakong says PAID for the newest md5 — Bakong side is fine; "
                "if the bot still says unpaid, the app was checking a "
                "different/older payment session."
            )
        elif isinstance(body, dict) and body.get("errorCode") == 1:
            verdicts.append(
                "Transaction not found for the newest md5 — token and IP are "
                "fine; the payer has not paid THIS exact QR (wrong QR, "
                "expired QR re-scan, or paid a different session)."
            )
    if not md5s:
        print("no md5 hashes found in DB to check")

    section("VERDICT")
    if verdicts:
        for v in verdicts:
            print(f"* {v}")
    else:
        print("* No blocking problem detected in config/token/IP/API.")


if __name__ == "__main__":
    main()
