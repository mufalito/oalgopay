# OalgoPay Examples

Three demos that exercise the full API surface. Each is a standalone
FastAPI app (~100 lines) that talks to a running OalgoPay gateway.

| Demo | What it shows | Port |
|---|---|---|
| [digital-shop](digital-shop/) | Full lifecycle: invoice → pay page → webhook → automated license-key delivery | 5001 |
| [credit-topup](credit-topup/) | SaaS pattern: buy API credits, webhook adds balance, spend credits | 5002 |
| [automated-payout](automated-payout/) | Payouts API with idempotency keys — retry-safe, never double-pays | 5003 |

## Prerequisites

1. OalgoPay running on `http://localhost:8096` with a loaded wallet
2. API keys from the admin panel (**API keys** tab):
   - a `merchant` key for demos 1-2 (set as `OALGOPAY_TOKEN`)
   - a `payouts` key for demo 3 (set as `OALGOPAY_PAYOUT_KEY`)
3. For webhook demos (1-2): register your demo's `/webhook` URL as a
   webhook endpoint in the OalgoPay admin panel (**Webhooks** tab) and set
   `OALGOPAY_WEBHOOK_SECRET` to the signing secret shown at creation

## Quick start

```bash
# from the repo root — each demo is self-contained, run in separate terminals

cd examples/digital-shop
OALGOPAY_TOKEN=<merchant-key> \
  uv run --no-project --with fastapi --with uvicorn --with httpx python shop.py

cd examples/credit-topup
OALGOPAY_TOKEN=<merchant-key> OALGOPAY_WEBHOOK_SECRET=<secret> \
  uv run --no-project --with fastapi --with uvicorn --with httpx python credits.py

cd examples/automated-payout
OALGOPAY_PAYOUT_KEY=<payouts-key> \
  uv run --no-project --with fastapi --with uvicorn --with httpx python payout.py
```

Then open:
- `http://localhost:5001` — buy a license
- `http://localhost:5002/account/alice` — buy credits
- `http://localhost:5003` — send a payout
