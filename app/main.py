"""FastAPI app assembly: lifespan (watcher, webhook worker), routers, static, errors."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config as config_mod
from . import db, watcher, webhooks
from .api import admin, merchant, pay, payouts, setup
from .blocknet import BlocknetClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("oalgopay")


def create_app(cfg: dict) -> FastAPI:
    config_mod.ensure_data_dir(cfg)
    data_dir = Path(cfg["data_dir"])
    db.connect(data_dir / "gateway.db")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = watcher.start(app.state)
        tasks.append(asyncio.create_task(webhooks.worker_loop(app.state), name="webhook-worker"))
        log.info("OalgoPay up on %s:%s", cfg["host"], cfg["port"])
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await app.state.client.aclose()

    app = FastAPI(
        title="OalgoPay",
        description=(
            "Self-hosted Blocknet (BNT) payment gateway.\n\n"
            "* **Merchant API** — authenticate with the `api_token` from `config.json` "
            "(Authorize button → Bearer).\n"
            "* **Public endpoints** — the customer pay page family, no auth.\n"
            "* **Admin JSON** — session-cookie endpoints for scripts "
            "(log in via `POST /admin/login` first).\n\n"
            "Written guides live in the repository's docs/ directory "
            "(API.md, WEBHOOKS.md, ADMIN.md, INSTALL.md). Admin panel: /admin."
        ),
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.config = cfg
    app.state.client = BlocknetClient(cfg["api_base"], cfg["datadir"])
    app.state.session_secret = config_mod.session_secret(cfg)
    app.state.chain_height = 0
    app.state.node_ok = False
    app.state.node_error = "starting"
    app.state.wallet_locked = False
    app.state.address = db.get_setting("last_known_address", "")
    app.state.setup_mode = not config_mod.is_configured(cfg)

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(merchant.router)
    app.include_router(pay.router)
    app.include_router(payouts.router)
    app.include_router(admin.router)
    app.include_router(setup.router)

    @app.exception_handler(HTTPException)
    async def http_exc_handler(request: Request, exc: HTTPException):
        """JSON errors for API surfaces, themed HTML for the public pay page."""
        path = request.url.path
        if path.startswith("/admin/frag") and exc.status_code == 401:
            resp = Response(status_code=200)
            resp.headers["HX-Redirect"] = (
                "/setup" if getattr(request.app.state, "setup_mode", False) else "/admin"
            )
            return resp
        if path.startswith("/api/") or path.startswith("/admin/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return pay.templates.TemplateResponse(
            request,
            "error.html",
            {"status_code": exc.status_code, "detail": exc.detail},
            status_code=exc.status_code,
        )

    return app
