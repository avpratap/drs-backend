"""profile, address book, payment methods, mandates, consent ledger

Revision ID: 0011_profile
Revises: 0010_hardening
Create Date: 2026-10-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography

revision: str = "0011_profile"
down_revision: Union[str, None] = "0010_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ts = dict(server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "user_profiles",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("display_name", sa.String(120)),
        sa.Column("avatar_url", sa.String(512)),
        sa.Column("date_of_birth", sa.Date()),
        sa.Column("gender", sa.String(16)),
        sa.Column("locale", sa.String(12), server_default="en-IN", nullable=False),
        sa.Column("alternate_phone", sa.String(16)),
        sa.Column("gstin", sa.String(15)),
        sa.Column("legal_name", sa.String(255)),
        sa.Column("notify_whatsapp", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("notify_sms", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("notify_push", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("notify_email", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("marketing_opt_in", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("quiet_hours_start", sa.Integer()),
        sa.Column("quiet_hours_end", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), **_ts),
        sa.Column("updated_at", sa.DateTime(timezone=True), **_ts),
    )

    op.create_table(
        "addresses",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "account_id",
            sa.BigInteger(),
            sa.ForeignKey("customer_accounts.id"),
            nullable=False,
        ),
        sa.Column("label", sa.String(40)),
        sa.Column("contact_name", sa.String(120)),
        sa.Column("contact_phone", sa.String(16)),
        sa.Column("line1", sa.String(255), nullable=False),
        sa.Column("line2", sa.String(255)),
        sa.Column("landmark", sa.String(255)),
        sa.Column("city", sa.String(120), nullable=False),
        sa.Column("state", sa.String(120), nullable=False),
        sa.Column("pincode", sa.String(10), nullable=False),
        sa.Column("country", sa.String(2), server_default="IN", nullable=False),
        sa.Column("geo", Geography(geometry_type="POINT", srid=4326, spatial_index=False)),
        sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), **_ts),
        sa.Column("updated_at", sa.DateTime(timezone=True), **_ts),
    )
    op.create_index("ix_addresses_account_id", "addresses", ["account_id"])
    op.create_index("ix_addresses_pincode", "addresses", ["pincode"])
    # At most one default address per account (among active rows).
    op.create_index(
        "uq_address_default",
        "addresses",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text("is_default = true AND status = 'active'"),
    )

    op.create_table(
        "payment_methods",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "account_id",
            sa.BigInteger(),
            sa.ForeignKey("customer_accounts.id"),
            nullable=False,
        ),
        sa.Column("method_type", sa.String(16), nullable=False),
        sa.Column("gateway", sa.String(16), server_default="razorpay", nullable=False),
        sa.Column("gateway_customer_id", sa.String(64)),
        sa.Column("gateway_token", sa.String(128)),
        sa.Column("display", sa.String(64)),
        sa.Column("brand", sa.String(32)),
        sa.Column("expiry_month", sa.Integer()),
        sa.Column("expiry_year", sa.Integer()),
        sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), **_ts),
        sa.Column("updated_at", sa.DateTime(timezone=True), **_ts),
    )
    op.create_index("ix_payment_methods_account_id", "payment_methods", ["account_id"])
    op.create_index(
        "uq_payment_method_default",
        "payment_methods",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text("is_default = true AND status = 'active'"),
    )

    op.create_table(
        "payment_mandates",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "account_id",
            sa.BigInteger(),
            sa.ForeignKey("customer_accounts.id"),
            nullable=False,
        ),
        sa.Column(
            "payment_method_id", sa.BigInteger(), sa.ForeignKey("payment_methods.id")
        ),
        sa.Column("gateway", sa.String(16), server_default="razorpay", nullable=False),
        sa.Column("gateway_mandate_id", sa.String(128), unique=True),
        sa.Column("max_amount", sa.Float(), nullable=False),
        sa.Column("frequency", sa.String(16), server_default="as_presented", nullable=False),
        sa.Column("valid_from", sa.Date()),
        sa.Column("valid_to", sa.Date()),
        sa.Column("status", sa.String(16), server_default="created", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), **_ts),
        sa.Column("updated_at", sa.DateTime(timezone=True), **_ts),
    )
    op.create_index("ix_payment_mandates_account_id", "payment_mandates", ["account_id"])

    op.create_table(
        "consents",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("purpose", sa.String(40), nullable=False),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column("source", sa.String(16), server_default="app", nullable=False),
        sa.Column("ip", sa.String(64)),
        sa.Column("user_agent", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), **_ts),
    )
    op.create_index("ix_consents_user_id", "consents", ["user_id"])
    op.create_index("ix_consents_lookup", "consents", ["user_id", "purpose", "id"])


def downgrade() -> None:
    op.drop_table("consents")
    op.drop_table("payment_mandates")
    op.drop_index("uq_payment_method_default", table_name="payment_methods")
    op.drop_table("payment_methods")
    op.drop_index("uq_address_default", table_name="addresses")
    op.drop_table("addresses")
    op.drop_table("user_profiles")
