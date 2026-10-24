"""Second review round: integrity backstops, consent defaults, lookup indexes.

- users.email: unique only among VERIFIED owners, case-insensitively (an unverified
  claim must not lock the real owner out of their address).
- user_profiles.notify_whatsapp defaults OFF and is OFF for every profile without a
  current whatsapp_optin consent (WhatsApp needs an opt-in).
- payments: at most one OPEN (creating/initiated) payment per order.
- Lookup indexes for the (ref_type, ref_id) ledger/payment checks run on every
  delivery/checkout/refund, and for FKs on growing tables.
- Backstops for rules enforced in code: one charge per delivery order (wallet/credit),
  one coupon redemption per order, one referral per referee, non-negative balances and
  coupon counters (unique ones only when existing data already satisfies them; the
  ledgers are append-only, so history is never rewritten to fit).
- referral_redemptions.rewarded_at: the referrer is paid on the referee's first paid order.
- products.pack_size Numeric(10,3) like variants (0.125 was stored as 0.13).

Revision ID: 0056_review_round2
Revises: 0055_review_hardening
Create Date: 2026-10-24
"""
import sqlalchemy as sa

from alembic import op

revision = "0056_review_round2"
down_revision = "0055_review_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- users.email: verified-only, case-insensitive uniqueness ---
    op.drop_constraint("uq_users_email", "users", type_="unique")
    op.execute("UPDATE users SET email = lower(btrim(email)) WHERE email IS NOT NULL")
    op.execute("UPDATE users SET email = NULL WHERE email = ''")
    # Case-variants that both claimed verification: keep the oldest verified owner.
    op.execute(
        """
        UPDATE users u SET email_verified_at = NULL FROM users k
        WHERE u.email = k.email AND u.email_verified_at IS NOT NULL
          AND k.email_verified_at IS NOT NULL AND u.id > k.id
        """
    )
    op.create_index(
        "uq_users_email_verified", "users", [sa.text("lower(email)")], unique=True,
        postgresql_where=sa.text("email_verified_at IS NOT NULL"),
    )
    op.create_index("ix_users_email", "users", ["email"])

    # --- WhatsApp is opt-in ---
    op.alter_column("user_profiles", "notify_whatsapp", server_default=sa.false())
    op.execute(
        """
        UPDATE user_profiles p SET notify_whatsapp = false
        WHERE notify_whatsapp AND COALESCE((
            SELECT c.action FROM consents c
            WHERE c.user_id = p.user_id AND c.purpose = 'whatsapp_optin'
            ORDER BY c.id DESC LIMIT 1), 'withdrawn') <> 'granted'
        """
    )

    # --- one open payment per order ---
    op.execute(
        """
        UPDATE payments p SET status = 'failed' FROM payments k
        WHERE p.ref_type = 'order' AND k.ref_type = 'order' AND p.ref_id = k.ref_id
          AND p.status IN ('creating', 'initiated') AND k.status IN ('creating', 'initiated')
          AND p.id < k.id
        """
    )
    op.create_index(
        "uq_payments_one_open_per_order", "payments", ["ref_id"], unique=True,
        postgresql_where=sa.text("ref_type = 'order' AND status IN ('creating', 'initiated')"),
    )
    _more_upgrade()
    # Referral rewards are paid on the referee's first paid order; every existing
    # redemption was already rewarded at redeem time.
    op.add_column(
        "referral_redemptions",
        sa.Column("rewarded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE referral_redemptions SET rewarded_at = created_at")


_INDEXES = [
    ("ix_wallet_txn_ref", "wallet_transactions", ["ref_type", "ref_id"]),
    ("ix_credit_ledger_ref", "credit_ledger", ["ref_type", "ref_id"]),
    ("ix_stock_movements_ref", "stock_movements", ["ref_type", "ref_id"]),
    ("ix_payments_ref", "payments", ["ref_type", "ref_id"]),
    ("ix_notifications_account_id", "notifications", ["account_id"]),
    ("ix_notifications_status_created", "notifications", ["status", "created_at"]),
    ("ix_stock_movements_batch_id", "stock_movements", ["batch_id"]),
    ("ix_order_items_product_id", "order_items", ["product_id"]),
    ("ix_delivery_order_items_product_id", "delivery_order_items", ["product_id"]),
    ("ix_delivery_orders_schedule_run_id", "delivery_orders", ["schedule_run_id"]),
    ("ix_subscriptions_account_id", "subscriptions", ["account_id"]),
    ("ix_stock_batches_location_id", "stock_batches", ["location_id"]),
    ("ix_coupon_redemptions_coupon_account", "coupon_redemptions", ["coupon_id", "account_id"]),
    ("ix_referral_redemptions_referee", "referral_redemptions", ["referee_account_id"]),
    ("ix_driver_locations_created_at", "driver_locations", ["created_at"]),
]

# (name, table, column, WHERE) partial unique indexes created only if the data allows.
_UNIQUE_IF_CLEAN = [
    ("uq_wallet_txn_one_charge_per_delivery", "wallet_transactions", "ref_id",
     "ref_type = 'delivery_order' AND txn_type = 'debit_order'"),
    ("uq_credit_one_charge_per_delivery", "credit_ledger", "ref_id",
     "ref_type = 'delivery_order' AND entry_type = 'charge'"),
    ("uq_coupon_redemption_order", "coupon_redemptions", "order_id", "order_id IS NOT NULL"),
    ("uq_referral_one_per_referee", "referral_redemptions", "referee_account_id", "true"),
]

# Enforced for new writes (NOT VALID: never fails the deploy on historic rows).
_CHECKS = [
    ("ck_coupons_used_count", "coupons",
     "used_count >= 0 AND (usage_limit IS NULL OR used_count <= usage_limit)"),
    ("ck_gift_cards_balance_nonneg", "gift_cards", "balance >= 0"),
    ("ck_loyalty_points_nonneg", "loyalty_accounts", "points_balance >= 0"),
]


def _more_upgrade() -> None:
    for name, table, cols in _INDEXES:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({', '.join(cols)})"
        )
    for name, table, col, where in _UNIQUE_IF_CLEAN:
        op.execute(
            f"""
            DO $$ BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM {table} WHERE {where} GROUP BY {col} HAVING count(*) > 1
              ) THEN
                CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} ({col}) WHERE {where};
              ELSE
                RAISE WARNING '{name} not created: existing duplicates in {table}.{col}';
              END IF;
            END $$;
            """
        )
    for name, table, expr in _CHECKS:
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expr}) NOT VALID")
    op.alter_column(
        "products", "pack_size", type_=sa.Numeric(10, 3), existing_nullable=False
    )


def _more_downgrade() -> None:
    op.alter_column(
        "products", "pack_size", type_=sa.Numeric(10, 2), existing_nullable=False
    )
    for name, table, _expr in _CHECKS:
        op.drop_constraint(name, table, type_="check")
    for name, _table, _col, _where in _UNIQUE_IF_CLEAN:
        op.execute(f"DROP INDEX IF EXISTS {name}")
    for name, _table, _cols in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")


def downgrade() -> None:
    op.drop_column("referral_redemptions", "rewarded_at")
    _more_downgrade()
    op.drop_index("uq_payments_one_open_per_order", table_name="payments")
    op.alter_column("user_profiles", "notify_whatsapp", server_default=sa.true())
    op.drop_index("ix_users_email", table_name="users")
    op.drop_index("uq_users_email_verified", table_name="users")
    op.create_unique_constraint("uq_users_email", "users", ["email"])
