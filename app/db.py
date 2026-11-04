"""Database access layer (SQLAlchemy 2.x, SQLite by default).

All SQL in the gateway lives here. The engine is created by connect() —
SQLite connections run with WAL and foreign_keys=ON, like always — and the
schema is brought up by Alembic (migrations/) on every boot, including the
one-time stamp bridge from the retired schema_migrations bookkeeping.
Reads return ORM entities (Invoice, Payment, …) that stay usable after their
session closes (expire_on_commit=False); writes stay Core statements, and
insert_invoice/insert_payout still take field dicts built from API payloads.
"""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any, NamedTuple, TypeVar

import sqlalchemy as sa
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, event
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql import Insert

from .models import (
    ApiKey,
    AuthChallenge,
    Invoice,
    Payment,
    Payout,
    Setting,
    WebhookDelivery,
    WebhookEndpoint,
    auth_attempts,
)

log = logging.getLogger("oalgopay.db")

DEFAULT_SETTINGS: dict[str, str] = {
    "confirmations_required": "10",
    "expiry_minutes": "60",
    "amount_fallback_matching": "0",
    "display_handle": "",
    "last_known_address": "",
}

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
# schema_migrations version -> alembic revision carrying that exact shape
_STAMP_BRIDGE = {"1": "0001", "2": "0002"}

_engine: Engine | None = None
_SessionFactory: sessionmaker


def _make_engine(url: str) -> Engine:
    """Engine with the app's connection policy: pooled, pre-pinged; SQLite
    connections run check_same_thread=False (anyio threadpool guards), a 30s
    busy timeout, and PRAGMA foreign_keys=ON + journal_mode=WAL each time a
    connection opens. Shared with migrations/env.py so Alembic sees the same
    setup; other dialects (PostgreSQL et al.) skip the PRAGMAs."""
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if ":memory:" in url:
            kwargs["poolclass"] = sa.StaticPool  # one shared in-memory database
    engine = sa.create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()
    return engine


def _alembic_config(engine: Engine) -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.attributes["engine"] = engine  # env.py reuses instead of building its own
    return cfg


def _stamp_legacy(engine: Engine) -> None:
    """Translate pre-Alembic bookkeeping into an alembic_version stamp so
    `upgrade head` starts from the right revision. Databases with tables but
    no bookkeeping (from before versioned migrations existed) need no stamp:
    revision 0001's creates are inspector-guarded, so upgrading from the
    base brings them current without touching what already exists."""
    tables = set(sa.inspect(engine).get_table_names())
    if "alembic_version" in tables:
        return
    if "schema_migrations" in tables:
        with engine.connect() as conn:
            versions = [int(r[0]) for r in conn.execute(sa.text("SELECT version FROM schema_migrations"))]
        applied = max(versions) if versions else 1
        stamp = _STAMP_BRIDGE.get(str(applied))
        if stamp is None:
            raise RuntimeError(
                f"unknown schema_migrations version {applied} — refusing to guess"
            )
        command.stamp(_alembic_config(engine), stamp)
        with engine.begin() as conn:
            conn.execute(sa.text("DROP TABLE schema_migrations"))
        log.info("stamped legacy schema_migrations v%d as alembic %s", applied, stamp)
        return
    # anything else (fresh, or pre-versioned-migrations tables): upgrade
    # head from the base revision — 0001 is guarded, 0002 adds what's missing.


def connect(path: str | Path) -> Engine:
    """Create the engine and bring the schema up (Alembic), then seed defaults.

    Accepts a full URL (`sqlite:///…`, `postgresql+psycopg://…`), a bare
    `:memory:`, or a bare filesystem path (normalized to a sqlite:/// URL)."""
    global _engine, _SessionFactory
    url = str(path)
    if "://" not in url:
        url = "sqlite:///:memory:" if url == ":memory:" else f"sqlite:///{url}"
    if _engine is not None:
        _engine.dispose()
    _engine = _make_engine(url)
    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)
    _stamp_legacy(_engine)
    command.upgrade(_alembic_config(_engine), "head")
    with _SessionFactory.begin() as s:
        for key, value in DEFAULT_SETTINGS.items():
            s.execute(
                _insert(Setting.__table__).values(key=key, value=value).on_conflict_do_nothing()
            )
    return _engine


def _insert(table: Any) -> Insert:
    """Dialect upsert-capable insert for the current engine."""
    if _engine is None:
        raise RuntimeError("database not initialised — call connect() first")
    if _engine.dialect.name == "sqlite":
        return sqlite_insert(table)
    if _engine.dialect.name == "postgresql":
        return postgresql_insert(table)
    return sa.insert(table)


_T = TypeVar("_T")


def _entities(stmt: sa.Selectable) -> list[_T]:
    """ORM entities; they stay readable after the session closes."""
    with _SessionFactory.begin() as s:
        return list(s.scalars(stmt).all())


def _entity(stmt: sa.Selectable) -> _T | None:
    with _SessionFactory.begin() as s:
        return s.scalars(stmt).first()


# --- settings ---------------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    with _SessionFactory.begin() as s:
        value = s.execute(sa.select(Setting.value).where(Setting.key == key)).scalar()
    return value if value is not None else default


def get_settings() -> dict[str, str]:
    with _SessionFactory.begin() as s:
        return {key: value for key, value in s.execute(sa.select(Setting.key, Setting.value))}


def set_setting(key: str, value: str) -> None:
    stmt = _insert(Setting.__table__).values(key=key, value=value)
    with _SessionFactory.begin() as s:
        s.execute(
            stmt.on_conflict_do_update(
                index_elements=[Setting.__table__.c.key],
                set_={"value": stmt.excluded.value},
            )
        )


# --- invoices ----------------------------------------------------------------

_INVOICE_COLS = (
    "id", "created_at", "created_height", "expires_at", "amount_atomic", "memo",
    "label", "metadata", "status", "paid_atomic", "confirmed_at", "late_payment",
    "overpaid",
)


def insert_invoice(inv: dict[str, Any]) -> None:
    """Plain insert: a PK collision raises IntegrityError (merchant API retries)."""
    with _SessionFactory.begin() as s:
        s.execute(
            sa.insert(Invoice.__table__).values({c: inv[c] for c in _INVOICE_COLS})
        )


def get_invoice(inv_id: str) -> Invoice | None:
    return _entity(sa.select(Invoice).where(Invoice.id == inv_id))


def list_invoices(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    statuses: tuple[str, ...] | None = None,
) -> list[Invoice]:
    stmt = sa.select(Invoice)
    clauses = []
    if status:
        clauses.append(Invoice.status == status)
    if statuses:
        clauses.append(Invoice.status.in_(statuses))
    if clauses:
        stmt = stmt.where(*clauses)
    stmt = stmt.order_by(Invoice.created_at.desc()).limit(limit).offset(offset)
    return _entities(stmt)


def update_invoice(inv_id: str, fields: dict[str, Any]) -> None:
    if not fields:
        return
    with _SessionFactory.begin() as s:
        s.execute(
            sa.update(Invoice.__table__).where(Invoice.__table__.c.id == inv_id).values(**fields)
        )


def invoice_status_counts() -> dict[str, int]:
    """Row counts grouped by invoice status."""
    stmt = sa.select(Invoice.__table__.c.status, sa.func.count()).group_by(
        Invoice.__table__.c.status
    )
    with _SessionFactory.begin() as s:
        return {status: int(n) for status, n in s.execute(stmt)}


# --- payments ------------------------------------------------------------------

def insert_payment(p: Payment) -> bool:
    """Insert a payment; returns False if (txid, output_index) already credited."""
    with _SessionFactory.begin() as s:
        result = s.execute(
            _insert(Payment.__table__)
            .values(
                txid=p.txid,
                output_index=p.output_index,
                invoice_id=p.invoice_id,
                amount_atomic=p.amount_atomic,
                block_height=p.block_height,
                memo_hex=p.memo_hex,
                matched_at=p.matched_at,
                match_method=p.match_method,
            )
            .on_conflict_do_nothing()
        )
        return result.rowcount > 0


def get_payment_keys() -> set[tuple[str, int]]:
    with _SessionFactory.begin() as s:
        rows = s.execute(sa.select(Payment.txid, Payment.output_index)).all()
    return {(txid, output_index) for txid, output_index in rows}


def payments_for_invoice(inv_id: str) -> list[Payment]:
    stmt = (
        sa.select(Payment)
        .where(Payment.invoice_id == inv_id)
        .order_by(Payment.matched_at, Payment.txid)
    )
    return _entities(stmt)


def payments_for_invoices(inv_ids: list[str]) -> dict[str, list[Payment]]:
    out: dict[str, list[Payment]] = {i: [] for i in inv_ids}
    if not inv_ids:
        return out
    stmt = (
        sa.select(Payment)
        .where(Payment.invoice_id.in_(inv_ids))
        .order_by(Payment.matched_at, Payment.txid)
    )
    for p in _entities(stmt):
        out[p.invoice_id].append(p)
    return out


# --- webhook endpoints ----------------------------------------------------------

def create_webhook_endpoint(url: str, secret: str) -> WebhookEndpoint:
    with _SessionFactory.begin() as s:
        result = s.execute(
            sa.insert(WebhookEndpoint.__table__).values(
                url=url, secret=secret, active=1, created_at=int(time.time())
            )
        )
        ep_id = result.inserted_primary_key[0]
        return s.get(WebhookEndpoint, ep_id)


def get_webhook_endpoint(ep_id: int) -> WebhookEndpoint | None:
    return _entity(
        sa.select(WebhookEndpoint).where(WebhookEndpoint.id == ep_id)
    )


def list_webhook_endpoints(active_only: bool = False) -> list[WebhookEndpoint]:
    stmt = sa.select(WebhookEndpoint).order_by(WebhookEndpoint.id)
    if active_only:
        stmt = stmt.where(WebhookEndpoint.active == 1)
    return _entities(stmt)


def update_webhook_endpoint(ep_id: int, fields: dict[str, Any]) -> None:
    if not fields:
        return
    with _SessionFactory.begin() as s:
        s.execute(
            sa.update(WebhookEndpoint.__table__)
            .where(WebhookEndpoint.__table__.c.id == ep_id)
            .values(**fields)
        )


def delete_webhook_endpoint(ep_id: int) -> None:
    with _SessionFactory.begin() as s:
        s.execute(
            sa.delete(WebhookDelivery.__table__).where(WebhookDelivery.__table__.c.endpoint_id == ep_id)
        )
        s.execute(
            sa.delete(WebhookEndpoint.__table__).where(WebhookEndpoint.__table__.c.id == ep_id)
        )


# --- webhook deliveries ----------------------------------------------------------

def enqueue_delivery(
    endpoint_id: int,
    event: str,
    invoice_id: str | None,
    next_attempt_at: int,
    event_id: str,
    previous_status: str | None,
) -> None:
    with _SessionFactory.begin() as s:
        s.execute(
            sa.insert(WebhookDelivery.__table__).values(
                endpoint_id=endpoint_id,
                event=event,
                event_id=event_id,
                previous_status=previous_status,
                invoice_id=invoice_id,
                next_attempt_at=next_attempt_at,
                created_at=int(time.time()),
            )
        )


class DeliveryWithEndpoint(NamedTuple):
    """A delivery row joined with its endpoint's url/secret (url/secret are
    None when the endpoint row was deleted — LEFT JOIN in list_deliveries)."""
    delivery: WebhookDelivery
    endpoint_url: str | None
    endpoint_secret: str | None


def pending_deliveries(now: int) -> list[DeliveryWithEndpoint]:
    stmt = (
        sa.select(WebhookDelivery, WebhookEndpoint.url, WebhookEndpoint.secret)
        .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
        .where(WebhookDelivery.delivered_at.is_(None), WebhookDelivery.next_attempt_at <= now)
        .order_by(WebhookDelivery.id)
        .limit(20)
    )
    with _SessionFactory.begin() as s:
        rows = s.execute(stmt).all()
    return [DeliveryWithEndpoint(d, url, secret) for d, url, secret in rows]


def update_delivery(delivery_id: int, fields: dict[str, Any]) -> None:
    with _SessionFactory.begin() as s:
        s.execute(
            sa.update(WebhookDelivery.__table__)
            .where(WebhookDelivery.__table__.c.id == delivery_id)
            .values(**fields)
        )


def list_deliveries(limit: int = 50) -> list[DeliveryWithEndpoint]:
    stmt = (
        sa.select(WebhookDelivery, WebhookEndpoint.url)
        .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id, isouter=True)
        .order_by(WebhookDelivery.id.desc())
        .limit(limit)
    )
    with _SessionFactory.begin() as s:
        rows = s.execute(stmt).all()
    return [DeliveryWithEndpoint(d, url, None) for d, url in rows]


def deliveries_for_invoice(inv_id: str) -> list[WebhookDelivery]:
    stmt = (
        sa.select(WebhookDelivery)
        .where(WebhookDelivery.invoice_id == inv_id)
        .order_by(WebhookDelivery.id)
    )
    return _entities(stmt)


# --- payouts ----------------------------------------------------------------------

_PAYOUT_COLS = (
    "created_at", "address", "amount_atomic", "memo", "txid", "fee_atomic",
    "idempotency_key", "error",
)


def insert_payout(p: dict[str, Any]) -> None:
    with _SessionFactory.begin() as s:
        s.execute(
            _insert(Payout.__table__)
            .values({c: p[c] for c in _PAYOUT_COLS})
            .on_conflict_do_nothing()
        )


def list_payouts(limit: int = 25) -> list[Payout]:
    stmt = sa.select(Payout).order_by(Payout.id.desc()).limit(limit)
    return _entities(stmt)


def get_payout_by_key(idempotency_key: str) -> Payout | None:
    return _entity(sa.select(Payout).where(Payout.idempotency_key == idempotency_key))


def get_payout(payout_id: int) -> Payout | None:
    return _entity(sa.select(Payout).where(Payout.id == payout_id))


def update_payout_error(key: str, error: str) -> None:
    with _SessionFactory.begin() as s:
        s.execute(
            sa.update(Payout.__table__)
            .where(Payout.__table__.c.idempotency_key == key)
            .values(error=error)
        )


def update_payout_sent(key: str, txid: str | None, fee_atomic: int | None) -> None:
    with _SessionFactory.begin() as s:
        s.execute(
            sa.update(Payout.__table__)
            .where(Payout.__table__.c.idempotency_key == key)
            .values(txid=txid, fee_atomic=fee_atomic, error=None)
        )


# --- api keys ------------------------------------------------------------------------

def create_api_key(kind: str, name: str, key: str) -> ApiKey | None:
    """Insert a full key; stores only its sha256. Returns the row (with prefix)."""
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    with _SessionFactory.begin() as s:
        s.execute(
            _insert(ApiKey.__table__)
            .values(kind=kind, name=name, key_hash=key_hash, prefix=key[:6], created_at=int(time.time()))
            .on_conflict_do_nothing()
        )
    return _entity(sa.select(ApiKey).where(ApiKey.key_hash == key_hash))


def verify_api_key(kind: str, key: str) -> ApiKey | None:
    """Active key of the given kind, or None. Updates last_used_at."""
    row = _entity(
        sa.select(ApiKey).where(
            ApiKey.kind == kind,
            ApiKey.key_hash == hashlib.sha256(key.encode()).hexdigest(),
            ApiKey.revoked_at.is_(None),
        )
    )
    if row:
        with _SessionFactory.begin() as s:
            s.execute(
                sa.update(ApiKey.__table__)
                .where(ApiKey.id == row.id)
                .values(last_used_at=int(time.time()))
            )
    return row


def list_api_keys() -> list[ApiKey]:
    return _entities(sa.select(ApiKey).order_by(ApiKey.kind, ApiKey.id))


def revoke_api_key(key_id: int) -> bool:
    with _SessionFactory.begin() as s:
        result = s.execute(
            sa.update(ApiKey.__table__)
            .where(ApiKey.__table__.c.id == key_id, ApiKey.__table__.c.revoked_at.is_(None))
            .values(revoked_at=int(time.time()))
        )
        return result.rowcount > 0


def has_active_api_keys(kind: str) -> bool:
    with _SessionFactory.begin() as s:
        row = s.execute(
            sa.select(1)
            .where(ApiKey.__table__.c.kind == kind, ApiKey.__table__.c.revoked_at.is_(None))
            .limit(1)
        ).first()
    return row is not None


# --- wallet sign-in challenges ---------------------------------------------------

def create_auth_challenge(nonce: str, now: int) -> None:
    """INSERT OR IGNORE (nonce is PK; collision astronomically unlikely)."""
    with _SessionFactory.begin() as s:
        s.execute(
            _insert(AuthChallenge.__table__)
            .values(nonce=nonce, created_at=now)
            .on_conflict_do_nothing()
        )


def get_auth_challenge(nonce: str) -> AuthChallenge | None:
    return _entity(sa.select(AuthChallenge).where(AuthChallenge.nonce == nonce))


def consume_auth_challenge(nonce: str, now: int) -> bool:
    """Atomically mark a challenge used; False if already used or missing."""
    with _SessionFactory.begin() as s:
        result = s.execute(
            sa.update(AuthChallenge.__table__)
            .where(AuthChallenge.__table__.c.nonce == nonce, AuthChallenge.__table__.c.used_at.is_(None))
            .values(used_at=now)
        )
        return result.rowcount > 0


def record_auth_failure(ip: str, now: int) -> None:
    with _SessionFactory.begin() as s:
        s.execute(sa.insert(auth_attempts).values(ip=ip, created_at=now))


def count_recent_auth_failures(ip: str, window_start: int) -> int:
    with _SessionFactory.begin() as s:
        n = s.execute(
            sa.select(sa.func.count())
            .select_from(auth_attempts)
            .where(auth_attempts.c.ip == ip, auth_attempts.c.created_at >= window_start)
        ).scalar()
    return int(n or 0)


def clear_auth_failures(ip: str) -> None:
    with _SessionFactory.begin() as s:
        s.execute(sa.delete(auth_attempts).where(auth_attempts.c.ip == ip))


def sweep_auth_challenges(now: int, challenge_ttl: int, lockout_window: int) -> None:
    """Issue-time cleanup: drop used/expired challenges and stale lockout attempts."""
    with _SessionFactory.begin() as s:
        s.execute(
            sa.delete(AuthChallenge.__table__).where(
                sa.or_(
                    AuthChallenge.__table__.c.used_at.is_not(None),
                    AuthChallenge.__table__.c.created_at < now - challenge_ttl,
                )
            )
        )
        s.execute(sa.delete(auth_attempts).where(auth_attempts.c.created_at < now - lockout_window))
