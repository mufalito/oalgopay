"""pre-migration-era normalization: created_height + delivery dedupe columns

Revision ID: 0002
Revises: 0001
Create Date: 2025-11-03

Adds the three columns deployments from before versioned migrations predate.
Each add is guarded by an inspector check so stamping an already-partially-
migrated database at 0001 and upgrading is safe.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "created_height" not in _columns("invoices"):
        op.add_column(
            "invoices",
            sa.Column("created_height", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        )
    dev_cols = _columns("webhook_deliveries")
    if "event_id" not in dev_cols:
        op.add_column("webhook_deliveries", sa.Column("event_id", sa.Text(), nullable=True))
    if "previous_status" not in dev_cols:
        op.add_column("webhook_deliveries", sa.Column("previous_status", sa.Text(), nullable=True))


def downgrade() -> None:
    dev_cols = _columns("webhook_deliveries")
    if "previous_status" in dev_cols:
        op.drop_column("webhook_deliveries", "previous_status")
    if "event_id" in dev_cols:
        op.drop_column("webhook_deliveries", "event_id")
    if "created_height" in _columns("invoices"):
        op.drop_column("invoices", "created_height")
