"""Async client for the local Blocknet daemon REST API (Bearer token from api.cookie).

ALL node interaction goes through BlocknetClient. The gateway holds no wallet
credentials: the only exception is wallet_load(), a one-shot pass-through
invoked exclusively by explicit user action (setup wizard / admin reload) —
the password is forwarded to the core and discarded. On 401 the cookie is
re-read once and the request retried once (daemon restarts regenerate it).
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

log = logging.getLogger("oalgopay.blocknet")


class BlocknetError(RuntimeError):
    def __init__(self, message: str, kind: str = "error", status: int | None = None):
        super().__init__(message)
        self.kind = kind  # "locked" | "unreachable" | "auth" | "error"
        self.status = status


class BlocknetClient:
    def __init__(self, api_base: str, datadir: str):
        self.api_base = api_base.rstrip("/")
        self.cookie_path = Path(datadir) / "api.cookie"
        self._cookie: str | None = None
        self._http = httpx.AsyncClient(base_url=self.api_base, timeout=10.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _read_cookie(self) -> str:
        try:
            return self.cookie_path.read_text().strip()
        except OSError as exc:
            raise BlocknetError(f"cannot read api.cookie at {self.cookie_path}: {exc}", "auth") from exc

    async def _request(
        self, method: str, path: str, *, json_body: Any = None, headers: dict[str, str] | None = None
    ) -> Any:
        for attempt in (0, 1):
            if self._cookie is None:
                self._cookie = self._read_cookie()
            hdrs = {"Authorization": f"Bearer {self._cookie}"}
            if headers:
                hdrs.update(headers)
            try:
                resp = await self._http.request(method, path, json=json_body, headers=hdrs)
            except httpx.HTTPError as exc:
                raise BlocknetError(f"daemon unreachable: {exc}", "unreachable") from exc
            if resp.status_code == 401 and attempt == 0:
                self._cookie = None  # daemon restarted: re-read cookie once, retry once
                continue
            if resp.status_code >= 400:
                detail = ""
                try:
                    detail = str(resp.json().get("error", ""))
                except Exception:
                    detail = resp.text[:200]
                kind = "locked" if "locked" in detail.lower() else "error"
                raise BlocknetError(
                    f"{method} {path} -> {resp.status_code}: {detail}", kind, resp.status_code
                )
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        raise BlocknetError("unreachable retry path exhausted", "auth")

    # --- endpoints ---------------------------------------------------------

    async def get_status(self) -> dict[str, Any]:
        return await self._request("GET", "/api/status")

    async def wallet_load(self, path: str, password: str) -> str:
        """One-shot, user-initiated wallet load on the core (setup wizard /
        admin reload action). The password is used for this request only —
        never stored, never logged. Returns "loaded" or "already-loaded".
        """
        try:
            await self._request(
                "POST", "/api/wallet/load",
                json_body={"path": path, "password": password},
            )
            return "loaded"
        except BlocknetError as exc:
            if "already loaded" in str(exc):
                return "already-loaded"
            raise

    async def wallet_address(self) -> str:
        data = await self._request("GET", "/api/wallet/address")
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for key in ("address", "stealth_address", "result"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
        raise BlocknetError(f"unexpected /api/wallet/address response: {data!r}")

    async def wallet_balance(self) -> dict[str, int]:
        data = await self._request("GET", "/api/wallet/balance")
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, int)}
        if isinstance(data, int):
            return {"total": data}
        raise BlocknetError(f"unexpected /api/wallet/balance response: {data!r}")

    async def history_page(self, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """Wallet outputs, newest-first. Mempool outputs carry block_height == 0."""
        return await self._request("GET", f"/api/wallet/history/page?limit={limit}&offset={offset}")

    async def send(
        self, recipients: list[dict[str, Any]], idempotency_key: str
    ) -> dict[str, Any]:
        """Spend wallet funds. Used only by scripts/e2e_test.py, never by the gateway."""
        return await self._request(
            "POST",
            "/api/wallet/send",
            json_body={"recipients": recipients},
            headers={"Idempotency-Key": idempotency_key},
        )

    async def stream_events(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Yield (event, data) tuples from the daemon SSE stream (/api/events).

        Reconnects forever with 5s -> 60s backoff; yields nothing while down.
        """
        backoff = 5.0
        while True:
            try:
                if self._cookie is None:
                    self._cookie = self._read_cookie()
                async with self._http.stream(
                    "GET",
                    "/api/events",
                    headers={"Authorization": f"Bearer {self._cookie}", "Accept": "text/event-stream"},
                    timeout=httpx.Timeout(10.0, read=None),
                ) as resp:
                    if resp.status_code == 401:
                        self._cookie = None
                        continue
                    resp.raise_for_status()
                    backoff = 5.0
                    event = "message"
                    data_lines: list[str] = []
                    async for line in resp.aiter_lines():
                        if line == "":
                            if data_lines:
                                try:
                                    payload: Any = json.loads("\n".join(data_lines))
                                except json.JSONDecodeError:
                                    payload = {}
                                yield event, payload if isinstance(payload, dict) else {}
                            event, data_lines = "message", []
                            continue
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].strip())
            except (httpx.HTTPError, BlocknetError) as exc:
                log.warning("SSE stream lost (%s); reconnecting in %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
