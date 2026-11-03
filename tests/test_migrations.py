"""Tests for the versioned schema-migration runner (db._migrate)."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402

# databases as shipped before versioned migrations existed: no created_height
# on invoices, no event_id/previous_status on webhook_deliveries, and none of
# the later tables — the runner must bring them fully current.
LEGACY_SCHEMA = """
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE invoices (
    id TEXT PRIMARY KEY, created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL, amount_atomic INTEGER NOT NULL,
    memo TEXT NOT NULL, label TEXT, metadata TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL, paid_atomic INTEGER NOT NULL DEFAULT 0,
    confirmed_at INTEGER, late_payment INTEGER NOT NULL DEFAULT 0,
    overpaid INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE webhook_endpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL,
    secret TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE TABLE webhook_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id INTEGER NOT NULL REFERENCES webhook_endpoints(id),
    event TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL DEFAULT 0, last_status INTEGER,
    last_error TEXT, delivered_at INTEGER, created_at INTEGER NOT NULL
);
"""


class TestMigrations(unittest.TestCase):
    def _versions(self) -> list[int]:
        return [r["version"] for r in db.query_all("SELECT version FROM schema_migrations ORDER BY version")]

    def test_fresh_database_stamps_all_versions(self):
        with tempfile.TemporaryDirectory() as td:
            db.connect(Path(td) / "fresh.db")
            self.assertEqual(self._versions(), list(range(1, db.SCHEMA_VERSION + 1)))

    def test_legacy_database_gets_missing_columns_and_versions(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "legacy.db")
            raw = sqlite3.connect(path)
            raw.executescript(LEGACY_SCHEMA)
            raw.close()

            db.connect(path)

            inv_cols = {r[1] for r in db.db().execute("PRAGMA table_info(invoices)")}
            self.assertIn("created_height", inv_cols)
            dev_cols = {r[1] for r in db.db().execute("PRAGMA table_info(webhook_deliveries)")}
            self.assertIn("event_id", dev_cols)
            self.assertIn("previous_status", dev_cols)
            # later tables materialized by the base SCHEMA
            self.assertIsNotNone(db.query_one("SELECT name FROM sqlite_master WHERE name = 'auth_challenges'"))
            self.assertEqual(self._versions(), list(range(1, db.SCHEMA_VERSION + 1)))

    def test_reconnect_does_not_reapply(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "t.db")
            db.connect(path)
            before = db.query_one("SELECT COUNT(*) AS n FROM schema_migrations")["n"]
            db.connect(path)
            after = db.query_one("SELECT COUNT(*) AS n FROM schema_migrations")["n"]
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
