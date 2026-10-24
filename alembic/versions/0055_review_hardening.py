"""Full-backend review hardening: integrity constraints + refund ledger marker.

- refunds.ledger_applied_at: the refund's wallet/order/credit-note effect is applied
  exactly once (re-applied idempotently by admin reconciliation after a failure).
- cart_items UNIQUE(cart_id, product_id): concurrent adds can't create duplicate lines.
- subscription_items UNIQUE(subscription_id, product_id): no duplicate product lines.
- shops: at most one is_home shop per account.
- payments amount CHECK: a zero amount is allowed ONLY for the no-gateway settlement of
  a fully-discounted order (gateway='none'); every gateway payment stays > 0.

Existing duplicates are merged/deactivated first so the indexes can be created.

Revision ID: 0055_review_hardening
Revises: 0054_shipment_create_lease
Create Date: 2026-10-24
"""
import sqlalchemy as sa

from alembic import op

revision = "0055_review_hardening"
down_revision = "0054_shipment_create_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_payments_amount_pos", "payments", type_="check")
    op.create_check_constraint(
        "ck_payments_amount_pos", "payments", "amount > 0 OR (gateway = 'none' AND amount = 0)"
    )
    op.add_column(
        "refunds", sa.Column("ledger_applied_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Refunds already 'processed' before this migration had their effect applied inline.
    op.execute("UPDATE refunds SET ledger_applied_at = created_at WHERE status = 'processed'")

    # cart_items: fold duplicate lines into the lowest id, then enforce uniqueness.
    op.execute(
        """
        UPDATE cart_items c SET qty = d.total
        FROM (SELECT cart_id, product_id, MIN(id) AS keep_id, SUM(qty) AS total
              FROM cart_items GROUP BY cart_id, product_id HAVING COUNT(*) > 1) d
        WHERE c.id = d.keep_id
        """
    )
    op.execute(
        """
        DELETE FROM cart_items c USING cart_items k
        WHERE c.cart_id = k.cart_id AND c.product_id = k.product_id AND c.id > k.id
        """
    )
    op.create_unique_constraint("uq_cart_items_cart_product", "cart_items", ["cart_id", "product_id"])

    # subscription_items: keep the newest row per (subscription, product), drop the rest
    # (their one-day overrides first — FK — those duplicates were never reachable by the
    # override endpoint anyway, which 500'd on MultipleResultsFound).
    op.execute(
        """
        DELETE FROM subscription_overrides o USING subscription_items s, subscription_items k
        WHERE o.subscription_item_id = s.id
          AND s.subscription_id = k.subscription_id AND s.product_id = k.product_id
          AND s.id < k.id
        """
    )
    op.execute(
        """
        DELETE FROM subscription_items s USING subscription_items k
        WHERE s.subscription_id = k.subscription_id AND s.product_id = k.product_id
          AND s.id < k.id
        """
    )
    op.create_unique_constraint(
        "uq_subscription_items_sub_product", "subscription_items", ["subscription_id", "product_id"]
    )

    # shops: one home shop per account (keep the oldest flagged).
    op.execute(
        """
        UPDATE shops s SET is_home = false FROM shops k
        WHERE s.account_id = k.account_id AND s.is_home AND k.is_home AND s.id > k.id
        """
    )
    op.create_index(
        "uq_shops_one_home_per_account", "shops", ["account_id"],
        unique=True, postgresql_where=sa.text("is_home"),
    )


def downgrade() -> None:
    op.drop_constraint("ck_payments_amount_pos", "payments", type_="check")
    op.create_check_constraint("ck_payments_amount_pos", "payments", "amount > 0")
    op.drop_index("uq_shops_one_home_per_account", table_name="shops")
    op.drop_constraint("uq_subscription_items_sub_product", "subscription_items", type_="unique")
    op.drop_constraint("uq_cart_items_cart_product", "cart_items", type_="unique")
    op.drop_column("refunds", "ledger_applied_at")
