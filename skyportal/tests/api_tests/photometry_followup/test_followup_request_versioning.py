"""Versioning and concurrency tests for follow-up commands.

Regression coverage for the protocol that keeps late facility responses
to a superseded submit/update/delete from overwriting newer state:

- each command fixes a FollowupRequest version;
- queued facility commands are atomically claimed and carry a stable
  command identity;
- responses are applied only while their version/allocation/target is
  still current, otherwise recorded as a queryable stale transaction;
- first-time submission keeps its existing semantics (version 1).
"""

import uuid
from datetime import timedelta

import sqlalchemy as sa

from baselayer.app.models import new_session
from skyportal.models import (
    FacilityTransaction,
    FacilityTransactionRequest,
)
from skyportal.models.facility_transaction import (
    CLAIM_LEASE_SECONDS,
    COMMAND_DELETE,
    COMMAND_RETRIEVE,
    COMMAND_SUBMIT,
    COMMAND_UPDATE,
    TRANSACTION_APPLIED,
    TRANSACTION_REQUEST_CLAIMED,
    TRANSACTION_REQUEST_COMPLETE,
    TRANSACTION_REQUEST_PENDING,
    TRANSACTION_STALE,
    apply_request_status,
    claim_facility_transaction_request,
    request_version_matches,
    stable_command_id,
)
from skyportal.tests import api
from skyportal.utils.naive_datetime import utcnow_naive


def _sedm_payload(priority=5):
    return {
        "priority": priority,
        "start_date": "3010-09-01",
        "end_date": "3012-09-01",
        "observation_type": "IFU",
        "exposure_time": 300,
        "maximum_airmass": 2,
        "maximum_fwhm": 1.2,
    }


def _post_sedm_request(allocation, source, token, priority=5):
    status, data = api(
        "POST",
        "followup_request",
        data={
            "allocation_id": allocation.id,
            "obj_id": source.id,
            "payload": _sedm_payload(priority),
        },
        token=token,
    )
    assert status == 200, data
    return data["data"]["id"]


def test_stable_command_id_is_deterministic():
    a = stable_command_id(7, 1, COMMAND_SUBMIT)
    b = stable_command_id(7, 1, COMMAND_SUBMIT)
    assert a == b
    # Every version and action gets its own command identity.
    assert stable_command_id(7, 2, COMMAND_SUBMIT) != a
    assert stable_command_id(7, 1, COMMAND_UPDATE) != a
    assert stable_command_id(8, 1, COMMAND_SUBMIT) != a


def test_first_submit_fixes_version_one(
    public_group_sedm_allocation, public_source, upload_data_token
):
    request_id = _post_sedm_request(
        public_group_sedm_allocation, public_source, upload_data_token
    )

    status, data = api("GET", f"followup_request/{request_id}", token=upload_data_token)
    assert status == 200
    assert data["data"]["version"] == 1
    assert data["data"]["status"] == "submitted"


def test_modify_and_cancel_each_fix_a_new_version(
    public_group_sedm_allocation, public_source, upload_data_token
):
    request_id = _post_sedm_request(
        public_group_sedm_allocation, public_source, upload_data_token
    )

    status, data = api(
        "PUT",
        f"followup_request/{request_id}",
        data={
            "allocation_id": public_group_sedm_allocation.id,
            "obj_id": public_source.id,
            "payload": _sedm_payload(priority=4),
        },
        token=upload_data_token,
    )
    assert status == 200, data

    status, data = api(
        "DELETE", f"followup_request/{request_id}", token=upload_data_token
    )
    assert status == 200, data

    status, data = api("GET", f"followup_request/{request_id}", token=upload_data_token)
    assert status == 200
    assert data["data"]["version"] == 3
    assert data["data"]["status"] == "deleted"

    # Every command exchange is bound to the version it was issued at,
    # with the stable command identity and an applied disposition.
    annotated = {
        (tx["action"], tx["followup_request_version"]): tx
        for tx in data["data"]["transactions"]
        if tx.get("command_id")
    }
    assert set(annotated) == {
        (COMMAND_SUBMIT, 1),
        (COMMAND_UPDATE, 2),
        (COMMAND_DELETE, 3),
    }
    for (action, version), tx in annotated.items():
        assert tx["disposition"] == TRANSACTION_APPLIED
        assert tx["command_id"] == stable_command_id(request_id, version, action)
        assert tx["allocation_id"] == public_group_sedm_allocation.id
        assert tx["obj_id"] == public_source.id


def test_response_to_old_version_cannot_overwrite_new_state(
    public_group_sedm_allocation, public_source, upload_data_token
):
    request_id = _post_sedm_request(
        public_group_sedm_allocation, public_source, upload_data_token
    )

    # The researcher changes priority: the request is now at version 2.
    status, data = api(
        "PUT",
        f"followup_request/{request_id}",
        data={
            "allocation_id": public_group_sedm_allocation.id,
            "obj_id": public_source.id,
            "payload": _sedm_payload(priority=2),
        },
        token=upload_data_token,
    )
    assert status == 200, data

    session = DBSession()
    try:
        # A late response to the original v1 submission must not apply.
        assert not request_version_matches(session, request_id, 1)
        assert request_version_matches(session, request_id, 2)
        applied = apply_request_status(session, request_id, 1, "deleted")
        assert applied is False
        session.commit()

        # The stale exchange stays queryable as a transaction without
        # touching the request.
        session.add(
            FacilityTransaction(
                request={"note": "late response"},
                followup_request_id=request_id,
                followup_request_version=1,
                action=COMMAND_SUBMIT,
                command_id=stable_command_id(request_id, 1, COMMAND_SUBMIT),
                disposition=TRANSACTION_STALE,
            )
        )
        session.commit()

        # A response to the current version applies; the transition gate
        # fires once even when the same result is delivered twice.
        assert apply_request_status(session, request_id, 2, "deleted")
        assert not apply_request_status(
            session, request_id, 2, "deleted", only_if_changed=True
        )
        session.commit()
    finally:
        DBSession.remove()

    status, data = api("GET", f"followup_request/{request_id}", token=upload_data_token)
    assert status == 200
    assert data["data"]["version"] == 2
    assert data["data"]["status"] == "deleted"


def test_queued_command_claim_is_atomic_and_reclaimable(
    public_group_sedm_allocation, public_source, upload_data_token
):
    request_id = _post_sedm_request(
        public_group_sedm_allocation, public_source, upload_data_token
    )

    session = DBSession()
    try:
        queued = FacilityTransactionRequest(
            method="GET",
            endpoint=f"https://example.org/jobs/{uuid.uuid4()}",
            data={},
            params={},
            headers={},
            followup_request_id=request_id,
            followup_request_version=1,
            action=COMMAND_RETRIEVE,
            command_id=stable_command_id(request_id, 1, COMMAND_RETRIEVE),
            status=TRANSACTION_REQUEST_PENDING,
        )
        session.add(queued)
        session.commit()

        # First worker wins the atomic claim.
        assert claim_facility_transaction_request(session, queued.id, "worker-1")
        session.commit()
        claimed = session.scalars(
            sa.select(FacilityTransactionRequest).where(
                FacilityTransactionRequest.id == queued.id
            )
        ).first()
        assert claimed.status == TRANSACTION_REQUEST_CLAIMED
        assert claimed.claimed_by == "worker-1"

        # A second worker (simulated by another claim attempt) cannot
        # claim it while the lease is live.
        assert not claim_facility_transaction_request(session, queued.id, "worker-2")
        session.rollback()

        # Once the lease elapses (crashed/restarted worker), the command
        # becomes claimable again.
        claimed.claimed_at = utcnow_naive() - timedelta(
            seconds=CLAIM_LEASE_SECONDS + 60
        )
        session.commit()

        assert claim_facility_transaction_request(session, queued.id, "worker-2")
        session.commit()

        # A finished command can never be claimed again.
        claimed = session.scalars(
            sa.select(FacilityTransactionRequest).where(
                FacilityTransactionRequest.id == queued.id
            )
        ).first()
        claimed.status = TRANSACTION_REQUEST_COMPLETE
        session.commit()
        assert not claim_facility_transaction_request(session, queued.id, "worker-3")
        session.rollback()
    finally:
        DBSession.remove()


def test_superseded_poll_leaves_queryable_stale_transaction(
    public_group_sedm_allocation, public_source, upload_data_token
):
    request_id = _post_sedm_request(
        public_group_sedm_allocation, public_source, upload_data_token
    )

    session = DBSession()
    try:
        queued = FacilityTransactionRequest(
            method="GET",
            endpoint=f"https://example.org/jobs/{uuid.uuid4()}",
            data={},
            params={},
            headers={},
            followup_request_id=request_id,
            followup_request_version=1,
            allocation_id=public_group_sedm_allocation.id,
            obj_id=public_source.id,
            action=COMMAND_RETRIEVE,
            command_id=stable_command_id(request_id, 1, COMMAND_RETRIEVE),
            status=TRANSACTION_REQUEST_PENDING,
        )
        session.add(queued)
        session.commit()

        # Cancellation fixes a newer version before the poll resolves.
        session.execute(
            sa.text("UPDATE followuprequests SET version = 2 WHERE id = :id"),
            {"id": request_id},
        )
        session.commit()

        # The v1 poll result no longer matches: it must not apply.
        assert not request_version_matches(
            session, request_id, 1, allocation_id=public_group_sedm_allocation.id
        )
        assert not apply_request_status(
            session, request_id, 1, "Photometry committed to database"
        )
        session.commit()

        # Finalize the way the queue worker does: stop polling, record
        # the exchange as stale, keep it out of any error terminal.
        queued.status = "stale"
        session.add(
            FacilityTransaction(
                request={"method": "GET", "endpoint": queued.endpoint},
                followup_request_id=request_id,
                followup_request_version=1,
                allocation_id=public_group_sedm_allocation.id,
                obj_id=public_source.id,
                action=COMMAND_RETRIEVE,
                command_id=stable_command_id(request_id, 1, COMMAND_RETRIEVE),
                disposition=TRANSACTION_STALE,
            )
        )
        session.commit()

        refreshed = session.scalars(
            sa.select(FacilityTransactionRequest).where(
                FacilityTransactionRequest.id == queued.id
            )
        ).first()
        assert refreshed.status == "stale"
        assert not refreshed.status.startswith("error")
        stale_tx = session.scalars(
            sa.select(FacilityTransaction).where(
                FacilityTransaction.followup_request_id == request_id,
                FacilityTransaction.disposition == TRANSACTION_STALE,
            )
        ).all()
        assert len(stale_tx) == 1
    finally:
        DBSession.remove()

    status, data = api("GET", f"followup_request/{request_id}", token=upload_data_token)
    assert status == 200
    # The late poll could not overwrite the request state.
    assert data["data"]["status"] == "submitted"
    assert data["data"]["version"] == 2
