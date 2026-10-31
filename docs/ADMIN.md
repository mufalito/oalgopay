# Admin Panel Guide

Reach it at `http://<host>:8096/admin` and sign in with the `admin_password`
from `config.json` (12-hour session cookie, HttpOnly + SameSite=Lax).

The panel is server-rendered fragments driven by vendored htmx — no external
CDN calls, no build step.

## Overview

Live dashboard, refreshed on visit:

- **Node** — API connected/down, chain height, peer count, sync + mempool.
  A degraded node (core down / wallet locked) shows a red banner; the gateway
  keeps creating invoices and resumes watching automatically.
- **Wallet** — the shop stealth address, lock state, balances
  (total/spendable/pending, BNT). When no wallet is loaded (e.g. after a
  core restart) a **Load wallet** form appears here: file + password are
  sent to the core once and never stored.
- **Handle** — the configured `$handle` (e.g. `$yourname`) with its
  blocknet.id resolution: **✓ signature valid** means the resolver payload
  was Ed25519-verified against `blocknet.id/.well-known/blocknet-id.json`
  and matches the wallet address. Shows `unverified (…)` if the resolver is
  unreachable or anything mismatches.
- **API keys** — generate and revoke merchant / payouts credentials. Keys are
  stored hashed (sha256); the full value is shown exactly once at creation.
  Kind matters: `merchant` keys create invoices, `payouts` keys are the only
  credentials that can move coins via `/api/v1/payouts`. Rotation without
  downtime: generate the replacement, deploy it, revoke the old one —
  revocation is immediate. Config-file tokens are seeded into this list on
  first boot and can be revoked like any other key.
- **Invoices** — counters per status.

## Invoices

- **Filter chips** — all / pending / seen / confirmed / expired / cancelled.
- **Create** — amount (BNT), optional label + expiry minutes (defaults from
  Settings). The response opens the new invoice's detail with its full JSON
  (links, URI, QR URL).
- **Row click** — detail view: status, amounts, memo, URIs, matched payments
  (`match_method`, block height, confirmations) and webhook delivery history.
- **Actions** (per status):
  - *Cancel* — pending/seen → cancelled (terminal).
  - *Confirm (manual)* — force-confirm; records
    `metadata.manual_confirm {at, previous_status, paid_atomic}` and emits
    `invoice.confirmed`.
  - *Resend webhooks* — enqueues `invoice.updated` to all active endpoints.

### Invoice lifecycle

```
pending ──payment matched──▶ seen ──confirmations≥required──▶ confirmed
   │                            │
   └── expiry (unpaid) ──▶ expired ──payment arrives late──▶ confirmed
                            (late_payment=1)
   pending/seen ──manual──▶ cancelled (terminal)
```

- Partially-paid invoices never expire — they stay `seen` until confirmed.
- Overpayment sets `overpaid=1` (invoice still confirms).
- All statuses are forward-only; `cancelled` is manual-only.

### How payments are matched

1. **memo** — the decrypted output memo contains an invoice id
   (`INV-` + 8 chars from the Crockford-ish alphabet). This is the primary
   path; the pay page / QR / URI carry it automatically.
2. **amount fallback** (if enabled in Settings) — a memo-less output matching
   the exact amount credits the **oldest** unpaid pending/seen invoice.
   Misattribution is possible when two unpaid invoices share an amount and
   the sender stripped the memo — disable in Settings for memo-only matching.

Confirmations = `chain_height − block_height`, minimum across the invoice's
payments (mempool outputs count as 0). The gateway polls the wallet on every
new block plus a 60 s fallback and re-checks thresholds every pass, so
invoices confirm even when matched deep below the threshold.

## Webhooks

- **Add endpoint** — URL (http/https); the signing secret is displayed once.
- **Pause/Resume**, **Delete** (removes its delivery history too),
  **Test** — sends a signed `webhook.test` ping.
- **Delivery log** — last 50 deliveries: event, invoice, attempts, HTTP
  status / error, delivered-at. Full signature + retry details:
  [WEBHOOKS.md](WEBHOOKS.md).

## Send

Manual payouts/refunds from the shop wallet to any address.

- Fields: recipient address, amount (BNT), optional memo.
- A browser confirm dialog guards submission; the button disables in-flight.
- Every send uses a fresh core `Idempotency-Key` (a replayed request cannot
  double-pay) and is recorded in the **payouts** audit table — address,
  amount, fee, memo, txid; core rejections are recorded with their error.
- ⚠️ A memo containing an `INV-…` id sent to the **shop's own address** will
  be credited to that invoice as a payment. Use memos deliberately.
- Expect "insufficient spendable balance" right after any send: the change
  output needs ~10 confirmations before it can fund the next send.

This is the only part of the gateway that can move coins, and it requires
an admin session — the merchant API has no spend capability.

## Settings

| Setting | Range | Effect |
|---|---|---|
| `confirmations_required` | 1–20 | blocks before `confirmed` |
| `expiry_minutes` | 1–10080 | default invoice lifetime |
| `amount_fallback_matching` | on/off | see matching above |
| `display_handle` | `""` or `$handle` | shown on pay page + used for bntpay.com links; blank = raw address |

Changes apply immediately (matching + expiry use the live values).
