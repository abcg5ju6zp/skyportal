__all__ = [
    "CLAIM_LEASE_SECONDS",
    "COMMAND_DELETE",
    "COMMAND_GET",
    "COMMAND_MESSAGE",
    "COMMAND_RETRIEVE",
    "COMMAND_SUBMIT",
    "COMMAND_UPDATE",
    "TRANSACTION_APPLIED",
    "TRANSACTION_INDETERMINATE",
    "TRANSACTION_REQUEST_CLAIMED",
    "TRANSACTION_REQUEST_COMPLETE",
    "TRANSACTION_REQUEST_PENDING",
    "TRANSACTION_REQUEST_STALE",
    "TRANSACTION_STALE",
    "FacilityTransaction",
    "FacilityTransactionRequest",
    "apply_request_status",
    "claim_facility_transaction_request",
    "request_version_matches",
    "stable_command_id",
]


import uuid
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as psql
from sqlalchemy.orm import relationship

from baselayer.app.models import Base

from ..utils.naive_datetime import utcnow_naive

# Outbound/inbound command kinds recorded on facility transactions.
COMMAND_SUBMIT = "submit"
COMMAND_UPDATE = "update"
COMMAND_DELETE = "delete"
COMMAND_RETRIEVE = "retrieve"
COMMAND_GET = "get"
COMMAND_MESSAGE = "message"

# Lifecycle of a queued FacilityTransactionRequest.
TRANSACTION_REQUEST_PENDING = "pending submission"
TRANSACTION_REQUEST_CLAIMED = "claimed"
# The request version was superseded before the poll resolved; the queued
# command is finished (no longer polled) without becoming an error.
TRANSACTION_REQUEST_STALE = "stale"
TRANSACTION_REQUEST_COMPLETE = "complete"

# Disposition of an individual FacilityTransaction's response.
TRANSACTION_APPLIED = "applied"
TRANSACTION_STALE = "stale"
TRANSACTION_INDETERMINATE = "indeterminate"

# A claimed-but-unfinished row whose worker died may be re-claimed after
# this lease elapses.
CLAIM_LEASE_SECONDS = 300

_COMMAND_UUID_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "skyportal.io/followup-command"
)


def stable_command_id(followup_request_id, version, action):
    """Deterministic idempotency key for one command on one request version.

    Network retries of the same logical command (worker restart, duplicate
    delivery) carry the identical key, so facilities and the local ledger
    can collapse them instead of creating a second remote observation.
    """
    return str(
        uuid.uuid5(
            _COMMAND_UUID_NAMESPACE,
            f"{int(followup_request_id)}:{int(version)}:{action}",
        )
    )


def claim_facility_transaction_request(
    session, request_id, worker_id, lease_seconds=CLAIM_LEASE_SECONDS
):
    """Atomically claim a queued command for a worker (sync session).

    Claims rows that are still pending, or claimed rows whose lease has
    expired (dead/restarted worker). Returns True iff this worker won the
    claim; the caller commits. With ``skip_locked`` concurrent workers
    never block on one another's rows.
    """
    cutoff = utcnow_naive() - timedelta(seconds=lease_seconds)
    row = session.execute(
        sa.select(
            FacilityTransactionRequest.status,
            FacilityTransactionRequest.claimed_at,
        )
        .where(FacilityTransactionRequest.id == request_id)
        .with_for_update(skip_locked=True)
    ).first()
    if row is None:
        return False

    status, claimed_at = row
    if status in (
        TRANSACTION_REQUEST_COMPLETE,
        TRANSACTION_REQUEST_STALE,
    ) or str(status).startswith("error:"):
        return False
    if (
        status == TRANSACTION_REQUEST_CLAIMED
        and claimed_at is not None
        and claimed_at > cutoff
    ):
        return False

    result = session.execute(
        sa.update(FacilityTransactionRequest)
        .where(FacilityTransactionRequest.id == request_id)
        .values(
            status=TRANSACTION_REQUEST_CLAIMED,
            claimed_at=utcnow_naive(),
            claimed_by=str(worker_id),
        )
    )
    return result.rowcount == 1


def _request_version_rows(session, request_id):
    from .followup_request import FollowupRequest

    return session.execute(
        sa.select(
            FollowupRequest.version,
            FollowupRequest.allocation_id,
            FollowupRequest.obj_id,
        ).where(FollowupRequest.id == request_id)
    ).first()


async def _request_version_rows_async(session, request_id):
    from .followup_request import FollowupRequest

    return (
        await session.execute(
            sa.select(
                FollowupRequest.version,
                FollowupRequest.allocation_id,
                FollowupRequest.obj_id,
            ).where(FollowupRequest.id == request_id)
        )
    ).first()


def request_version_matches(
    session, request_id, version, allocation_id=None, obj_id=None
):
    """Whether ``version`` (and optional snapshots) still name the current
    request version."""
    row = _request_version_rows(session, request_id)
    return (
        row is not None
        and row.version == version
        and (allocation_id is None or row.allocation_id == allocation_id)
        and (obj_id is None or row.obj_id == obj_id)
    )


async def request_version_matches_async(
    session, request_id, version, allocation_id=None, obj_id=None
):
    row = await _request_version_rows_async(session, request_id)
    return (
        row is not None
        and row.version == version
        and (allocation_id is None or row.allocation_id == allocation_id)
        and (obj_id is None or row.obj_id == obj_id)
    )


def apply_request_status(
    session, request_id, version, status, only_if_changed=False
):
    """Conditionally set the request status.

    The UPDATE matches only while the request is still at ``version``; a
    late response for a superseded version therefore cannot overwrite a
    newer state. With ``only_if_changed`` it additionally requires the
    status to actually differ, which makes the return value the
    once-gate for follow-up notifications/Flow pushes.
    """
    from .followup_request import FollowupRequest

    conditions = [
        FollowupRequest.id == request_id,
        FollowupRequest.version == version,
    ]
    if only_if_changed:
        conditions.append(FollowupRequest.status != status)
    result = session.execute(
        sa.update(FollowupRequest).where(*conditions).values(status=status)
    )
    return result.rowcount == 1


async def apply_request_status_async(
    session, request_id, version, status, only_if_changed=False
):
    from .followup_request import FollowupRequest

    conditions = [
        FollowupRequest.id == request_id,
        FollowupRequest.version == version,
    ]
    if only_if_changed:
        conditions.append(FollowupRequest.status != status)
    result = await session.execute(
        sa.update(FollowupRequest).where(*conditions).values(status=status)
    )
    return result.rowcount == 1


class FacilityTransaction(Base):
    created_at = sa.Column(
        sa.DateTime,
        nullable=False,
        default=utcnow_naive,
        index=True,
        doc="UTC time this FacilityTransaction was created.",
    )

    request = sa.Column(psql.JSONB, doc="Serialized HTTP request.")
    response = sa.Column(psql.JSONB, doc="Serialized HTTP response.")

    followup_request_id = sa.Column(
        sa.ForeignKey("followuprequests.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
        doc="The ID of the FollowupRequest this message pertains to.",
    )

    followup_request = relationship(
        "FollowupRequest",
        back_populates="transactions",
        doc="The FollowupRequest this message pertains to.",
    )

    followup_request_version = sa.Column(
        sa.Integer,
        nullable=True,
        index=True,
        doc=(
            "Version of the FollowupRequest this command was issued "
            "against. Responses for a stale version are recorded but "
            "never applied to the current request."
        ),
    )

    allocation_id = sa.Column(
        sa.Integer,
        nullable=True,
        doc="Allocation the command was fixed to at send time.",
    )

    obj_id = sa.Column(
        sa.String,
        nullable=True,
        doc="Target Obj the command was fixed to at send time.",
    )

    action = sa.Column(
        sa.String(),
        nullable=True,
        index=True,
        doc="Facility action: submit, update, delete, retrieve, get, message.",
    )

    command_id = sa.Column(
        sa.String(),
        nullable=True,
        doc=(
            "Stable idempotency key of this command, shared by network "
            "retries. See stable_command_id()."
        ),
    )

    disposition = sa.Column(
        sa.String(),
        nullable=True,
        index=True,
        doc=(
            "Whether the response was 'applied' to the request, recorded "
            "as 'stale' (command version superseded), or left "
            "'indeterminate' (local/partial failure pending reconciliation)."
        ),
    )

    observation_plan_request_id = sa.Column(
        sa.ForeignKey("observationplanrequests.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
        doc="The ID of the ObservationPlanRequest this message pertains to.",
    )

    observation_plan_request = relationship(
        "ObservationPlanRequest",
        back_populates="transactions",
        doc="The ObservationPlanRequest this message pertains to.",
        passive_deletes=True,
    )

    initiator_id = sa.Column(
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
        doc="The ID of the User who initiated the transaction.",
    )
    initiator = relationship(
        "User",
        back_populates="transactions",
        doc="The User who initiated the transaction.",
        foreign_keys="FacilityTransaction.initiator_id",
    )

    __table_args__ = (
        # One applied response per stable command id: a retried command may
        # leave several indeterminate rows, but it can be applied exactly
        # once.
        sa.Index(
            "facilitytransactions_command_id_applied_key",
            "command_id",
            unique=True,
            postgresql_where=sa.text("disposition = 'applied'"),
        ),
    )


class FacilityTransactionRequest(Base):
    created_at = sa.Column(
        sa.DateTime,
        nullable=False,
        default=utcnow_naive,
        index=True,
        doc="UTC time this FacilityTransactionRequest was created.",
    )

    last_query = sa.Column(
        sa.DateTime,
        nullable=False,
        default=utcnow_naive,
        index=True,
        doc="UTC time this FacilityTransactionRequest was last queried.",
    )

    method = sa.Column(
        sa.String,
        nullable=False,
        doc="HTTP method. Options include GET, POST, PUT, DELETE, etc.",
    )

    endpoint = sa.Column(
        sa.String,
        nullable=False,
        doc="Endpoint for the transaction request",
    )

    data = sa.Column(psql.JSONB, doc="Data to post.")
    params = sa.Column(psql.JSONB, doc="URL parameters, in get.")
    headers = sa.Column(psql.JSONB, doc="Request headers.")

    followup_request_id = sa.Column(
        sa.ForeignKey("followuprequests.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
        doc="The ID of the FollowupRequest this message pertains to.",
    )

    followup_request = relationship(
        "FollowupRequest",
        back_populates="transaction_requests",
        doc="The FollowupRequest this message pertains to.",
    )

    followup_request_version = sa.Column(
        sa.Integer,
        nullable=True,
        index=True,
        doc="FollowupRequest version this queued command was fixed to.",
    )

    allocation_id = sa.Column(
        sa.Integer,
        nullable=True,
        doc="Allocation this queued command was fixed to at enqueue time.",
    )

    obj_id = sa.Column(
        sa.String,
        nullable=True,
        doc="Target Obj this queued command was fixed to at enqueue time.",
    )

    action = sa.Column(
        sa.String(),
        nullable=True,
        doc="Facility action this queued command performs (typically retrieve).",
    )

    command_id = sa.Column(
        sa.String(),
        nullable=True,
        doc="Stable idempotency key; identical across network retries.",
    )

    claimed_at = sa.Column(
        sa.DateTime,
        nullable=True,
        index=True,
        doc="UTC time a worker last claimed this queued command.",
    )

    claimed_by = sa.Column(
        sa.String(),
        nullable=True,
        doc="Identifier of the worker that claimed this queued command.",
    )

    observation_plan_request_id = sa.Column(
        sa.ForeignKey("observationplanrequests.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
        doc="The ID of the ObservationPlanRequest this message pertains to.",
    )

    observation_plan_request = relationship(
        "ObservationPlanRequest",
        back_populates="transaction_requests",
        doc="The ObservationPlanRequest this message pertains to.",
        passive_deletes=True,
    )

    initiator_id = sa.Column(
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
        doc="The ID of the User who initiated the transaction.",
    )
    initiator = relationship(
        "User",
        back_populates="transaction_requests",
        doc="The User who initiated the transaction.",
        foreign_keys="FacilityTransactionRequest.initiator_id",
    )

    status = sa.Column(
        sa.String(),
        nullable=False,
        default=TRANSACTION_REQUEST_PENDING,
        index=True,
        doc="The status of the request.",
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "command_id", name="facilitytransactionrequests_command_id_key"
        ),
    )
