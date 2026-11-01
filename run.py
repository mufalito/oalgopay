#!/usr/bin/env python3
"""OalgoPay entry point: uv run run.py [--config config.json] [--host H] [--port P]."""
from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from app.config import ConfigError, default_config, load_config
from app.main import create_app


def main() -> int:
    parser = argparse.ArgumentParser(description="OalgoPay")
    parser.add_argument("--config", default="config.json", help="path to config.json")
    parser.add_argument("--host", default=None, help="override listen host")
    parser.add_argument("--port", type=int, default=None, help="override listen port")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
        bind_host = cfg["host"]
    except ConfigError:
        cfg = default_config()
        # While unconfigured the setup wizard is unauthenticated: bind
        # loopback only so nobody on the network can race the owner through
        # setup (or probe /setup/check). Override with
        # OALGOPAY_UNCONFIGURED_BIND=0.0.0.0 for remote headless setups
        # (the Docker compose file does).
        bind_host = os.environ.get("OALGOPAY_UNCONFIGURED_BIND", "127.0.0.1")
    if args.host:
        cfg["host"] = args.host
    if args.port:
        cfg["port"] = args.port
    if bind_host != cfg["host"]:
        print(
            f"starting in SETUP MODE on {bind_host}:{cfg['port']} — "
            f"open http://{bind_host}:{cfg['port']}/setup to configure",
            file=sys.stderr,
        )

    app = create_app(cfg)
    app.state.effective_bind = bind_host
    uvicorn.run(app, host=bind_host, port=int(cfg["port"]), log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
