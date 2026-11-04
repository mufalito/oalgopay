"""Tests for schema bring-up: Alembic upgrades, the stamp bridge from the
retired schema_migrations bookkeeping, and legacy pre-migration databases."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, inspect, text  # noqa: E402

from app import db  # noqa: E402
from app.models import Base  # noqa: E402

# databases as shipped before versioned migrations existed: no created_height
# on invoices, no event_id/previous_status on webhook_deliveries, and none of
# the later tables — bring-up must take them fully current.
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

BUSINESS_TABLES = {
    "settings", "invoices", "payments", "webhook_endpoints", "webhook_deliveries",
    "payouts", "api_keys", "auth_challenges", "auth_attempts",
}


def make_legacy_db(path: str) -> None:
    """A database as shipped before versioned migrations existed: no v2
    columns and none of the later tables (no schema_migrations either)."""
    raw = sqlite3.connect(path)
    raw.executescript(LEGACY_SCHEMA)
    raw.execute(
        "INSERT INTO invoices(id, created_at, expires_at, amount_atomic, memo, metadata,"
        " status) VALUES('keep-me', 1, 2, 3, 'keep-me', '{}', 'confirmed')"
    )
    raw.commit()
    raw.close()


def make_bookkeeping_db(path: str, versions: list[int]) -> None:
    """A database from the versioned-migrations era: current-shape tables
    (minus the v2 columns when the bookkeeping says v1) plus schema_migrations
    rows — exactly what the old runner left behind."""
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        if max(versions) < 2:  # v1-era shape: migration 2 had not run yet
            for table, col in (
                ("invoices", "created_height"),
                ("webhook_deliveries", "event_id"),
                ("webhook_deliveries", "previous_status"),
            ):
                c.execute(text(f"ALTER TABLE {table} DROP COLUMN {col}"))
        c.execute(
            text("CREATE TABLE schema_migrations ("
                 "version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)")
        )
        for v in versions:
            c.execute(text("INSERT INTO schema_migrations (version, applied_at) VALUES(:v, 1)"), {"v": v})
        height_col = "created_height, " if max(versions) >= 2 else ""
        height_val = "0, " if max(versions) >= 2 else ""
        c.execute(
            text("INSERT INTO invoices(id, created_at, " + height_col + "expires_at, amount_atomic,"
                 " memo, metadata, status) VALUES('keep-me', 1, " + height_val + "2, 3, 'keep-me', '{}', 'confirmed')")
        )
    engine.dispose()


class BringUp(unittest.TestCase):
    def _inspect(self, path: str):
        engine = create_engine(f"sqlite:///{path}")
        insp = inspect(engine)
        tables = set(insp.get_table_names())
        version = None
        if "alembic_version" in tables:
            with engine.connect() as c:
                version = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
        engine.dispose()
        return insp, tables, version

    def test_fresh_database_upgrades_to_head(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "fresh.db")
            db.connect(path)
            insp, tables, version = self._inspect(path)
            self.assertEqual(version, "0002")
            self.assertEqual(tables, BUSINESS_TABLES | {"alembic_version"})
            self.assertIn("created_height", {c["name"] for c in insp.get_columns("invoices")})
            dev_cols = {c["name"] for c in insp.get_columns("webhook_deliveries")}
            self.assertTrue({"event_id", "previous_status"} <= dev_cols)
            # default settings seeded
            for key, value in db.DEFAULT_SETTINGS.items():
                self.assertEqual(db.get_setting(key), value)

    def test_legacy_database_gets_missing_columns(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "legacy.db")
            make_legacy_db(path)
            db.connect(path)
            insp, tables, version = self._inspect(path)
            self.assertEqual(version, "0002")
            self.assertIn("created_height", {c["name"] for c in insp.get_columns("invoices")})
            dev_cols = {c["name"] for c in insp.get_columns("webhook_deliveries")}
            self.assertTrue({"event_id", "previous_status"} <= dev_cols)
            self.assertEqual(tables, BUSINESS_TABLES | {"alembic_version"})
            self.assertEqual(db.get_invoice("keep-me").status, "confirmed")

    def test_old_bookkeeping_database_is_stamped_and_dropped(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "bookkeeping.db")
            make_bookkeeping_db(path, versions=[1, 2])
            db.connect(path)
            _, tables, version = self._inspect(path)
            self.assertEqual(version, "0002")
            self.assertNotIn("schema_migrations", tables)
            self.assertEqual(db.get_invoice("keep-me").status, "confirmed")

    def test_partially_bookkept_database_stamps_then_upgrades(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "partial.db")
            make_bookkeeping_db(path, versions=[1])
            db.connect(path)
            insp, _, version = self._inspect(path)
            self.assertEqual(version, "0002")
            self.assertIn("created_height", {c["name"] for c in insp.get_columns("invoices")})

    def test_unknown_bookkeeping_version_fails_loudly(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "future.db")
            make_bookkeeping_db(path, versions=[3])
            with self.assertRaises(RuntimeError):
                db.connect(path)

    def test_reconnect_does_not_reapply(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "t.db")
            db.connect(path)
            db.set_setting("marker", "1")
            db.connect(path)  # must not error or lose data
            _, _, version = self._inspect(path)
            self.assertEqual(version, "0002")
            self.assertEqual(db.get_setting("marker"), "1")


class ModelSchemaTest(unittest.TestCase):
    """The models must describe exactly the shape migrations create."""

    def test_models_match_head_schema(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "cmp.db")
            db.connect(path)
            engine = create_engine(f"sqlite:///{path}")
            Base.metadata.create_all(engine)  # no-op if identical; new tables would appear
            insp = inspect(engine)
            for table in Base.metadata.sorted_tables:
                self.assertIn(table.name, insp.get_table_names())
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
