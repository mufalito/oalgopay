# API Credit Top-Up — OalgoPay Demo

A minimal SaaS-style demo: users buy API credits with BNT. The webhook
credits their account when payment confirms. Shows the merchant API +
webhook integration pattern any subscription/credit service would use.

## Setup

```bash
cd examples/credit-topup
OALGOPAY_TOKEN=<merchant key> OALGOPAY_WEBHOOK_SECRET=<webhook secret> \
  uv run --no-project --with fastapi --with uvicorn --with httpx python credits.py
```

## Flow

1. User visits their balance page (`/account/{username}`)
2. Clicks "Add 100 credits — 0.5 BNT" → shop creates invoice → pay page
3. Webhook `invoice.confirmed` → credits added to the user's balance
4. User can spend credits via `POST /account/{username}/use` (mock API call)
