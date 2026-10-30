"""Public pay page: HTML, QR SVG, status JSON. No auth."""
from __future__ import annotations

import io
from pathlib import Path

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from .. import db, invoices
from .schemas import PayStatusOut

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["pay"])


def _get_invoice_or_404(inv_id: str) -> dict:
    if not invoices.is_valid_id(inv_id):
        raise HTTPException(status_code=404, detail="invoice not found")
    inv = db.get_invoice(inv_id)
    if not inv:
        raise HTTPException(status_code=404, detail="invoice not found")
    return inv


def _public_view(inv: dict, settings: dict[str, str], address: str):
    handle = settings.get("display_handle", "")
    display = handle if handle else address
    return {
        "id": inv["id"],
        "status": inv["status"],
        "amount_atomic": inv["amount_atomic"],
        "amount_bnt": invoices.format_bnt(inv["amount_atomic"]),
        "amount_bnt_padded": invoices.format_bnt_padded(inv["amount_atomic"]),
        "paid_atomic": inv["paid_atomic"],
        "paid_bnt": invoices.format_bnt(inv["paid_atomic"]),
        "memo": inv["memo"],
        "label": inv["label"],
        "expires_at": inv["expires_at"],
        "late_payment": bool(inv["late_payment"]),
        "overpaid": bool(inv["overpaid"]),
        "confirmations_required": int(settings.get("confirmations_required", "10")),
        "display": display,
        "address": address,
        "is_handle": bool(handle),
        "uri": invoices.invoice_uri(address, inv["amount_atomic"], inv["memo"]),
        "bntpay_url": invoices.bntpay_url(handle, address, inv["amount_atomic"], inv["memo"]),
    }


@router.get("/pay/{inv_id}")
async def pay_page(inv_id: str, request: Request):
    inv = _get_invoice_or_404(inv_id)
    settings = db.get_settings()
    address = db.get_setting("last_known_address", "")
    view = _public_view(inv, settings, address)
    sv = await _status_view(inv_id, request)
    return templates.TemplateResponse(request, "pay.html", {"inv": view, "sv": sv})


@router.get("/pay/{inv_id}/qr.svg")
async def pay_qr(inv_id: str):
    inv = _get_invoice_or_404(inv_id)

    address = db.get_setting("last_known_address", "")
    uri = invoices.invoice_uri(address, inv["amount_atomic"], inv["memo"])
    qr = qrcode.QRCode(image_factory=qrcode.image.svg.SvgPathImage, border=2)
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image()
    buf = io.BytesIO()
    img.save(buf)
    return Response(content=buf.getvalue(), media_type="image/svg+xml")


async def _status_view(inv_id: str, request: Request) -> dict:
    inv = _get_invoice_or_404(inv_id)
    settings = db.get_settings()
    chain_height = getattr(request.app.state, "chain_height", None) or 0
    payments = db.payments_for_invoice(inv_id)
    required = int(settings.get("confirmations_required", "10"))
    confs = min(
        (0 if p["block_height"] == 0 else max(0, chain_height - p["block_height"])) for p in payments
    ) if payments else 0
    return {
        "id": inv["id"],
        "status": inv["status"],
        "paid_atomic": inv["paid_atomic"],
        "paid_bnt": invoices.format_bnt(inv["paid_atomic"]),
        "confirmations": confs,
        "confirmations_required": required,
        "expires_at": inv["expires_at"],
        "late_payment": bool(inv["late_payment"]),
        "overpaid": bool(inv["overpaid"]),
        "underpaid": 0 < inv["paid_atomic"] < inv["amount_atomic"],
        "payments": [
            {
                "txid": p["txid"],
                "amount_atomic": p["amount_atomic"],
                "amount_bnt": invoices.format_bnt(p["amount_atomic"]),
                "confirmations": 0 if p["block_height"] == 0 else max(0, chain_height - p["block_height"]),
            }
            for p in payments
        ],
    }


@router.get(
    "/pay/{inv_id}/status",
    response_model=PayStatusOut,
    summary="Invoice status (public, for polling)",
)
async def pay_status(inv_id: str, request: Request):
    return await _status_view(inv_id, request)


@router.get("/pay/{inv_id}/status-frag", include_in_schema=False)
async def pay_status_frag(inv_id: str, request: Request):
    view = await _status_view(inv_id, request)
    return templates.TemplateResponse(request, "frags/_pay_status.html", {"inv": view})
