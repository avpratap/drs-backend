"""shipments + tracking events + product shipping dimensions

Revision ID: 0032_shipments
Revises: 0031_hardening5
Create Date: 2026-10-17
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0032_shipments"
down_revision: str | None = "0031_hardening5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("products", sa.Column("weight_kg", sa.Numeric(8, 3)))
    op.add_column("products", sa.Column("length_cm", sa.Numeric(8, 2)))
    op.add_column("products", sa.Column("width_cm", sa.Numeric(8, 2)))
    op.add_column("products", sa.Column("height_cm", sa.Numeric(8, 2)))

    op.create_table(
        "shipments",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("shipment_no", sa.String(32), nullable=False),
        sa.Column("order_id", sa.BigInteger(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id"), nullable=False),
        sa.Column("status", sa.String(20), server_default="draft", nullable=False),
        sa.Column("courier", sa.String(64)),
        sa.Column("awb", sa.String(64)),
        sa.Column("tracking_url", sa.String(512)),
        sa.Column("label_url", sa.String(512)),
        sa.Column("shipping_cost", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("cod_amount", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("weight_kg", sa.Numeric(8, 3), server_default="0.5", nullable=False),
        sa.Column("to_name", sa.String(120)),
        sa.Column("to_phone", sa.String(16)),
        sa.Column("to_address", sa.String(512)),
        sa.Column("to_city", sa.String(120)),
        sa.Column("to_state", sa.String(120)),
        sa.Column("to_pincode", sa.String(10), nullable=False),
        sa.Column("gateway_order_id", sa.String(64)),
        sa.Column("gateway_shipment_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("shipment_no", name="uq_shipment_no"),
    )
    op.create_index("ix_shipments_order_id", "shipments", ["order_id"])
    op.create_index("ix_shipments_account_id", "shipments", ["account_id"])
    op.create_index("ix_shipments_awb", "shipments", ["awb"])
    op.create_index("ix_shipments_gateway_shipment_id", "shipments", ["gateway_shipment_id"])

    op.create_table(
        "shipment_tracking_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("shipment_id", sa.BigInteger(), sa.ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("location", sa.String(120)),
        sa.Column("note", sa.String(255)),
        sa.Column("raw", postgresql.JSONB()),
        sa.Column("event_time", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tracking_events_shipment_id", "shipment_tracking_events", ["shipment_id"])


def downgrade() -> None:
    op.drop_index("ix_tracking_events_shipment_id", table_name="shipment_tracking_events")
    op.drop_table("shipment_tracking_events")
    for ix in (
        "ix_shipments_gateway_shipment_id",
        "ix_shipments_awb",
        "ix_shipments_account_id",
        "ix_shipments_order_id",
    ):
        op.drop_index(ix, table_name="shipments")
    op.drop_table("shipments")
    for col in ("height_cm", "width_cm", "length_cm", "weight_kg"):
        op.drop_column("products", col)
