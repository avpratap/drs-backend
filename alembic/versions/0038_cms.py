"""content/CMS: articles + static pages + banners

Revision ID: 0038_cms
Revises: 0037_variants
Create Date: 2026-10-17
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0038_cms"
down_revision: str | None = "0037_variants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "articles",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("slug", sa.String(160), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("excerpt", sa.String(500)),
        sa.Column("author_name", sa.String(120)),
        sa.Column("category", sa.String(64)),
        sa.Column("tags", postgresql.JSONB()),
        sa.Column("status", sa.String(10), server_default="draft", nullable=False),
        sa.Column("meta_title", sa.String(255)),
        sa.Column("meta_description", sa.String(320)),
        sa.Column("cover_image_url", sa.String(512)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("slug", name="uq_article_slug"),
    )
    op.create_index("ix_articles_category", "articles", ["category"])
    op.create_index("ix_articles_status", "articles", ["status"])

    op.create_table(
        "static_pages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("slug", sa.String(160), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("status", sa.String(10), server_default="draft", nullable=False),
        sa.Column("meta_title", sa.String(255)),
        sa.Column("meta_description", sa.String(320)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("slug", name="uq_static_page_slug"),
    )

    op.create_table(
        "banners",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("image_url", sa.String(512), nullable=False),
        sa.Column("cta_link", sa.String(512)),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("valid_from", sa.Date()),
        sa.Column("valid_to", sa.Date()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("banners")
    op.drop_table("static_pages")
    op.drop_index("ix_articles_status", table_name="articles")
    op.drop_index("ix_articles_category", table_name="articles")
    op.drop_table("articles")
