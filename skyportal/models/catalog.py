__all__ = [
    "CatalogQuery",
    "CatalogQuerySubjob",
    "CatalogQueryAttempt",
    "CatalogQueryCandidate",
    "CatalogQueryCandidateData",
]

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as psql
from sqlalchemy.orm import relationship

from baselayer.app.models import (
    AccessibleIfRelatedRowsAreAccessible,
    AccessibleIfUserMatches,
    Base,
    CustomUserAccessControl,
    join_model,
)

from .followup_request import updatable_by_token_with_listener_acl
from .group import Group


class CatalogQuery(Base):
    """A request to one or more external catalogs."""

    __tablename__ = "catalog_queries"

    # TODO: Make read-accessible via target groups
    create = read = AccessibleIfRelatedRowsAreAccessible(allocation="read")
    update = delete = (
        (
            AccessibleIfUserMatches("allocation.group.users")
            | AccessibleIfUserMatches("requester")
        )
        & read
    ) | CustomUserAccessControl(updatable_by_token_with_listener_acl)

    requester_id = sa.Column(
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="ID of the User who requested the catalog query.",
    )

    requester = relationship(
        "User",
        back_populates="catalog_queries",
        doc="The User who requested the catalog query.",
        foreign_keys=[requester_id],
    )

    query_uid = sa.Column(
        sa.String,
        unique=True,
        nullable=False,
        default=lambda: str(uuid.uuid4()),
        doc="Stable unique identifier of this catalog query, assigned at accept time.",
    )

    payload = sa.Column(
        psql.JSONB,
        nullable=False,
        doc="Content of the catalog query.",
    )

    status = sa.Column(
        sa.String(),
        nullable=False,
        default="pending",
        index=True,
        doc="The status of the query.",
    )

    ra = sa.Column(
        sa.Float,
        nullable=True,
        doc="Cone-search right ascension in degrees, when given explicitly.",
    )
    dec = sa.Column(
        sa.Float,
        nullable=True,
        doc="Cone-search declination in degrees, when given explicitly.",
    )
    radius = sa.Column(
        sa.Float,
        nullable=True,
        doc="Cone-search radius in degrees, when given explicitly.",
    )

    publish_results = sa.Column(
        sa.Boolean,
        nullable=False,
        default=True,
        server_default=sa.text("true"),
        doc=(
            "Whether validated candidates are published as visible Objs and "
            "photometry. False keeps results in the query-private staging area."
        ),
    )

    permission_snapshot = sa.Column(
        psql.JSONB,
        nullable=False,
        default=dict,
        server_default=sa.text("'{}'::jsonb"),
        doc="Operator permission snapshot taken when the query was accepted.",
    )

    request_key = sa.Column(
        sa.String(),
        nullable=True,
        doc=(
            "Stable hash of the normalized request; identical active requests "
            "share one query via a partial unique index."
        ),
    )

    cancel_requested = sa.Column(
        sa.Boolean,
        nullable=False,
        default=False,
        server_default=sa.text("false"),
        doc="Set when a cancellation has been requested but not yet finalized.",
    )

    terminal_at = sa.Column(
        sa.DateTime,
        nullable=True,
        index=True,
        doc="UTC time the query entered its final terminal state, if any.",
    )

    notified = sa.Column(
        sa.Boolean,
        nullable=False,
        default=False,
        server_default=sa.text("false"),
        doc="Whether the single terminal notification has already been sent.",
    )

    allocation_id = sa.Column(
        sa.ForeignKey("allocations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    allocation = relationship("Allocation", back_populates="catalog_queries")

    # One active query per normalized request key; terminal queries may repeat.
    __table_args__ = (
        sa.Index(
            "ix_catalog_queries_active_request_key",
            "request_key",
            unique=True,
            postgresql_where=sa.text(
                "request_key IS NOT NULL AND status IN "
                "('pending', 'running', 'publishing')"
            ),
        ),
    )

    target_groups = relationship(
        "Group",
        secondary="catalog_queries_groups",
        passive_deletes=True,
        doc="Groups to share the resulting data from this catalog query with.",
        overlaps="groups",
    )

    subjobs = relationship(
        "CatalogQuerySubjob",
        cascade="save-update, merge, refresh-expire, expunge, delete",
        passive_deletes=True,
        order_by="CatalogQuerySubjob.id",
        doc="One catalog fetch sub-request per selected catalog.",
    )

    candidates = relationship(
        "CatalogQueryCandidate",
        cascade="save-update, merge, refresh-expire, expunge, delete",
        passive_deletes=True,
        order_by="CatalogQueryCandidate.id",
        doc="Staged candidate sources belonging to this query.",
    )


class CatalogQuerySubjob(Base):
    """A single catalog fetch within a CatalogQuery."""

    __tablename__ = "catalog_query_subjobs"

    create = read = update = delete = AccessibleIfRelatedRowsAreAccessible(
        catalog_query="read"
    )

    query_id = sa.Column(
        sa.ForeignKey("catalog_queries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="ID of the owning CatalogQuery.",
    )
    catalog_query = relationship(
        "CatalogQuery",
        foreign_keys=[query_id],
        back_populates="subjobs",
        doc="The CatalogQuery this sub-request belongs to.",
    )

    catalog_name = sa.Column(
        sa.String(),
        nullable=False,
        doc="Name of the external catalog (ZTF-Fink, LSXPS, Gaia, TESS).",
    )

    status = sa.Column(
        sa.String(),
        nullable=False,
        default="pending",
        index=True,
        doc="Sub-request state: pending, claimed, succeeded, failed, cancelled.",
    )

    request_key = sa.Column(
        sa.String(),
        nullable=False,
        doc="Stable idempotency key for this catalog request within the query.",
    )

    claimed_by = sa.Column(
        sa.String(),
        nullable=True,
        doc="Identifier of the worker currently holding the claim.",
    )
    claimed_at = sa.Column(
        sa.DateTime, nullable=True, doc="UTC time of the latest claim."
    )
    last_heartbeat = sa.Column(
        sa.DateTime, nullable=True, doc="UTC time of the latest worker heartbeat."
    )
    lease_expires = sa.Column(
        sa.DateTime,
        nullable=True,
        index=True,
        doc="UTC time the current claim expires and becomes reclaimable.",
    )

    attempts_count = sa.Column(
        sa.Integer,
        nullable=False,
        default=0,
        server_default=sa.text("0"),
        doc="Number of remote attempts started for this sub-request.",
    )

    pagination = sa.Column(
        psql.JSONB,
        nullable=True,
        doc="Resumable pagination/fetch-progress marker.",
    )

    result_summary = sa.Column(
        psql.JSONB,
        nullable=True,
        doc="Counts, fetched ids and publish outcome summary.",
    )

    error = sa.Column(
        sa.String(),
        nullable=True,
        doc="Persisted failure reason for the last terminal failure.",
    )

    confirmed_at = sa.Column(
        sa.DateTime,
        nullable=True,
        doc="UTC time the sub-request reached a confirmed terminal state.",
    )

    attempts = relationship(
        "CatalogQueryAttempt",
        cascade="save-update, merge, refresh-expire, expunge, delete",
        passive_deletes=True,
        order_by="CatalogQueryAttempt.attempt_no",
        doc="Remote attempts made for this sub-request.",
    )

    candidates = relationship(
        "CatalogQueryCandidate",
        cascade="save-update, merge, refresh-expire, expunge, delete",
        passive_deletes=True,
        order_by="CatalogQueryCandidate.id",
        doc="Staged candidates fetched by this sub-request.",
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "query_id", "catalog_name", name="catalog_query_subjobs_query_catalog_uc"
        ),
    )


class CatalogQueryAttempt(Base):
    """One remote attempt against a catalog, including uncertain probes."""

    __tablename__ = "catalog_query_attempts"

    create = read = update = delete = AccessibleIfRelatedRowsAreAccessible(
        subjob="read"
    )

    subjob_id = sa.Column(
        sa.ForeignKey("catalog_query_subjobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="ID of the owning CatalogQuerySubjob.",
    )
    subjob = relationship(
        "CatalogQuerySubjob",
        foreign_keys=[subjob_id],
        back_populates="attempts",
        doc="The sub-request this attempt belongs to.",
    )

    attempt_no = sa.Column(
        sa.Integer,
        nullable=False,
        doc="Sequence number of this attempt within its sub-request.",
    )

    state = sa.Column(
        sa.String(),
        nullable=False,
        default="in_flight",
        index=True,
        doc="Attempt state: in_flight, uncertain, succeeded, failed.",
    )

    remote_handle = sa.Column(
        psql.JSONB,
        nullable=True,
        doc="Opaque remote job/query handle used to reconcile uncertain attempts.",
    )
    request_summary = sa.Column(
        psql.JSONB, nullable=True, doc="Summary of the outbound request."
    )
    response_summary = sa.Column(
        psql.JSONB, nullable=True, doc="Summary of the remote response."
    )
    error = sa.Column(
        sa.String(), nullable=True, doc="Error text for failed/uncertain attempts."
    )

    started_at = sa.Column(sa.DateTime, nullable=True, doc="Attempt start UTC time.")
    finished_at = sa.Column(
        sa.DateTime, nullable=True, doc="Attempt finish UTC time, if finished."
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "subjob_id", "attempt_no", name="catalog_query_attempts_subjob_no_uc"
        ),
    )


class CatalogQueryCandidate(Base):
    """A candidate source staged for a CatalogQuery, pre-publication."""

    __tablename__ = "catalog_query_candidates"

    create = read = update = delete = AccessibleIfRelatedRowsAreAccessible(
        catalog_query="read"
    )

    query_id = sa.Column(
        sa.ForeignKey("catalog_queries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="ID of the owning CatalogQuery.",
    )
    catalog_query = relationship(
        "CatalogQuery",
        foreign_keys=[query_id],
        back_populates="candidates",
        doc="The CatalogQuery this candidate belongs to.",
    )

    subjob_id = sa.Column(
        sa.ForeignKey("catalog_query_subjobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="ID of the sub-request that produced this candidate.",
    )
    subjob = relationship(
        "CatalogQuerySubjob",
        foreign_keys=[subjob_id],
        back_populates="candidates",
        doc="The sub-request that produced this candidate.",
    )

    catalog_name = sa.Column(
        sa.String(), nullable=False, doc="Catalog this candidate came from."
    )
    external_id = sa.Column(
        sa.String(),
        nullable=False,
        doc="Object identifier as assigned by the external catalog.",
    )
    ra = sa.Column(sa.Float, nullable=True, doc="Candidate right ascension, degrees.")
    dec = sa.Column(sa.Float, nullable=True, doc="Candidate declination, degrees.")

    raw_data = sa.Column(
        psql.JSONB, nullable=False, doc="Full parsed candidate payload."
    )

    validation_status = sa.Column(
        sa.String(),
        nullable=False,
        default="pending",
        doc="Validation outcome: pending, valid, invalid.",
    )
    validation_error = sa.Column(
        sa.String(), nullable=True, doc="Why the candidate failed validation."
    )

    resolution = sa.Column(
        sa.String(),
        nullable=True,
        doc="Association outcome at publish time: new, existing, rejected.",
    )
    resolved_obj_id = sa.Column(
        sa.ForeignKey("objs.id", ondelete="SET NULL"),
        nullable=True,
        doc="Obj this candidate was resolved to, once published.",
    )
    published_at = sa.Column(
        sa.DateTime,
        nullable=True,
        doc="UTC time the candidate and its data were published.",
    )

    data_rows = relationship(
        "CatalogQueryCandidateData",
        cascade="save-update, merge, refresh-expire, expunge, delete",
        passive_deletes=True,
        order_by="CatalogQueryCandidateData.id",
        doc="Staged photometry/series payloads for this candidate.",
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "subjob_id",
            "external_id",
            name="catalog_query_candidates_subjob_external_uc",
        ),
    )


class CatalogQueryCandidateData(Base):
    """Staged photometry or photometric-series payload for one candidate."""

    __tablename__ = "catalog_query_candidate_data"

    create = read = update = delete = AccessibleIfRelatedRowsAreAccessible(
        candidate="read"
    )

    candidate_id = sa.Column(
        sa.ForeignKey("catalog_query_candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="ID of the owning staged candidate.",
    )
    candidate = relationship(
        "CatalogQueryCandidate",
        foreign_keys=[candidate_id],
        back_populates="data_rows",
        doc="The candidate this data payload belongs to.",
    )

    kind = sa.Column(
        sa.String(),
        nullable=False,
        doc="Payload kind: photometry or photometric_series.",
    )
    payload = sa.Column(
        psql.JSONB,
        nullable=False,
        doc="JSON-serialized payload shaped for the existing publish entrypoints.",
    )

    validation_status = sa.Column(
        sa.String(),
        nullable=False,
        default="pending",
        doc="Validation outcome: pending, valid, invalid.",
    )
    validation_error = sa.Column(
        sa.String(), nullable=True, doc="Why the data payload failed validation."
    )

    published_at = sa.Column(
        sa.DateTime, nullable=True, doc="UTC time the payload was published."
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "candidate_id", "kind", name="catalog_query_candidate_data_candidate_uc"
        ),
    )


CatalogQueryTargetGroup = join_model(
    "catalog_queries_groups",
    CatalogQuery,
    Group,
    new_name="CatalogQueryTargetGroup",
    overlaps="target_groups",
)
CatalogQueryTargetGroup.create = CatalogQueryTargetGroup.update = (
    CatalogQueryTargetGroup.delete
) = (
    AccessibleIfUserMatches("defaultobservationplanrequest.requester")
    & CatalogQueryTargetGroup.read
)
