# Installation & Operations Guide

Self-hosted Blocknet (BNT) payment gateway. Everything — code, core binary,
chain data, wallet — lives in one directory.

## Contents

- [Requirements](#requirements)
- [Directory layout](#directory-layout)
- [First start](#first-start)
- [Running under systemd](#running-under-systemd)
- [Backup & restore](#backup--restore)
- [Upgrading](#upgrading)
- [Troubleshooting](#troubleshooting)

## Requirements

- Linux x86_64, Python 3.14+, [`uv`](https://docs.astral.sh/uv/) 
- ~2 GB disk for the chain database
- Outbound p2p (tcp/28080 by default) + the Blocknet network

## Directory layout

```
paymentgateway/
├── run.py                    gateway entry point
├── config.json               secrets + bind address (600)
├── config.example.json       template
├── app/                      gateway source (see README for module map)
├── bin/                      Blocknet core 0.22.0 binary + zip + SHA256SUMS
├── data/                     0700
│   ├── gateway.db            gateway SQLite (WAL)
│   ├── session_secret        admin session signing key (600)
1. **Install deps**

   ```bash
   uv sync
   ```

2. **Start the core (headless)**

   ```bash
   bin/blocknet-core-amd64-linux --api 127.0.0.1:18512 \
       --data "$PWD/data/blocknet" &
   ```

   The core starts with **no wallet loaded**. Load + unlock the shop wallet
   once per core start (this is the only step that touches the password —
   the gateway never does):

   ```bash
   curl -X POST -H "Authorization: Bearer $(cat data/blocknet/api.cookie)" \
        -H 'Content-Type: application/json' \
        -d "{\"path\":\"$PWD/shop.wallet.dat\",\"password\":\"$(cat shop.wallet.pw)\"}" \
        http://127.0.0.1:18512/api/wallet/load
   ```

3. **Start the gateway and run the setup wizard**

   ```bash
   uv run run.py            # or: uv run run.py --host 0.0.0.0 --port 8096
   ```

   Open `http://<host>:8096` — without a `config.json` the gateway boots in
   **setup mode** and redirects to `/setup`. The wizard:

   - prefills the core API address (`http://127.0.0.1:18512`) and the data
     directory (auto-detected: the app's `data/blocknet`, else the GUI's
     `~/.local/share/com.blocknet.wallet/data`),
   - **Test connection** live-checks the core: reachable? chain height?
     wallet loaded + unlocked (distinct messages for "locked" and
     "no wallet loaded"),
   - generates an admin password + API token (regenerate button),
   - optional **wallet step**: wallet file + password load the wallet on the
     core immediately — the password is forwarded once and discarded, never
     written to `config.json` or anywhere else,
   - takes `public_base_url` (defaults to the address you're browsing from),
     confirmations (10), expiry (60 min), display handle, and an optional
     webhook URL,
   - writes `config.json` (mode 600) and applies everything **without a
     restart** — you land on the admin login.

   Hand-editing `config.json` remains supported for advanced/unattended
   setups (`cp config.example.json config.json`, edit the keys below,
   `chmod 600 config.json`):

   | key               | meaning                                                          |
   |-------------------|------------------------------------------------------------------|
   | `host` / `port`   | gateway listen address (default `0.0.0.0:8096`)                   |
   | `api_base`        | core REST base (default `http://127.0.0.1:18512`)                 |
   | `datadir`         | core data dir — where `api.cookie` is read from                   |
   | `admin_password`  | admin panel login                                                 |
   | `api_token`       | merchant API bearer token                                         |
   | `public_base_url` | externally reachable URL used to build `pay_url`                  |

   Admin panel: `http://<host>:8096/admin`. Create your first invoice from
   the panel (Invoices → Create) or the [merchant API](API.md).

## Running under systemd

Two user units keep the pair alive. `~/.config/systemd/user/oalgopay-core.service`:

```ini
[Unit]
Description=Blocknet core (OalgoPay)
After=network-online.target

[Service]
# adjust /opt/oalgopay to wherever you installed OalgoPay
WorkingDirectory=/opt/oalgopay
ExecStart=/opt/oalgopay/bin/blocknet-core-amd64-linux --api 127.0.0.1:18512 --data /opt/oalgopay/data/blocknet
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

`~/.config/systemd/user/oalgopay.service`:

```ini
[Unit]
Description=OalgoPay
After=network-online.target oalgopay-core.service

[Service]
WorkingDirectory=/opt/oalgopay
ExecStart=/opt/oalgopay/.venv/bin/python run.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now oalgopay-core oalgopay
journalctl --user -u oalgopay -f
```

> After a core restart the wallet is unloaded (by design). Reload it from
> the admin panel's Overview — the **Load wallet** form appears whenever no
> wallet is loaded — or with the `api/wallet/load` curl from
> [First start](#first-start). The password is sent to the core once and
> never stored by the gateway.

## Backup & restore

- **Must back up:** `shop.wallet.dat` (and `shop.wallet.pw`), `config.json`,
  `data/gateway.db` (invoice/payment/payout history), `data/session_secret`
  (invalidates admin sessions if lost).
- Copy `data/gateway.db` while the gateway is stopped, or rely on SQLite WAL
  (safe to copy the three `.db*` files hot).
- Restore = put files back and start; the watcher reconciles the last 2016
  blocks on boot, so payments that landed while down are picked up.
- The chain db (`data/blocknet/blocknet-mainnet.chain.db`) does not need
  backup — it re-syncs from the network.

## Upgrading

1. Stop the gateway (`systemctl --user stop oalgopay`).
2. `git pull` / copy new sources, then `uv sync`.
3. For core upgrades: download the new core zip to `bin/`, verify SHA256,
   stop `oalgopay-core`, replace the binary, start again. **Match the core
   version to the chain db format** — check the release notes of
   `blocknetprivacy/core` before jumping versions.
4. Start the gateway. Schema migrations run automatically on boot
   (`CREATE TABLE IF NOT EXISTS …`).

## Troubleshooting

| Symptom | Meaning / fix |
|---|---|
| Panel banner "Node degraded … api.cookie" | Core not running → start `oalgopay-core`. The gateway re-reads the cookie on 401 automatically once it's back. |
| Panel shows wallet `no wallet loaded` / `locked` | Use the **Load wallet** form on the Overview (or the `api/wallet/load` curl in First start). Watching resumes automatically; no gateway restart needed. |
| Invoice stuck `pending` after a customer paid | Blocks may be slow (check node height in Overview). This core reveals sent-to-self outputs once mined; the watcher polls every 60 s + on every new block. If the customer really paid, the startup/2016-block reconciliation or the next poll will match it. |
| `Send` fails with "insufficient spendable balance … pending (confirming)" | Normal after any send: the change output needs ~10 confirmations before it is spendable again. Wait for blocks. |
| Webhook delivery log shows ConnectError rows | Endpoint unreachable/down. Retries: 30 s, 2 m, 10 m, 30 m, 2 h, then permanent fail. Test with the "Test" button once your receiver is up. |
| Admin panel logs me out | Sessions last 12 h; log in again. If `data/session_secret` was recreated, all sessions invalidate. |
| Pay page QR/URI shows a wrong address | `last_known_address` (Settings store) is refreshed from the core whenever reachable; restart the gateway with the core up, or load the wallet so the address can be re-read. |

## Security notes

- Keep the core API localhost-bound (`--api 127.0.0.1:18512`); the gateway
  authenticates with the regenerated `api.cookie`.
- The gateway process can move coins **only** through the admin Send feature
  (admin session required). The merchant API is read/create-only.
- Serve the panel + pay page over TLS (reverse proxy) before exposing beyond
  LAN; rotate `admin_password` / `api_token` periodically.
- File modes: `config.json` 600, `data/` 0700, `shop.wallet.*` 600.
