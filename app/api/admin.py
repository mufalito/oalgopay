"""Admin panel: htmx fragments + actions (session-cookie auth).

JSON surface kept for programmatic use (e2e driver): /admin/api/overview,
/admin/api/settings (GET/PATCH), /admin/api/webhooks (POST/DELETE/{id} DELETE).
Everything else renders Jinja fragments swapped in by htmx.
"""
from __future__ import annotations

import hmac
import json
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db, handle, invoices, webhooks
from .schemas import SettingsOut, WebhookCreateRequest, WebhookOut
from ..auth import SESSION_COOKIE, check_session_token, make_session_token, require_admin
from ..blocknet import BlocknetError
from .merchant import cancel_invoice_service, create_invoice_service, serialize_invoice

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["admin"])


async def body_params(request: Request) -> dict[str, Any]:
    """Accept htmx form-encoding or JSON bodies interchangeably.

    Parses application/x-www-form-urlencoded directly (no python-multipart
    dependency; this gateway has no file uploads).
    """
    raw = await request.body()
    out: dict[str, Any] = {}
    if not raw:
        return out
    if raw[:1] in (b"{", b"["):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            pass
    try:
        pairs = urllib.parse.parse_qsl(raw.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        return {}
    for k, v in pairs:
        if v.isdigit() and k in (
            "confirmations_required", "expiry_minutes", "amount_fallback_matching",
            "expires_in_min", "active",
        ):
            out[k] = int(v)
        else:
            out[k] = v
    return out


def _is_admin(request: Request) -> bool:
    return check_session_token(request.cookies.get(SESSION_COOKIE), request.app.state.session_secret)


# --- session + shell --------------------------------------------------------

@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    if getattr(request.app.state, "setup_mode", False):
        return RedirectResponse("/setup", status_code=302)
    if not _is_admin(request):
        return templates.TemplateResponse(request, "login.html", {})
    return templates.TemplateResponse(request, "admin.html", {})


@router.post("/admin/login")
async def login(request: Request, response: Response):
    if getattr(request.app.state, "setup_mode", False):
        raise HTTPException(status_code=403, detail="setup required — open /setup")
    params = await body_params(request)
    password = str(params.get("password", ""))
    if not hmac.compare_digest(password, request.app.state.config["admin_password"]):
        if request.headers.get("HX-Request"):
            return HTMLResponse(
                '<div class="text-sm text-peach-700" id="login-error">wrong password</div>', status_code=200
            )
        raise HTTPException(status_code=401, detail="wrong password")
    token = make_session_token(request.app.state.session_secret)
    response.set_cookie(SESSION_COOKIE, token, max_age=12 * 3600, httponly=True, samesite="lax")
    if request.headers.get("HX-Request"):
        resp = HTMLResponse('<div id="login-error"></div>')
        resp.headers["HX-Redirect"] = "/admin"
        resp.set_cookie(SESSION_COOKIE, token, max_age=12 * 3600, httponly=True, samesite="lax")
        return resp
    return {"ok": True}


@router.post("/admin/logout")
async def logout(request: Request, response: Response):
    response.delete_cookie(SESSION_COOKIE)
    if request.headers.get("HX-Request"):
        resp = HTMLResponse("")
        resp.headers["HX-Redirect"] = "/admin"
        return resp
    return {"ok": True}


def frag(request: Request, template: str, ctx: dict[str, Any]) -> HTMLResponse:
    return HTMLResponse(templates.env.get_template(template).render(**ctx))


# --- fragments --------------------------------------------------------------

@router.get("/admin/frag/overview", response_class=HTMLResponse)
async def frag_overview(request: Request):
    require_admin(request)
    state = request.app.state
    node: dict[str, Any] = {"ok": False, "error": getattr(state, "node_error", "") or "unreachable"}
    wallet: dict[str, Any] = {"address": db.get_setting("last_known_address", ""), "locked": getattr(state, "wallet_locked", False)}
    try:
        status = await state.client.get_status()
        node = {
            "ok": True, "height": status.get("chain_height"), "peers": status.get("peers"),
            "syncing": status.get("syncing"), "mempool_size": status.get("mempool_size"),
        }
    except BlocknetError as exc:
        node["error"] = str(exc)
    try:
        address = await state.client.wallet_address()
        db.set_setting("last_known_address", address)
        wallet["address"], wallet["locked"] = address, False
    except BlocknetError as exc:
        if exc.kind == "locked":
            wallet["locked"] = True
        elif "no wallet loaded" in str(exc):
            wallet["not_loaded"] = True
        else:
            wallet["error"] = str(exc)
    try:
        wallet["balance"] = await state.client.wallet_balance()
    except BlocknetError:
        wallet["balance"] = None

    settings = db.get_settings()
    display_handle = settings.get("display_handle", "")
    handle_info = await handle.resolve_handle(display_handle) if display_handle else None
    counts = {s: 0 for s in ("pending", "seen", "confirmed", "expired", "cancelled")}
    for row in db.query_all("SELECT status, COUNT(*) AS n FROM invoices GROUP BY status"):
        counts[row["status"]] = row["n"]
    return frag(request, "frags/_overview.html", {
        "node": node, "wallet": wallet, "handle_info": handle_info, "counts": counts,
    })


def _fmt_balance(balance: Any) -> list[tuple[str, str]]:
    """Render only the amount fields of the daemon balance dict as BNT —
    the rest (heights, counters, flags) are not amounts."""
    from ..invoices import format_bnt

    if not isinstance(balance, dict):
        return []
    wanted = ("total", "spendable", "pending", "pending_unconfirmed")
    return [(k, format_bnt(balance[k])) for k in wanted if isinstance(balance.get(k), int)]


templates.env.globals["fmt_balance"] = _fmt_balance
templates.env.globals["fmt_time"] = lambda unix: (
    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(unix)) if unix else "—"
)
templates.env.globals["fmt_bnt"] = lambda atoms: f"{atoms / 1e8:.8f}".rstrip("0").rstrip(".") or "0"


@router.get("/admin/frag/invoices", response_class=HTMLResponse)
async def frag_invoices(request: Request, status: str = "", limit: int = 50):
    require_admin(request)
    rows = db.list_invoices(status=status or None, limit=max(1, min(limit, 200)))
    return frag(request, "frags/_invoices.html", {"invoices": rows, "status": status})


@router.get("/admin/frag/invoice/{inv_id}", response_class=HTMLResponse)
async def frag_invoice(request: Request, inv_id: str):
    require_admin(request)
    inv = db.get_invoice(inv_id)
    if not inv:
        raise HTTPException(status_code=404, detail="invoice not found")
    out = serialize_invoice(request, inv)
    out["deliveries"] = [
        {
            "id": d["id"], "event": d["event"], "attempts": d["attempts"],
            "last_status": d["last_status"], "last_error": d["last_error"],
            "delivered_at": d["delivered_at"], "created_at": d["created_at"],
        }
        for d in db.deliveries_for_invoice(inv_id)
    ]
    return frag(request, "frags/_invoice_detail.html", {"inv": out})


@router.post("/admin/frag/invoice", response_class=HTMLResponse)
async def frag_create_invoice(request: Request):
    require_admin(request)
    params = await body_params(request)
    body: dict[str, Any] = {}
    if params.get("amount_bnt"):
        body["amount_bnt"] = str(params["amount_bnt"])
    if params.get("label"):
        body["label"] = str(params["label"])
    if params.get("expires_in_min"):
        body["expires_in_min"] = int(params["expires_in_min"])
    invoice = create_invoice_service(body, request)
    rows = db.list_invoices(limit=50)
    table_html = templates.env.get_template("frags/_invoices_table.html").render(
        invoices=rows,
        inv={**invoice, "payments": invoice.get("payments", []), "deliveries": []},
    )
    return HTMLResponse(
        f'<div id="inv-table-wrap" hx-swap-oob="innerHTML">{table_html}</div>'
    )


@router.post("/admin/frag/invoice/{inv_id}/cancel", response_class=HTMLResponse)
async def frag_cancel_invoice(inv_id: str, request: Request):
    require_admin(request)
    cancel_invoice_service(inv_id, request)
    return await frag_invoice(request, inv_id)


@router.post("/admin/frag/invoice/{inv_id}/confirm", response_class=HTMLResponse)
async def frag_confirm_invoice(inv_id: str, request: Request):
    """Manual override: mark confirmed, note the override in metadata."""
    require_admin(request)
    inv = db.get_invoice(inv_id)
    if not inv:
        raise HTTPException(status_code=404, detail="invoice not found")
    if inv["status"] not in ("confirmed", "cancelled"):
        try:
            meta = json.loads(inv["metadata"]) if inv["metadata"] else {}
        except json.JSONDecodeError:
            meta = {}
        meta["manual_confirm"] = {
            "at": int(time.time()), "previous_status": inv["status"],
            "paid_atomic": inv["paid_atomic"],
        }
        db.update_invoice(inv_id, {
            "status": "confirmed", "confirmed_at": int(time.time()),
            "metadata": json.dumps(meta),
        })
        webhooks.enqueue_event(
            request.app.state, "invoice.confirmed", inv_id,
            previous_status=inv["status"],
        )
    return await frag_invoice(request, inv_id)


@router.post("/admin/frag/invoice/{inv_id}/resend", response_class=HTMLResponse)
async def frag_resend_webhooks(inv_id: str, request: Request):
    require_admin(request)
    inv = db.get_invoice(inv_id)
    if not inv:
        raise HTTPException(status_code=404, detail="invoice not found")
    webhooks.enqueue_event(request.app.state, "invoice.updated", inv_id, inv["status"])
    return await frag_invoice(request, inv_id)


def _webhooks_ctx() -> dict[str, Any]:
    endpoints = [
        {
            "id": e["id"], "url": e["url"], "active": bool(e["active"]),
            "created_at": e["created_at"], "secret_hint": e["secret"][:4] + "…",
        }
        for e in db.list_webhook_endpoints()
    ]
    deliveries = [
        {
            "id": d["id"], "event": d["event"], "invoice_id": d["invoice_id"],
            "attempts": d["attempts"], "last_status": d["last_status"],
            "last_error": d["last_error"], "delivered_at": d["delivered_at"],
        }
        for d in db.list_deliveries(50)
    ]
    return {"endpoints": endpoints, "deliveries": deliveries}


@router.get("/admin/frag/webhooks", response_class=HTMLResponse)
async def frag_webhooks(request: Request):
    require_admin(request)
    return frag(request, "frags/_webhooks.html", _webhooks_ctx())


@router.post("/admin/frag/webhook", response_class=HTMLResponse)
async def frag_create_webhook(request: Request):
    require_admin(request)
    params = await body_params(request)
    url = str(params.get("url", ""))
    if not url.startswith(("http://", "https://")):
        return HTMLResponse('<div class="banner-err">url must be an http(s) URL</div>', status_code=422)
    secret = secrets.token_hex(32)
    db.create_webhook_endpoint(url, secret)
    note = f'<div class="banner-warn mb-4 break-all">Endpoint signing secret (shown once): <b class="font-mono">{secret}</b></div>'
    return HTMLResponse(
        note + templates.env.get_template("frags/_webhooks.html").render(**_webhooks_ctx())
    )


@router.post("/admin/frag/webhook/{ep_id}/toggle", response_class=HTMLResponse)
async def frag_toggle_webhook(ep_id: int, request: Request):
    require_admin(request)
    ep = db.get_webhook_endpoint(ep_id)
    if not ep:
        raise HTTPException(status_code=404, detail="endpoint not found")
    db.update_webhook_endpoint(ep_id, {"active": 0 if ep["active"] else 1})
    return await frag_webhooks(request)


@router.post("/admin/frag/webhook/{ep_id}/test", response_class=HTMLResponse)
async def frag_test_webhook(ep_id: int, request: Request):
    require_admin(request)
    if not db.get_webhook_endpoint(ep_id):
        raise HTTPException(status_code=404, detail="endpoint not found")
    webhooks.enqueue_event(request.app.state, "webhook.test", None)
    return await frag_webhooks(request)


@router.post("/admin/frag/webhook/{ep_id}/delete", response_class=HTMLResponse)
async def frag_delete_webhook(ep_id: int, request: Request):
    require_admin(request)
    if not db.get_webhook_endpoint(ep_id):
        raise HTTPException(status_code=404, detail="endpoint not found")
    db.delete_webhook_endpoint(ep_id)
    return await frag_webhooks(request)


@router.get("/admin/frag/settings", response_class=HTMLResponse)
async def frag_settings(request: Request, saved: bool = False):
    require_admin(request)
    return frag(request, "frags/_settings.html", {"settings": db.get_settings(), "saved": saved})


SETTING_VALIDATORS = {
    "confirmations_required": lambda v: isinstance(v, int) and 1 <= v <= 20,
    "expiry_minutes": lambda v: isinstance(v, int) and 1 <= v <= 10080,
    "amount_fallback_matching": lambda v: isinstance(v, int) and v in (0, 1),
    "display_handle": lambda v: isinstance(v, str) and (v == "" or (v.startswith("$") and len(v) > 1)),
}


def apply_settings(body: dict[str, Any]) -> dict[str, str]:
    errors = []
    for key, value in body.items():
        if key not in SETTING_VALIDATORS:
            errors.append(f"unknown setting {key}")
        elif not SETTING_VALIDATORS[key](value):
            errors.append(f"invalid value for {key}")
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))
    for key, value in body.items():
        db.set_setting(key, str(value))
    return db.get_settings()


@router.post("/admin/frag/settings", response_class=HTMLResponse)
async def frag_save_settings(request: Request):
    require_admin(request)
    params = await body_params(request)
    apply_settings(params)
    return frag(request, "frags/_settings.html", {"settings": db.get_settings(), "saved": True})


# --- wallet reload (one-shot pass-through, password never stored) -----------

@router.post("/admin/frag/wallet-load", response_class=HTMLResponse)
async def frag_wallet_load(request: Request):
    """User-initiated wallet load on the core (e.g. after a core restart).
    Forwards path + password to the core once and discards them."""
    require_admin(request)
    params = await body_params(request)
    path = str(params.get("wallet_path", "")).strip()
    password = str(params.get("wallet_password", ""))
    if not path or not password:
        overview_html = (await frag_overview(request)).body.decode("utf-8")
        return HTMLResponse(
            '<div class="banner-warn">Wallet file and password are both required.</div>' + overview_html
        )
    try:
        result = await request.app.state.client.wallet_load(path, password)
        banner = f'<div class="banner-ok">Wallet {result} — payments are being watched again.</div>'
    except BlocknetError as exc:
        banner = f'<div class="banner-err">Wallet not loaded: {exc}</div>'
    overview_html = (await frag_overview(request)).body.decode("utf-8")
    return HTMLResponse(banner + overview_html)


# --- send (manual payouts/refunds) -----------------------------------------

@router.get("/admin/frag/send", response_class=HTMLResponse)
async def frag_send_view(request: Request):
    require_admin(request)
    return frag(request, "frags/_send.html", _send_ctx(None, None))


def _send_ctx(result: dict[str, Any] | None, error: str | None) -> dict[str, Any]:
    return {"payouts": db.list_payouts(), "result": result, "error": error}


@router.post("/admin/frag/send", response_class=HTMLResponse)
async def frag_send(request: Request):
    """Spend wallet funds to an arbitrary address. Admin-only; every send
    carries a fresh daemon Idempotency-Key and is recorded in the payouts log.
    Sending to this gateway's own address with an INV- memo will credit that
    invoice as paid — use memos deliberately."""
    require_admin(request)
    import uuid

    params = await body_params(request)
    address = str(params.get("address", "")).strip()
    memo = str(params.get("memo", "")).strip() or None
    try:
        amount_atomic = invoices.parse_amount_bnt(str(params.get("amount_bnt", "")))
    except invoices.AmountError as exc:
        return frag(request, "frags/_send.html", _send_ctx(None, f"amount: {exc}"))
    if not address:
        return frag(request, "frags/_send.html", _send_ctx(None, "address is required"))

    key = str(uuid.uuid4())
    recipient: dict[str, Any] = {"address": address, "amount": amount_atomic}
    if memo:
        recipient["memo_text"] = memo
    try:
        sent = await request.app.state.client.send([recipient], key)
    except BlocknetError as exc:
        db.insert_payout({
            "created_at": int(time.time()), "address": address,
            "amount_atomic": amount_atomic, "memo": memo, "txid": None,
            "fee_atomic": None, "idempotency_key": key, "error": str(exc),
        })
        return frag(request, "frags/_send.html", _send_ctx(None, f"daemon rejected: {exc}"))
    fee = sent.get("fee")
    txid = sent.get("txid")
    db.insert_payout({
        "created_at": int(time.time()), "address": address,
        "amount_atomic": amount_atomic, "memo": memo, "txid": txid,
        "fee_atomic": fee if isinstance(fee, int) else None,
        "idempotency_key": key, "error": None,
    })
    result = {
        "txid": txid,
        "amount_atomic": amount_atomic,
        "fee_atomic": fee if isinstance(fee, int) else None,
        "address": address,
    }
    return frag(request, "frags/_send.html", _send_ctx(result, None))


# --- minimal JSON surface (programmatic: e2e driver, scripts) ---------------

@router.get("/admin/api/overview")
async def json_overview(request: Request):
    require_admin(request)
    state = request.app.state
    node = {"ok": False, "error": getattr(state, "node_error", "") or "unreachable"}
    try:
        node = {**(await state.client.get_status()), "ok": True}
    except BlocknetError as exc:
        node["error"] = str(exc)
    wallet = {
        "address": db.get_setting("last_known_address", ""),
        "locked": getattr(state, "wallet_locked", False),
    }
    try:
        wallet["balance"] = await state.client.wallet_balance()
    except BlocknetError:
        wallet["balance"] = None
    return {"node": node, "wallet": wallet}


@router.get("/admin/api/settings", response_model=SettingsOut, summary="Current settings")
async def json_get_settings(request: Request):
    require_admin(request)
    return db.get_settings()


@router.patch(
    "/admin/api/settings",
    response_model=SettingsOut,
    summary="Update settings",
    description="Keys: confirmations_required (1–20), expiry_minutes (1–10080), "
    "amount_fallback_matching (0/1), display_handle ('' or '$handle').",
)
async def json_patch_settings(body: dict[str, Any], request: Request):
    require_admin(request)
    return apply_settings(body)


@router.post(
    "/admin/api/webhooks",
    status_code=201,
    response_model=WebhookOut,
    summary="Register webhook endpoint",
)
async def json_create_webhook(body: WebhookCreateRequest, request: Request):
    require_admin(request)
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="url must be an http(s) URL")
    secret = secrets.token_hex(32)
    ep = db.create_webhook_endpoint(body.url, secret)
    return {"id": ep["id"], "url": ep["url"], "active": True, "secret": secret}


@router.delete("/admin/api/webhooks/{ep_id}")
async def json_delete_webhook(ep_id: int, request: Request):
    require_admin(request)
    if not db.get_webhook_endpoint(ep_id):
        raise HTTPException(status_code=404, detail="endpoint not found")
    db.delete_webhook_endpoint(ep_id)
    return {"ok": True}

# htmx fragments and HTML shells are not API surface — keep them out of /docs.
for _route in router.routes:
    _path = getattr(_route, "path", "")
    if "/admin/frag/" in _path or _path in ("/admin",):
        _route.include_in_schema = False
