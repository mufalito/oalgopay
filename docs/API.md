# Merchant API Reference

Base URL: `http://<gateway-host>:8096`

Auth for `/api/v1/*` (invoices): HTTP bearer token — a **merchant** API key,
generated (and revoked) in the admin panel's **API keys** tab. The
`api_token` from `config.json`/setup is seeded as one on first boot:

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

## Payouts API — automated payouts

Separate credential on purpose: `Authorization: Bearer <payouts API key>`
(generated in the admin panel's **API keys** tab, kind: `payouts`; the
`payout_api_key` from `config.json`/setup is seeded as one on first boot).
Apps that create invoices with a merchant key **cannot** move coins.

```bash
PK=…   # payouts API key (admin panel → API keys → Generate, kind: payouts)

# pay out — supply idempotency_key so retries can never double-pay
curl -s -X POST localhost:8096/api/v1/payouts \
  -H "Authorization: Bearer $PK" -H 'Content-Type: application/json' \
  -d '{"address":"ULP…","amount_bnt":"1.25","memo":"withdrawal #77",
       "idempotency_key":"withdraw-77"}'
# → 201 {"txid": "…", "amount_atomic": 125000000, "fee_atomic": …,
#         "idempotency_key": "withdraw-77", "replayed": false}

# replaying the SAME idempotency_key returns the original payout
# (replayed: true) without sending again.

curl -s -H "Authorization: Bearer $PK" localhost:8096/api/v1/payouts?limit=25   # history
curl -s -H "Authorization: Bearer $PK" localhost:8096/api/v1/payouts/4          # one payout
```

Every attempt is recorded in the payouts audit log (rejected attempts keep
the core's error). A memo containing an `INV-…` id sent to this gateway's
own address credits that invoice as paid — use memos deliberately.

---

# Wallet sign-in verification — `POST /api/v1/auth/*`

"Sign in with Blocknet wallet" for **other services you run** (faucet,
forum, …): your backend mints a single-use challenge, the user signs its
message with their wallet, and you post the address+signature pair back
here — the gateway forwards it to the node's signature verifier and returns
the verdict. You mint your own session on a 200; the gateway hosts no
sign-in page and never sees yours.

Auth: `Authorization: Bearer <auth API key>` — generated in the admin
panel's **API keys** tab (kind: `auth`). Server-to-server only: keep the key
on your backend, never in browser code.

## 1. Mint challenge — `POST /api/v1/auth/challenge`

```bash
AK=…   # auth API key (admin panel → API keys → Generate, kind: auth)
curl -s -X POST localhost:8096/api/v1/auth/challenge -H "Authorization: Bearer $AK"
```

```json
{"nonce": "9f86d081884c7d659a2feaa0c55ad015",
 "message": "oalgopay:login:9f86d081884c7d659a2feaa0c55ad015",
 "expires_at": 1793218743}
```

Show `message` to the user for signing. The format is fixed —
`oalgopay:login:<nonce>` — and the nonce is globally single-use, so a
signature obtained for any other site's challenge can never verify here.

## 2. Verify — `POST /api/v1/auth/verify`

```bash
curl -s -X POST localhost:8096/api/v1/auth/verify \
  -H "Authorization: Bearer $AK" -H 'Content-Type: application/json' \
  -d '{"nonce":"9f86d081884c7d659a2feaa0c55ad015",
       "address":"ULP…", "signature":"<128 hex chars>"}'
```

`200 OK` — the signature is valid for `address`:

```json
{"nonce": "9f86d081884c7d659a2feaa0c55ad015", "address": "ULP…", "verified": true}
```

On a 200, log the user in on your side (`address` is the identity). The
challenge is consumed; replaying it returns 400.

## Errors

| Code | When |
|------|------|
| 400  | unknown challenge / challenge already used / challenge expired (10 min) — request a new one |
| 401  | missing/incorrect auth API key; or the signature failed (node-validated: wrong signer, malformed address/signature) |
| 403  | no auth key exists yet (API disabled), or gateway is in setup mode |
| 422  | request body outside hygiene bounds (nonce 8–64, address 8–160, signature 64–256 chars) |
| 429  | lockout: ≥ 5 definite failures from your client IP within 15 min — retry after the window |
| 503  | verification backend (the node) unreachable/erroring — safe to retry the SAME challenge |

## Integration notes

- **Verdicts vs infra**: a definite verdict (node says valid/invalid, or
  rejects the input as malformed) consumes the challenge; definite invalids
  also count toward the lockout. A 503 means *no verdict* — the challenge
  stays usable and nothing is counted; retry the same nonce+signature once
  the node is back.
- **Lockout is per client IP of the API caller** (your backend's IP as the
  gateway sees it). `X-Forwarded-For` is not trusted; behind a reverse proxy
  all callers share the proxy's IP unless you enable `--proxy-headers`.
- **Sessions are yours**: mint your own cookie/token on a 200. This API only
  verifies possession of the wallet's signing key.
