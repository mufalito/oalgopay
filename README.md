# OalgoPay

Self-hosted Blocknet (BNT) payment gateway: merchant REST API, public pay page
with QR + `blocknet://` URI, htmx admin panel, signed webhooks, and a payment
confirmation watcher. Python 3.14 / FastAPI / SQLite (WAL). BNT-denominated only.

```
Blocknet core (bin/, headless)  ──api.cookie──▶  watcher (SSE + poll)  ──▶  SQLite (data/)
                                                      │
                      merchant REST API ◀──────────── ┴ ──▶ signed webhooks ──▶ your shop
                      public pay page (/pay/{id}, QR, htmx live status)
                      admin panel (/admin, htmx fragments)
```

## Documentation

| Doc | What's inside |
|-----|----------------|
| [docs/INSTALL.md](docs/INSTALL.md) | setup, first start, systemd, backup/restore, upgrades, troubleshooting |
| [docs/API.md](docs/API.md) | merchant REST reference, public pay endpoints, admin JSON surface |
| [docs/WEBHOOKS.md](docs/WEBHOOKS.md) | events, payloads, HMAC verification (Python/Node), retries, local testing |
| [docs/ADMIN.md](docs/ADMIN.md) | panel guide: overview, invoices + matching, webhooks, send, settings |

Interactive OpenAPI docs: **`/docs`** (Swagger UI — Authorize → Bearer →
`api_token`) and **`/redoc`**; linked from the admin panel nav ("API Docs ↗").

## Quickstart

```bash
uv sync
uv run run.py               # serves on 0.0.0.0:8096
```

Then open **`http://localhost:8096/setup`** — with no `config.json` the
gateway boots in **setup mode**: `/setup` prefills the core address and
datadir (auto-detected), live-tests the connection (node reachable, wallet
loaded + unlocked), generates secrets for you, and applies everything
without a restart. Hand-editing still works for advanced cases
(`cp config.example.json config.json`, edit, `chmod 600 config.json`).

`config.json` keys:

| key                | meaning                                                        |
|--------------------|----------------------------------------------------------------|
| `host` / `port`    | listen address (default `0.0.0.0:8096`)                        |
| `api_base`         | daemon REST base (default `http://127.0.0.1:18512`)            |
| `datadir`          | core data dir — used to read `api.cookie` for Bearer auth       |
| `admin_password`   | admin panel login                                              |
| `api_token`        | merchant API bearer token                                      |
| `public_base_url`  | URL customers can reach; used to build `pay_url`               |

On first start the gateway creates `data/` (0700) containing `gateway.db` and a
`session_secret` (0600). The core API cookie is re-read automatically whenever
the core regenerates it (401 → re-read → retry once).

`run.py` flags: `--config config.json --host H --port P` (CLI overrides config).

## Runtime model

- **Self-contained layout.** Everything lives in this directory: the gateway
  code, the vendored Blocknet core (`bin/`), the node data (`data/blocknet/`,
  0700), and the shop wallet (`shop.wallet.dat` + `shop.wallet.pw`). The
  headless core is the official daemon-only build, version-locked to the chain
  data:

  ```bash
  # current setup (already in place):
  bin/blocknet-core-amd64-linux --api 127.0.0.1:18512 \
      --data "$PWD/data/blocknet" &
  # the wallet is NOT auto-loaded headless; load it once per core start:
  curl -X POST -H "Authorization: Bearer $(cat data/blocknet/api.cookie)" \
       -H 'Content-Type: application/json' \
       -d "{\"path\":\"$PWD/shop.wallet.dat\",\"password\":\"$(cat shop.wallet.pw)\"}" \
       http://127.0.0.1:18512/api/wallet/load
  uv run run.py
  ```

  (`bin/blocknet-core-0.22.0.zip` + `SHA256SUMS.txt` are kept for provenance;
  core releases: github.com/blocknetprivacy/core — the version must match the
  chain db.)

- **The gateway never loads/unloads/unlocks wallets** — the core/GUI owns
  wallet state. If the wallet is not loaded or is locked, the panel shows a
  degraded banner and payment watching resumes once it is unlocked.

- **One stealth address serves all invoices**; payments are attributed via the
  `memo` (the invoice id) carried in `blocknet://ADDR?amount=X&memo=INV-…`
  URIs. Outputs are confidential on-chain; the wallet reveals them
  (amount+memo) into its history as it scans — mempool pickup can lag
  inclusion by up to a minute, and a just-sent change output is not spendable
  until confirmed.

- **Watcher**: SSE `new_block` + 60s fallback poll + 30s expiry sweep +
  startup reconciliation over the last 2016 blocks (catches payments made
  while the gateway was down).

- **Invoices**: `pending → seen → confirmed` (forward-only); `expired` when
  unpaid past expiry; `cancelled` is manual/terminal. Partially-paid invoices
  never expire — they stay `seen` until confirmed.

### Matching rules (`app/invoices.py`, pure + unit-tested)

1. **memo** — decoded wallet-output memo contains an invoice id
   (`INV-[A-HJ-NP-Z2-9]{8}`) whose invoice is pending/seen/expired.
2. **amount fallback** (optional, default on) — exact-amount match against the
   *oldest* unpaid (`paid_atomic = 0`) pending/seen invoice.

⚠️ Amount fallback exists because senders can drop the memo. If two unpaid
invoices share an exact amount and a memo-less payment arrives, it is credited
to the older one — possibly wrongly. Disable via Settings →
amount fallback matching = disabled (memo-only).

Confirmations = `chain_height − block_height`, minimum across the invoice's
payments (mempool = 0). An invoice confirms when
`paid ≥ amount AND min_confirmations ≥ confirmations_required`. Extra payments
set `overpaid`; payments on expired invoices set `late_payment` and still
confirm normally.

## Merchant API

Auth: `Authorization: Bearer <api_token>`.

```bash
TOKEN=…   # api_token from config.json

# create
curl -s localhost:8096/api/v1/invoices -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"amount_bnt":"12.5", "label":"order 1001", "metadata":{"cart_id":1001}}'
# or atomic units: {"amount_atomic": 1250000000, "expires_in_min": 30}

# list / inspect / cancel
curl -s "localhost:8096/api/v1/invoices?status=seen&limit=20" -H "Authorization: Bearer $TOKEN"
curl -s localhost:8096/api/v1/invoices/INV-XXXX -H "Authorization: Bearer $TOKEN"
curl -s -X POST localhost:8096/api/v1/invoices/INV-XXXX/cancel -H "Authorization: Bearer $TOKEN"
```

Invoice object (created response + GET):

```json
{
  "id": "INV-6TJRE4AF", "status": "pending",
  "amount_atomic": 10000000, "amount_bnt": "0.1",
  "memo": "INV-6TJRE4AF", "label": "order 1001", "metadata": {"cart_id": 1001},
  "created_at": 1793215143, "expires_at": 1793218743,
  "paid_atomic": 0, "paid_bnt": "0",
  "confirmations_required": 10, "late_payment": false, "overpaid": false, "underpaid": false,
  "confirmed_at": null,
  "uri": "blocknet://ULPUe…?amount=0.1&memo=INV-6TJRE4AF",
  "bntpay_url": "https://bntpay.com/$yourname?amount=0.1&memo=INV-6TJRE4AF",
  "pay_url": "http://localhost:8096/pay/INV-6TJRE4AF",
  "qr_url": "http://localhost:8096/pay/INV-6TJRE4AF/qr.svg",
  "payments": []
}
```

`payments[]` entries add `txid, output_index, block_height, confirmations,
matched_at, match_method ("memo"|"amount")`. Statuses:
`pending | seen | confirmed | expired | cancelled`.

Errors: JSON `{"detail": "…"}` with 401 (bad token), 404, 409 (cancel of a
terminal invoice), 422 (bad amount / >8 decimal places / negative / zero).

## Public pay page

- `GET /pay/{id}` — amount, pay-to line (`$handle` if configured, else the raw
  address), memo + copy buttons, QR of the `blocknet://` URI, "Open in Wallet"
  and "Open BNTPay page" buttons, live status via an htmx-polled fragment.
- `GET /pay/{id}/qr.svg` — QR as SVG.
- `GET /pay/{id}/status` — JSON status (used by integrations; the page itself
  uses `GET /pay/{id}/status-frag`).

## Webhooks

Each active endpoint receives, for `payment.seen`, `invoice.confirmed`,
`invoice.expired` (and `webhook.test` pings):

- `POST <url>` with JSON `{"event_id": …, "event": …, "event_created_at": <unix>, "previous_status": …, "invoice": {…}}`
- Headers `X-OalgoPay-Event`, `X-OalgoPay-Event-Id`, `X-OalgoPay-Invoice`,
  `X-OalgoPay-Signature: t=<unix>,v1=<hex>`
  where `v1 = HMAC-SHA256(endpoint_secret, "<t>." + raw_body)`.

Retries: 30s, 2m, 10m, 30m, 2h (5 attempts), then permanent fail (kept in the
delivery log). Verify with a constant-time compare:

```python
import hashlib, hmac, time

def verify(secret: str, header: str, raw: bytes) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(","))
    t, v1 = parts["t"], parts["v1"]
    if abs(time.time() - int(t)) > 300:   # replay window
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(v1, expected)
```

Manage endpoints in the admin panel (Webhooks tab); the signing secret is shown
once at creation. A minimal JSON surface also exists for scripts:
`POST/DELETE /admin/api/webhooks`, `GET /admin/api/overview`,
`GET|PATCH /admin/api/settings` (session cookie auth).

## Admin panel

`http://<host>:8096/admin` (password from config; 12h session cookie).

- **Overview** — node height/peers/sync, wallet address + balance, blocknet.id
  handle verification (Ed25519 signature checked against
  `blocknet.id/.well-known/blocknet-id.json`), invoice counters.
- **Invoices** — filter/create/cancel/confirm (manual override, recorded in
  `metadata.manual_confirm`)/resend webhooks, per-invoice payment + delivery
  timeline.
- **Webhooks** — endpoint CRUD, test ping, delivery log with attempts/status.
- **Send** — manual payouts/refunds to any address. Confirm dialog, fresh daemon
  `Idempotency-Key` per send, full audit trail (`payouts` table: address,
  amount, fee, memo, txid; daemon rejections recorded too). Note: a memo
  containing an `INV-…` id sent to the shop's own address will credit that
  invoice as paid — use memos deliberately. This is the only gateway feature
  that can move coins, and it is admin-session-only (never the merchant API).
- **Settings** — `confirmations_required` (1–20), `expiry_minutes`,
  `amount_fallback_matching`, `display_handle` (e.g. `$yourname`; shown on the
  pay page and used for the bntpay.com link).

The panel is server-rendered Jinja fragments driven by vendored htmx
(`app/static/htmx.min.js`); no external CDN calls.

## Tests & e2e

```bash
uv run python -m unittest             # matcher unit tests (pure logic)
uv run python scripts/e2e_test.py     # REAL 0.1 BNT self-send through the core
```

The e2e driver registers a local webhook receiver, drops `confirmations_required`
to 1 (restores it afterwards), pays a real invoice from the shop wallet to
itself, and asserts seen → confirmed transitions plus HMAC-valid
`payment.seen` / `invoice.confirmed` deliveries. Prerequisites: core running,
shop wallet loaded + unlocked, spendable balance (a just-sent change output
needs ~10 confirmations before it can fund the next send). Real cost ≈ the
transaction fee (~0.0005 BNT per run).

## systemd user service

`~/.config/systemd/user/oalgopay.service`:

```ini
[Unit]
Description=OalgoPay
After=network-online.target

[Service]
WorkingDirectory=/home/hope/DistroBox/ubuntu-ai/paymentgateway
ExecStartPre=/home/hope/DistroBox/ubuntu-ai/paymentgateway/bin/blocknet-core-amd64-linux --api 127.0.0.1:18512 --data /home/hope/DistroBox/ubuntu-ai/paymentgateway/data/blocknet
ExecStart=/home/hope/DistroBox/ubuntu-ai/paymentgateway/.venv/bin/python run.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now oalgopay
journalctl --user -u oalgopay -f
```

## Security notes

- Keep the core API localhost-bound (`--api 127.0.0.1:18512`); the gateway
  authenticates with the regenerated `api.cookie`.
- `config.json`, `data/` (session secret + DB + chain), `shop.wallet.dat/pw`
  are sensitive: keep the 600/0700 modes.
- Serve the admin panel and pay page over TLS (reverse proxy) before exposing
  the gateway beyond your LAN; rotate `admin_password`/`api_token` periodically.
- The gateway holds no wallet keys and never unlocks the wallet.

## Layout

```
run.py  config.example.json  pyproject.toml
bin/                    vendored Blocknet core 0.22.0 (+ zip + SHA256SUMS)
data/                   0700: gateway.db, session_secret, blocknet/ (core datadir)
shop.wallet.dat/.pw     shop wallet (loaded/unlocked by the core, not the gateway)
app/config.py           settings load + session secret bootstrap
app/db.py               ALL SQL (sqlite3, WAL) + schema
app/blocknet.py         core client: Bearer cookie auth, SSE, history, send (e2e only)
app/invoices.py         invoice ids/URIs + pure matcher (unit-tested, no I/O)
app/watcher.py          SSE + 60s poll + 30s expiry sweep + 2016-block reconciliation
app/webhooks.py         signed deliveries, retry backoff, HMAC
app/auth.py             admin session cookie + merchant bearer
app/handle.py           blocknet.id handle resolution + Ed25519 verification
app/api/                merchant.py (REST) · pay.py (public) · admin.py (htmx + JSON)
app/templates/          pay.html, admin.html, login.html, error.html, frags/*
app/static/             htmx.min.js (vendored), pay.css/js, admin.css
scripts/e2e_test.py     real-money end-to-end driver
```

## Credits

- [htmx](https://htmx.org) (BSD-2-Clause) — vendored at `app/static/htmx.min.js`
- [FastAPI](https://fastapi.tiangolo.com), [uvicorn](https://www.uvicorn.org),
  [httpx](https://www.python-httpx.org), [qrcode](https://github.com/lincolnloop/python-qrcode),
  [Jinja2](https://jinja.palletsprojects.com), [cryptography](https://cryptography.io)
- Blocknet core releases: [blocknetprivacy/core](https://github.com/blocknetprivacy/core)
