"""SQLite access layer. All SQL in the gateway lives here (WAL, foreign_keys=ON)."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
    id TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    amount_atomic INTEGER NOT NULL,
    memo TEXT NOT NULL,
    label TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    paid_atomic INTEGER NOT NULL DEFAULT 0,
    confirmed_at INTEGER,
    late_payment INTEGER NOT NULL DEFAULT 0,
    overpaid INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS payments (
    txid TEXT NOT NULL,
    output_index INTEGER NOT NULL,
    invoice_id TEXT NOT NULL REFERENCES invoices(id),
    amount_atomic INTEGER NOT NULL,
    block_height INTEGER NOT NULL,
    memo_hex TEXT,
    matched_at INTEGER NOT NULL,
    match_method TEXT NOT NULL,
    PRIMARY KEY (txid, output_index)
);
CREATE INDEX IF NOT EXISTS idx_payments_invoice ON payments(invoice_id);
CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);
CREATE TABLE IF NOT EXISTS webhook_endpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    secret TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id INTEGER NOT NULL REFERENCES webhook_endpoints(id),
    event TEXT NOT NULL,
    event_id TEXT,
    previous_status TEXT,
    invoice_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL,
    last_status INTEGER,
    last_error TEXT,
    delivered_at INTEGER,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deliveries_pending ON webhook_deliveries(next_attempt_at)
    WHERE delivered_at IS NULL;
CREATE TABLE IF NOT EXISTS payouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    address TEXT NOT NULL,
    amount_atomic INTEGER NOT NULL,
    memo TEXT,
    txid TEXT,
    fee_atomic INTEGER,
    idempotency_key TEXT NOT NULL UNIQUE,
    error TEXT
);
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,             -- 'merchant' | 'payouts'
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,  -- sha256 hex of the full key
    prefix TEXT NOT NULL,           -- first chars, for display
    created_at INTEGER NOT NULL,
    last_used_at INTEGER,
    revoked_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_api_keys_lookup ON api_keys(kind, key_hash);
"""

DEFAULT_SETTINGS: dict[str, str] = {
    "confirmations_required": "10",
    "expiry_minutes": "60",
    "amount_fallback_matching": "1",
    "display_handle": "",
    "last_known_address": "",
}

_conn: sqlite3.Connection | None = None


def connect(path: str | Path) -> sqlite3.Connection:
    global _conn
    _conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.executescript(SCHEMA)
    # lightweight migrations for pre-existing databases
    _cols = {r[1] for r in _conn.execute("PRAGMA table_info(webhook_deliveries)")}
    for _col, _decl in (("event_id", "TEXT"), ("previous_status", "TEXT")):
        if _col not in _cols:
            _conn.execute(f"ALTER TABLE webhook_deliveries ADD COLUMN {_col} {_decl}")
    for key, value in DEFAULT_SETTINGS.items():
        _conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))
    return _conn


def db() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("database not initialised — call connect() first")
    return _conn


# --- generic helpers -------------------------------------------------------

def query_all(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in db().execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    row = db().execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    return db().execute(sql, params)



# --- settings --------------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def get_settings() -> dict[str, str]:
    return {r["key"]: r["value"] for r in query_all("SELECT key, value FROM settings")}


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# --- invoices --------------------------------------------------------------

def insert_invoice(inv: dict[str, Any]) -> None:
    execute(
        "INSERT INTO invoices(id, created_at, expires_at, amount_atomic, memo, label, metadata,"
        " status, paid_atomic, confirmed_at, late_payment, overpaid)"
        " VALUES(:id, :created_at, :expires_at, :amount_atomic, :memo, :label, :metadata,"
        " :status, :paid_atomic, :confirmed_at, :late_payment, :overpaid)",
        inv,
    )


def get_invoice(inv_id: str) -> dict[str, Any] | None:
    return query_one("SELECT * FROM invoices WHERE id = ?", (inv_id,))


def list_invoices(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    statuses: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    sql, params = "SELECT * FROM invoices", []
    clauses = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if statuses:
        clauses.append(f"status IN ({','.join('?' * len(statuses))})")
        params.extend(statuses)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return query_all(sql, tuple(params))


def update_invoice(inv_id: str, fields: dict[str, Any]) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    fields = dict(fields, id=inv_id)
    execute(f"UPDATE invoices SET {sets} WHERE id = :id", fields)


# --- payments --------------------------------------------------------------

def insert_payment(p: dict[str, Any]) -> bool:
    """Insert a payment; returns False if (txid, output_index) already credited."""
    cur = execute(
        "INSERT OR IGNORE INTO payments(txid, output_index, invoice_id, amount_atomic,"
        " block_height, memo_hex, matched_at, match_method)"
        " VALUES(:txid, :output_index, :invoice_id, :amount_atomic, :block_height,"
        " :memo_hex, :matched_at, :match_method)",
        p,
    )
    return cur.rowcount > 0


def get_payment_keys() -> set[tuple[str, int]]:
    return {(r["txid"], r["output_index"]) for r in query_all("SELECT txid, output_index FROM payments")}


def payments_for_invoice(inv_id: str) -> list[dict[str, Any]]:
    return query_all(
        "SELECT * FROM payments WHERE invoice_id = ? ORDER BY matched_at, txid", (inv_id,)
    )


def payments_for_invoices(inv_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {i: [] for i in inv_ids}
    if not inv_ids:
        return out
    placeholders = ",".join("?" * len(inv_ids))
    for p in query_all(
        f"SELECT * FROM payments WHERE invoice_id IN ({placeholders}) ORDER BY matched_at, txid",
        tuple(inv_ids),
    ):
        out[p["invoice_id"]].append(p)
    return out


# --- webhook endpoints -----------------------------------------------------

def create_webhook_endpoint(url: str, secret: str) -> dict[str, Any]:
    cur = execute(
        "INSERT INTO webhook_endpoints(url, secret, active, created_at) VALUES(?, ?, 1, ?)",
        (url, secret, int(time.time())),
    )
    return query_one("SELECT * FROM webhook_endpoints WHERE id = ?", (cur.lastrowid,))  # type: ignore[arg-type]


def get_webhook_endpoint(ep_id: int) -> dict[str, Any] | None:
    return query_one("SELECT * FROM webhook_endpoints WHERE id = ?", (ep_id,))


def list_webhook_endpoints(active_only: bool = False) -> list[dict[str, Any]]:
    if active_only:
        return query_all("SELECT * FROM webhook_endpoints WHERE active = 1 ORDER BY id")
    return query_all("SELECT * FROM webhook_endpoints ORDER BY id")


def update_webhook_endpoint(ep_id: int, fields: dict[str, Any]) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    fields = dict(fields, id=ep_id)
    execute(f"UPDATE webhook_endpoints SET {sets} WHERE id = :id", fields)


def delete_webhook_endpoint(ep_id: int) -> None:
    execute("DELETE FROM webhook_deliveries WHERE endpoint_id = ?", (ep_id,))
    execute("DELETE FROM webhook_endpoints WHERE id = ?", (ep_id,))


# --- webhook deliveries ----------------------------------------------------

def enqueue_delivery(
    endpoint_id: int,
    event: str,
    invoice_id: str | None,
    next_attempt_at: int,
    event_id: str,
    previous_status: str | None,
) -> None:
    execute(
        "INSERT INTO webhook_deliveries(endpoint_id, event, event_id, previous_status,"
        " invoice_id, next_attempt_at, created_at)"
        " VALUES(?, ?, ?, ?, ?, ?, ?)",
        (endpoint_id, event, event_id, previous_status, invoice_id, next_attempt_at, int(time.time())),
    )


def pending_deliveries(now: int) -> list[dict[str, Any]]:
    return query_all(
        "SELECT d.*, e.url AS endpoint_url, e.secret AS endpoint_secret FROM webhook_deliveries d"
        " JOIN webhook_endpoints e ON e.id = d.endpoint_id"
        " WHERE d.delivered_at IS NULL AND d.next_attempt_at <= ?"
        " ORDER BY d.id LIMIT 20",
        (now,),
    )


def update_delivery(delivery_id: int, fields: dict[str, Any]) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    fields = dict(fields, id=delivery_id)
    execute(f"UPDATE webhook_deliveries SET {sets} WHERE id = :id", fields)


def list_deliveries(limit: int = 50) -> list[dict[str, Any]]:
    return query_all(
        "SELECT d.*, e.url AS endpoint_url FROM webhook_deliveries d"
        " LEFT JOIN webhook_endpoints e ON e.id = d.endpoint_id"
        " ORDER BY d.id DESC LIMIT ?",
        (limit,),
    )


def deliveries_for_invoice(inv_id: str) -> list[dict[str, Any]]:
    return query_all(
        "SELECT * FROM webhook_deliveries WHERE invoice_id = ? ORDER BY id", (inv_id,)
    )


def insert_payout(p: dict[str, Any]) -> None:
    execute(
        "INSERT OR IGNORE INTO payouts(created_at, address, amount_atomic, memo, txid,"
        " fee_atomic, idempotency_key, error)"
        " VALUES(:created_at, :address, :amount_atomic, :memo, :txid, :fee_atomic,"
        " :idempotency_key, :error)",
        p,
    )


def list_payouts(limit: int = 25) -> list[dict[str, Any]]:
    return query_all("SELECT * FROM payouts ORDER BY id DESC LIMIT ?", (limit,))


def get_payout_by_key(idempotency_key: str) -> dict[str, Any] | None:
    return query_one("SELECT * FROM payouts WHERE idempotency_key = ?", (idempotency_key,))


# --- api keys ---------------------------------------------------------------

def create_api_key(kind: str, name: str, key: str) -> dict[str, Any]:
    """Insert a full key; stores only its sha256. Returns the row (with prefix)."""
    import hashlib
    import time as _time

    key_hash = hashlib.sha256(key.encode()).hexdigest()
    execute(
        "INSERT OR IGNORE INTO api_keys(kind, name, key_hash, prefix, created_at)"
        " VALUES(?, ?, ?, ?, ?)",
        (kind, name, key_hash, key[:6], int(_time.time())),
    )
    return query_one("SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,)) or {}


def verify_api_key(kind: str, key: str) -> dict[str, Any] | None:
    """Active key of the given kind, or None. Updates last_used_at."""
    import hashlib
    import time as _time

    row = query_one(
        "SELECT * FROM api_keys WHERE kind = ? AND key_hash = ? AND revoked_at IS NULL",
        (kind, hashlib.sha256(key.encode()).hexdigest()),
    )
    if row:
        execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (int(_time.time()), row["id"]))
    return row


def list_api_keys() -> list[dict[str, Any]]:
    return query_all("SELECT * FROM api_keys ORDER BY kind, id")


def revoke_api_key(key_id: int) -> bool:
    import time as _time

    cur = execute(
        "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (int(_time.time()), key_id),
    )
    return cur.rowcount > 0


def has_active_api_keys(kind: str) -> bool:
    return query_one(
        "SELECT 1 FROM api_keys WHERE kind = ? AND revoked_at IS NULL LIMIT 1", (kind,)
    ) is not None
