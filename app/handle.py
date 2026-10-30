"""blocknet.id handle resolution + Ed25519 signature verification (admin overview only)."""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

RESOLVER_BASE = "https://blocknet.id/api/v1/resolve"
WELL_KNOWN_URL = "https://blocknet.id/.well-known/blocknet-id.json"
CACHE_TTL = 60

_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def resolve_handle(handle: str) -> dict[str, Any]:
    """Resolve $handle via blocknet.id and verify its Ed25519 signature.

    Returns {handle, address, verified, updated_at, error}.
    Never raises: resolver outages are reported as verified=False + error.
    """
    handle = handle.lstrip("$")
    now = time.time()
    cached = _cache.get(handle)
    if cached and cached[0] > now:
        return cached[1]

    result: dict[str, Any] = {
        "handle": f"${handle}",
        "address": None,
        "verified": False,
        "updated_at": None,
        "error": None,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            env_resp, wk_resp = await asyncio.gather(
                http.get(f"{RESOLVER_BASE}/{handle}"), http.get(WELL_KNOWN_URL)
            )
            env_resp.raise_for_status()
            wk_resp.raise_for_status()
            envelope = env_resp.json()
            well_known = wk_resp.json()
        payload = envelope.get("payload")
        sig = envelope.get("sig")
        pubkey_b64 = well_known.get("signing_pubkey")
        if not isinstance(payload, str) or not sig or not pubkey_b64:
            raise ValueError("resolver response missing payload/sig/pubkey")

        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pubkey_b64))
        pub.verify(base64.b64decode(sig), payload.encode())  # sig covers payload string bytes

        payload_obj = json.loads(payload)
        if payload_obj.get("handle") != handle:
            raise ValueError("payload handle mismatch")
        if payload_obj.get("address") != envelope.get("address"):
            raise ValueError("payload/envelope address mismatch")
        if payload_obj.get("updated_at_unix") != envelope.get("updated_at"):
            raise ValueError("payload/envelope updated_at mismatch")

        result.update(
            address=envelope.get("address"),
            verified=True,
            updated_at=envelope.get("updated_at"),
        )
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}".strip()
        result["error"] = "resolver unreachable" if isinstance(exc, (httpx.HTTPError, httpx.HTTPStatusError)) else msg
    _cache[handle] = (now + CACHE_TTL, result)
    return result
