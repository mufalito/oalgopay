"""Digital goods shop — OalgoPay demo.

Customer buys a license key with BNT. The shop creates an invoice, redirects
to the OalgoPay hosted pay page, receives a signed webhook when the payment
confirms, and generates a license key automatically.

Run: OALGOPAY_TOKEN=<merchant key> uv run python shop.py
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

# --- config ------------------------------------------------------------------

OALGOPAY = os.environ.get("OALGOPAY_URL", "http://localhost:8096")
TOKEN = os.environ.get("OALGOPAY_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("OALGOPAY_WEBHOOK_SECRET", "")
PRICE_BNT = "1.0"

if not TOKEN:
    print("Set OALGOPAY_TOKEN to a merchant API key (admin panel → API keys)")
    raise SystemExit(1)

app = FastAPI()
orders: dict[str, dict] = {}  # invoice_id -> {"license_key": str|None, "created": float}


# --- helpers ------------------------------------------------------------------

def verify_webhook(secret: str, header: str, raw: bytes, tolerance: int = 300) -> bool:
    """Verify X-OalgoPay-Signature: t=<unix>,v1=<hex hmac>."""
    parts = dict(p.split("=", 1) for p in header.split(","))
    t, v1 = parts.get("t", ""), parts.get("v1", "")
    if abs(time.time() - int(t or 0)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(v1, expected)


# --- pages ----------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    """Show the product + any fulfilled orders."""
    fulfilled = [
        o for o in orders.values() if o["license_key"]
    ]
    keys_html = "".join(
        f"<li style='color:green;font-family:monospace'>{o['license_key']} (paid ✓)</li>"
        for o in fulfilled
    )
    return f"""
    <html><body style="font-family:system-ui;max-width:600px;margin:4rem auto;padding:0 1rem">
      <h1>Demo Shop</h1>
      <h2>Product: Pro License</h2>
      <p>Price: <strong>{PRICE_BNT} BNT</strong></p>
      <form action="/buy" method="post">
        <button style="padding:0.8rem 2rem;font-size:1.1rem;cursor:pointer"
                type="submit">Buy License — {PRICE_BNT} BNT</button>
      </form>
      <h3>Your licenses</h3>
      <ul>{keys_html or "<li style='color:#888'>No purchases yet</li>"}</ul>
    </body></html>
    """


@app.post("/buy")
async def buy():
    """Create an OalgoPay invoice and redirect to the hosted pay page."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{OALGOPAY}/api/v1/invoices",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "amount_bnt": PRICE_BNT,
                "label": "Pro License",
                "metadata": {"product": "pro-license"},
            },
        )
        if resp.status_code != 201:
            raise HTTPException(502, f"OalgoPay error: {resp.text[:200]}")
        invoice = resp.json()
    orders[invoice["id"]] = {"license_key": None, "created": time.time()}
    return RedirectResponse(invoice["pay_url"], status_code=302)


# --- webhook receiver -----------------------------------------------------------

@app.post("/webhook")
async def webhook(request: Request):
    """Receive signed OalgoPay webhooks; fulfill on invoice.confirmed."""
    raw = await request.body()
    sig = request.headers.get("x-oalgopay-signature", "")

    if WEBHOOK_SECRET:
        if not verify_webhook(WEBHOOK_SECRET, sig, raw):
            raise HTTPException(401, "invalid signature")

    import json

    payload = json.loads(raw)
    event = payload.get("event")
    invoice = payload.get("invoice") or {}
    inv_id = invoice.get("id")

    if event == "invoice.confirmed" and inv_id and inv_id in orders:
        if not orders[inv_id]["license_key"]:
            orders[inv_id]["license_key"] = f"LICENSE-{secrets.token_hex(8).upper()}"
            print(f"[shop] fulfilled {inv_id} → {orders[inv_id]['license_key']}")

    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    if not WEBHOOK_SECRET:
        print("Tip: set OALGOPAY_WEBHOOK_SECRET to verify webhook signatures")
    print(f"Shop running at http://localhost:5001 (OalgoPay at {OALGOPAY})")
    uvicorn.run(app, host="0.0.0.0", port=5001)
