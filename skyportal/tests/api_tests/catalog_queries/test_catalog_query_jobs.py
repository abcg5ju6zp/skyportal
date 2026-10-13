"""Offline integration tests for the persistent catalog-query pipeline.

Adapters are replaced with in-process fakes; worker passes are driven
directly through `tick(...)`, never through the network or a remote catalog.
"""

import threading
import time
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa

from skyportal.models import (
    CatalogQuery,
    CatalogQueryCandidate,
    CatalogQuerySubjob,
    DBSession,
    Obj,
    Photometry,
    UserNotification,
    new_session,
)
from skyportal.tests import api
from skyportal.utils.catalog_query import jobs
from skyportal.utils.catalog_query.jobs import (
    QUERY_PUBLISHING,
    AdapterDataError,
    UncertainResponse,
    claim_subjobs,
    finalize_query,
    process_subjob,
    stage_candidate,
    tick,
)
from skyportal.utils.catalog_query.publish import _publish_candidate, publish_query
from skyportal.utils.naive_datetime import utcnow_naive

CONFIG = {
    "poll_interval": 1,
    "claim_limit": 10,
    "lease_seconds": 300,
    "max_attempts": 3,
    "request_timeout_seconds": 5,
}

PAYLOAD = {
    "startDate": "2024-01-01T00:00:00",
    "endDate": "2024-02-01T00:00:00",
}


@pytest.fixture(autouse=True)
def _quiet_flows(monkeypatch):
    pushes = []

    def _record(*args, **kwargs):
        pushes.append((args, kwargs))

    monkeypatch.setattr(jobs, "send_terminal_flows", _record)
    yield pushes


def _make_allocation(token, group_id, filters=None):
    name = str(uuid.uuid4())
    status, data = api(
        "POST",
        "telescope",
        data={
            "name": name,
            "nickname": name,
            "lat": 0.0,
            "lon": 0.0,
            "elevation": 0.0,
            "diameter": 10.0,
        },
        token=token,
    )
    assert status == 200
    telescope_id = data["data"]["id"]

    instrument_name = str(uuid.uuid4())
    status, data = api(
        "POST",
        "instrument",
        data={
            "name": instrument_name,
            "type": "imager",
            "band": "NIR",
            "filters": filters or ["ztfr"],
            "telescope_id": telescope_id,
        },
        token=token,
    )
    assert status == 200
    instrument_id = data["data"]["id"]

    status, data = api(
        "POST",
        "allocation",
        data={
            "group_id": group_id,
            "instrument_id": instrument_id,
            "pi": "Test PI",
            "hours_allocated": 200,
            "validity_ranges": [
                {
                    "start_date": "2020-01-01T00:00:00.000Z",
                    "end_date": "3021-01-01T00:00:00.000Z",
                }
            ],
            "proposal_id": f"P-{uuid.uuid4()}",
        },
        token=token,
    )
    assert status == 200
    return data["data"]["id"], instrument_id


def _post_query(
    token,
    allocation_id,
    group_id,
    catalogs,
    *,
    publish_results=None,
    payload_extra=None,
):
    payload = dict(PAYLOAD)
    if isinstance(catalogs, str):
        payload["catalogName"] = catalogs
    else:
        payload["catalogNames"] = catalogs
    if payload_extra:
        payload.update(payload_extra)
    body = {
        "allocation_id": allocation_id,
        "payload": payload,
        "target_group_ids": [group_id],
    }
    if publish_results is not None:
        body["publish_results"] = publish_results
    return api("POST", "catalog_queries", data=body, token=token)


def _get(query_id):
    with DBSession() as session:
        return session.get(CatalogQuery, query_id)


def _subjobs(query_id):
    with DBSession() as session:
        rows = session.scalars(
            sa.select(CatalogQuerySubjob)
            .where(CatalogQuerySubjob.query_id == query_id)
            .options(sa.orm.selectinload(CatalogQuerySubjob.attempts))
            .order_by(CatalogQuerySubjob.id)
        ).all()
        return [
            {
                "id": row.id,
                "catalog_name": row.catalog_name,
                "status": row.status,
                "error": row.error,
                "claimed_by": row.claimed_by,
                "attempts_count": row.attempts_count,
                "result_summary": row.result_summary,
                "attempts": [
                    {"attempt_no": a.attempt_no, "state": a.state}
                    for a in row.attempts
                ],
            }
            for row in rows
        ]


def _candidate_rows(query_id):
    with DBSession() as session:
        rows = session.scalars(
            sa.select(CatalogQueryCandidate)
            .where(CatalogQueryCandidate.query_id == query_id)
            .order_by(CatalogQueryCandidate.id)
        ).all()
        return [
            {
                "id": row.id,
                "subjob_id": row.subjob_id,
                "catalog_name": row.catalog_name,
                "external_id": row.external_id,
                "validation_status": row.validation_status,
                "validation_error": row.validation_error,
                "resolution": row.resolution,
                "published_at": row.published_at,
            }
            for row in rows
        ]


def _notifications(query_id):
    query = _get(query_id)
    with DBSession() as session:
        return session.scalar(
            sa.select(sa.func.count())
            .select_from(UserNotification)
            .where(
                UserNotification.user_id == query.requester_id,
                UserNotification.notification_type == "gcnevent_catalog_query",
                UserNotification.text.like(f"%{query.query_uid}%"),
            )
        )


def _expire_leases(query_id):
    past = utcnow_naive() - timedelta(minutes=10)
    with DBSession() as session:
        session.query(CatalogQuerySubjob).filter(
            CatalogQuerySubjob.query_id == query_id
        ).update({CatalogQuerySubjob.lease_expires: past}, synchronize_session=False)
        session.commit()


def _drive(query_id, adapters, *, publisher=None, ticks=8, expire=True):
    for _ in range(ticks):
        if expire:
            _expire_leases(query_id)
        tick(
            session_factory=DBSession,
            adapters=adapters,
            publisher=publisher,
            config=CONFIG,
        )
        if _get(query_id).terminal_at is not None:
            return
    raise AssertionError(f"query {query_id} never reached a terminal state")


def _candidate(external_id, ra=10.0, dec=20.0, photometry=None, series=None):
    return {
        "external_id": external_id,
        "ra": ra,
        "dec": dec,
        "raw_data": {"id": external_id, "ra": ra, "dec": dec},
        "photometry": photometry,
        "photometric_series": series,
    }


def _ok_adapter(stages, calls=None, summary=None):
    def run(ctx):
        if calls is not None:
            calls.append({"attempt_no": ctx.attempt_no, "pagination": ctx.pagination})
        for stage in stages:
            assert ctx.stage(**stage) is not None
        return summary or {"objects": len(stages)}

    return run


def _uncertain_adapter(calls, stages, succeed_on):
    def run(ctx):
        calls.append({"attempt_no": ctx.attempt_no, "pagination": ctx.pagination})
        if len(calls) < succeed_on:
            ctx.heartbeat(pagination={"seen": len(calls)})
            raise UncertainResponse("simulated timeout")
        for stage in stages:
            ctx.stage(**stage)
        return {"objects": len(stages)}

    return run


def test_accept_persists_and_runs_end_to_end(public_group, super_admin_token):
    gid = public_group.id

    allocation_id, _ = _make_allocation(super_admin_token, gid)
    external_id = f"cq-{uuid.uuid4().hex[:8]}"

    status, data = _post_query(
        super_admin_token, allocation_id, gid, "Gaia"
    )
    assert status == 200
    query_id = data["data"]["id"]

    # Persisted before any worker activity.
    query = _get(query_id)
    assert query.status == "pending"
    assert query.query_uid is not None
    assert query.permission_snapshot["target_group_ids"] == [gid]
    subjobs = _subjobs(query_id)
    assert [s["catalog_name"] for s in subjobs] == ["Gaia"]
    assert all(s["status"] == "pending" for s in subjobs)

    with DBSession() as session:
        obj_count_before = session.scalar(
            sa.select(sa.func.count())
            .select_from(Obj)
            .where(Obj.id == external_id)
        )
    assert obj_count_before == 0

    adapters = {"Gaia": _ok_adapter([_candidate(external_id)])}
    # No publisher wired: results must remain staged, never published.
    _drive(query_id, adapters, publisher=None)

    query = _get(query_id)
    assert query.status.startswith("completed")
    assert query.terminal_at is not None
    with DBSession() as session:
        obj_count_after = session.scalar(
            sa.select(sa.func.count())
            .select_from(Obj)
            .where(Obj.id == external_id)
        )
    assert obj_count_after == 0
    candidates = _candidate_rows(query_id)
    assert len(candidates) == 1
    assert candidates[0]["external_id"] == external_id

    status, data = api(
        "GET", f"catalog_queries/{query_id}/candidates", token=super_admin_token
    )
    assert status == 200
    assert data["data"]["total"] == 1

    status, data = api(
        "GET", f"catalog_queries/{query_id}", token=super_admin_token
    )
    assert status == 200
    detail = data["data"]
    assert len(detail["subjobs"]) == 1
    assert detail["subjobs"][0]["status"] == "succeeded"
    assert detail["subjobs"][0]["attempts"][0]["state"] == "succeeded"


def test_duplicate_submission_is_idempotent(public_group, super_admin_token):
    gid = public_group.id

    allocation_id, _ = _make_allocation(super_admin_token, gid)

    def post(catalogs):
        return _post_query(
            super_admin_token,
            allocation_id,
            gid,
            catalogs,
            payload_extra={"localizationCumprob": 0.9},
        )

    status, first = post("Gaia")
    assert status == 200
    first_id = first["data"]["id"]

    # Legacy string and list forms are equivalent.
    status, again = post(["Gaia"])
    assert status == 200
    assert again["data"]["id"] == first_id

    # Adding a catalog is a different job; list order is not.
    status, multi = post(["Gaia", "TESS"])
    assert status == 200
    multi_id = multi["data"]["id"]
    assert multi_id != first_id
    status, again = post(["TESS", "Gaia"])
    assert status == 200
    assert again["data"]["id"] == multi_id

    assert len(_subjobs(first_id)) == 1
    assert len(_subjobs(multi_id)) == 2

    # A different publish flag is a different request.
    status, read_only = _post_query(
        super_admin_token,
        allocation_id,
        gid,
        "Gaia",
        publish_results=False,
    )
    assert status == 200
    assert read_only["data"]["id"] != first_id

    # Terminalized queries allow a fresh (new-version) submission.
    adapters = {
        "Gaia": _ok_adapter([]),
        "TESS": _ok_adapter([]),
    }
    _drive(first_id, adapters, publisher=None)
    status, newer = post("Gaia")
    assert status == 200
    assert newer["data"]["id"] != first_id
    assert _get(newer["data"]["id"]).query_uid != _get(first_id).query_uid


def test_claim_mutex_lease_and_takeover(public_group, super_admin_token):
    gid = public_group.id

    allocation_id, _ = _make_allocation(super_admin_token, gid)
    status, data = _post_query(
        super_admin_token,
        allocation_id,
        gid,
        ["Gaia", "TESS", "LSXPS"],
    )
    query_id = data["data"]["id"]
    now = utcnow_naive()
    barrier = threading.Barrier(2, timeout=10)
    results = {}

    def worker_a():
        session = new_session()
        results["a"] = claim_subjobs(
            session, "worker-a", now=now, lease_seconds=300
        )
        barrier.wait()
        time.sleep(0.3)
        session.commit()
        session.close()

    def worker_b():
        barrier.wait()
        time.sleep(0.1)
        session = new_session()
        results["b1"] = claim_subjobs(
            session, "worker-b", now=now, lease_seconds=300
        )
        session.rollback()
        session.close()
        session = new_session()
        results["b2"] = claim_subjobs(
            session, "worker-b", now=now, lease_seconds=300
        )
        session.rollback()
        session.close()

    thread_a = threading.Thread(target=worker_a)
    thread_b = threading.Thread(target=worker_b)
    thread_a.start()
    thread_b.start()
    thread_a.join()
    thread_b.join()

    assert len(results["a"]) == 3
    # b1 ran against A's uncommitted claim; b2 after commit but lease is fresh.
    assert results["b1"] == []
    assert results["b2"] == []

    # Once the lease expires, worker B takes every sub-request over.
    _expire_leases(query_id)
    with DBSession() as session:
        taken = claim_subjobs(session, "worker-b", now=now, lease_seconds=300)
    assert {c.subjob_id for c in taken} == {s["id"] for s in _subjobs(query_id)}
    assert all(s["claimed_by"] == "worker-b" for s in _subjobs(query_id))


def test_uncertain_response_resumes_same_attempt(public_group, super_admin_token):
    gid = public_group.id

    allocation_id, _ = _make_allocation(super_admin_token, gid)
    external_id = f"cq-{uuid.uuid4().hex[:8]}"
    status, data = _post_query(
        super_admin_token, allocation_id, gid, "Gaia"
    )
    query_id = data["data"]["id"]

    calls = []
    adapters = {
        "Gaia": _uncertain_adapter(calls, [_candidate(external_id)], succeed_on=2)
    }
    _drive(query_id, adapters, publisher=None, ticks=4)

    subjobs = _subjobs(query_id)
    assert subjobs[0]["status"] == "succeeded"
    # Both passes reconcile the same attempt; the second saw saved pagination.
    assert len(calls) == 2
    assert calls[0]["attempt_no"] == calls[1]["attempt_no"] == 1
    assert calls[1]["pagination"] == {"seen": 1}
    attempts = subjobs[0]["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["state"] == "succeeded"
    assert {c["external_id"] for c in _candidate_rows(query_id)} == {external_id}

    # Exhausting the attempt budget fails the sub-request deterministically.
    status, data = _post_query(
        super_admin_token, allocation_id, gid, "TESS"
    )
    q2 = data["data"]["id"]
    cfg = dict(CONFIG, max_attempts=1)
    for _ in range(3):
        _expire_leases(q2)
        tick(
            session_factory=DBSession,
            adapters={"TESS": _uncertain_adapter([], [], succeed_on=99)},
            publisher=None,
            config=cfg,
        )
        if _get(q2).terminal_at is not None:
            break
    subjob2 = _subjobs(q2)[0]
    assert subjob2["status"] == "failed"
    assert "exceeded 1 attempts" in subjob2["error"]


def test_invalid_candidates_never_publish(public_group, super_admin_token):
    gid = public_group.id

    allocation_id, _ = _make_allocation(super_admin_token, gid)
    good_id = f"cq-good-{uuid.uuid4().hex[:6]}"
    bad_phot = {
        "obj_id": "x",
        "filter": ["ztfr"],
        # mjd deliberately missing
    }
    stages = [
        _candidate("bad id", ra=1.0, dec=2.0),
        _candidate(f"cq-badra-{uuid.uuid4().hex[:6]}", ra=400.0, dec=2.0),
        _candidate(
            f"cq-badphot-{uuid.uuid4().hex[:6]}",
            photometry=bad_phot,
        ),
        _candidate(good_id),
    ]
    status, data = _post_query(
        super_admin_token, allocation_id, gid, "Gaia"
    )
    query_id = data["data"]["id"]
    _drive(query_id, {"Gaia": _ok_adapter(stages)}, publisher=publish_query)

    rows = {c["external_id"]: c for c in _candidate_rows(query_id)}
    assert rows[good_id]["validation_status"] == "valid"
    assert rows[good_id]["resolution"] == "new"
    assert rows[good_id]["published_at"] is not None
    invalid = [c for c in rows.values() if c["validation_status"] == "invalid"]
    assert len(invalid) == 2
    assert all(c["published_at"] is None for c in invalid)

    # The malformed photometry payload invalidates its data row, not the
    # candidate: the source may exist but no photometry is published.
    status, detail = api(
        "GET", f"catalog_queries/{query_id}/candidates", token=super_admin_token
    )
    assert status == 200
    badphot = next(
        c
        for c in detail["data"]["candidates"]
        if c["external_id"].startswith("cq-badphot")
    )
    assert badphot["data"][0]["validation_status"] == "invalid"
    with DBSession() as session:
        assert session.scalar(
            sa.select(sa.func.count())
            .select_from(Obj)
            .where(Obj.id == good_id)
        ) == 1


def test_existing_object_association(public_group, super_admin_token):
    gid = public_group.id

    existing_id = f"cq-exist-{uuid.uuid4().hex[:6]}"
    new_id = f"cq-new-{uuid.uuid4().hex[:6]}"
    status, data = api(
        "POST",
        "sources",
        data={
            "id": existing_id,
            "ra": 33.3,
            "dec": 11.1,
            "group_ids": [gid],
        },
        token=super_admin_token,
    )
    assert status == 200

    allocation_id, _ = _make_allocation(super_admin_token, gid)
    status, data = _post_query(
        super_admin_token, allocation_id, gid, "Gaia"
    )
    query_id = data["data"]["id"]
    stages = [_candidate(existing_id, ra=33.3, dec=11.1), _candidate(new_id)]
    _drive(query_id, {"Gaia": _ok_adapter(stages)}, publisher=publish_query)

    rows = {c["external_id"]: c for c in _candidate_rows(query_id)}
    assert rows[existing_id]["resolution"] == "existing"
    assert rows[new_id]["resolution"] == "new"
    with DBSession() as session:
        assert session.scalar(
            sa.select(sa.func.count())
            .select_from(Obj)
            .where(Obj.id.in_([existing_id, new_id]))
        ) == 2


def test_publish_photometry_rows(public_group, super_admin_token):
    gid = public_group.id

    allocation_id, instrument_id = _make_allocation(
        super_admin_token, gid, filters=["ztfr"]
    )
    external_id = f"cq-phot-{uuid.uuid4().hex[:6]}"
    photometry = {
        "obj_id": external_id,
        "instrument_id": instrument_id,
        "group_ids": [gid],
        "mjd": [59328.59, 59328.60],
        "mag": [20.1, 20.2],
        "magerr": [0.1, 0.1],
        "filter": ["ztfr", "ztfr"],
        "ra": [12.0, 12.0],
        "dec": [34.0, 34.0],
        "limiting_mag": [20.5, 20.5],
        "magsys": ["ab", "ab"],
    }
    status, data = _post_query(
        super_admin_token, allocation_id, gid, "Gaia"
    )
    query_id = data["data"]["id"]
    _drive(
        query_id,
        {"Gaia": _ok_adapter([_candidate(external_id, photometry=photometry)])},
        publisher=publish_query,
    )
    with DBSession() as session:
        nphot = session.scalar(
            sa.select(sa.func.count())
            .select_from(Photometry)
            .where(Photometry.obj_id == external_id)
        )
    assert nphot == 2


def test_partial_success_marks_partial_and_publishes(
    public_group, super_admin_token
):
    gid = public_group.id
    allocation_id, _ = _make_allocation(super_admin_token, gid)
    good_id = f"cq-part-{uuid.uuid4().hex[:6]}"
    status, data = _post_query(
        super_admin_token,
        allocation_id,
        gid,
        ["Gaia", "TESS", "LSXPS"],
    )
    query_id = data["data"]["id"]

    always_uncertain = _uncertain_adapter([], [], succeed_on=99)

    def data_error(ctx):
        raise AdapterDataError("malformed catalog response")

    adapters = {
        "Gaia": _ok_adapter([_candidate(good_id)]),
        "TESS": always_uncertain,
        "LSXPS": data_error,
    }
    _drive(query_id, adapters, publisher=publish_query, ticks=6)

    query = _get(query_id)
    assert query.status.startswith("completed_partial")
    by_name = {s["catalog_name"]: s for s in _subjobs(query_id)}
    assert by_name["Gaia"]["status"] == "succeeded"
    assert by_name["LSXPS"]["status"] == "failed"
    assert by_name["LSXPS"]["error"] == "malformed catalog response"
    assert by_name["TESS"]["status"] == "failed"
    assert "exceeded 3 attempts" in by_name["TESS"]["error"]
    with DBSession() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(Obj).where(Obj.id == good_id)
        ) == 1
    assert _notifications(query_id) == 1


def test_terminal_transition_and_notification_happen_once(
    public_group, super_admin_token
):
    gid = public_group.id
    allocation_id, _ = _make_allocation(super_admin_token, gid)
    status, data = _post_query(
        super_admin_token, allocation_id, gid, "Gaia"
    )
    query_id = data["data"]["id"]
    _drive(query_id, {"Gaia": _ok_adapter([_candidate(f"cq-{uuid.uuid4().hex[:6]}")])})

    # Repeated finalize is a no-op, not a second terminal.
    with DBSession() as session:
        r1 = finalize_query(session, query_id, "w1")
    with DBSession() as session:
        r2 = finalize_query(session, query_id, "w2")
    assert r1["became_terminal"] is False
    assert r2["became_terminal"] is False
    assert _notifications(query_id) == 1


def test_cancel_drops_late_writes_and_terminalizes_once(
    public_group, super_admin_token
):
    gid = public_group.id
    allocation_id, _ = _make_allocation(super_admin_token, gid)
    status, data = _post_query(
        super_admin_token,
        allocation_id,
        gid,
        ["Gaia", "TESS"],
    )
    query_id = data["data"]["id"]

    tess_calls = []
    adapters = {
        "Gaia": _ok_adapter([_candidate(f"cq-gaia-{uuid.uuid4().hex[:6]}")]),
        "TESS": _uncertain_adapter(tess_calls, [], succeed_on=99),
    }
    tick(session_factory=DBSession, adapters=adapters, config=CONFIG)

    status, data = api(
        "POST", f"catalog_queries/{query_id}/cancel", token=super_admin_token
    )
    assert status == 200
    assert _get(query_id).cancel_requested is True

    # Late response for the still-claimed TESS sub-request must be rejected.
    tess_subjob = next(
        s for s in _subjobs(query_id) if s["catalog_name"] == "TESS"
    )
    with DBSession() as session:
        accepted = stage_candidate(
            session,
            subjob_id=tess_subjob["id"],
            worker_id=tess_subjob["claimed_by"],
            external_id="late-response",
            ra=1.0,
            dec=2.0,
            raw_data={"id": "late-response"},
        )
    assert accepted is None

    _drive(query_id, adapters, publisher=None, ticks=3)
    query = _get(query_id)
    assert query.status == "cancelled"
    by_name = {s["catalog_name"]: s for s in _subjobs(query_id)}
    assert by_name["TESS"]["status"] == "cancelled"
    assert by_name["Gaia"]["status"] == "succeeded"
    # Gaia's confirmed staging is retained; TESS's late write never landed.
    candidates = _candidate_rows(query_id)
    assert len(candidates) == 1
    assert candidates[0]["catalog_name"] == "Gaia"
    assert _notifications(query_id) == 1

    status, _ = api(
        "POST", f"catalog_queries/{query_id}/cancel", token=super_admin_token
    )
    assert status == 409


def test_cancel_before_any_claim_settles(public_group, super_admin_token):
    gid = public_group.id

    allocation_id, _ = _make_allocation(super_admin_token, gid)
    status, data = _post_query(
        super_admin_token, allocation_id, gid, "Gaia"
    )
    query_id = data["data"]["id"]
    status, _ = api(
        "POST", f"catalog_queries/{query_id}/cancel", token=super_admin_token
    )
    assert status == 200
    deadline = utcnow_naive() + timedelta(seconds=15)
    while utcnow_naive() < deadline:
        if _get(query_id).terminal_at is not None:
            break
        time.sleep(0.5)
    query = _get(query_id)
    assert query.terminal_at is not None
    assert query.status == "cancelled"
    assert all(s["status"] == "cancelled" for s in _subjobs(query_id))


def test_new_version_isolated_from_old_late_writes(
    public_group, super_admin_token
):
    gid = public_group.id
    allocation_id, _ = _make_allocation(super_admin_token, gid)
    status, data = _post_query(
        super_admin_token, allocation_id, gid, "Gaia"
    )
    q1 = data["data"]["id"]
    _drive(q1, {"Gaia": _ok_adapter([_candidate(f"cq-old-{uuid.uuid4().hex[:6]}")])})

    status, data = _post_query(
        super_admin_token, allocation_id, gid, "Gaia"
    )
    q2 = data["data"]["id"]
    assert q1 != q2

    old_subjob = _subjobs(q1)[0]
    with DBSession() as session:
        accepted = stage_candidate(
            session,
            subjob_id=old_subjob["id"],
            worker_id=old_subjob["claimed_by"] or "anyone",
            external_id="super-late",
            ra=1.0,
            dec=2.0,
            raw_data={"id": "super-late"},
        )
    assert accepted is None
    assert _candidate_rows(q2) == []


def test_read_only_query_never_publishes(public_group, super_admin_token):
    gid = public_group.id

    allocation_id, _ = _make_allocation(super_admin_token, gid)
    external_id = f"cq-ro-{uuid.uuid4().hex[:6]}"
    status, data = _post_query(
        super_admin_token,
        allocation_id,
        gid,
        "Gaia",
        publish_results=False,
    )
    query_id = data["data"]["id"]

    def forbidden_publisher(qid):
        raise AssertionError("read-only queries must never invoke publication")

    _drive(
        query_id,
        {"Gaia": _ok_adapter([_candidate(external_id)])},
        publisher=forbidden_publisher,
    )
    query = _get(query_id)
    assert query.publish_results is False
    assert query.status.startswith("completed")
    with DBSession() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(Obj).where(Obj.id == external_id)
        ) == 0
    candidates = _candidate_rows(query_id)
    assert len(candidates) == 1
    assert candidates[0]["published_at"] is None

    status, data = api(
        "GET", f"catalog_queries/{query_id}/candidates", token=super_admin_token
    )
    assert status == 200
    assert data["data"]["candidates"][0]["external_id"] == external_id


def test_no_results_keeps_legacy_text_and_gcn_listing(
    public_group, super_admin_token, gcn_GRB180116A
):
    gid = public_group.id
    # Capture fixture scalar before any direct DBSession blocks detach it.
    gcnevent_id = gcn_GRB180116A.id
    allocation_id, _ = _make_allocation(super_admin_token, gid)
    status, data = _post_query(
        super_admin_token,
        allocation_id,
        gid,
        "Gaia",
        payload_extra={"gcnevent_id": gcnevent_id},
    )
    query_id = data["data"]["id"]
    _drive(query_id, {"Gaia": _ok_adapter([])}, publisher=None)
    assert _get(query_id).status == "completed: No new objects"

    status, data = api(
        "GET", f"gcn_event/{gcnevent_id}/catalog_query", token=super_admin_token
    )
    assert status == 200
    assert any(q["id"] == query_id for q in data["data"])

    # The direct-session worker passes close the shared test session,
    # detaching the factory object; re-attach it so its teardown can walk it.
    DBSession().add(gcn_GRB180116A)


def test_permission_snapshot_gates_publication(
    public_group, super_admin_token
):
    gid = public_group.id
    allocation_id, _ = _make_allocation(super_admin_token, gid)
    external_id = f"cq-perm-{uuid.uuid4().hex[:6]}"
    status, data = _post_query(
        super_admin_token, allocation_id, gid, "Gaia"
    )
    query_id = data["data"]["id"]

    def blocked_publisher(qid):
        # Simulate the requester having lost every snapshot target group.
        with DBSession() as session:
            query = session.get(CatalogQuery, qid)
            query.permission_snapshot = {
                **query.permission_snapshot,
                "target_group_ids": [99999999],
            }
            session.commit()
        return publish_query(qid)

    # Run two passes while publication is blocked; the query must remain active.
    for _ in range(2):
        tick(
            session_factory=DBSession,
            adapters={"Gaia": _ok_adapter([_candidate(external_id)])},
            publisher=blocked_publisher,
            config=CONFIG,
        )
    query = _get(query_id)
    assert query.terminal_at is None
    assert query.status == QUERY_PUBLISHING
    assert "publish_error" in (_subjobs(query_id)[0]["result_summary"] or {})
    assert _notifications(query_id) == 0
    with DBSession() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(Obj).where(Obj.id == external_id)
        ) == 0

    # Restore access: the next pass publishes and terminalizes exactly once.
    with DBSession() as session:
        query = session.get(CatalogQuery, query_id)
        query.permission_snapshot = {
            **query.permission_snapshot,
            "target_group_ids": [gid],
        }
        session.commit()
    _drive(query_id, {"Gaia": _ok_adapter([])}, publisher=publish_query, ticks=2)
    query = _get(query_id)
    assert query.terminal_at is not None
    assert query.status.startswith("completed")
    assert _notifications(query_id) == 1
    with DBSession() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(Obj).where(Obj.id == external_id)
        ) == 1


def test_crash_during_publish_is_idempotent(public_group, super_admin_token):
    gid = public_group.id

    allocation_id, _ = _make_allocation(super_admin_token, gid)
    id1, id2 = f"cq-c1-{uuid.uuid4().hex[:6]}", f"cq-c2-{uuid.uuid4().hex[:6]}"
    status, data = _post_query(
        super_admin_token, allocation_id, gid, "Gaia"
    )
    query_id = data["data"]["id"]
    adapters = {"Gaia": _ok_adapter([_candidate(id1), _candidate(id2)])}

    # Stage candidates without finalizing, as if the worker died after fetch.
    with DBSession() as session:
        claimed = claim_subjobs(session, "crash-w", lease_seconds=300)
        sid = claimed[0].subjob_id

    process_subjob(DBSession, sid, "crash-w", adapters, CONFIG)
    assert _get(query_id).terminal_at is None
    assert _subjobs(query_id)[0]["status"] == "succeeded"

    state = {"calls": 0}

    def crashing_publisher(qid):
        state["calls"] += 1
        if state["calls"] == 1:
            user_id = _get(qid).requester_id
            session = new_session()
            try:
                from skyportal.models import User

                user = session.scalar(sa.select(User).where(User.id == user_id))
                candidate = next(
                    c for c in _candidate_rows(qid) if c["external_id"] == id1
                )
                live = session.get(CatalogQueryCandidate, candidate["id"])
                _publish_candidate(
                    session, live, user, [gid], utcnow_naive()
                )
            finally:
                session.close()
            raise RuntimeError("worker crashed mid-publish")
        return publish_query(qid)

    # First finalize pass only flips the state to publishing (no publish yet).
    with DBSession() as session:
        result = finalize_query(
            session, query_id, "crash-w", publisher=crashing_publisher
        )
    assert result is None
    assert _get(query_id).status == QUERY_PUBLISHING

    # Second pass publishes candidate 1, then the worker crashes mid-publish.
    with pytest.raises(RuntimeError, match="crashed mid-publish"):
        with DBSession() as session:
            finalize_query(session, query_id, "crash-w", publisher=crashing_publisher)
    assert state["calls"] == 1
    query = _get(query_id)
    assert query.terminal_at is None
    assert query.status == QUERY_PUBLISHING

    # The due-query sweep re-enters finalize and finishes without duplicating.
    for _ in range(3):
        tick(
            session_factory=DBSession,
            adapters={},
            publisher=publish_query,
            config=CONFIG,
        )
        if _get(query_id).terminal_at is not None:
            break
    query = _get(query_id)
    assert query.status.startswith("completed")
    with DBSession() as session:
        assert session.scalar(
            sa.select(sa.func.count())
            .select_from(Obj)
            .where(Obj.id.in_([id1, id2]))
        ) == 2
    assert _notifications(query_id) == 1


def test_concurrent_finalizers_publish_once(public_group, super_admin_token):
    gid = public_group.id

    allocation_id, _ = _make_allocation(super_admin_token, gid)
    external_id = f"cq-conc-{uuid.uuid4().hex[:6]}"
    status, data = _post_query(
        super_admin_token, allocation_id, gid, "Gaia"
    )
    query_id = data["data"]["id"]

    # Stage one candidate and mark the sub-request succeeded, as after a fetch.
    with DBSession() as session:
        claimed = claim_subjobs(session, "stage-w", lease_seconds=300)
        sid = claimed[0].subjob_id
    process_subjob(
        DBSession, sid, "stage-w", {"Gaia": _ok_adapter([_candidate(external_id)])}, CONFIG
    )
    with DBSession() as session:
        query = session.get(CatalogQuery, query_id)
        query.status = QUERY_PUBLISHING
        session.commit()

    entered = threading.Event()
    release = threading.Event()
    calls = []
    results = {}

    def blocking_publisher(qid):
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(timeout=10)
        return publish_query(qid)

    def worker(worker_id):
        session = new_session()
        try:
            results[threading.get_ident()] = finalize_query(
                session, query_id, worker_id, publisher=blocking_publisher
            )
        finally:
            session.close()

    threads = [
        threading.Thread(target=worker, args=("conc-w1",)),
        threading.Thread(target=worker, args=("conc-w2",)),
    ]
    for t in threads:
        t.start()
    assert entered.wait(timeout=5)
    # Give the loser time to hit the advisory lock and back out.
    deadline = utcnow_naive() + timedelta(seconds=5)
    while sum(1 for t in threads if not t.is_alive()) < 1:
        assert utcnow_naive() < deadline
        time.sleep(0.05)
    release.set()
    for t in threads:
        t.join()

    assert len(calls) == 1
    assert sum(1 for r in results.values() if r and r["became_terminal"]) == 1
    with DBSession() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(Obj).where(Obj.id == external_id)
        ) == 1
    assert _notifications(query_id) == 1
