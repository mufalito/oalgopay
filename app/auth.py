"""Authentication: admin session cookie + merchant bearer token."""
from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import HTTPException, Request

SESSION_COOKIE = "oalgopay_session"
SESSION_TTL = 12 * 3600


def make_session_token(secret: bytes) -> str:
    exp = int(time.time()) + SESSION_TTL
    mac = hmac.new(secret, str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{mac}"


def check_session_token(token: str | None, secret: bytes) -> bool:
    if not token or "." not in token:
        return False
    exp_str, mac = token.split(".", 1)
    if not exp_str.isdigit() or not hmac.compare_digest(
        mac, hmac.new(secret, exp_str.encode(), hashlib.sha256).hexdigest()
    ):
        return False
    return int(exp_str) > time.time()


def require_admin(request: Request) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if not check_session_token(token, request.app.state.session_secret):
        raise HTTPException(status_code=401, detail="admin session required")


