"""Automated payouts API — POST/GET /api/v1/payouts.

Auth: `Authorization: Bearer <payout_api_key>` (separate from the merchant
api_token on purpose: apps that create invoices must not be able to move
coins). Disabled entirely while `payout_api_key` is unset/blank.
"""
from __future__ import annotations

import hmac
import re
import secrets as pysecrets
import time
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from .. import db
from ..blocknet import BlocknetError
from .schemas import PayoutCreatedOut, PayoutCreateRequest, PayoutOut

router = APIRouter(prefix="/api/v1", tags=["payouts"])
_bearer = HTTPBearer(auto_error=False, description="payout_api_key from config.json")

_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,100}$")


def require_payout_key(
    creds: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)],
    request: Request,
) -> None:
    key = str(request.app.state.config.get("payout_api_key") or "")
    if not key:
        raise HTTPException(status_code=403, detail="payouts API is disabled (set payout_api_key in config.json)")
    if creds is None or not hmac.compare_digest(creds.credentials, key):
        raise HTTPException(status_code=401, detail="invalid payout API key")


@router.post(
    "/payouts",
    status_code=201,
    response_model=PayoutCreatedOut,
    summary="Create payout (automated)",
)
async def create_payout(body: PayoutCreateRequest, request: Request, _: None = Security(require_payout_key)):
    """Send funds to any address. Supply `idempotency_key` to make retries
    safe: a replay with the same key returns the original payout without
    sending again. Sending to this gateway's own address with an INV-… memo
    credits that invoice as paid."""
    client_key = body.idempotency_key

    if client_key:
        if not _KEY_RE.match(client_key):
            raise HTTPException(
                status_code=422,
                detail="idempotency_key must be 8-100 chars of A-Za-z0-9._:-",
            )
        existing = db.get_payout_by_key(client_key)
        if existing and existing["txid"]:
            return {**existing, "replayed": True}

    amount = body.amount_atomic_resolved
    address = body.address.strip()

    # mark the attempt first (error rows record failures), then send
    attempt_key = client_key or ("auto_" + pysecrets.token_hex(16))
    db.insert_payout({
        "created_at": int(time.time()), "address": address,
        "amount_atomic": amount, "memo": body.memo, "txid": None,
        "fee_atomic": None, "idempotency_key": attempt_key, "error": "pending",
    })
    try:
        result = await _send_with_key(request, address, amount, body.memo, attempt_key)
    except BlocknetError as exc:
        db.execute(
            "UPDATE payouts SET error = ? WHERE idempotency_key = ?",
            (str(exc), attempt_key),
        )
        raise HTTPException(status_code=422, detail=f"core rejected: {exc}") from exc

    row = db.get_payout_by_key(attempt_key)
    return {**(row or result), "replayed": False}


async def _send_with_key(request: Request, address: str, amount: int, memo: str | None, key: str) -> dict[str, Any]:
    """send_service but with a caller-chosen idempotency key, updating the
    pre-inserted payout row instead of inserting a second one."""
    import uuid

    recipient: dict[str, Any] = {"address": address, "amount": amount}
    if memo:
        recipient["memo_text"] = memo
    sent = await request.app.state.client.send([recipient], str(uuid.uuid4()))
    fee = sent.get("fee")
    txid = sent.get("txid")
    db.execute(
        "UPDATE payouts SET txid = ?, fee_atomic = ?, error = NULL WHERE idempotency_key = ?",
        (txid, fee if isinstance(fee, int) else None, key),
    )
    return {
        "txid": txid, "address": address, "amount_atomic": amount,
        "fee_atomic": fee if isinstance(fee, int) else None, "memo": memo,
    }


@router.get("/payouts", response_model=list[PayoutOut], summary="List payouts")
async def list_payouts_api(request: Request, _: None = Security(require_payout_key), limit: int = 25):
    return db.list_payouts(max(1, min(limit, 200)))


@router.get("/payouts/{payout_id}", response_model=PayoutOut, summary="Get payout")
async def get_payout_api(payout_id: int, request: Request, _: None = Security(require_payout_key)):
    row = db.query_one("SELECT * FROM payouts WHERE id = ?", (payout_id,))
    if not row:
        raise HTTPException(status_code=404, detail="payout not found")
    return row
