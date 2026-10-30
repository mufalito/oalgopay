"""First-run setup wizard: /setup.

Active only while the gateway is unconfigured (placeholder secrets). The
wizard prefills sane defaults, live-checks the core connection, writes
config.json (0600), applies everything to the running process, then sends
the user to the admin login.
"""
from __future__ import annotations

import json
import os
import re
import secrets as pysecrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import config as config_mod
from .. import db
from ..blocknet import BlocknetClient, BlocknetError

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["setup"])


def _setup_mode(request: Request) -> bool:
    return bool(getattr(request.app.state, "setup_mode", False))


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    if not _setup_mode(request):
        return RedirectResponse("/admin", status_code=302)
    cfg = request.app.state.config
    base = str(request.base_url).rstrip("/")
    return templates.TemplateResponse(request, "setup.html", {
        "api_base": cfg.get("api_base", "http://127.0.0.1:18512"),
        "datadir": cfg.get("datadir", config_mod.detect_datadir()),
        "public_base_url": cfg.get("public_base_url") or base,
        "suggest_pw": pysecrets.token_hex(8),
        "suggest_token": pysecrets.token_hex(24),
        "confirmations": db.get_setting("confirmations_required", "10"),
        "expiry": db.get_setting("expiry_minutes", "60"),
        "display_handle": db.get_setting("display_handle", ""),
    })


@router.get("/setup/check", response_class=HTMLResponse)
async def setup_check(request: Request, api_base: str = "", datadir: str = ""):
    """Live fragment: is the core reachable at api_base, and is the wallet
    loaded+unlocked there (cookie read from datadir)?"""
    rows: list[dict[str, Any]] = []
    if not api_base.startswith(("http://", "https://")):
        rows.append({"ok": False, "k": "Core API", "v": "address must start with http://"})
        return templates.TemplateResponse(request, "frags/_setup_check.html", {"rows": rows})
    client = BlocknetClient(api_base.rstrip("/"), datadir)
    try:
        status = await client.get_status()
        rows.append({
            "ok": True, "k": "Core API",
            "v": f"connected · height {status.get('chain_height')} · {status.get('peers')} peers",
        })
    except BlocknetError as exc:
        rows.append({"ok": False, "k": "Core API", "v": str(exc)})
    try:
        addr = await client.wallet_address()
        rows.append({"ok": True, "k": "Wallet", "v": f"loaded + unlocked · {addr[:20]}…"})
    except BlocknetError as exc:
        msg = str(exc)
        if "locked" in msg:
            rows.append({"ok": False, "k": "Wallet", "v": "loaded but LOCKED — unlock it, payments can't be watched"})
        elif "no wallet loaded" in msg:
            rows.append({"ok": False, "k": "Wallet", "v": "no wallet loaded — load it on the core to accept payments"})
        else:
            rows.append({"ok": False, "k": "Wallet", "v": msg})
    finally:
        await client.aclose()
    return templates.TemplateResponse(request, "frags/_setup_check.html", {"rows": rows})


_HANDLE_RE = re.compile(r"^\$[A-Za-z0-9_.-]{1,64}$")


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if raw[:1] in (b"{",):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            pass
    import urllib.parse

    pairs = urllib.parse.parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True)
    out: dict[str, Any] = {}
    for k, v in pairs:
        if v.isdigit() and k in ("confirmations_required", "expiry_minutes"):
            out[k] = int(v)
        else:
            out[k] = v
    return out


@router.post("/setup")
async def setup_submit(request: Request):
    if not _setup_mode(request):
        raise HTTPException(status_code=409, detail="setup already completed")
    params = await _body(request)

    errors: list[str] = []
    admin_password = str(params.get("admin_password", "")).strip()
    api_token = str(params.get("api_token", "")).strip()
    api_base = str(params.get("api_base", "")).strip().rstrip("/")
    datadir = str(params.get("datadir", "")).strip()
    public_base_url = str(params.get("public_base_url", "")).strip().rstrip("/")
    display_handle = str(params.get("display_handle", "")).strip()
    webhook_url = str(params.get("webhook_url", "")).strip()
    wallet_path = str(params.get("wallet_path", "")).strip()
    wallet_password = str(params.get("wallet_password", ""))
    confirmations = params.get("confirmations_required", 10)
    expiry = params.get("expiry_minutes", 60)

    if len(admin_password) < 8:
        errors.append("admin password must be at least 8 characters")
    if len(api_token) < 16:
        errors.append("API token must be at least 16 characters")
    if not api_base.startswith(("http://", "https://")):
        errors.append("core API address must start with http:// or https://")
    if not datadir:
        errors.append("core data directory is required (where api.cookie lives)")
    if public_base_url and not public_base_url.startswith(("http://", "https://")):
        errors.append("public base URL must start with http:// or https://")
    if display_handle and not _HANDLE_RE.match(display_handle):
        errors.append("display handle must look like $name (or be left blank)")
    if webhook_url and not webhook_url.startswith(("http://", "https://")):
        errors.append("webhook URL must start with http:// or https://")
    if not isinstance(confirmations, int) or not 1 <= confirmations <= 20:
        errors.append("confirmations required must be 1–20")
    if not isinstance(expiry, int) or not 1 <= expiry <= 10080:
        errors.append("expiry minutes must be 1–10080")

    if errors:
        resp = templates.TemplateResponse(
            request, "frags/_setup_check.html",
            {"rows": [{"ok": False, "k": "Fix these", "v": "; ".join(errors)}]},
        )
        resp.status_code = 200  # keep the wizard on the same page
        return resp

    # persist config.json
    state = request.app.state
    cfg = state.config
    cfg.update({
        "api_base": api_base,
        "datadir": datadir,
        "admin_password": admin_password,
        "api_token": api_token,
        "public_base_url": public_base_url or str(request.base_url).rstrip("/"),
    })
    cfg_path = config_mod.DEFAULT_CONFIG_PATH
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
    os.chmod(cfg_path, 0o600)

    # apply to the running process
    config_mod.ensure_data_dir(cfg)
    await state.client.aclose()
    state.client = BlocknetClient(cfg["api_base"], cfg["datadir"])
    state.setup_mode = False
    db.set_setting("confirmations_required", str(confirmations))
    db.set_setting("expiry_minutes", str(expiry))
    db.set_setting("display_handle", display_handle)
    if webhook_url:
        db.create_webhook_endpoint(webhook_url, pysecrets.token_hex(32))

    wallet_note = ""
    wallet_failed = False
    if wallet_path and wallet_password:
        try:
            result = await state.client.wallet_load(wallet_path, wallet_password)
            wallet_note = f"Wallet {result} on the core. "
        except BlocknetError as exc:
            wallet_note = f"Wallet NOT loaded — {exc}. "
            wallet_failed = True
    elif wallet_path or wallet_password:
        wallet_note = "Wallet NOT loaded — both file and password are required. "
        wallet_failed = True

    if wallet_failed:
        # stay on the page so the message is actually read; setup itself is done
        return HTMLResponse(
            f'<div class="banner">Setup complete — {wallet_note}'
            f'The admin panel is at <a href="/admin">/admin</a> '
            f'(password you set above).</div>'
        )
    resp = HTMLResponse(
        f'<div class="banner ok">Setup complete — {wallet_note}opening the admin panel…</div>'
    )
    resp.headers["HX-Redirect"] = "/admin"
    return resp
