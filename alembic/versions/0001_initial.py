"""initial schema: postgis + users, customer_accounts, shops, delivery_assignments, products

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-10
"""
from typing import Sequence, Union

import geoalchemy2
import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("phone", sa.String(16), nullable=False),
        sa.Column("phone_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("role", sa.String(16), server_default="customer", nullable=False),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("token_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("phone", name="uq_users_phone"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_phone", "users", ["phone"])

    op.create_table(
        "customer_accounts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("owner_user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("account_type", sa.String(16), server_default="retail", nullable=False),
        sa.Column("billing_mode", sa.String(20), server_default="prepaid_wallet", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "shops",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("address", sa.String(), nullable=True),
        sa.Column(
            "geo",
            geoalchemy2.Geography(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_shops_account_id", "shops", ["account_id"])

    op.create_table(
        "delivery_assignments",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("delivery_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), sa.ForeignKey("shops.id"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("delivery_id", "shop_id", name="uq_delivery_shop"),
    )
    op.create_index("ix_delivery_assignments_delivery_id", "delivery_assignments", ["delivery_id"])
    op.create_index("ix_delivery_assignments_shop_id", "delivery_assignments", ["shop_id"])

    op.create_table(
        "products",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("uom", sa.String(16), server_default="piece", nullable=False),
        sa.Column("pack_size", sa.Numeric(10, 2), server_default="1", nullable=False),
        sa.Column("is_subscribable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("perishable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("hsn_code", sa.String(16), nullable=True),
        sa.Column("gst_rate", sa.Numeric(4, 2), server_default="0", nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("sku", name="uq_products_sku"),
    )


def downgrade() -> None:
    op.drop_table("products")
    op.drop_table("delivery_assignments")
    op.drop_table("shops")
    op.drop_table("customer_accounts")
    op.drop_index("ix_users_phone", table_name="users")
    op.drop_table("users")
