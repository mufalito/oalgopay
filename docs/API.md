# Merchant API Reference

Base URL: `http://<gateway-host>:8096`

Auth for `/api/v1/*`: HTTP bearer token — the `api_token` from `config.json`:

> **Interactive API docs are built in**: [`/docs`](http://localhost:8096/docs)
> (Swagger UI — click **Authorize**, choose Bearer, paste your `api_token`,
> then use "Try it out") and [`/redoc`](http://localhost:8096/redoc).
> The raw schema: `/openapi.json`.

```
Authorization: Bearer <api_token>
```

All bodies are JSON. Errors are JSON `{"detail": "…"}`.

---

## Create invoice — `POST /api/v1/invoices`

Request:

| field            | type           | required | notes                                   |
|------------------|----------------|----------|-----------------------------------------|
| `amount_bnt`     | string/number  | one of*  | decimal BNT, ≤ 8 dp, > 0 (e.g. `"12.5"`) |
| `amount_atomic`  | integer        | one of*  | atomic units, 1 BNT = 100,000,000        |
| `label`          | string         | no       | free-form display label                  |
| `expires_in_min` | integer        | no       | 1–10080; default = `expiry_minutes` setting (60) |
| `metadata`       | object         | no       | arbitrary JSON, echoed back verbatim     |

\* exactly one of `amount_bnt` / `amount_atomic`.

```bash
curl -s -X POST localhost:8096/api/v1/invoices \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"amount_bnt":"0.1", "label":"order 1001", "metadata":{"cart_id":1001}}'
```

`201 Created` — invoice object:

```json
{
  "id": "INV-6TJRE4AF",
  "status": "pending",
  "amount_atomic": 10000000,
  "amount_bnt": "0.1",
  "memo": "INV-6TJRE4AF",
  "label": "order 1001",
  "metadata": {"cart_id": 1001},
  "created_at": 1793215143,
  "expires_at": 1793218743,
  "paid_atomic": 0,
  "paid_bnt": "0",
  "confirmations_required": 10,
  "late_payment": false,
  "overpaid": false,
  "underpaid": false,
  "confirmed_at": null,
  "uri": "blocknet://ULPUe…?amount=0.1&memo=INV-6TJRE4AF",
  "bntpay_url": "https://bntpay.com/$yourname?amount=0.1&memo=INV-6TJRE4AF",
  "pay_url": "http://localhost:8096/pay/INV-6TJRE4AF",
  "qr_url": "http://localhost:8096/pay/INV-6TJRE4AF/qr.svg",
  "payments": []
}
```

Field notes:

- `memo` **is** the invoice id — payments carry it to be matched.
- `uri` — `blocknet://` payment URI (QR encodes this).
- `bntpay_url` — hosted BNTPay checkout for the configured `$handle`.
- `pay_url` — your hosted pay page; **the link to give customers**. Built from
  `public_base_url`.
- `payments[]` — grows as outputs are matched; `paid_atomic` / `paid_bnt` are
  the **aggregate sum** across all matched payments (partials supported;
  `underpaid: true` while 0 < paid < amount). Each entry:
  `{txid, output_index, amount_atomic, amount_bnt, block_height,
  confirmations, matched_at, match_method}` with `match_method` ∈
  `memo` | `amount`.

## List invoices — `GET /api/v1/invoices`

Query: `status=` (`pending|seen|confirmed|expired|cancelled`),
`limit=` (1–200, default 50), `offset=` (default 0). Newest first.

```json
{"invoices": [ …invoice objects… ], "limit": 50, "offset": 0}
```

## Get invoice — `GET /api/v1/invoices/{id}`

Full invoice object including `payments[]`.

## Cancel invoice — `POST /api/v1/invoices/{id}/cancel`

Allowed only from `pending`/`seen`; cancelled is terminal. Cancelled invoices
are never matched again.

## Errors

| Code | When |
|------|------|
| 401  | missing/incorrect bearer token |
| 404  | unknown invoice id |
| 409  | cancelling a confirmed/expired/cancelled invoice |
| 422  | bad amount (zero, negative, > 8 dp), both/neither amount field, bad `expires_in_min`, unknown status filter |

---

# Public endpoints (no auth)

Used by the hosted pay page; safe for integrations.

| Endpoint                   | Returns |
|----------------------------|---------|
| `GET /pay/{id}`            | HTML pay page (QR, copy buttons, countdown, live status via htmx) |
| `GET /pay/{id}/qr.svg`     | `image/svg+xml` QR of the `blocknet://` URI |
| `GET /pay/{id}/status`     | JSON status (below) |
| `GET /pay/{id}/status-frag`| HTML status fragment (htmx polling) |

`/pay/{id}/status`:

```json
{
  "id": "INV-6TJRE4AF",
  "status": "seen",
  "paid_atomic": 10000000,
  "paid_bnt": "0.1",
  "confirmations": 3,
  "confirmations_required": 10,
  "expires_at": 1793218743,
  "late_payment": false,
  "overpaid": false,
  "underpaid": false,
  "payments": [
    {"txid": "2e507498…", "amount_atomic": 10000000, "amount_bnt": "0.1", "confirmations": 3}
  ]
}
```

Poll this every few seconds to render your own status UI, or just link
customers to `pay_url`.

# Minimal admin JSON surface (session cookie auth)

For scripts/drivers. Log in first:

```bash
curl -s -c jar -X POST localhost:8096/admin/login \
  -H 'Content-Type: application/json' -d '{"password":"…"}'
```

| Endpoint | Purpose |
|----------|---------|
| `GET /admin/api/overview` | node status + wallet address/balance |
| `GET /admin/api/settings` | current settings |
| `PATCH /admin/api/settings` | `{confirmations_required: 1-20, expiry_minutes: 1-10080, amount_fallback_matching: 0/1, display_handle: "" or "$handle"}` |
| `POST /admin/api/webhooks` | `{url}` → `201 {id, url, secret}` (secret shown once) |
| `DELETE /admin/api/webhooks/{id}` | remove endpoint (+ its delivery log) |
| `POST /admin/api/send` | `{address, amount_bnt \| amount_atomic, memo?}` → `{txid, amount_atomic, fee_atomic, …}` — spend funds (fresh Idempotency-Key per call, recorded in the payouts log) |
| `GET /admin/api/payouts?limit=25` | payout audit trail (successful + rejected) |

Everything else in the panel is htmx fragments (`/admin/frag/*`) — drive
those in a browser, or read `app/api/admin.py`.
