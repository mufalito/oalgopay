"""Merchant REST API (Bearer token auth) — schema-typed for OpenAPI docs."""
from __future__ import annotations

import hmac
import json
import sqlite3
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from .. import db, invoices

from .schemas import InvoiceCreateRequest, InvoiceListOut, InvoiceOut

router = APIRouter(prefix="/api/v1", tags=["merchant"])
_bearer = HTTPBearer(auto_error=False, description="api_token from config.json")

VALID_STATUSES = ("pending", "seen", "confirmed", "expired", "cancelled")


def require_merchant(
    creds: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)],
    request: Request,
) -> None:
    """DB-backed bearer check (sha256 at rest, revocable from the panel)."""
    if getattr(request.app.state, "setup_mode", False):
        raise HTTPException(status_code=403, detail="setup required — open /setup")
    if creds is None or not db.verify_api_key("merchant", creds.credentials):
        raise HTTPException(
            status_code=401,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )



def serialize_invoice(request: Request, inv: dict[str, Any], include_payments: bool = True) -> dict[str, Any]:
    state = request.app.state
    payments = db.payments_for_invoice(inv["id"]) if include_payments else []
    return invoices.invoice_to_json(
        inv,
        payments,
        settings=db.get_settings(),
        public_base_url=state.config["public_base_url"],
        address=db.get_setting("last_known_address", ""),
        chain_height=getattr(state, "chain_height", None),
    )


def create_invoice_service(body: InvoiceCreateRequest | dict[str, Any], request: Request) -> dict[str, Any]:
    """Shared by the merchant route and the admin panel form."""
    if not isinstance(body, InvoiceCreateRequest):
        try:
            body = InvoiceCreateRequest(**body)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="; ".join(e["msg"] for e in exc.errors())) from exc

    settings = db.get_settings()
    expires_in_min = body.expires_in_min or int(settings.get("expiry_minutes", "60"))
    inv = invoices.build_invoice(
        now=int(time.time()),
        expiry_minutes=expires_in_min,
        amount_atomic=body.amount_atomic_resolved,
        label=body.label,
        metadata=body.metadata,
    )
    inv["memo"] = inv["id"]
    row = dict(inv, metadata=json.dumps(inv["metadata"]))
    for _ in range(5):  # retry on astronomically unlikely id collision
        try:
            db.insert_invoice(row)
            break
        except sqlite3.IntegrityError:
            row["id"] = row["memo"] = inv["id"] = invoices.mint_id()
    else:
        raise HTTPException(status_code=500, detail="could not mint unique invoice id")
    return serialize_invoice(request, inv)


def cancel_invoice_service(inv_id: str, request: Request) -> dict[str, Any]:
    inv = db.get_invoice(inv_id)
    if not inv:
        raise HTTPException(status_code=404, detail="invoice not found")
    if inv["status"] not in ("pending", "seen"):
        raise HTTPException(status_code=409, detail=f"cannot cancel invoice in status {inv['status']}")
    db.update_invoice(inv_id, {"status": "cancelled"})
    return serialize_invoice(request, db.get_invoice(inv_id))


@router.post("/invoices", status_code=201, response_model=InvoiceOut, summary="Create invoice")
async def create_invoice(
    body: InvoiceCreateRequest, request: Request, _: None = Depends(require_merchant)
):
    """Creates a BNT invoice and returns its payment links
    (`pay_url`, `blocknet://` URI, bntpay.com URL, QR)."""
    return create_invoice_service(body, request)


@router.get("/invoices", response_model=InvoiceListOut, summary="List invoices (newest first)")
async def list_invoices(
    request: Request,
    _: None = Depends(require_merchant),
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    if status and status not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail="invalid status filter")
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    rows = db.list_invoices(status=status, limit=limit, offset=offset)
    return {
        "invoices": [serialize_invoice(request, r) for r in rows],
        "limit": limit,
        "offset": offset,
    }


@router.get("/invoices/{inv_id}", response_model=InvoiceOut, summary="Get invoice")
async def get_invoice(inv_id: str, request: Request, _: None = Depends(require_merchant)):
    inv = db.get_invoice(inv_id)
    if not inv:
        raise HTTPException(status_code=404, detail="invoice not found")
    return serialize_invoice(request, inv)


@router.post("/invoices/{inv_id}/cancel", response_model=InvoiceOut, summary="Cancel invoice")
async def cancel_invoice(inv_id: str, request: Request, _: None = Depends(require_merchant)):
    """Allowed only from `pending`/`seen`; cancelled is terminal."""
    return cancel_invoice_service(inv_id, request)
