"""Wallet sign-in verification API — POST /api/v1/auth/challenge + /auth/verify.

Lets other services run by the same operator (faucet, forum, …) offer
"Sign in with Blocknet wallet" without hosting their own node: the third-party
backend mints a single-use challenge here, has the user's wallet sign its
message, and posts the address+signature pair back for verification against
the core's public /api/verify. The caller mints its own session on a 200 —
this API is verification only (no hosted sign-in page, no redirects).

Auth: `Authorization: Bearer <auth API key>` (admin panel → API keys →
Generate, kind `auth`). Keep the key server-side — never in browser code.
"""
from __future__ import annotations

import secrets
import time
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .. import db
from ..blocknet import BlocknetError
from .schemas import SigninChallengeOut, SigninVerifiedOut, SigninVerifyRequest

router = APIRouter(prefix="/api/v1", tags=["signin"])
_bearer = HTTPBearer(auto_error=False, description="auth API key from the admin panel")

CHALLENGE_PREFIX = "oalgopay:login:"
CHALLENGE_TTL_S = 600
LOCKOUT_FAILURES = 5
LOCKOUT_WINDOW_S = 900


def classify_verify(outcome: bool | BlocknetError) -> str:
    """'valid' | 'invalid' | 'infra'. 200-verdicts and node 4xx are definite
    (retrying cannot help); 429/5xx/unreachable/auth are infra — no verdict,
    the challenge stays usable and nothing counts against the lockout."""
    if isinstance(outcome, bool):
        return "valid" if outcome else "invalid"
    if (
        outcome.status is None
        or outcome.status == 429
        or outcome.status >= 500
        or outcome.kind in ("unreachable", "auth")
    ):
        return "infra"
    return "invalid"


def require_auth_key(
    creds: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)],
    request: Request,
) -> None:
    if getattr(request.app.state, "setup_mode", False):
        raise HTTPException(status_code=403, detail="setup required — open /setup")
    if not db.has_active_api_keys("auth"):
        raise HTTPException(status_code=403, detail="sign-in verification API is disabled (create an auth key in the admin panel)")
    if creds is None or not db.verify_api_key("auth", creds.credentials):
        raise HTTPException(status_code=401, detail="invalid auth API key")


@router.post("/auth/challenge", response_model=SigninChallengeOut, summary="Mint a single-use sign-in challenge")
async def create_challenge(request: Request, _: None = Security(require_auth_key)) -> SigninChallengeOut:
    """The fixed prefix + globally single-use nonce make a signature obtained
    for some other site's challenge useless against this message."""
    now = int(time.time())
    db.sweep_auth_challenges(now, CHALLENGE_TTL_S, LOCKOUT_WINDOW_S)
    nonce = secrets.token_hex(16)
    db.create_auth_challenge(nonce, now)
    return SigninChallengeOut(nonce=nonce, message=f"{CHALLENGE_PREFIX}{nonce}", expires_at=now + CHALLENGE_TTL_S)


@router.post("/auth/verify", response_model=SigninVerifiedOut, summary="Verify a wallet signature")
async def verify(
    body: SigninVerifyRequest, request: Request, _: None = Security(require_auth_key)
) -> SigninVerifiedOut:
    ip = request.client.host if request.client else "unknown"
    now = int(time.time())

    # Per-IP lockout on definite failures (the API caller's IP — behind a
    # reverse proxy all callers share it unless --proxy-headers is enabled).
    if db.count_recent_auth_failures(ip, now - LOCKOUT_WINDOW_S) >= LOCKOUT_FAILURES:
        raise HTTPException(status_code=429, detail="too many failed verification attempts — retry in 15 minutes")

    row = db.get_auth_challenge(body.nonce)
    if row is None:
        raise HTTPException(status_code=400, detail="unknown challenge")
    if row["used_at"] is not None:
        raise HTTPException(status_code=400, detail="challenge already used")
    if now - int(row["created_at"]) > CHALLENGE_TTL_S:
        raise HTTPException(status_code=400, detail="challenge expired — request a new one")

    try:
        result = await request.app.state.client.verify_message(
            body.address, f"{CHALLENGE_PREFIX}{body.nonce}", body.signature
        )
        exc: BlocknetError | None = None
    except BlocknetError as e:
        result, exc = None, e

    verdict = classify_verify(result if exc is None else exc)
    if verdict == "infra":
        # No verdict: challenge NOT consumed, nothing counted against the lockout.
        raise HTTPException(status_code=503, detail="verification backend unavailable — try again shortly")

    if not db.consume_auth_challenge(body.nonce, now):
        raise HTTPException(status_code=400, detail="challenge already used")

    if verdict == "invalid":
        db.record_auth_failure(ip, now)
        detail = f"signature verification failed: {exc}" if exc is not None else "signature does not match"
        raise HTTPException(status_code=401, detail=detail)

    db.clear_auth_failures(ip)
    return SigninVerifiedOut(nonce=body.nonce, address=body.address, verified=True)
