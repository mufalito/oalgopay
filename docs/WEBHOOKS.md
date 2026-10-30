# Webhooks Guide

The gateway signs and delivers HTTP callbacks whenever an invoice's state
changes. Manage endpoints in the admin panel (**Webhooks** tab) — the signing
secret is shown **once** at creation.

## Events

| Event               | When |
|---------------------|------|
| `payment.seen`      | a wallet output is newly matched to an invoice (before confirmation) |
| `invoice.confirmed` | invoice transitions into `confirmed` (paid + enough confirmations) |
| `invoice.expired`   | unpaid invoice passes its expiry (30 s sweep) |
| `invoice.updated`   | manual "Resend webhooks" from the panel |
| `webhook.test`      | the panel's "Test" button |

## Delivery

- `POST <your-url>` with JSON body:

  ```json
  {
    "event_id": "evt_28f48a9ebb7f3ca5",
    "event": "invoice.confirmed",
    "event_created_at": 1793325619,
    "previous_status": "seen",
    "invoice": { "…full invoice object incl. payments[]…" }
  }
  ```

  The `invoice` object is identical to
  `GET /api/v1/invoices/{id}` (see [API.md](API.md)). For `webhook.test`
  it is `null`.

  **Deduping**: `event_id` and `event_created_at` are assigned once, at
  enqueue time — every retry and every endpoint of the same event carries
  the identical pair, so process by `event_id` idempotently.
  `previous_status` is the invoice's status immediately before the
  transition (`null` for `webhook.test`).

- Headers:

  | Header                | Value |
  |-----------------------|-------|
  | `Content-Type`        | `application/json` |
  | `User-Agent`          | `OalgoPay/0.1` |
  | `X-OalgoPay-Event`    | event name |
  | `X-OalgoPay-Event-Id` | same `event_id` as the body |
  | `X-OalgoPay-Invoice`  | invoice id (empty for `webhook.test`) |
  | `X-OalgoPay-Signature`| `t=<unix>,v1=<hex>` |

- Retries: any non-2xx or connection error retries with backoff
  **30 s → 2 m → 10 m → 30 m → 2 h** (5 attempts), then the delivery is
  permanently failed but kept in the panel's delivery log with the last
  error. The worker runs every 5 s.

## Verifying signatures

`v1 = HMAC-SHA256(endpoint_secret, "<t>" + "." + raw_request_body)` — hex
encoded. Always compare in constant time and enforce a replay window on `t`.

Python:

```python
import hashlib, hmac, time

def verify(secret: str, header: str, raw: bytes, tolerance: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(","))
    t, v1 = parts["t"], parts["v1"]
    if abs(time.time() - int(t)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(v1, expected)
```

Node:

```js
const crypto = require("crypto");

function verify(secret, header, raw) {
  const parts = Object.fromEntries(header.split(",").map(p => p.split("=")));
  const expected = crypto.createHmac("sha256", secret)
                         .update(parts.t + "." + raw.toString())
                         .digest("hex");
  const fresh = Math.abs(Date.now() / 1000 - Number(parts.t)) < 300;
  return fresh && crypto.timingSafeEqual(
    Buffer.from(expected), Buffer.from(parts.v1));
}
```

## Local testing

Minimal receiver to try it out:

```python
# uv run python receiver.py  (then add http://127.0.0.1:9000/hook in the panel)
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        print(self.headers.get("X-OalgoPay-Event"), json.loads(raw)["invoice"]["id"])
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass

HTTPServer(("127.0.0.1", 9000), H).serve_forever()
```

Or run the full real-money e2e (`scripts/e2e_test.py`), which registers its
own receiver and asserts both transitions arrive with valid signatures.
