"""Second review round: integrity backstops, consent defaults, lookup indexes.

- users.email: unique only among VERIFIED owners, case-insensitively (an unverified
  claim must not lock the real owner out of their address).
- user_profiles.notify_whatsapp defaults OFF and is OFF for every profile without a
  current whatsapp_optin consent (WhatsApp needs an opt-in).
- payments: at most one OPEN (creating/initiated) payment per order.

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


def downgrade() -> None:
    op.drop_index("uq_payments_one_open_per_order", table_name="payments")
    op.alter_column("user_profiles", "notify_whatsapp", server_default=sa.true())
    op.drop_index("ix_users_email", table_name="users")
    op.drop_index("uq_users_email_verified", table_name="users")
    op.create_unique_constraint("uq_users_email", "users", ["email"])
