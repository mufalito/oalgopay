#!/usr/bin/env python3
"""Real-money e2e verification of OalgoPay.

Prerequisites (runtime state, not managed by the gateway):
  - Blocknet daemon running with --api 127.0.0.1:18512 (api.cookie in datadir)
  - Shop wallet loaded AND unlocked in the daemon (the GUI normally owns this)
  - Gateway running (uv run run.py)

What it does:
  1. Starts a local webhook receiver on 127.0.0.1:8977.
  2. Sets confirmations_required=1 via the admin API (restores the old value on exit).
  3. Registers the receiver as a webhook endpoint (secret shown once -> used for HMAC).
  4. Creates a 0.1 BNT invoice via the merchant API.
  5. Sends a REAL 0.1 BNT self-payment via the daemon API (memo_text = invoice id).
  6. Waits for status: pending -> seen (mempool) -> confirmed (1 conf, ~5 min block).
  7. Verifies the receiver got payment.seen + invoice.confirmed with a valid
     X-OalgoPay-Signature (HMAC-SHA256 recomputed over "<t>." + raw body).

Run: uv run python scripts/e2e_test.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402

GATEWAY = "http://127.0.0.1:8096"
RECEIVER_PORT = 8977
AMOUNT_BNT = "0.1"
SEEN_TIMEOUT = 8 * 60      # this core reveals sent-to-self outputs once mined (~5 min block)
CONFIRM_TIMEOUT = 20 * 60  # ~5 min block time + poll interval, generous


class Receiver(BaseHTTPRequestHandler):
    hits: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        Receiver.hits.append(
            {
                "path": self.path,
                "headers": {k: v for k, v in self.headers.items()},
                "raw": raw,
            }
        )
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # silence
        pass


def step(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"FATAL: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def daemon_cookie(cfg: dict) -> str:
    return (Path(cfg["datadir"]) / "api.cookie").read_text().strip()


def verify_signature(secret: str, header: str, raw: bytes) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    t, v1 = parts.get("t", ""), parts.get("v1", "")
    expected = hmac.new(secret.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(v1, expected)


def main() -> None:
    cfg = load_config(ROOT / "config.json")
    token = cfg["api_token"]
    admin_pw = cfg["admin_password"]

    # --- webhook receiver ---------------------------------------------------
    server = ThreadingHTTPServer(("127.0.0.1", RECEIVER_PORT), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    step(f"webhook receiver listening on 127.0.0.1:{RECEIVER_PORT}")

    gw = httpx.Client(base_url=GATEWAY, timeout=15.0)
    restored = {"key": None}

    try:
        # --- admin session + settings ---------------------------------------
        r = gw.post("/admin/login", json={"password": admin_pw})
        r.raise_for_status()
        step("admin session established")

        old = gw.get("/admin/api/settings").raise_for_status().json()
        restored["key"] = old["confirmations_required"]
        gw.patch(
            "/admin/api/settings", json={"confirmations_required": 1}
        ).raise_for_status()
        step(f"confirmations_required: {restored['key']} -> 1 (restored on exit)")

        # --- webhook endpoint ------------------------------------------------
        r = gw.post(
            "/admin/api/webhooks", json={"url": f"http://127.0.0.1:{RECEIVER_PORT}/hook"}
        )
        r.raise_for_status()
        ep = r.json()
        secret = ep["secret"]
        step(f"webhook endpoint #{ep['id']} registered (secret {secret[:8]}…)")

        # --- invoice ---------------------------------------------------------
        r = gw.post(
            "/api/v1/invoices",
            headers={"Authorization": f"Bearer {token}"},
            json={"amount_bnt": AMOUNT_BNT, "label": "e2e", "metadata": {"test": True}},
        )
        r.raise_for_status()
        inv = r.json()
        inv_id = inv["id"]
        step(f"invoice {inv_id} created for {AMOUNT_BNT} BNT")
        print(f"        uri: {inv['uri']}")

        # --- real send via daemon ---------------------------------------------
        cookie = daemon_cookie(cfg)
        d = httpx.Client(base_url=cfg["api_base"], timeout=30.0)
        addr = d.get("/api/wallet/address", headers={"Authorization": f"Bearer {cookie}"}).json()
        if isinstance(addr, dict):
            addr = addr.get("address") or addr.get("result")
        step(f"shop wallet address: {addr[:24]}…")
        amount_atomic = int(float(AMOUNT_BNT) * 1e8)
        r = d.post(
            "/api/wallet/send",
            headers={
                "Authorization": f"Bearer {cookie}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
            json={
                "recipients": [
                    {
                        "address": addr,
                        "amount": amount_atomic,
                        "memo_text": inv_id,
                    }
                ]
            },
        )
        if r.status_code >= 400:
            die(f"/api/wallet/send failed: {r.status_code} {r.text[:300]}")
        sent = r.json()
        txid = sent.get("txid")
        step(f"SENT {AMOUNT_BNT} BNT txid={txid} fee={sent.get('fee')}")
        # --- wait for seen ----------------------------------------------------
        deadline = time.time() + SEEN_TIMEOUT
        status = None
        while time.time() < deadline:
            try:
                status = gw.get(f"/pay/{inv_id}/status").json()
            except httpx.HTTPError:
                time.sleep(3)  # gateway restarting — keep waiting
                continue
            if status["status"] != "pending":
                break
            time.sleep(3)
        if status is None or status["status"] == "pending":
            die(f"invoice never left pending within {SEEN_TIMEOUT}s (last: {status})")
        step(f"status: {status['status']} (paid {status['paid_bnt']} BNT, confs {status['confirmations']})")
        if status["status"] != "seen":
            print(f"NOTE: jumped straight to {status['status']} (block may have landed fast)")

        # --- wait for confirmed ------------------------------------------------
        deadline = time.time() + CONFIRM_TIMEOUT
        while time.time() < deadline:
            try:
                status = gw.get(f"/pay/{inv_id}/status").json()
            except httpx.HTTPError:
                time.sleep(5)
                continue
            if status["status"] == "confirmed":
                break
            time.sleep(5)
        if status["status"] != "confirmed":
            die(f"invoice not confirmed within {CONFIRM_TIMEOUT}s (last: {status})")
        step(f"CONFIRMED: paid {status['paid_bnt']} BNT, confs {status['confirmations']}")

        # --- webhook assertions -------------------------------------------------
        deadline = time.time() + 60
        while time.time() < deadline and len(Receiver.hits) < 2:
            time.sleep(2)
        events = []
        for hit in Receiver.hits:
            sig_ok = verify_signature(secret, hit["headers"].get("X-OalgoPay-Signature", ""), hit["raw"])
            payload = json.loads(hit["raw"])
            events.append((payload.get("event"), sig_ok))
            assert sig_ok, f"bad signature for {payload.get('event')}"
            assert hit["headers"].get("X-OalgoPay-Event") == payload.get("event")
            if payload.get("invoice"):
                assert payload["invoice"]["id"] == inv_id, "webhook carries foreign invoice"
        got = {e for e, _ in events}
        assert "payment.seen" in got, f"payment.seen missing (got {got})"
        assert "invoice.confirmed" in got, f"invoice.confirmed missing (got {got})"
        step(f"webhooks verified: {sorted(got)} — all HMAC signatures valid")

        print("\nE2E PASS ✓  invoice", inv_id, "seen + confirmed; webhooks signed and delivered")
    finally:
        try:
            if restored["key"] is not None:
                gw.patch(
                    "/admin/api/settings", json={"confirmations_required": int(restored["key"])}
                )
                step(f"confirmations_required restored to {restored['key']}")
        except Exception as exc:
            print(f"WARNING: could not restore settings: {exc}")
        try:
            if ep:
                gw.delete(f"/admin/api/webhooks/{ep['id']}")
                step(f"webhook endpoint #{ep['id']} removed")
        except Exception as exc:
            print(f"WARNING: could not remove webhook endpoint: {exc}")
        server.shutdown()


if __name__ == "__main__":
    main()
