"""base schema: pre-migration-era shape

Revision ID: 0001
Revises:
Create Date: 2025-11-03

The shape every database had before versioned migrations existed: today's
tables minus invoices.created_height, webhook_deliveries.event_id and
webhook_deliveries.previous_status (added in 0002).

Every create is guarded by an inspector check — the moral equivalent of the
old `CREATE TABLE/INDEX IF NOT EXISTS` base schema — so a database that
predates versioned migrations (and therefore carries no alembic_version)
can upgrade straight from the base revision: missing tables and indexes
appear, existing ones are left alone.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _ensure_index(table: str, name: str, columns: list, **kw) -> None:
    insp = sa.inspect(op.get_bind())
    if name not in {ix["name"] for ix in insp.get_indexes(table)}:
        op.create_index(name, table, columns, unique=False, **kw)


def upgrade() -> None:
    existing = _existing_tables()

    if "settings" not in existing:
        op.create_table(
            "settings",
            sa.Column("key", sa.String(), nullable=False),
            sa.Column("value", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("key"),
        )

    if "invoices" not in existing:
        op.create_table(
            "invoices",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
            sa.Column("expires_at", sa.BigInteger(), nullable=False),
            sa.Column("amount_atomic", sa.BigInteger(), nullable=False),
            sa.Column("memo", sa.Text(), nullable=False),
            sa.Column("label", sa.Text(), nullable=True),
            sa.Column("metadata", sa.Text(), server_default=sa.text("'{}'"), nullable=False),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("paid_atomic", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
            sa.Column("confirmed_at", sa.BigInteger(), nullable=True),
            sa.Column("late_payment", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("overpaid", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    _ensure_index("invoices", "idx_invoices_status", ["status"])
    _ensure_index("invoices", "idx_invoices_created", [sa.text("created_at DESC")])

    if "payments" not in existing:
        op.create_table(
            "payments",
            sa.Column("txid", sa.Text(), nullable=False),
            sa.Column("output_index", sa.Integer(), nullable=False),
            sa.Column("invoice_id", sa.Text(), nullable=False),
            sa.Column("amount_atomic", sa.BigInteger(), nullable=False),
            sa.Column("block_height", sa.BigInteger(), nullable=False),
            sa.Column("memo_hex", sa.Text(), nullable=True),
            sa.Column("matched_at", sa.BigInteger(), nullable=False),
            sa.Column("match_method", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("txid", "output_index"),
            sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"]),
        )
    _ensure_index("payments", "idx_payments_invoice", ["invoice_id"])
    _ensure_index("payments", "idx_payments_invoice_matched", ["invoice_id", "matched_at"])

    if "webhook_endpoints" not in existing:
        op.create_table(
            "webhook_endpoints",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("secret", sa.Text(), nullable=False),
            sa.Column("active", sa.Integer(), server_default=sa.text("1"), nullable=False),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    if "webhook_deliveries" not in existing:
        op.create_table(
            "webhook_deliveries",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("endpoint_id", sa.Integer(), nullable=False),
            sa.Column("event", sa.Text(), nullable=False),
            sa.Column("invoice_id", sa.Text(), nullable=True),
            sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("next_attempt_at", sa.BigInteger(), nullable=False),
            sa.Column("last_status", sa.Integer(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("delivered_at", sa.BigInteger(), nullable=True),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["endpoint_id"], ["webhook_endpoints.id"]),
        )
    _ensure_index(
        "webhook_deliveries",
        "idx_deliveries_pending",
        ["next_attempt_at"],
        sqlite_where=sa.text("delivered_at IS NULL"),
        postgresql_where=sa.text("delivered_at IS NULL"),
    )

    if "payouts" not in existing:
        op.create_table(
            "payouts",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
            sa.Column("address", sa.Text(), nullable=False),
            sa.Column("amount_atomic", sa.BigInteger(), nullable=False),
            sa.Column("memo", sa.Text(), nullable=True),
            sa.Column("txid", sa.Text(), nullable=True),
            sa.Column("fee_atomic", sa.BigInteger(), nullable=True),
            sa.Column("idempotency_key", sa.Text(), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("idempotency_key"),
        )

    if "api_keys" not in existing:
        op.create_table(
            "api_keys",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("key_hash", sa.Text(), nullable=False),
            sa.Column("prefix", sa.Text(), nullable=False),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
            sa.Column("last_used_at", sa.BigInteger(), nullable=True),
            sa.Column("revoked_at", sa.BigInteger(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("key_hash"),
        )
    _ensure_index("api_keys", "idx_api_keys_lookup", ["kind", "key_hash"])

    if "auth_challenges" not in existing:
        op.create_table(
            "auth_challenges",
            sa.Column("nonce", sa.Text(), nullable=False),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
            sa.Column("used_at", sa.BigInteger(), nullable=True),
            sa.PrimaryKeyConstraint("nonce"),
        )

    if "auth_attempts" not in existing:
        op.create_table(
            "auth_attempts",
            sa.Column("ip", sa.Text(), nullable=False),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
        )
    _ensure_index("auth_attempts", "idx_auth_attempts_lookup", ["ip", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_auth_attempts_lookup", table_name="auth_attempts")
    op.drop_table("auth_attempts")
    op.drop_table("auth_challenges")
    op.drop_index("idx_api_keys_lookup", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_table("payouts")
    op.drop_index("idx_deliveries_pending", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_table("webhook_endpoints")
    op.drop_index("idx_payments_invoice_matched", table_name="payments")
    op.drop_index("idx_payments_invoice", table_name="payments")
    op.drop_table("payments")
    op.drop_index("idx_invoices_created", table_name="invoices")
    op.drop_index("idx_invoices_status", table_name="invoices")
    op.drop_table("invoices")
    op.drop_table("settings")
