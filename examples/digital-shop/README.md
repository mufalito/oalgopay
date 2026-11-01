# Digital Goods Shop — OalgoPay Demo

A minimal e-commerce demo: customers buy a digital product, pay in BNT,
and receive a license key automatically when the payment confirms.

**What it demonstrates:** the full OalgoPay lifecycle —
invoice creation (merchant API) → hosted pay page → payment detection →
signed webhooks (`payment.seen`, `invoice.confirmed`) → automated fulfillment.

## Setup

```bash
cd examples/digital-shop
OALGOPAY_TOKEN=<merchant key> uv run --no-project \
  --with fastapi --with uvicorn --with httpx python shop.py
```



The shop runs on `http://localhost:5001` and expects OalgoPay on `http://localhost:8096`.
Set `OALGOPAY_TOKEN` to a merchant API key (from the admin panel's API keys tab).

## Flow

1. Customer visits the shop, clicks "Buy License — 1.0 BNT"
2. Shop creates an invoice via `POST /api/v1/invoices` and redirects to the pay page
3. Customer pays (QR / `blocknet://` / BNTPay)
4. OalgoPay calls the shop's webhook (`POST /webhook`) with an HMAC-SHA256 signature
5. On `invoice.confirmed`, the shop generates a license key and marks the order fulfilled
6. Customer reloads the page and sees their key

## Files

- `shop.py` — the entire demo (~90 lines): product page, invoice creation, webhook receiver, signature verification, license key generation
