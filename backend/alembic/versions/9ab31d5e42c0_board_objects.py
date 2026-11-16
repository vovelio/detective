"""Durable public board annotations.

Revision ID: 9ab31d5e42c0
Revises: 15d25b13ab98
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "9ab31d5e42c0"
down_revision = "15d25b13ab98"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "board_objects",
        sa.Column("game_id", sa.UUID(), sa.ForeignKey("games.id"), primary_key=True),
        sa.Column("id", sa.String(110), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version >= 1", name="ck_board_version"),
        sa.CheckConstraint(
            "kind IN ('card_position', 'stroke', 'arrow', 'note')", name="ck_board_kind"
        ),
    )


def downgrade() -> None:
    op.drop_table("board_objects")
