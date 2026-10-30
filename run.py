#!/usr/bin/env python3
"""OalgoPay entry point: uv run run.py [--config config.json] [--host H] [--port P]."""
from __future__ import annotations

import argparse
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
    except ConfigError as exc:
        print(f"note: {exc}", file=sys.stderr)
        print("starting in SETUP MODE — open http://<host>:<port>/setup to configure", file=sys.stderr)
        cfg = default_config()
    if args.host:
        cfg["host"] = args.host
    if args.port:
        cfg["port"] = args.port

    app = create_app(cfg)
    uvicorn.run(app, host=cfg["host"], port=int(cfg["port"]), log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
