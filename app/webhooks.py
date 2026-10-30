"""Signed outbound webhooks: enqueue, retry with backoff, HMAC-SHA256 signatures."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any

import httpx

from . import db, invoices

log = logging.getLogger("oalgopay.webhooks")

RETRY_BACKOFF = [30, 120, 600, 1800, 7200]  # 30s, 2m, 10m, 30m, 2h
MAX_ATTEMPTS = 5
WORKER_INTERVAL = 5.0


def sign_payload(secret: str, timestamp: int, raw_body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def build_event_payload(
    state: Any,
    event: str,
    invoice_id: str | None,
    event_id: str,
    event_created_at: int,
    previous_status: str | None,
) -> dict[str, Any]:
    """Envelope for a delivery. event_id / event_created_at / previous_status
    are fixed at enqueue time, so retries carry an identical, dedupable header."""
    payload: dict[str, Any] = {
        "event_id": event_id,
        "event": event,
        "event_created_at": event_created_at,
        "previous_status": previous_status,
    }
    if invoice_id:
        inv = db.get_invoice(invoice_id)
        if inv:
            payments = db.payments_for_invoice(invoice_id)
            payload["invoice"] = invoices.invoice_to_json(
                inv,
                payments,
                settings=db.get_settings(),
                public_base_url=state.config["public_base_url"],
                address=db.get_setting("last_known_address", ""),
                chain_height=getattr(state, "chain_height", None),
            )
            return payload
    payload["invoice"] = None
    return payload


def enqueue_event(
    state: Any,
    event: str,
    invoice_id: str | None = None,
    previous_status: str | None = None,
) -> None:
    """Fan an event out to every active endpoint (delivery rows; worker sends them)."""
    now = int(time.time())
    event_id = "evt_" + secrets.token_hex(8)
    for ep in db.list_webhook_endpoints(active_only=True):
        db.enqueue_delivery(ep["id"], event, invoice_id, now, event_id, previous_status)


async def process_deliveries(state: Any) -> int:
    """One pass over due, undelivered rows. Returns number processed."""
    now = int(time.time())
    due = db.pending_deliveries(now)
    sent = 0
    async with httpx.AsyncClient(timeout=10.0) as http:
        for d in due:
            payload = build_event_payload(
                state, d["event"], d["invoice_id"],
                d["event_id"], d["created_at"], d["previous_status"],
            )
            raw = json.dumps(payload, separators=(",", ":")).encode()
            ts = int(time.time())
            sig = sign_payload(d["endpoint_secret"], ts, raw)
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "OalgoPay/0.1",
                "X-OalgoPay-Event": d["event"],
                "X-OalgoPay-Event-Id": d["event_id"] or "",
                "X-OalgoPay-Invoice": d["invoice_id"] or "",
                "X-OalgoPay-Signature": sig,
            }
            attempts = d["attempts"] + 1
            try:
                resp = await http.post(d["endpoint_url"], content=raw, headers=headers)
                status, err = resp.status_code, ""
            except httpx.HTTPError as exc:
                status, err = None, f"{type(exc).__name__}: {exc}"
            if status is not None and 200 <= status < 300:
                db.update_delivery(
                    d["id"],
                    {
                        "attempts": attempts,
                        "last_status": status,
                        "last_error": "",
                        "delivered_at": int(time.time()),
                        "next_attempt_at": int(time.time()),
                    },
                )
                log.info("webhook %s [%s] delivered to %s (HTTP %s)", d["id"], d["event"], d["endpoint_url"], status)
            elif attempts >= MAX_ATTEMPTS:
                db.update_delivery(
                    d["id"],
                    {
                        "attempts": attempts,
                        "last_status": status,
                        "last_error": err or f"HTTP {status}",
                        "next_attempt_at": 2**31 - 1,  # permanent fail; row kept in log
                    },
                )
                log.warning("webhook %s permanently failed after %s attempts", d["id"], attempts)
            else:
                delay = retry_backoff(min(attempts, len(RETRY_BACKOFF)) - 1)
                db.update_delivery(
                    d["id"],
                    {
                        "attempts": attempts,
                        "last_status": status,
                        "last_error": err or f"HTTP {status}",
                        "next_attempt_at": now + delay,
                    },
                )
            sent += 1
    return sent


def retry_backoff(i: int) -> int:
    return RETRY_BACKOFF[max(0, min(i, len(RETRY_BACKOFF) - 1))]


async def worker_loop(state: Any) -> None:
    log.info("webhook worker started")
    while True:
        try:
            await process_deliveries(state)
        except Exception:
            log.exception("webhook worker pass failed")
        await asyncio.sleep(WORKER_INTERVAL)
