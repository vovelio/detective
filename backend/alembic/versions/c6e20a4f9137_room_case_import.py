"""Room-scoped imported cases and lobby catalogue notifications.

Revision ID: c6e20a4f9137
Revises: 9ab31d5e42c0
"""

import sqlalchemy as sa
from alembic import op

revision = "c6e20a4f9137"
down_revision = "9ab31d5e42c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scenarios", sa.Column("scope_room_id", sa.UUID(), nullable=True))
    op.create_foreign_key("fk_scenario_scope_room", "scenarios", "rooms", ["scope_room_id"], ["id"])
    op.create_index("ix_scenarios_scope_room_id", "scenarios", ["scope_room_id"])
    op.add_column(
        "rooms", sa.Column("catalog_revision", sa.Integer(), nullable=False, server_default="0")
    )


def downgrade() -> None:
    # Imported cases must not silently become global built-ins on downgrade.
    # Refuse while any scope remains rather than destroying games or leaking cases.
    connection = op.get_bind()
    count = connection.scalar(
        sa.text("SELECT count(*) FROM scenarios WHERE scope_room_id IS NOT NULL")
    )
    if count:
        raise RuntimeError("Cannot downgrade while room imports exist")
    op.drop_column("rooms", "catalog_revision")
    op.drop_index("ix_scenarios_scope_room_id", table_name="scenarios")
    op.drop_constraint("fk_scenario_scope_room", "scenarios", type_="foreignkey")
    op.drop_column("scenarios", "scope_room_id")
