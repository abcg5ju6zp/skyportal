"""Claim, attempt, stage and finalize external catalog query jobs."""

import math
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import timedelta

import sqlalchemy as sa
from baselayer.app.env import load_env
from baselayer.log import make_log

from skyportal.models import (
    CatalogQuery,
    CatalogQueryAttempt,
    CatalogQueryCandidate,
    CatalogQueryCandidateData,
    CatalogQuerySubjob,
    DBSession,
    new_session,
)
from skyportal.utils.naive_datetime import utcnow_naive

from .validation import validate_candidate, validate_data_payload

env, cfg = load_env()
log = make_log("catalog_query_queue")

# Query lifecycle.
QUERY_PENDING = "pending"
QUERY_RUNNING = "running"
QUERY_PUBLISHING = "publishing"
QUERY_COMPLETED = "completed"
QUERY_COMPLETED_PARTIAL = "completed_partial"
QUERY_FAILED = "failed"
QUERY_CANCELLED = "cancelled"
QUERY_ACTIVE_STATUSES = (
    QUERY_PENDING,
    QUERY_RUNNING,
    QUERY_PUBLISHING,
)
QUERY_TERMINAL_STATUSES = (
    QUERY_COMPLETED,
    QUERY_COMPLETED_PARTIAL,
    QUERY_FAILED,
    QUERY_CANCELLED,
)

# Sub-request lifecycle.
SUBJOB_PENDING = "pending"
SUBJOB_CLAIMED = "claimed"
SUBJOB_SUCCEEDED = "succeeded"
SUBJOB_FAILED = "failed"
SUBJOB_CANCELLED = "cancelled"
SUBJOB_ACTIVE_STATUSES = (SUBJOB_PENDING, SUBJOB_CLAIMED)
SUBJOB_TERMINAL_STATUSES = (
    SUBJOB_SUCCEEDED,
    SUBJOB_FAILED,
    SUBJOB_CANCELLED,
)

# Attempt lifecycle.
ATTEMPT_IN_FLIGHT = "in_flight"
ATTEMPT_UNCERTAIN = "uncertain"
ATTEMPT_SUCCEEDED = "succeeded"
ATTEMPT_FAILED = "failed"

STATEMENT_TIMEOUT = "120s"

# pg_advisory_lock namespace ("CQPU") serializing one query's publish phase.
PUBLISH_LOCK_NAMESPACE = 0x43515055

DEFAULT_CONFIG = {
    "poll_interval": 10,
    "claim_limit": 4,
    "lease_seconds": 300,
    "max_attempts": 3,
}


class UncertainResponse(Exception):
    """Remote outcome unknown (timeout, dropped connection, 5xx); reconcile."""


class AdapterDataError(Exception):
    """Deterministic catalog/data error; retrying cannot help."""


class PublishError(Exception):
    """Staged-result publication failed. fatal=True makes the query terminal."""

    def __init__(self, message, *, fatal=True):
        super().__init__(message)
        self.fatal = fatal


def queue_config():
    return {**DEFAULT_CONFIG, **((cfg.get("app.catalog_query_queue") or {}))}


def new_worker_id():
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def set_statement_timeout(session):
    session.execute(sa.text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))


def _json_safe(value):
    """Coerce numpy/pandas scalars and NaNs into JSONB-compatible values."""
    try:
        import numpy as np
    except ImportError:  # pragma: no cover
        np = None

    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if np is not None:
        if isinstance(value, np.generic):
            value = value.item()
        if isinstance(value, np.ndarray):
            return [_json_safe(v) for v in value.tolist()]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


@dataclass
class ClaimedSubjob:
    query_id: int
    subjob_id: int
    catalog_name: str
    attempt_no: int
    worker_id: str


def claim_subjobs(session, worker_id, *, limit=None, now=None, lease_seconds=None):
    """Atomically claim due sub-requests; returns ClaimedSubjob list."""
    now = now or utcnow_naive()
    cfg = queue_config()
    limit = limit or cfg["claim_limit"]
    lease_seconds = lease_seconds or cfg["lease_seconds"]

    set_statement_timeout(session)
    rows = session.scalars(
        sa.select(CatalogQuerySubjob)
        .where(
            CatalogQuerySubjob.catalog_query.has(
                sa.and_(
                    CatalogQuery.status.in_(QUERY_ACTIVE_STATUSES),
                    CatalogQuery.terminal_at.is_(None),
                    CatalogQuery.cancel_requested.is_(False),
                )
            ),
            sa.or_(
                CatalogQuerySubjob.status == SUBJOB_PENDING,
                sa.and_(
                    CatalogQuerySubjob.status == SUBJOB_CLAIMED,
                    CatalogQuerySubjob.lease_expires < now,
                ),
            ),
        )
        .order_by(CatalogQuerySubjob.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()

    claimed = []
    touched_queries = set()
    for subjob in rows:
        subjob.attempts_count += 1
        latest_attempt = (
            session.scalars(
                sa.select(CatalogQueryAttempt)
                .where(CatalogQueryAttempt.subjob_id == subjob.id)
                .order_by(CatalogQueryAttempt.attempt_no.desc())
            )
        ).first()
        if latest_attempt is not None and latest_attempt.state in (
            ATTEMPT_IN_FLIGHT,
            ATTEMPT_UNCERTAIN,
        ):
            # Reconcile the original attempt instead of opening a new one.
            attempt = latest_attempt
            attempt.state = ATTEMPT_IN_FLIGHT
            attempt.error = None
            attempt.started_at = attempt.started_at or now
            attempt.finished_at = None
        else:
            attempt = CatalogQueryAttempt(
                subjob_id=subjob.id,
                attempt_no=subjob.attempts_count,
                state=ATTEMPT_IN_FLIGHT,
                started_at=now,
                request_summary={"catalog_name": subjob.catalog_name},
            )
            session.add(attempt)

        subjob.status = SUBJOB_CLAIMED
        subjob.claimed_by = worker_id
        subjob.claimed_at = now
        subjob.last_heartbeat = now
        subjob.lease_expires = now + timedelta(seconds=lease_seconds)
        subjob.error = None
        touched_queries.add(subjob.query_id)
        claimed.append(
            ClaimedSubjob(
                query_id=subjob.query_id,
                subjob_id=subjob.id,
                catalog_name=subjob.catalog_name,
                attempt_no=attempt.attempt_no,
                worker_id=worker_id,
            )
        )

    for query_id in touched_queries:
        query = session.get(CatalogQuery, query_id)
        if query is not None and query.status == QUERY_PENDING:
            query.status = QUERY_RUNNING

    session.commit()
    return claimed


def heartbeat(
    session,
    subjob_id,
    worker_id,
    *,
    pagination=None,
    response_summary=None,
    now=None,
):
    """Advance the lease and persist fetch progress from inside an adapter."""
    now = now or utcnow_naive()
    cfg = queue_config()
    set_statement_timeout(session)
    subjob = session.get(CatalogQuerySubjob, subjob_id)
    if subjob is None or subjob.claimed_by != worker_id:
        return False
    if pagination is not None:
        subjob.pagination = _json_safe(pagination)
    subjob.last_heartbeat = now
    subjob.lease_expires = now + timedelta(seconds=cfg["lease_seconds"])
    if response_summary is not None:
        attempt = (
            session.scalars(
                sa.select(CatalogQueryAttempt)
                .where(
                    CatalogQueryAttempt.subjob_id == subjob_id,
                    CatalogQueryAttempt.state == ATTEMPT_IN_FLIGHT,
                )
                .order_by(CatalogQueryAttempt.attempt_no.desc())
            )
        ).first()
        if attempt is not None:
            attempt.response_summary = _json_safe(response_summary)
    session.commit()
    return True


def stage_candidate(
    session,
    *,
    subjob_id,
    worker_id,
    external_id,
    ra=None,
    dec=None,
    raw_data=None,
    photometry=None,
    photometric_series=None,
):
    """Upsert one validated candidate into this query's private staging area.

    Returns the staged candidate, or None when the write is a late/rejected
    response (sub-request no longer owned by this worker, or query cancelled).
    """
    set_statement_timeout(session)
    subjob = session.get(CatalogQuerySubjob, subjob_id)
    if subjob is None or subjob.claimed_by != worker_id:
        log(f"Dropping staged candidate {external_id}: subjob not owned by worker")
        return None
    query = subjob.catalog_query
    if (
        query.terminal_at is not None
        or query.cancel_requested
        or query.status not in QUERY_ACTIVE_STATUSES
        or subjob.status != SUBJOB_CLAIMED
    ):
        log(
            f"Dropping staged candidate {external_id}: query {query.id} no "
            "longer accepting responses"
        )
        return None

    external_id = str(external_id)
    status, error = validate_candidate(external_id, ra, dec)

    candidate = session.scalars(
        sa.select(CatalogQueryCandidate).where(
            CatalogQueryCandidate.subjob_id == subjob_id,
            CatalogQueryCandidate.external_id == external_id,
        )
    ).first()
    if candidate is not None and candidate.published_at is not None:
        # Published rows are immutable to late re-fetches.
        return candidate
    if candidate is None:
        candidate = CatalogQueryCandidate(
            query_id=query.id,
            subjob_id=subjob_id,
            catalog_name=subjob.catalog_name,
            external_id=external_id,
        )
        session.add(candidate)
    candidate.ra = ra
    candidate.dec = dec
    candidate.raw_data = _json_safe(raw_data if raw_data is not None else {})
    candidate.validation_status = status
    candidate.validation_error = error

    for kind, payload in (
        ("photometry", photometry),
        ("photometric_series", photometric_series),
    ):
        if payload is None:
            continue
        payload = _json_safe(payload)
        data_status, data_error = validate_data_payload(kind, payload)
        data_row = session.scalars(
            sa.select(CatalogQueryCandidateData).where(
                CatalogQueryCandidateData.candidate_id == candidate.id,
                CatalogQueryCandidateData.kind == kind,
            )
        ).first() if candidate.id else None
        if data_row is None:
            # Flush to obtain candidate.id before attaching the data row.
            session.flush()
            data_row = CatalogQueryCandidateData(
                candidate_id=candidate.id, kind=kind
            )
            session.add(data_row)
        data_row.payload = payload
        data_row.validation_status = data_status
        data_row.validation_error = data_error

    session.commit()
    return candidate


@dataclass
class AdapterContext:
    query_id: int
    query_uid: str
    subjob_id: int
    catalog_name: str
    attempt_no: int
    worker_id: str
    requester_id: int
    payload: dict
    ra: object
    dec: object
    radius: object
    publish_results: bool
    permission_snapshot: dict
    pagination: object
    session_factory: object

    def heartbeat(self, **kwargs):
        with self.session_factory() as session:
            return heartbeat(
                session, self.subjob_id, self.worker_id, **kwargs
            )

    def stage(self, **kwargs):
        with self.session_factory() as session:
            return stage_candidate(
                session,
                subjob_id=self.subjob_id,
                worker_id=self.worker_id,
                **kwargs,
            )


def _load_context(session_factory, subjob_id, worker_id):
    with session_factory() as session:
        set_statement_timeout(session)
        subjob = session.scalars(
            sa.select(CatalogQuerySubjob)
            .options(sa.orm.joinedload(CatalogQuerySubjob.catalog_query))
            .where(CatalogQuerySubjob.id == subjob_id)
        ).first()
        if subjob is None:
            return None
        query = subjob.catalog_query
        if (
            subjob.status != SUBJOB_CLAIMED
            or subjob.claimed_by != worker_id
            or query.terminal_at is not None
            or query.cancel_requested
            or query.status not in QUERY_ACTIVE_STATUSES
        ):
            return None
        attempt = (
            session.scalars(
                sa.select(CatalogQueryAttempt)
                .where(
                    CatalogQueryAttempt.subjob_id == subjob_id,
                    CatalogQueryAttempt.state == ATTEMPT_IN_FLIGHT,
                )
                .order_by(CatalogQueryAttempt.attempt_no.desc())
            )
        ).first()
        return AdapterContext(
            query_id=query.id,
            query_uid=query.query_uid,
            subjob_id=subjob.id,
            catalog_name=subjob.catalog_name,
            attempt_no=attempt.attempt_no if attempt else subjob.attempts_count,
            worker_id=worker_id,
            requester_id=query.requester_id,
            payload=dict(query.payload or {}),
            ra=query.ra,
            dec=query.dec,
            radius=query.radius,
            publish_results=query.publish_results,
            permission_snapshot=dict(query.permission_snapshot or {}),
            pagination=subjob.pagination,
            session_factory=session_factory,
        )


def _finish_attempt(session, subjob_id, state, *, now, error=None, summary=None):
    attempt = (
        session.scalars(
            sa.select(CatalogQueryAttempt)
            .where(CatalogQueryAttempt.subjob_id == subjob_id)
            .order_by(CatalogQueryAttempt.attempt_no.desc())
        )
    ).first()
    if attempt is not None:
        attempt.state = state
        attempt.finished_at = now
        if error is not None:
            attempt.error = str(error)[:4000]
        if summary is not None:
            attempt.response_summary = _json_safe(summary)


def process_subjob(session_factory, subjob_id, worker_id, adapters, config, now=None):
    """Run one claimed sub-request through its adapter and terminalize it."""
    now = now or utcnow_naive()
    ctx = _load_context(session_factory, subjob_id, worker_id)
    if ctx is None:
        return None

    adapter = adapters.get(ctx.catalog_name)
    if adapter is None:
        failure = f"no adapter registered for catalog {ctx.catalog_name}"
        with session_factory() as session:
            subjob = session.get(CatalogQuerySubjob, subjob_id)
            subjob.status = SUBJOB_FAILED
            subjob.error = failure
            subjob.confirmed_at = now
            _finish_attempt(
                session, subjob_id, ATTEMPT_FAILED, now=now, error=failure
            )
            session.commit()
        return SUBJOB_FAILED

    try:
        summary = adapter(ctx)
    except AdapterDataError as e:
        log(f"Subjob {subjob_id} data error: {e}")
        with session_factory() as session:
            subjob = session.get(CatalogQuerySubjob, subjob_id)
            subjob.status = SUBJOB_FAILED
            subjob.error = str(e)[:4000]
            subjob.confirmed_at = now
            _finish_attempt(
                session, subjob_id, ATTEMPT_FAILED, now=now, error=e
            )
            session.commit()
        return SUBJOB_FAILED
    except UncertainResponse as e:
        return _settle_uncertain(session_factory, subjob_id, e, config, now)
    except Exception as e:
        # Unknown failures may be transient; treat as uncertain until the
        # attempt budget is exhausted.
        log(f"Subjob {subjob_id} unexpected error: {e!r}")
        return _settle_uncertain(session_factory, subjob_id, e, config, now)

    with session_factory() as session:
        set_statement_timeout(session)
        subjob = session.scalars(
            sa.select(CatalogQuerySubjob)
            .options(sa.orm.joinedload(CatalogQuerySubjob.catalog_query))
            .where(CatalogQuerySubjob.id == subjob_id)
        ).first()
        if (
            subjob is not None
            and subjob.status == SUBJOB_CLAIMED
            and subjob.claimed_by == worker_id
        ):
            if (
                subjob.catalog_query.cancel_requested
                or subjob.catalog_query.terminal_at is not None
            ):
                subjob.status = SUBJOB_CANCELLED
                subjob.confirmed_at = now
                _finish_attempt(
                    session,
                    subjob_id,
                    ATTEMPT_UNCERTAIN,
                    now=now,
                    error="query cancelled before confirmation",
                )
                session.commit()
                return SUBJOB_CANCELLED
            subjob.status = SUBJOB_SUCCEEDED
            subjob.error = None
            subjob.confirmed_at = now
            subjob.result_summary = _json_safe(summary)
            _finish_attempt(
                session,
                subjob_id,
                ATTEMPT_SUCCEEDED,
                now=now,
                summary=summary,
            )
            session.commit()
            return SUBJOB_SUCCEEDED
    return None


def _settle_uncertain(session_factory, subjob_id, exc, config, now):
    with session_factory() as session:
        set_statement_timeout(session)
        subjob = session.get(CatalogQuerySubjob, subjob_id)
        if subjob is None or subjob.claimed_by is None:
            return None
        attempts_left = subjob.attempts_count < config["max_attempts"]
        if attempts_left:
            subjob.status = SUBJOB_CLAIMED
            subjob.error = str(exc)[:4000]
            _finish_attempt(
                session,
                subjob_id,
                ATTEMPT_UNCERTAIN,
                now=now,
                error=exc,
            )
            state = SUBJOB_CLAIMED
        else:
            subjob.status = SUBJOB_FAILED
            subjob.error = (
                f"exceeded {config['max_attempts']} attempts: {exc}"
            )[:4000]
            subjob.confirmed_at = now
            _finish_attempt(
                session,
                subjob_id,
                ATTEMPT_FAILED,
                now=now,
                error=subjob.error,
            )
            state = SUBJOB_FAILED
        session.commit()
    return state


def request_cancel(session, query_id, *, now=None):
    """Flag an active query for cancellation. Terminal queries are rejected."""
    now = now or utcnow_naive()
    set_statement_timeout(session)
    query = session.scalars(
        sa.select(CatalogQuery).where(CatalogQuery.id == query_id).with_for_update()
    ).first()
    if query is None or query.terminal_at is not None:
        return False
    query.cancel_requested = True
    session.commit()
    return True


def _status_text(status, subjobs, publish_summary, read_only, staged_count):
    failed = [s for s in subjobs if s.status == SUBJOB_FAILED]
    new_ids = list((publish_summary or {}).get("new_obj_ids") or [])
    existing_ids = list((publish_summary or {}).get("existing_obj_ids") or [])

    if status == QUERY_FAILED:
        reasons = "; ".join(
            f"{s.catalog_name}: {s.error or 'unknown error'}" for s in failed
        )
        return f"failed: {reasons}" if reasons else "failed"

    if not failed:
        if read_only and staged_count:
            return f"completed: {staged_count} candidate(s) staged"
        if new_ids:
            return f"completed: Added {','.join(new_ids)}"
        if existing_ids:
            return f"completed: matched {','.join(existing_ids)}"
        return "completed: No new objects"

    parts = []
    if new_ids or existing_ids:
        parts.append(f"Added {','.join(new_ids + existing_ids)}")
    if read_only and staged_count:
        parts.append(f"{staged_count} candidate(s) staged")
    for subjob in failed:
        parts.append(
            f"{subjob.catalog_name} failed: {subjob.error or 'unknown error'}"
        )
    return f"completed_partial: {'; '.join(parts)}"


def send_terminal_flows(requester_id, dateobs):
    """Push the single terminal refresh and notification fetch."""
    from baselayer.app.flow import Flow

    flow = Flow()
    if requester_id is not None:
        flow.push(
            user_id=requester_id,
            action_type="skyportal/FETCH_NOTIFICATIONS",
            payload={},
        )
    flow.push(
        user_id="*",
        action_type="skyportal/REFRESH_GCNEVENT_CATALOG_QUERIES",
        payload={"gcnEvent_dateobs": dateobs} if dateobs else {},
    )


def finalize_query(session, query_id, worker_id, *, publisher=None, now=None):
    """Aggregate sub-requests, run publish, and transition the query once.

    Returns {"became_terminal": bool, "status": str} when the query is ready
    to settle, or None when sub-requests are still in flight.
    """
    now = now or utcnow_naive()
    set_statement_timeout(session)
    query = session.scalars(
        sa.select(CatalogQuery)
        .where(CatalogQuery.id == query_id)
        .with_for_update()
    ).first()
    if query is None:
        return None
    if query.terminal_at is not None:
        return {"became_terminal": False, "status": query.status}

    subjobs = list(
        session.scalars(
            sa.select(CatalogQuerySubjob)
            .where(CatalogQuerySubjob.query_id == query_id)
            .with_for_update()
            .order_by(CatalogQuerySubjob.id)
        )
    )
    active = [s for s in subjobs if s.status in SUBJOB_ACTIVE_STATUSES]

    if query.cancel_requested:
        live = [
            s
            for s in active
            if s.status == SUBJOB_CLAIMED
            and (s.lease_expires is None or s.lease_expires > now)
        ]
        if live:
            return None
        for subjob in active:
            subjob.status = SUBJOB_CANCELLED
            subjob.confirmed_at = now
        session.commit()
        # No publish on cancellation; terminalize outside the decision lock.
        return _terminal_guarded(query_id, QUERY_CANCELLED, "cancelled", None, now)

    if active:
        return None

    succeeded = [s for s in subjobs if s.status == SUBJOB_SUCCEEDED]
    failed = [s for s in subjobs if s.status == SUBJOB_FAILED]
    if not succeeded and not failed:
        session.commit()
        return _terminal_guarded(query_id, QUERY_CANCELLED, "cancelled", None, now)

    read_only = not query.publish_results
    staged_count = session.scalar(
        sa.select(sa.func.count())
        .select_from(CatalogQueryCandidate)
        .where(CatalogQueryCandidate.query_id == query_id)
    )
    final_status = (
        QUERY_FAILED
        if not succeeded
        else QUERY_COMPLETED_PARTIAL
        if failed
        else QUERY_COMPLETED
    )

    if query.publish_results:
        if query.status != QUERY_PUBLISHING:
            # First publish-eligible pass only flips the state.
            query.status = QUERY_PUBLISHING
            session.commit()
            return None

        # Publish with all decision locks released: child rows take FOR KEY
        # SHARE on these parents, so holding FOR UPDATE across publication
        # deadlocks. Per-candidate idempotency plus the conditional terminal
        # transition make concurrent finalizers safe.
        session.commit()

        if publisher is None:
            text = _status_text(
                final_status, subjobs, None, read_only, staged_count or 0
            )
            return _terminal_guarded(query_id, final_status, text, None, now)

        # Serialize publishers across workers: without this two workers can both
        # select the same unpublished candidates (the staging select takes no
        # row lock) and double-create Objs. A session-level advisory lock also
        # releases itself if the worker dies, so the next pass re-enters and
        # per-candidate idempotency does the rest.
        acquired = session.execute(
            sa.text("SELECT pg_try_advisory_lock(:ns, :qid)"),
            {"ns": PUBLISH_LOCK_NAMESPACE, "qid": query_id},
        ).scalar()
        if not acquired:
            return None

        try:
            publish_summary = publisher(query_id)
        except PublishError as e:
            _record_publish_deferral(query_id, e)
            return None
        # Any other exception propagates; committed per-candidate work is
        # skipped on the next pass, so nothing gets duplicated.
        finally:
            try:
                session.execute(
                    sa.text("SELECT pg_advisory_unlock(:ns, :qid)"),
                    {"ns": PUBLISH_LOCK_NAMESPACE, "qid": query_id},
                )
            except Exception as e:
                log(f"Could not release publish lock for query {query_id}: {e}")

        text = _status_text(
            final_status, subjobs, publish_summary, read_only, staged_count or 0
        )
        return _terminal_guarded(
            query_id, final_status, text, publish_summary, now
        )

    text = _status_text(
        final_status, subjobs, None, read_only, staged_count or 0
    )
    session.commit()
    return _terminal_guarded(query_id, final_status, text, None, now)


def _record_publish_deferral(query_id, exc):
    with new_session() as session:
        set_statement_timeout(session)
        query = session.get(CatalogQuery, query_id)
        if query is None or query.terminal_at is not None:
            return
        first = session.scalars(
            sa.select(CatalogQuerySubjob)
            .where(CatalogQuerySubjob.query_id == query_id)
            .order_by(CatalogQuerySubjob.id)
        ).first()
        if first is not None:
            first.result_summary = {
                **(first.result_summary or {}),
                "publish_error": str(exc)[:1000],
            }
        query.status = QUERY_PUBLISHING
        session.commit()
        log(f"Query {query_id} publication deferred: {exc}")


def _terminal_guarded(query_id, status, text, publish_summary, now):
    """Move one query to a terminal state exactly once.

    The terminal transition and the notification commit in one transaction on
    an independent session, so racing finalizers (queue worker vs. cancel API)
    serialize on the row lock and exactly one of them notifies.
    """
    from skyportal.models import UserNotification

    new_status = QUERY_CANCELLED if status == QUERY_CANCELLED else text
    with new_session() as session:
        set_statement_timeout(session)
        query = session.scalars(
            sa.select(CatalogQuery)
            .where(CatalogQuery.id == query_id)
            .with_for_update()
        ).first()
        if query is None:
            session.rollback()
            return {"became_terminal": False, "status": None}
        if query.terminal_at is not None:
            current_status = query.status
            session.rollback()
            return {"became_terminal": False, "status": current_status}

        dateobs = (query.payload or {}).get("localizationDateobs")
        requester_id = query.requester_id
        query.status = new_status
        query.terminal_at = now
        query.notified = True
        notification_id = None
        if requester_id is not None:
            notification = UserNotification(
                user_id=requester_id,
                text=f"Catalog query on *{dateobs or query.query_uid}*: {new_status}",
                notification_type="gcnevent_catalog_query",
                url=f"/gcn_events/{dateobs}" if dateobs else None,
            )
            session.add(notification)
        session.commit()
        if requester_id is not None:
            notification_id = notification.id
        new_obj_ids = list((publish_summary or {}).get("new_obj_ids") or [])

    try:
        send_terminal_flows(requester_id, dateobs)
    except Exception as e:
        log(f"Terminal flow push failed for query {query_id}: {e}")

    return {
        "became_terminal": True,
        "status": new_status,
        "notification_id": notification_id,
        "new_obj_ids": new_obj_ids,
    }


def _finalize_due_queries(session_factory, worker_id, publisher, now):
    """Finalize cancelled/publishing queries even when nothing was claimed."""
    with session_factory() as session:
        set_statement_timeout(session)
        query_ids = session.scalars(
            sa.select(CatalogQuery.id)
            .where(
                CatalogQuery.terminal_at.is_(None),
                sa.or_(
                    CatalogQuery.cancel_requested.is_(True),
                    CatalogQuery.status == QUERY_PUBLISHING,
                ),
            )
            .limit(50)
        ).all()
    results = []
    for query_id in query_ids:
        with session_factory() as session:
            results.append(
                finalize_query(
                    session, query_id, worker_id, publisher=publisher, now=now
                )
            )
    return results


def tick(
    *,
    worker_id=None,
    session_factory=None,
    adapters=None,
    publisher=None,
    config=None,
    now=None,
):
    """One processing pass: claim, run adapters, settle and finalize."""
    worker_id = worker_id or new_worker_id()
    session_factory = session_factory or DBSession
    config = config or queue_config()
    if adapters is None:
        from .adapters import ADAPTERS

        adapters = ADAPTERS

    with session_factory() as session:
        claimed = claim_subjobs(
            session,
            worker_id,
            limit=config["claim_limit"],
            now=now,
            lease_seconds=config["lease_seconds"],
        )

    outcomes = []
    query_ids = {c.query_id for c in claimed}
    for claim in claimed:
        try:
            outcome = process_subjob(
                session_factory,
                claim.subjob_id,
                worker_id,
                adapters,
                config,
                now=now,
            )
        except Exception as e:  # defensive: one bad catalog must not stop the pass
            log(f"Error processing subjob {claim.subjob_id}: {e!r}")
            outcome = None
        outcomes.append(outcome)
        query_ids.add(claim.query_id)

    finalized = []
    for query_id in query_ids:
        try:
            with session_factory() as session:
                result = finalize_query(
                    session, query_id, worker_id, publisher=publisher, now=now
                )
        except Exception as e:  # a transient finalize failure must not kill the pass
            log(f"Error finalizing query {query_id}: {e!r}")
            result = None
        if result is not None:
            finalized.append(result)
    try:
        finalized.extend(
            r
            for r in _finalize_due_queries(
                session_factory, worker_id, publisher, now
            )
            if r is not None and r.get("became_terminal")
        )
    except Exception as e:
        log(f"Error in due-query sweep: {e!r}")

    return {
        "worker_id": worker_id,
        "claimed": [c.subjob_id for c in claimed],
        "outcomes": outcomes,
        "finalized": finalized,
    }
