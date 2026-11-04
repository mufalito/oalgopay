"""SQLAlchemy 2.x declarative models — the single source of schema truth.

The gateway's tables are defined here and materialized by Alembic
(migrations/). SQLite is the default engine; types stay dialect-portable so
switching to PostgreSQL is a `database_url` change, not a schema change:
unix epochs are BigInteger (int8, not int4) and flags stay Integer 0/1.

No relationships are declared — queries join explicitly, as this codebase
always has.
"""
from __future__ import annotations

from sqlalchemy import BigInteger, Column, ForeignKey, Index, Integer, String, Table, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_height: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_atomic: Mapped[int] = mapped_column(BigInteger, nullable=False)
    memo: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The attribute cannot be `metadata` (reserved by DeclarativeBase); the
    # column and every dict key in the API surface stay `metadata`.
    meta: Mapped[str] = mapped_column("metadata", Text, nullable=False, server_default=text("'{}'"))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    paid_atomic: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    confirmed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    late_payment: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    overpaid: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        Index("idx_invoices_status", "status"),
        Index("idx_invoices_created", text("created_at DESC")),
    )


class Payment(Base):
    __tablename__ = "payments"

    txid: Mapped[str] = mapped_column(Text, primary_key=True)
    output_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[str] = mapped_column(
        Text, ForeignKey("invoices.id"), nullable=False
    )
    amount_atomic: Mapped[int] = mapped_column(BigInteger, nullable=False)
    block_height: Mapped[int] = mapped_column(BigInteger, nullable=False)
    memo_hex: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    match_method: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_payments_invoice", "invoice_id"),
        Index("idx_payments_invoice_matched", "invoice_id", "matched_at"),
    )


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    secret: Mapped[str] = mapped_column(Text, nullable=False)
    # Integer 0/1, NOT Boolean: the dicts the API layer consumes (and writes
    # back) carry ints; bool coercion stays at the API boundary.
    active: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    endpoint_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("webhook_endpoints.id"), nullable=False
    )
    event: Mapped[str] = mapped_column(Text, nullable=False)
    event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        Index(
            "idx_deliveries_pending",
            "next_attempt_at",
            sqlite_where=text("delivered_at IS NULL"),
            postgresql_where=text("delivered_at IS NULL"),
        ),
    )


class Payout(Base):
    __tablename__ = "payouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    amount_atomic: Mapped[int] = mapped_column(BigInteger, nullable=False)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    txid: Mapped[str | None] = mapped_column(Text, nullable=True)
    fee_atomic: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # 'merchant' | 'payouts' | 'auth'
    name: Mapped[str] = mapped_column(Text, nullable=False)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # sha256 hex
    prefix: Mapped[str] = mapped_column(Text, nullable=False)  # first chars, for display
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_used_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    revoked_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (Index("idx_api_keys_lookup", "kind", "key_hash"),)


class AuthChallenge(Base):
    __tablename__ = "auth_challenges"

    nonce: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


# auth_attempts has no primary key (today's schema has none), so it cannot be
# an ORM class — declare it as a core Table in the same metadata. It is only
# ever inserted, counted, and deleted through db.py.
auth_attempts = Table(
    "auth_attempts",
    Base.metadata,
    Column("ip", Text, nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Index("idx_auth_attempts_lookup", "ip", "created_at"),
)
