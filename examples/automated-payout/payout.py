"""Automated payout — OalgoPay demo.

Shows the payouts API with idempotency keys: the same key retried returns
the original result instead of sending twice. Demonstrates the pattern for
automated withdrawals, refunds, or affiliate payouts.

Run: OALGOPAY_PAYOUT_KEY=<key> uv run python payout.py
"""
from __future__ import annotations

import os

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

OALGOPAY = os.environ.get("OALGOPAY_URL", "http://localhost:8096")
PAYOUT_KEY = os.environ.get("OALGOPAY_PAYOUT_KEY", "")

if not PAYOUT_KEY:
    print("Set OALGOPAY_PAYOUT_KEY (admin panel → API keys → payouts)")
    raise SystemExit(1)

app = FastAPI()


async def payouts_api(method: str, path: str, json_body: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.request(
            method,
            f"{OALGOPAY}{path}",
            headers={"Authorization": f"Bearer {PAYOUT_KEY}"},
            json=json_body,
        )
        return {"status": resp.status_code, "body": resp.json()}


@app.get("/")
async def index():
    """Show recent payouts."""
    result = await payouts_api("GET", "/api/v1/payouts?limit=10")
    payouts = result["body"] if isinstance(result["body"], list) else []
    rows = "".join(
        f"<tr><td>{p['id']}</td><td>{p['amount_atomic'] / 1e8:.8f}</td>"
        f"<td>{p['memo'] or '—'}</td>"
        f"<td style='font-family:monospace;font-size:0.8em'>{(p['txid'] or p.get('error') or 'pending')[:24]}…</td></tr>"
        for p in payouts
    )
    return f"""
    <html><body style="font-family:system-ui;max-width:700px;margin:3rem auto;padding:0 1rem">
      <h1>Payout Dashboard</h1>
      <h2>Send a payout</h2>
      <form action="/payout" method="post">
        <input name="address" placeholder="recipient (ULP…)" style="width:100%;padding:0.5rem;margin:0.3rem 0" required>
        <input name="amount" placeholder="amount (BNT)" style="padding:0.5rem;margin:0.3rem 0" required>
        <input name="memo" placeholder="memo (optional)" style="padding:0.5rem;margin:0.3rem 0">
        <input name="idempotency_key" placeholder="idempotency key (e.g. withdraw-123)" style="padding:0.5rem;margin:0.3rem 0;width:100%" required>
        <br><button style="padding:0.6rem 1.5rem;cursor:pointer;margin-top:0.5rem" type="submit">Send</button>
      </form>
      <h2>Recent payouts</h2>
      <table border="1" cellpadding="6" style="border-collapse:collapse;font-size:0.9em">
        <tr><th>#</th><th>Amount</th><th>Memo</th><th>Txid</th></tr>
        {rows or "<tr><td colspan=4>No payouts yet</td></tr>"}
      </table>
    </body></html>
    """


@app.post("/payout")
async def payout(address: str, amount: str, memo: str = "", idempotency_key: str = ""):
    """Create a payout with an idempotency key — retry-safe."""
    body: dict = {"address": address, "amount_bnt": amount}
    if memo:
        body["memo"] = memo
    if idempotency_key:
        body["idempotency_key"] = idempotency_key

    result = await payouts_api("POST", "/api/v1/payouts", body)

    if result["status"] == 201:
        payout_result = result["body"]
        color = "green" if payout_result.get("replayed") else "blue"
        replay_note = (
            " (replayed — original payout returned, no double-send)"
            if payout_result.get("replayed")
            else ""
        )
        return JSONResponse({
            "message": f"txid: {payout_result.get('txid')}{replay_note}",
            "replayed": payout_result.get("replayed"),
            "txid": payout_result.get("txid"),
        })
    return JSONResponse(result, status_code=result["status"])


@app.get("/payout/{payout_id}")
async def get_payout(payout_id: int):
    """Check a specific payout's status."""
    return await payouts_api("GET", f"/api/v1/payouts/{payout_id}")


if __name__ == "__main__":
    import uvicorn

    print(f"Payout demo at http://localhost:5003 (OalgoPay at {OALGOPAY})")
    uvicorn.run(app, host="0.0.0.0", port=5003)
