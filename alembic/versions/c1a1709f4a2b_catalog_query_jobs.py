"""catalog query jobs, staging and attempts

Revision ID: c1a1709f4a2b
Revises: 28442e0a1830
Create Date: 2026-10-13 00:00:00.000000

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "c1a1709f4a2b"
down_revision = "28442e0a1830"
branch_labels = None
depends_on = None


ACTIVE_QUERY_STATUSES = (
    "pending",
    "running",
    "publishing",
)


def upgrade():
    op.add_column(
        "catalog_queries",
        sa.Column("query_uid", sa.String(), nullable=True),
    )
    op.execute(
        "UPDATE catalog_queries SET query_uid = gen_random_uuid()::text "
        "WHERE query_uid IS NULL"
    )
    op.alter_column("catalog_queries", "query_uid", nullable=False)
    op.create_unique_constraint(
        "catalog_queries_query_uid_key", "catalog_queries", ["query_uid"]
    )

    op.add_column(
        "catalog_queries", sa.Column("ra", sa.Float(), nullable=True)
    )
    op.add_column(
        "catalog_queries", sa.Column("dec", sa.Float(), nullable=True)
    )
    op.add_column(
        "catalog_queries", sa.Column("radius", sa.Float(), nullable=True)
    )
    op.add_column(
        "catalog_queries",
        sa.Column(
            "publish_results",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "catalog_queries",
        sa.Column(
            "permission_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "catalog_queries",
        sa.Column("request_key", sa.String(), nullable=True),
    )
    op.add_column(
        "catalog_queries",
        sa.Column(
            "cancel_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "catalog_queries",
        sa.Column("terminal_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        op.f("ix_catalog_queries_terminal_at"),
        "catalog_queries",
        ["terminal_at"],
        unique=False,
    )
    op.add_column(
        "catalog_queries",
        sa.Column(
            "notified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_catalog_queries_active_request_key",
        "catalog_queries",
        ["request_key"],
        unique=True,
        postgresql_where=sa.text(
            "request_key IS NOT NULL AND status IN "
            f"({', '.join(repr(s) for s in ACTIVE_QUERY_STATUSES)})"
        ),
    )

    op.create_table(
        "catalog_query_subjobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified", sa.DateTime(), nullable=False),
        sa.Column("query_id", sa.Integer(), nullable=False),
        sa.Column("catalog_name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("request_key", sa.String(), nullable=False),
        sa.Column("claimed_by", sa.String(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("last_heartbeat", sa.DateTime(), nullable=True),
        sa.Column("lease_expires", sa.DateTime(), nullable=True),
        sa.Column(
            "attempts_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "pagination",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "result_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["query_id"], ["catalog_queries.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "query_id",
            "catalog_name",
            name="catalog_query_subjobs_query_catalog_uc",
        ),
    )
    op.create_index(
        op.f("ix_catalog_query_subjobs_query_id"),
        "catalog_query_subjobs",
        ["query_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_catalog_query_subjobs_status"),
        "catalog_query_subjobs",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_catalog_query_subjobs_lease_expires"),
        "catalog_query_subjobs",
        ["lease_expires"],
        unique=False,
    )
    op.create_index(
        op.f("ix_catalog_query_subjobs_created_at"),
        "catalog_query_subjobs",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "catalog_query_attempts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified", sa.DateTime(), nullable=False),
        sa.Column("subjob_id", sa.Integer(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column(
            "remote_handle",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "request_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "response_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["subjob_id"],
            ["catalog_query_subjobs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subjob_id",
            "attempt_no",
            name="catalog_query_attempts_subjob_no_uc",
        ),
    )
    op.create_index(
        op.f("ix_catalog_query_attempts_subjob_id"),
        "catalog_query_attempts",
        ["subjob_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_catalog_query_attempts_state"),
        "catalog_query_attempts",
        ["state"],
        unique=False,
    )
    op.create_index(
        op.f("ix_catalog_query_attempts_created_at"),
        "catalog_query_attempts",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "catalog_query_candidates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified", sa.DateTime(), nullable=False),
        sa.Column("query_id", sa.Integer(), nullable=False),
        sa.Column("subjob_id", sa.Integer(), nullable=False),
        sa.Column("catalog_name", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("ra", sa.Float(), nullable=True),
        sa.Column("dec", sa.Float(), nullable=True),
        sa.Column(
            "raw_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("validation_status", sa.String(), nullable=False),
        sa.Column("validation_error", sa.String(), nullable=True),
        sa.Column("resolution", sa.String(), nullable=True),
        sa.Column("resolved_obj_id", sa.String(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["query_id"], ["catalog_queries.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["subjob_id"],
            ["catalog_query_subjobs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_obj_id"], ["objs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subjob_id",
            "external_id",
            name="catalog_query_candidates_subjob_external_uc",
        ),
    )
    op.create_index(
        op.f("ix_catalog_query_candidates_query_id"),
        "catalog_query_candidates",
        ["query_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_catalog_query_candidates_subjob_id"),
        "catalog_query_candidates",
        ["subjob_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_catalog_query_candidates_created_at"),
        "catalog_query_candidates",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "catalog_query_candidate_data",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified", sa.DateTime(), nullable=False),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("validation_status", sa.String(), nullable=False),
        sa.Column("validation_error", sa.String(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["catalog_query_candidates.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate_id",
            "kind",
            name="catalog_query_candidate_data_candidate_uc",
        ),
    )
    op.create_index(
        op.f("ix_catalog_query_candidate_data_candidate_id"),
        "catalog_query_candidate_data",
        ["candidate_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_catalog_query_candidate_data_created_at"),
        "catalog_query_candidate_data",
        ["created_at"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        op.f("ix_catalog_query_candidate_data_created_at"),
        table_name="catalog_query_candidate_data",
    )
    op.drop_index(
        op.f("ix_catalog_query_candidate_data_candidate_id"),
        table_name="catalog_query_candidate_data",
    )
    op.drop_table("catalog_query_candidate_data")

    op.drop_index(
        op.f("ix_catalog_query_candidates_created_at"),
        table_name="catalog_query_candidates",
    )
    op.drop_index(
        op.f("ix_catalog_query_candidates_subjob_id"),
        table_name="catalog_query_candidates",
    )
    op.drop_index(
        op.f("ix_catalog_query_candidates_query_id"),
        table_name="catalog_query_candidates",
    )
    op.drop_table("catalog_query_candidates")

    op.drop_index(
        op.f("ix_catalog_query_attempts_created_at"),
        table_name="catalog_query_attempts",
    )
    op.drop_index(
        op.f("ix_catalog_query_attempts_state"),
        table_name="catalog_query_attempts",
    )
    op.drop_index(
        op.f("ix_catalog_query_attempts_subjob_id"),
        table_name="catalog_query_attempts",
    )
    op.drop_table("catalog_query_attempts")

    op.drop_index(
        op.f("ix_catalog_query_subjobs_created_at"),
        table_name="catalog_query_subjobs",
    )
    op.drop_index(
        op.f("ix_catalog_query_subjobs_lease_expires"),
        table_name="catalog_query_subjobs",
    )
    op.drop_index(
        op.f("ix_catalog_query_subjobs_status"),
        table_name="catalog_query_subjobs",
    )
    op.drop_index(
        op.f("ix_catalog_query_subjobs_query_id"),
        table_name="catalog_query_subjobs",
    )
    op.drop_table("catalog_query_subjobs")

    op.drop_index(
        "ix_catalog_queries_active_request_key", table_name="catalog_queries"
    )
    op.drop_column("catalog_queries", "notified")
    op.drop_index(
        op.f("ix_catalog_queries_terminal_at"), table_name="catalog_queries"
    )
    op.drop_column("catalog_queries", "terminal_at")
    op.drop_column("catalog_queries", "cancel_requested")
    op.drop_column("catalog_queries", "request_key")
    op.drop_column("catalog_queries", "permission_snapshot")
    op.drop_column("catalog_queries", "publish_results")
    op.drop_column("catalog_queries", "radius")
    op.drop_column("catalog_queries", "dec")
    op.drop_column("catalog_queries", "ra")
    op.drop_constraint(
        "catalog_queries_query_uid_key", "catalog_queries", type_="unique"
    )
    op.drop_column("catalog_queries", "query_uid")
