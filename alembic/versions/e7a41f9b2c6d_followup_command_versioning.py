"""followup command versioning

Revision ID: e7a41f9b2c6d
Revises: 28442e0a1830
Create Date: 2026-10-13

"""

import sqlalchemy as sa

from alembic import op

revision = "e7a41f9b2c6d"
down_revision = "28442e0a1830"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "followuprequests",
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )

    op.add_column(
        "facilitytransactions",
        sa.Column("followup_request_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "facilitytransactions",
        sa.Column("allocation_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "facilitytransactions",
        sa.Column("obj_id", sa.String(), nullable=True),
    )
    op.add_column(
        "facilitytransactions",
        sa.Column("action", sa.String(), nullable=True),
    )
    op.add_column(
        "facilitytransactions",
        sa.Column("command_id", sa.String(), nullable=True),
    )
    op.add_column(
        "facilitytransactions",
        sa.Column("disposition", sa.String(), nullable=True),
    )
    op.create_index(
        op.f("ix_facilitytransactions_followup_request_version"),
        "facilitytransactions",
        ["followup_request_version"],
        unique=False,
    )
    op.create_index(
        op.f("ix_facilitytransactions_action"),
        "facilitytransactions",
        ["action"],
        unique=False,
    )
    op.create_index(
        op.f("ix_facilitytransactions_disposition"),
        "facilitytransactions",
        ["disposition"],
        unique=False,
    )
    # One applied response per stable command identity; indeterminate and
    # stale rows may repeat the key.
    op.create_index(
        "facilitytransactions_command_id_applied_key",
        "facilitytransactions",
        ["command_id"],
        unique=True,
        postgresql_where=sa.text("disposition = 'applied'"),
    )

    op.add_column(
        "facilitytransactionrequests",
        sa.Column("followup_request_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "facilitytransactionrequests",
        sa.Column("allocation_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "facilitytransactionrequests",
        sa.Column("obj_id", sa.String(), nullable=True),
    )
    op.add_column(
        "facilitytransactionrequests",
        sa.Column("action", sa.String(), nullable=True),
    )
    op.add_column(
        "facilitytransactionrequests",
        sa.Column("command_id", sa.String(), nullable=True),
    )
    op.add_column(
        "facilitytransactionrequests",
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "facilitytransactionrequests",
        sa.Column("claimed_by", sa.String(), nullable=True),
    )
    op.create_index(
        op.f("ix_facilitytransactionrequests_followup_request_version"),
        "facilitytransactionrequests",
        ["followup_request_version"],
        unique=False,
    )
    op.create_index(
        op.f("ix_facilitytransactionrequests_claimed_at"),
        "facilitytransactionrequests",
        ["claimed_at"],
        unique=False,
    )
    op.create_unique_constraint(
        "facilitytransactionrequests_command_id_key",
        "facilitytransactionrequests",
        ["command_id"],
    )


def downgrade():
    op.drop_constraint(
        "facilitytransactionrequests_command_id_key",
        "facilitytransactionrequests",
    )
    op.drop_index(
        op.f("ix_facilitytransactionrequests_claimed_at"),
        table_name="facilitytransactionrequests",
    )
    op.drop_index(
        op.f("ix_facilitytransactionrequests_followup_request_version"),
        table_name="facilitytransactionrequests",
    )
    for column in [
        "claimed_by",
        "claimed_at",
        "command_id",
        "action",
        "obj_id",
        "allocation_id",
        "followup_request_version",
    ]:
        op.drop_column("facilitytransactionrequests", column)

    op.drop_index(
        "facilitytransactions_command_id_applied_key",
        table_name="facilitytransactions",
    )
    op.drop_index(
        op.f("ix_facilitytransactions_disposition"),
        table_name="facilitytransactions",
    )
    op.drop_index(
        op.f("ix_facilitytransactions_action"),
        table_name="facilitytransactions",
    )
    op.drop_index(
        op.f("ix_facilitytransactions_followup_request_version"),
        table_name="facilitytransactions",
    )
    for column in [
        "disposition",
        "command_id",
        "action",
        "obj_id",
        "allocation_id",
        "followup_request_version",
    ]:
        op.drop_column("facilitytransactions", column)

    op.drop_column("followuprequests", "version")
