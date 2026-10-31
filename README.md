<h1 align="center">OalgoPay</h1>

<p align="center">
  Self-hosted Blocknet (BNT) payment gateway.<br>
  Merchant API · hosted pay page · signed webhooks · admin panel.<br><br>
  <img src="https://img.shields.io/badge/network-Blocknet%20Mainnet-aaff00?style=flat-square&labelColor=000">
  <img src="https://img.shields.io/badge/python-3.14-aaff00?style=flat-square&labelColor=000">
  <img src="https://img.shields.io/badge/license-MIT-aaff00?style=flat-square&labelColor=000">
  <img src="https://img.shields.io/badge/platform-Linux%20x86__64-aaff00?style=flat-square&labelColor=000">
</p>

<p align="center">
  <img src="docs/img/pay-page.png" width="420" alt="OalgoPay hosted pay page">
  <img src="docs/img/admin-overview.png" width="460" alt="OalgoPay admin panel">
</p>

---

## What is this?

OalgoPay turns a [Blocknet](https://github.com/blocknetprivacy) node into a
payment processor you fully control: create invoices from your shop, give
customers a clean hosted pay page (QR, `blocknet://` one-tap, bntpay.com
link), watch the chain for their payment, and get cryptographically signed
webhooks the moment money is seen and confirmed. No third party, no custody —
your node, your wallet, your gateway.

```
Blocknet core (headless) ──api.cookie──▶ watcher (SSE + poll) ──▶ SQLite
                                                │
              merchant REST API ◀────────────── ┴──▶ signed webhooks ──▶ your shop
              hosted pay page (/pay/{id}, QR)
              admin panel (/admin)
```

## Features

| | |
|---|---|
| **Invoices** | `INV-` ids double as payment memos; BNT or atomic amounts (≤ 8 dp); labels + arbitrary metadata; forward-only lifecycle `pending → seen → confirmed` (+ `expired`, `cancelled`) |
| **Pay page** | QR of the `blocknet://` URI, copy buttons, live status, countdown, "Open in Wallet", bntpay.com link — htmx-polled, no page reloads |
| **Matching** | memo-first; optional exact-amount fallback (oldest unpaid invoice); idempotent by txid+output; overpaid / underpaid / late-payment flags |
| **Webhooks** | `payment.seen`, `invoice.confirmed`, `invoice.expired` (+ manual resend, test pings); `event_id` for dedupe, `previous_status` for transitions; HMAC-SHA256 signatures with timestamp binding; retries 30 s → 2 h |
| **Admin panel** | node/wallet/handle health, invoice CRUD + manual confirm, webhook management + delivery log, manual payouts with audit trail, live settings |
| **Setup wizard** | first run at `/setup`: detects your core, live-tests the connection, generates secrets — no config editing required |
| **Payouts API** | separate `payout_api_key` + `POST /api/v1/payouts` with client idempotency keys — retry-safe automation that can never double-pay |
| **API docs** | OpenAPI at `/docs` + `/redoc`, typed schemas, working Authorize button |
| **Self-contained** | one directory: app, core binary, chain data, wallet. Nothing phones home. |

## Quick start

1. Install [uv](https://docs.astral.sh/uv/) and Python 3.14+, then:

   ```bash
   uv sync
   uv run run.py
   ```

   Prefer Docker? `cp config.example.json config.json && docker compose up -d --build`
   — core + gateway containers, wallet flow included ([docs/DOCKER.md](docs/DOCKER.md)).

2. Open `http://localhost:8096` — the [setup wizard](docs/INSTALL.md) walks
   you through the rest: core address, secrets (generated for you),
   public URL, payment defaults.

3. Start (or point at) a Blocknet core and load your wallet — the wizard's
   connection test tells you exactly what's missing.

<p align="center">
  <img src="docs/img/setup-wizard.png" width="420" alt="OalgoPay first-run setup wizard">
</p>

> **Core binary:** download the daemon-only build that matches your chain
> data from [blocknetprivacy/core](https://github.com/blocknetprivacy/core)
> releases (verify against `SHA256SUMS.txt`) — see [bin/README.md](bin/README.md).

### Create your first invoice

```bash
TOKEN=…   # the api_token from config.json / setup

curl -s -X POST localhost:8096/api/v1/invoices \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"amount_bnt":"12.5", "label":"order 1001", "metadata":{"cart_id":1001}}'
```

The response carries everything you need: `pay_url` (the link to give the
customer), the `blocknet://` URI, a bntpay.com checkout, and the QR. Full
reference: **`/docs`** on your running gateway, or [docs/API.md](docs/API.md).

### Receive webhooks

```json
{
  "event_id": "evt_ff9274aea3289f25",
  "event": "invoice.confirmed",
  "event_created_at": 1793329011,
  "previous_status": "seen",
  "invoice": { "…": "…" }
}
```

Signed with `X-OalgoPay-Signature: t=<unix>,v1=<hex>` —
`v1 = HMAC-SHA256(secret, "<t>." + raw_body)`. Verification snippets and the
retry/dedupe contract: [docs/WEBHOOKS.md](docs/WEBHOOKS.md).

---

## Documentation

- [Installation & Operations](docs/INSTALL.md) — first start, systemd, backup/restore, upgrades, troubleshooting
- [Merchant API Reference](docs/API.md) — every endpoint, schema, and error
- [Webhooks Guide](docs/WEBHOOKS.md) — events, payloads, signature verification, testing
- [Admin Panel Guide](docs/ADMIN.md) — invoices & matching rules, payouts, settings
- Interactive OpenAPI: `/docs` (Swagger UI) and `/redoc` on the running gateway

## Security

- The core API stays localhost-bound; the gateway authenticates with the
  regenerated `api.cookie` and holds **no wallet keys** — it never loads or
  unlocks wallets.
- Only the admin panel's Send feature can move coins (admin session required);
  the merchant API is create/read-only.
- Keep `config.json`, `data/`, and `shop.wallet.*` private (600/0700) — they
  are git-ignored by design. Serve the panel over TLS before exposing the
  gateway beyond your LAN.

## Tests

```bash
uv run python -m unittest          # matcher, confirmation sweep, amounts (pure logic)
uv run python scripts/e2e_test.py  # real-money end-to-end: pay → seen → confirmed → signed webhooks
```

## License

[MIT](LICENSE)
