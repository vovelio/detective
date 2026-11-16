"""initial_persistence"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "15d25b13ab98"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "anonymous_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        op.f("ix_anonymous_sessions_expires_at"), "anonymous_sessions", ["expires_at"], unique=False
    )
    op.create_table(
        "scenarios",
        sa.Column("id", sa.String(length=100), nullable=False),
        sa.Column("version", sa.String(length=20), nullable=False),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("package_name", sa.String(length=80), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "rooms",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=24), nullable=False),
        sa.Column("host_member_id", sa.Uuid(), nullable=False),
        sa.Column("selected_case_id", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["selected_case_id"],
            ["scenarios.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_table(
        "games",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("room_id", sa.Uuid(), nullable=False),
        sa.Column("scenario_id", sa.String(length=100), nullable=True),
        sa.Column("phase", sa.String(length=20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint("revision >= 0", name="ck_game_revision"),
        sa.ForeignKeyConstraint(
            ["room_id"],
            ["rooms.id"],
        ),
        sa.ForeignKeyConstraint(
            ["scenario_id"],
            ["scenarios.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("room_id"),
    )
    op.create_table(
        "members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("room_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("nickname", sa.String(length=20), nullable=False),
        sa.Column("nickname_key", sa.String(length=100), nullable=False),
        sa.Column("seat", sa.Integer(), nullable=False),
        sa.Column(
            "joined_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("seat >= 0 AND seat < 8", name="ck_member_seat"),
        sa.ForeignKeyConstraint(
            ["room_id"],
            ["rooms.id"],
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["anonymous_sessions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "room_id", name="uq_member_room"),
        sa.UniqueConstraint("room_id", "nickname_key", name="uq_member_nickname"),
        sa.UniqueConstraint("room_id", "seat", name="uq_member_seat"),
        sa.UniqueConstraint("room_id", "session_id", name="uq_member_session"),
    )
    op.create_index(op.f("ix_members_room_id"), "members", ["room_id"], unique=False)
    op.create_index(op.f("ix_members_session_id"), "members", ["session_id"], unique=False)
    op.create_table(
        "game_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("client_action_id", sa.Uuid(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("game_revision", sa.Integer(), nullable=False),
        sa.Column("command_type", sa.String(length=32), nullable=False),
        sa.Column("result_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["anonymous_sessions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "session_id", "client_action_id", name="uq_game_action"),
    )
    op.create_index(op.f("ix_game_actions_game_id"), "game_actions", ["game_id"], unique=False)
    op.create_table(
        "game_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("public_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "revision", name="uq_game_event"),
    )
    op.create_index(op.f("ix_game_events_game_id"), "game_events", ["game_id"], unique=False)
    op.create_foreign_key(
        "fk_room_host",
        "rooms",
        "members",
        ["host_member_id", "id"],
        ["id", "room_id"],
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    op.drop_constraint("fk_room_host", "rooms", type_="foreignkey")
    op.drop_index(op.f("ix_game_events_game_id"), table_name="game_events")
    op.drop_table("game_events")
    op.drop_index(op.f("ix_game_actions_game_id"), table_name="game_actions")
    op.drop_table("game_actions")
    op.drop_index(op.f("ix_members_session_id"), table_name="members")
    op.drop_index(op.f("ix_members_room_id"), table_name="members")
    op.drop_table("members")
    op.drop_table("games")
    op.drop_table("rooms")
    op.drop_table("scenarios")
    op.drop_index(op.f("ix_anonymous_sessions_expires_at"), table_name="anonymous_sessions")
    op.drop_table("anonymous_sessions")
