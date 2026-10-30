"""Configuration loading and data-directory bootstrap."""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config.example.json"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"

REQUIRED_KEYS = (
    "host",
    "port",
    "api_base",
    "datadir",
    "admin_password",
    "api_token",
    "public_base_url",
)
PLACEHOLDER_PREFIX = "CHANGE_ME"


class ConfigError(RuntimeError):
    """Raised when the configuration is missing or unusable."""


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate config.json. Refuses to run on missing file or placeholder secrets."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise ConfigError(
            f"config file not found: {cfg_path}. "
            f"Copy {EXAMPLE_CONFIG_PATH.name} to config.json, edit it, then `chmod 600 config.json`."
        )
    try:
        cfg = json.loads(cfg_path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file {cfg_path} is not valid JSON: {exc}") from exc

    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        raise ConfigError(f"config file {cfg_path} is missing keys: {', '.join(missing)}")
    for key in ("admin_password", "api_token"):
        value = str(cfg[key])
        if not value or value.startswith(PLACEHOLDER_PREFIX):
            raise ConfigError(f"config key '{key}' still holds a placeholder — set a real secret.")
    cfg.setdefault("data_dir", str(DEFAULT_DATA_DIR))
    cfg["public_base_url"] = str(cfg["public_base_url"]).rstrip("/")
    return cfg


def default_config() -> dict[str, Any]:
    """Sane defaults for first-run setup mode (secrets are placeholders).
    OALGOPAY_API_BASE / OALGOPAY_DATADIR env vars override the core
    location (used by the Docker setup, where the core is another container)."""
    import os

    return {
        "host": "0.0.0.0",
        "port": 8096,
        "api_base": os.environ.get("OALGOPAY_API_BASE", "http://127.0.0.1:18512"),
        "datadir": os.environ.get("OALGOPAY_DATADIR") or detect_datadir(),
        "admin_password": PLACEHOLDER_PREFIX + "_admin_password",
        "api_token": PLACEHOLDER_PREFIX + "_merchant_api_token",
        "public_base_url": "",
        "data_dir": os.environ.get("OALGOPAY_DATA_DIR", str(DEFAULT_DATA_DIR)),
    }


def detect_datadir() -> str:
    """Prefer the contained core datadir, then the GUI's, then the default."""
    contained = PROJECT_ROOT / "data" / "blocknet"
    if (contained / "api.cookie").exists() or (contained / "settings.json").exists():
        return str(contained)
    gui = Path.home() / ".local" / "share" / "com.blocknet.wallet" / "data"
    if gui.exists():
        return str(gui)
    return str(contained)


def is_configured(cfg: dict[str, Any]) -> bool:
    """True when real secrets are set (not setup placeholders)."""
    for key in ("admin_password", "api_token"):
        value = str(cfg.get(key, ""))
        if not value or value.startswith(PLACEHOLDER_PREFIX):
            return False
    return True


def ensure_data_dir(cfg: dict[str, Any]) -> Path:
    """Create data/ (0700) and the session secret (0600) on first start. Returns data dir."""
    data_dir = Path(cfg["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    secret_path = data_dir / "session_secret"
    if not secret_path.exists():
        secret_path.write_text(secrets.token_hex(32))
        os.chmod(secret_path, 0o600)
    else:
        os.chmod(secret_path, 0o600)
    return data_dir


def session_secret(cfg: dict[str, Any]) -> bytes:
    return (Path(cfg["data_dir"]) / "session_secret").read_bytes().strip()
