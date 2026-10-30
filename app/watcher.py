"""Payment confirmation watcher: SSE new_block + 60s poll + 30s expiry sweep
+ startup reconciliation (2016-block window)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from . import db, invoices, webhooks
from .blocknet import BlocknetError

log = logging.getLogger("oalgopay.watcher")

POLL_INTERVAL = 60.0
SWEEP_INTERVAL = 30.0
RECONCILE_WINDOW = 2016  # blocks
PAGE_LIMIT = 100

MATCHABLE_STATUSES = ("pending", "seen", "expired")


def _wallet_outputs(page: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(page, dict):
        return []
    outputs = page.get("outputs") or page.get("items") or []
    return [o for o in outputs if isinstance(o, dict) and o.get("txid")]


async def poll_wallet(state: Any) -> None:
    """One matcher pass over wallet history page 1. Idempotent; serialized by lock."""
    async with state.watch_lock:
        status = await state.client.get_status()
        chain_height = int(status.get("chain_height", 0) or 0)
        state.chain_height = chain_height
        state.node_ok = True
        state.node_error = ""
        state.peers = status.get("peers", 0)
        state.syncing = bool(status.get("syncing", False))

        address = db.get_setting("last_known_address", "")
        try:
            address = await state.client.wallet_address()
            db.set_setting("last_known_address", address)
            state.wallet_locked = False
        except BlocknetError as exc:
            if exc.kind == "locked":
                state.wallet_locked = True
                state.node_error = "wallet is locked (unlock in the Blocknet GUI)"
            else:
                raise
        state.address = address

        page = await state.client.history_page(limit=PAGE_LIMIT)
        outputs = _wallet_outputs(page)
        _run_matcher(state, outputs, chain_height)


def _run_matcher(state: Any, outputs: list[dict[str, Any]], chain_height: int) -> None:
    active = db.list_invoices(statuses=MATCHABLE_STATUSES, limit=100000)
    if not active:
        return
    payments_by_invoice = db.payments_for_invoices([i["id"] for i in active])
    existing_keys = db.get_payment_keys()
    settings = db.get_settings()
    now = int(time.time())
    result = invoices.match_outputs(
        outputs, chain_height, active, payments_by_invoice, existing_keys,
        settings, now=now,
    )
    inserted: set[tuple[str, int]] = set()
    event_invoice_ids: set[str] = set()
    for p in result["payments"]:
        if db.insert_payment(p):
            inserted.add((p["txid"], p["output_index"]))
            event_invoice_ids.add(p["invoice_id"])
        else:
            log.debug("payment %s:%s already credited, skipping", p["txid"], p["output_index"])
    for inv_id, fields in result["invoice_updates"].items():
        db.update_invoice(inv_id, fields)
        if fields.get("status") == "confirmed":
            event_invoice_ids.add(inv_id)
    for e in result["events"]:
        if (e["payment"]["txid"], e["payment"]["output_index"]) in inserted:
            webhooks.enqueue_event(state, e["event"], e["invoice_id"], e.get("previous_status"))
    if result["payments"]:
        log.info(
            "matched %d payment(s) for invoice(s) %s",
            len(result["payments"]),
            ", ".join(sorted(event_invoice_ids)),
        )

    # Confirmation sweep: payments matched below the threshold confirm as
    # blocks land, even when no new outputs arrive.
    reloaded = db.list_invoices(statuses=("seen", "expired"), limit=100000)
    if reloaded:
        re_pays = db.payments_for_invoices([i["id"] for i in reloaded])
        sweep = invoices.recompute_confirmations(reloaded, re_pays, chain_height, settings, now)
        for inv_id, fields in sweep["invoice_updates"].items():
            db.update_invoice(inv_id, fields)
        for e in sweep["events"]:
            webhooks.enqueue_event(state, e["event"], e["invoice_id"], e.get("previous_status"))
        if sweep["invoice_updates"]:
            log.info(
                "confirmed invoice(s) %s (%d confs at height %d)",
                ", ".join(sorted(sweep["invoice_updates"])),
                int(settings.get("confirmations_required", "10")),
                chain_height,
            )


async def reconcile(state: Any) -> None:
    """Catch up payments that arrived while the gateway was down: walk history
    newest-first up to RECONCILE_WINDOW blocks back or page exhaustion."""
    try:
        async with state.watch_lock:
            status = await state.client.get_status()
            chain_height = int(status.get("chain_height", 0) or 0)
            state.chain_height = chain_height
            state.node_ok = True
            floor = max(0, chain_height - RECONCILE_WINDOW)
            collected: list[dict[str, Any]] = []
            offset = 0
            total = None
            while True:
                page = await state.client.history_page(limit=PAGE_LIMIT, offset=offset)
                outputs = _wallet_outputs(page)
                if total is None:
                    total = page.get("total") if isinstance(page, dict) else None
                if not outputs:
                    break
                heights = [int(o.get("block_height", 0) or 0) for o in outputs]
                in_window = [o for o, h in zip(outputs, heights) if h == 0 or h >= floor]
                collected.extend(in_window)
                oldest = min(heights)
                offset += len(outputs)
                if oldest > 0 and oldest < floor:
                    break
                if total is not None and offset >= int(total):
                    break
                if offset > 10000:
                    break
            log.info(
                "reconciliation: %d output(s) within %d blocks (chain %d)",
                len(collected), RECONCILE_WINDOW, chain_height,
            )
            _run_matcher(state, collected, chain_height)
    except BlocknetError as exc:
        state.node_ok = False
        state.node_error = str(exc)
        log.warning("reconciliation failed: %s", exc)


def expiry_sweep(state: Any) -> None:
    now = int(time.time())
    stale = db.list_invoices(statuses=("pending", "seen"), limit=100000)
    expired_ids = [
        i["id"] for i in stale if i["paid_atomic"] == 0 and i["expires_at"] < now
    ]
    for inv_id in expired_ids:
        db.update_invoice(inv_id, {"status": "expired"})
        webhooks.enqueue_event(state, "invoice.expired", inv_id, "pending")
        log.info("invoice %s expired", inv_id)


async def sse_loop(state: Any) -> None:
    """Consume daemon SSE; on new_block run a matcher pass. Also drives initial reconcile."""
    await reconcile(state)
    backoff_warned = False
    while True:
        try:
            async for event, data in state.client.stream_events():
                if not state.node_ok:
                    backoff_warned = False
                state.node_ok = True
                state.node_error = ""
                if data.get("chain_height"):
                    state.chain_height = int(data["chain_height"])
                if event == "new_block":
                    try:
                        await poll_wallet(state)
                    except BlocknetError as exc:
                        state.node_ok = False
                        state.node_error = str(exc)
                        log.warning("poll after new_block failed: %s", exc)
        except Exception as exc:  # stream_events reconnects internally; this is a safety net
            if not backoff_warned:
                log.warning("SSE consumer error: %s", exc)
                backoff_warned = True
        await asyncio.sleep(5.0)


async def poll_loop(state: Any) -> None:
    """Fallback poll for mempool-only changes (SSE only fires on new blocks)."""
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            await poll_wallet(state)
        except BlocknetError as exc:
            state.node_ok = False
            state.node_error = str(exc)
            log.warning("poll failed: %s", exc)
        except Exception:
            log.exception("poll loop error")


async def sweep_loop(state: Any) -> None:
    while True:
        try:
            expiry_sweep(state)
        except Exception:
            log.exception("expiry sweep error")
        await asyncio.sleep(SWEEP_INTERVAL)


def start(state: Any) -> list[asyncio.Task]:
    state.watch_lock = asyncio.Lock()
    return [
        asyncio.create_task(sse_loop(state), name="watcher-sse"),
        asyncio.create_task(poll_loop(state), name="watcher-poll"),
        asyncio.create_task(sweep_loop(state), name="watcher-sweep"),
    ]
