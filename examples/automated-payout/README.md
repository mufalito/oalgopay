# Automated Payout — OalgoPay Demo

Demonstrates the payouts API with idempotency keys: send funds, retry safely,
query status. This is the pattern for automated withdrawals, refunds, or
affiliate payouts.

## Setup

```bash
cd examples/automated-payout
OALGOPAY_PAYOUT_KEY=<payouts key> \
  uv run --no-project --with fastapi --with uvicorn --with httpx python payout.py
```

Get a payouts key from the admin panel → API keys → generate (kind: `payouts`).

## What it shows

- `POST /api/v1/payouts` with a client idempotency key — retried requests
  return the original result (`replayed: true`), never double-pay
- `GET /api/v1/payouts` — payout history
- `GET /api/v1/payouts/{id}` — check a specific payout's status
