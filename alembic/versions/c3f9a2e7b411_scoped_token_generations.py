"""Scoped token series, generations and snapshot join tables

Revision ID: c3f9a2e7b411
Revises: 28442e0a1830
Create Date: 2026-10-13

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "c3f9a2e7b411"
down_revision = "28442e0a1830"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "token_series",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column(
            "overlap_seconds",
            sa.Integer(),
            nullable=False,
            server_default="60",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "created_by_id",
            "name",
            name="token_series_name_userid_unique_constraint",
        ),
    )
    op.create_index(
        op.f("ix_token_series_created_at"), "token_series", ["created_at"]
    )

    op.add_column(
        "tokens", sa.Column("series_id", sa.String(), nullable=True)
    )
    op.create_foreign_key(
        "tokens_series_id_fkey",
        "tokens",
        "token_series",
        ["series_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        op.f("ix_tokens_series_id"), "tokens", ["series_id"], unique=False
    )
    op.add_column(
        "tokens",
        sa.Column(
            "generation",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "tokens", sa.Column("expires_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "tokens", sa.Column("revoked_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "tokens", sa.Column("retire_at", sa.DateTime(), nullable=True)
    )
    # Generation names live on the series; multiple generations of one owner
    # legitimately share a name. Legacy duplicate-name rejection stays in the
    # handler, and series rows carry the (owner, name) unique constraint.
    op.drop_constraint(
        "token_name_userid_unique_constraint", "tokens", type_="unique"
    )

    op.create_table(
        "group_tokens",
        sa.Column("token_id", sa.String(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["token_id"], ["tokens.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_id", "group_id"),
    )
    op.create_index(
        op.f("ix_group_tokens_created_at"), "group_tokens", ["created_at"]
    )
    op.create_index(
        "group_tokens_reverse_ind",
        "group_tokens",
        ["group_id", "token_id"],
        unique=False,
    )

    op.create_table(
        "stream_tokens",
        sa.Column("token_id", sa.String(), nullable=False),
        sa.Column("stream_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["token_id"], ["tokens.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["stream_id"], ["streams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_id", "stream_id"),
    )
    op.create_index(
        op.f("ix_stream_tokens_created_at"), "stream_tokens", ["created_at"]
    )
    op.create_index(
        "stream_tokens_reverse_ind",
        "stream_tokens",
        ["stream_id", "token_id"],
        unique=False,
    )


def downgrade():
    op.drop_index("stream_tokens_reverse_ind", table_name="stream_tokens")
    op.drop_index(op.f("ix_stream_tokens_created_at"), table_name="stream_tokens")
    op.drop_table("stream_tokens")

    op.drop_index("group_tokens_reverse_ind", table_name="group_tokens")
    op.drop_index(op.f("ix_group_tokens_created_at"), table_name="group_tokens")
    op.drop_table("group_tokens")

    # Generations legitimately share a name; remove them before restoring
    # the legacy per-user unique name constraint.
    op.execute("DELETE FROM tokens WHERE series_id IS NOT NULL")

    op.drop_column("tokens", "retire_at")
    op.drop_column("tokens", "revoked_at")
    op.drop_column("tokens", "expires_at")
    op.drop_column("tokens", "generation")
    op.drop_index(op.f("ix_tokens_series_id"), table_name="tokens")
    op.drop_constraint("tokens_series_id_fkey", "tokens", type_="foreignkey")
    op.drop_column("tokens", "series_id")
    op.create_unique_constraint(
        "token_name_userid_unique_constraint", "tokens", ["created_by_id", "name"]
    )

    op.drop_index(op.f("ix_token_series_created_at"), table_name="token_series")
    op.drop_table("token_series")
