"""API credit top-up — OalgoPay demo.

Users buy API credits with BNT. A webhook credits their account when the
payment confirms. Demonstrates: merchant API integration, webhook-driven
state changes, metadata for user attribution.

Run: OALGOPAY_TOKEN=<key> uv run python credits.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Dict

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

OALGOPAY = os.environ.get("OALGOPAY_URL", "http://localhost:8096")
TOKEN = os.environ.get("OALGOPAY_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("OALGOPAY_WEBHOOK_SECRET", "")
CREDIT_PRICE_BNT = "0.5"
CREDITS_PER_PURCHASE = 100

if not TOKEN:
    print("Set OALGOPAY_TOKEN to a merchant API key")
    raise SystemExit(1)

app = FastAPI()

# in-memory "database": username -> {"credits": int, "invoices": dict}
accounts: Dict[str, dict] = {}


def _get_account(username: str) -> dict:
    if username not in accounts:
        accounts[username] = {"credits": 0, "invoices": {}}
    return accounts[username]


def _verify(sig_header: str, raw: bytes) -> bool:
    parts = dict(p.split("=", 1) for p in sig_header.split(","))
    t, v1 = parts.get("t", ""), parts.get("v1", "")
    if abs(time.time() - int(t or 0)) > 300:
        return False
    expected = hmac.new(
        WEBHOOK_SECRET.encode(), f"{t}.".encode() + raw, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(v1, expected)


@app.get("/account/{username}", response_class=HTMLResponse)
async def balance_page(username: str):
    acct = _get_account(username)
    return f"""
    <html><body style="font-family:system-ui;max-width:500px;margin:4rem auto;padding:0 1rem">
      <h1>{username}</h1>
      <p>Credits: <strong style="font-size:1.5rem">{acct["credits"]}</strong></p>
      <form action="/account/{username}/topup" method="post">
        <button style="padding:0.6rem 1.5rem;cursor:pointer" type="submit">
          Add {CREDITS_PER_PURCHASE} credits — {CREDIT_PRICE_BNT} BNT
        </button>
      </form>
      <form action="/account/{username}/use" method="post" style="margin-top:0.5rem">
        <button style="padding:0.6rem 1.5rem;cursor:pointer" type="submit">Use 1 credit</button>
      </form>
    </body></html>
    """


@app.post("/account/{username}/topup")
async def topup(username: str):
    acct = _get_account(username)
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{OALGOPAY}/api/v1/invoices",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "amount_bnt": CREDIT_PRICE_BNT,
                "label": f"credits:{username}",
                "metadata": {"username": username, "credits": CREDITS_PER_PURCHASE},
            },
        )
        if resp.status_code != 201:
            raise HTTPException(502, f"OalgoPay: {resp.text[:200]}")
        invoice = resp.json()
    acct["invoices"][invoice["id"]] = {"status": "pending"}
    return RedirectResponse(invoice["pay_url"], status_code=302)


@app.post("/account/{username}/use")
async def use_credit(username: str):
    acct = _get_account(username)
    if acct["credits"] < 1:
        return HTMLResponse(
            f"<p>No credits left. <a href='/account/{username}'>Top up</a></p>"
        )
    acct["credits"] -= 1
    return RedirectResponse(f"/account/{username}", status_code=302)


@app.post("/webhook")
async def webhook(request: Request):
    raw = await request.body()
    if WEBHOOK_SECRET:
        sig = request.headers.get("x-oalgopay-signature", "")
        if not _verify(sig, raw):
            raise HTTPException(401, "invalid signature")

    payload = json.loads(raw)
    event = payload.get("event")
    invoice = payload.get("invoice") or {}
    meta = invoice.get("metadata") or {}
    username = meta.get("username")

    if event == "invoice.confirmed" and username:
        acct = _get_account(username)
        credits = int(meta.get("credits", 0))
        acct["credits"] += credits
        print(f"[credits] {username} +{credits} (invoice {invoice.get('id')})")

    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    print(f"Credit service at http://localhost:5002 (OalgoPay at {OALGOPAY})")
    print(f"Try: http://localhost:5002/account/alice")
    uvicorn.run(app, host="0.0.0.0", port=5002)
