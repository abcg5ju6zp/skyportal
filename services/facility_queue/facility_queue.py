import os
import socket
import time
from io import StringIO

import astropy.units as u
import numpy as np
import pandas as pd
import requests
import sqlalchemy as sa
from astropy.time import Time, TimeDelta
from requests.auth import HTTPBasicAuth

from baselayer.app.env import load_env
from baselayer.app.flow import Flow
from baselayer.app.models import init_db
from baselayer.log import make_log
from skyportal.models import (
    DBSession,
    FacilityTransaction,
    FacilityTransactionRequest,
    FollowupRequest,
)
from skyportal.models.facility_transaction import (
    CLAIM_LEASE_SECONDS,
    COMMAND_RETRIEVE,
    TRANSACTION_APPLIED,
    TRANSACTION_INDETERMINATE,
    TRANSACTION_REQUEST_CLAIMED,
    TRANSACTION_REQUEST_COMPLETE,
    TRANSACTION_REQUEST_PENDING,
    TRANSACTION_REQUEST_STALE,
    TRANSACTION_STALE,
    claim_facility_transaction_request,
    stable_command_id,
)
from skyportal.utils.naive_datetime import utcnow_naive

env, cfg = load_env()
log = make_log("facility_queue")

init_db(**cfg["database"])

ZTF_FORCED_URL = cfg["app.ztf_forced_endpoint"]
ZTF_PHOTOMETRY_CODES = {
    0: "Successful execution",
    56: "One or more epochs have photometry measurements that may be impacted by bad (including NaN'd) pixels",
    57: "One or more epochs had no reference image catalog source falling with 5 arcsec",
    58: "One or more epochs had a reference image PSF-catalog that does not exist in the archive",
    59: "One or more epochs may have suspect photometric uncertainties due to early creation date of difference image in production",
    60: "One or more epochs had upsampled diff-image PSF dimensions that were not odd integers",
    61: "One or more epochs had diff-image cutouts that were off the image or too close to an edge",
    62: "Requested start JD was before official survey start date [3/17/18] and was reset to 2018-03-17T00:00:00.0 UT",
    63: "No records (epochs) returned by database query",
    64: "Catastrophic error (see log output)",
    65: "Requested end JD is before official survey start date [3/17/18]",
    255: "Database connection or query execution error (see log output)",
}

request_session = requests.Session()
request_session.trust_env = (
    False  # Otherwise pre-existing netrc config will override auth headers
)

WAIT_TIME_BETWEEN_QUERIES = 120  # in seconds
CUTOFF_TIME_DAYS = 7  # max lookback time for requests to be processed

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

# Statuses after which no further polling is needed and a single Flow
# refresh is appropriate. In-progress strings stay quiet until resolved.
_TERMINAL_STATUS_MARKERS = (
    "committed to database",
    "no photometry",
)


def _serialize_requests_response(response):
    """Small JSON-safe snapshot of a requests.Response for the ledger."""
    try:
        content = response.text
    except Exception:  # noqa: BLE001 - best-effort audit snapshot
        content = None
    return {
        "url": response.url,
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "content": content[:5000] if isinstance(content, str) else content,
    }


def _record_transaction(
    session,
    req,
    followup_request,
    disposition,
    response=None,
):
    """Record the poll exchange against the fixed command version so the
    outcome stays queryable even when it cannot be applied."""
    response_dict = (
        _serialize_requests_response(response) if response is not None else None
    )
    transaction = FacilityTransaction(
        request={
            "method": req.method,
            "endpoint": req.endpoint,
            "data": req.data,
            "params": req.params,
            "headers": req.headers,
        },
        response=response_dict,
        followup_request=followup_request if followup_request is not None else None,
        followup_request_version=req.followup_request_version,
        allocation_id=req.allocation_id,
        obj_id=req.obj_id,
        action=req.action or COMMAND_RETRIEVE,
        command_id=req.command_id
        or (
            stable_command_id(
                req.followup_request_id,
                req.followup_request_version,
                req.action or COMMAND_RETRIEVE,
            )
            if req.followup_request_version is not None
            else None
        ),
        disposition=disposition,
        initiator_id=req.initiator_id,
    )
    session.add(transaction)


def _snapshot_matches(req, followup_request):
    """Poll results may only attach to the request version/allocation/
    target this command was fixed to."""
    return (
        (
            req.followup_request_version is None
            or followup_request.version == req.followup_request_version
        )
        and (
            req.allocation_id is None
            or followup_request.allocation_id == req.allocation_id
        )
        and (req.obj_id is None or followup_request.obj_id == req.obj_id)
    )


def _apply_status(session, req, followup_request, status, response=None):
    """Conditionally apply a polled status. Returns one of "applied",
    "unchanged", "stale". Records the exchange when stale so it remains
    queryable, and pushes a single Flow refresh on determined transitions."""
    result = session.execute(
        sa.update(FollowupRequest)
        .where(
            FollowupRequest.id == followup_request.id,
            FollowupRequest.version == req.followup_request_version,
            FollowupRequest.status != status,
        )
        .values(status=status)
    )
    if result.rowcount == 1:
        lowered = status.lower()
        if "error:" in lowered or any(
            marker in lowered for marker in _TERMINAL_STATUS_MARKERS
        ):
            try:
                Flow().push(
                    "*",
                    "skyportal/REFRESH_SOURCE",
                    payload={"obj_key": followup_request.obj.internal_key},
                )
            except Exception as e:  # noqa: BLE001 - refresh is best-effort
                log(f"Could not push Flow update for request {req.id}: {e}")
        return "applied"

    # Either the version moved on or this exact status was already set.
    current = session.execute(
        sa.select(FollowupRequest.version, FollowupRequest.status).where(
            FollowupRequest.id == followup_request.id
        )
    ).first()
    if current is not None and current.version == req.followup_request_version:
        return "unchanged"

    _record_transaction(session, req, followup_request, TRANSACTION_STALE, response)
    return "stale"


def _finalize_stale(session, req, followup_request, response=None):
    """A superseded version answered: stop polling without an error and
    without touching the current request."""
    _record_transaction(session, req, followup_request, TRANSACTION_STALE, response)
    req.status = TRANSACTION_REQUEST_STALE
    session.add(req)
    session.commit()
    log(
        f"Request {req.id}: response belongs to superseded version "
        f"{req.followup_request_version}; not applied"
    )


def _release_indeterminate(session, req, followup_request, response=None, note=""):
    """Local/partial failure: leave a queryable indeterminate transaction
    and requeue for a later poll instead of reaching an error terminal."""
    _record_transaction(
        session, req, followup_request, TRANSACTION_INDETERMINATE, response
    )
    req.status = TRANSACTION_REQUEST_PENDING
    req.last_query = utcnow_naive()
    session.add(req)
    session.commit()
    log(f"Request {req.id}: indeterminate ({note}); left queryable for retry")


def service():
    while True:
        queue = []
        try:
            with DBSession() as session:
                cutoff_time = Time.now() - TimeDelta(CUTOFF_TIME_DAYS * u.day)
                last_query_dt = Time.now() - TimeDelta(WAIT_TIME_BETWEEN_QUERIES * u.s)
                lease_cutoff = Time.now() - TimeDelta(CLAIM_LEASE_SECONDS * u.s)
                requests = (
                    session.query(FacilityTransactionRequest)
                    .where(
                        FacilityTransactionRequest.created_at >= cutoff_time.datetime,
                        sa.or_(
                            sa.and_(
                                FacilityTransactionRequest.status
                                == TRANSACTION_REQUEST_PENDING,
                                sa.or_(
                                    FacilityTransactionRequest.last_query
                                    < last_query_dt.datetime,
                                    FacilityTransactionRequest.last_query.is_(None),
                                    FacilityTransactionRequest.last_query
                                    == FacilityTransactionRequest.created_at,
                                ),
                            ),
                            sa.and_(
                                FacilityTransactionRequest.status
                                == TRANSACTION_REQUEST_CLAIMED,
                                sa.or_(
                                    FacilityTransactionRequest.claimed_at.is_(None),
                                    FacilityTransactionRequest.claimed_at
                                    < lease_cutoff.datetime,
                                ),
                            ),
                        ),
                    )
                    .all()
                )
                for req in requests:
                    if req.followup_request_id is not None:
                        queue.append(req.id)
        except Exception as e:  # noqa: BLE001 - retry next loop
            log(f"Error retrieving requests to process: {e}")
            time.sleep(15)
            continue

        if len(queue) == 0:
            # this is a retrieval queue service. Requests were sent before and
            # we are just waiting for the results. No rush to check the queue
            time.sleep(30)
            continue

        for req_id in queue:
            if req_id is None:
                log("No request ID found in queue. Continuing to next request.")
                continue

            time.sleep(5)
            with DBSession() as session:
                try:
                    req = session.scalars(
                        sa.select(FacilityTransactionRequest).where(
                            FacilityTransactionRequest.id == req_id
                        )
                    ).first()
                    if req is None:
                        log(f"Facility transaction request {req_id} not found.")
                        continue

                    # Atomic claim: only one worker may send this command;
                    # a crashed worker's claim is reclaimed after the lease.
                    if not claim_facility_transaction_request(
                        session, req.id, WORKER_ID
                    ):
                        log(f"Request {req.id} already claimed; skipping")
                        continue
                    session.commit()

                    followup_request = session.scalars(
                        sa.select(FollowupRequest).where(
                            FollowupRequest.id == req.followup_request_id
                        )
                    ).first()
                    if followup_request is None:
                        log(f"Follow-up request {req.followup_request_id} not found.")
                        continue

                    # Rows created before versioning carry no snapshot; fix
                    # them to whatever is current the first time we poll.
                    if req.followup_request_version is None:
                        req.followup_request_version = followup_request.version
                        req.allocation_id = followup_request.allocation_id
                        req.obj_id = followup_request.obj_id
                        req.action = req.action or COMMAND_RETRIEVE
                        session.add(req)
                        session.commit()

                    # Serialize against submit/update/delete commands for
                    # the duration of the poll application.
                    followup_request = session.scalars(
                        sa.select(FollowupRequest)
                        .where(FollowupRequest.id == req.followup_request_id)
                        .with_for_update()
                    ).first()
                    if followup_request is None:
                        continue
                    if not _snapshot_matches(req, followup_request):
                        _finalize_stale(session, req, followup_request)
                        continue

                    instrument = followup_request.allocation.instrument
                    altdata = followup_request.allocation.altdata

                    if instrument.name == "ATLAS":
                        from skyportal.facility_apis.atlas import commit_photometry

                        response = request_session.request(
                            req.method,
                            req.endpoint,
                            json=req.data,
                            params=req.params,
                            headers=req.headers,
                        )

                        if response.status_code == 200:
                            try:
                                json_response = response.json()
                            except ValueError:
                                _release_indeterminate(
                                    session,
                                    req,
                                    followup_request,
                                    response,
                                    note="no JSON data returned",
                                )
                                continue

                            if json_response["finishtimestamp"]:
                                followup_request.status = (
                                    "Committing photometry to database"
                                )
                                session.add(followup_request)
                                if json_response["result_url"] is not None:
                                    try:
                                        commit_photometry(
                                            json_response,
                                            altdata,
                                            followup_request.id,
                                            instrument.id,
                                            followup_request.requester.id,
                                            parent_session=session,
                                            duplicates="update",
                                        )
                                    except Exception as e:  # noqa: BLE001 - requeue
                                        session.rollback()
                                        _release_indeterminate(
                                            session,
                                            req,
                                            followup_request,
                                            response,
                                            note=f"error committing photometry: {e}",
                                        )
                                        continue
                                else:
                                    # No result file: record it rather than
                                    # leaving the request mid-commit.
                                    if (
                                        _apply_status(
                                            session,
                                            req,
                                            followup_request,
                                            "No photometry to commit to database",
                                            response,
                                        )
                                        == "stale"
                                    ):
                                        _finalize_stale(
                                            session, req, followup_request, response
                                        )
                                        continue

                                if not _snapshot_matches(req, followup_request):
                                    _finalize_stale(
                                        session, req, followup_request, response
                                    )
                                    continue
                                _record_transaction(
                                    session,
                                    req,
                                    followup_request,
                                    TRANSACTION_APPLIED,
                                    response,
                                )
                                req.status = TRANSACTION_REQUEST_COMPLETE
                                session.add(req)
                                session.commit()
                                log(f"Job with ID {req.id} completed")
                                continue

                            elif json_response["starttimestamp"]:
                                status = f"Job is running (started at {json_response['starttimestamp']})"
                                outcome = _apply_status(
                                    session,
                                    req,
                                    followup_request,
                                    status,
                                )
                                if outcome == "stale":
                                    _finalize_stale(
                                        session, req, followup_request, response
                                    )
                                    continue
                                req.last_query = utcnow_naive()
                                session.add(req)
                                session.commit()
                                log(f"Job {req.id}: {status}")
                            else:
                                status = f"Waiting for job to start (queued at {json_response['timestamp']})"
                                outcome = _apply_status(
                                    session,
                                    req,
                                    followup_request,
                                    status,
                                )
                                if outcome == "stale":
                                    _finalize_stale(
                                        session, req, followup_request, response
                                    )
                                    continue
                                req.last_query = utcnow_naive()
                                session.add(req)
                                session.commit()
                                log(f"Job {req.id}: {status}")
                        else:
                            outcome = _apply_status(
                                session,
                                req,
                                followup_request,
                                f"error: {response.text}",
                                response,
                            )
                            if outcome == "stale":
                                _finalize_stale(
                                    session, req, followup_request, response
                                )
                                continue
                            req.last_query = utcnow_naive()
                            session.add(req)
                            session.commit()

                    elif instrument.name == "ZTF":
                        from skyportal.facility_apis.ztf import commit_photometry

                        keys = ["ra", "dec", "jdstart", "jdend"]

                        response = request_session.request(
                            req.method,
                            req.endpoint,
                            json=req.data,
                            params=req.params,
                            headers=req.headers,
                            auth=HTTPBasicAuth(
                                altdata["ipac_http_user"], altdata["ipac_http_password"]
                            ),
                        )

                        if "Zero records returned" in str(response.text):
                            log(
                                "Found no records yet for this ZTF forced photometry account."
                            )
                            req.last_query = utcnow_naive()
                            session.add(req)
                            session.commit()
                            continue
                        elif response.status_code == 200:
                            try:
                                df_result = pd.read_html(StringIO(response.text))[0]
                            except (ImportError, ValueError) as e:
                                _release_indeterminate(
                                    session,
                                    req,
                                    followup_request,
                                    response,
                                    note=f"unparseable job table: {e}",
                                )
                                continue
                            df_result.rename(
                                inplace=True,
                                columns={"startJD": "jdstart", "endJD": "jdend"},
                            )
                            df_result = df_result.replace({np.nan: None})
                            if not set(keys).issubset(df_result.columns):
                                status = "In progress: RA, Dec, jdstart, and jdend required in response."
                                outcome = _apply_status(
                                    session, req, followup_request, status, response
                                )
                                if outcome == "stale":
                                    _finalize_stale(
                                        session, req, followup_request, response
                                    )
                                    continue
                                req.last_query = utcnow_naive()
                                session.add(req)
                                session.commit()
                                continue

                            index_match = None
                            for index, row in df_result.iterrows():
                                if all(
                                    np.isclose(row[key], req.data[key]) for key in keys
                                ):
                                    index_match = index
                                    break
                            if index_match is None:
                                status = "In progress: No matching response from forced photometry service. Waiting for database update."
                                outcome = _apply_status(
                                    session, req, followup_request, status, response
                                )
                                if outcome == "stale":
                                    _finalize_stale(
                                        session, req, followup_request, response
                                    )
                                    continue
                                req.last_query = utcnow_naive()
                                session.add(req)
                                session.commit()
                                continue

                            row = df_result.loc[index_match]
                            if row["lightcurve"] is None:
                                status = "In progress: Light curve not yet available. Waiting for it to complete."
                                outcome = _apply_status(
                                    session, req, followup_request, status, response
                                )
                                if outcome == "stale":
                                    _finalize_stale(
                                        session, req, followup_request, response
                                    )
                                    continue
                                req.last_query = utcnow_naive()
                                session.add(req)
                                session.commit()
                                continue

                            lightcurve = row["lightcurve"]
                            exitcode = row["exitcode"]
                            exitcode_text = ZTF_PHOTOMETRY_CODES[exitcode]

                            if exitcode in [63, 64, 65, 255]:
                                status = f"No photometry available: {exitcode_text}"
                                outcome = _apply_status(
                                    session, req, followup_request, status, response
                                )
                                if outcome == "stale":
                                    _finalize_stale(
                                        session, req, followup_request, response
                                    )
                                    continue
                                _record_transaction(
                                    session,
                                    req,
                                    followup_request,
                                    TRANSACTION_APPLIED,
                                    response,
                                )
                                req.last_query = utcnow_naive()
                                req.status = TRANSACTION_REQUEST_COMPLETE
                                session.add(req)
                                session.commit()
                                log(
                                    f"Job with ID {req.id} has no forced photometry: {exitcode_text}"
                                )
                            else:
                                dataurl = f"{ZTF_FORCED_URL}/{lightcurve}"
                                try:
                                    commit_photometry(
                                        dataurl,
                                        altdata,
                                        followup_request.id,
                                        instrument.id,
                                        followup_request.requester.id,
                                        parent_session=session,
                                        duplicates="update",
                                    )
                                except Exception as e:  # noqa: BLE001 - requeue
                                    session.rollback()
                                    if "Failed to commit photometry" in str(e):
                                        # Persistent data-side failure: keep
                                        # queryable for another attempt
                                        # rather than an error terminal.
                                        _release_indeterminate(
                                            session,
                                            req,
                                            followup_request,
                                            response,
                                            note=str(e),
                                        )
                                    else:
                                        req2 = session.scalars(
                                            sa.select(FacilityTransactionRequest).where(
                                                FacilityTransactionRequest.id == req.id
                                            )
                                        ).first()
                                        fr2 = session.scalars(
                                            sa.select(FollowupRequest)
                                            .where(
                                                FollowupRequest.id
                                                == req.followup_request_id
                                            )
                                            .with_for_update()
                                        ).first()
                                        if fr2 is None or not _snapshot_matches(
                                            req2, fr2
                                        ):
                                            _finalize_stale(
                                                session, req2, fr2, response
                                            )
                                            continue
                                        status = "In progress: Light curve not yet available. Waiting for it to complete."
                                        _apply_status(
                                            session, req2, fr2, status, response
                                        )
                                        req2.last_query = utcnow_naive()
                                        session.add(req2)
                                        session.commit()
                                    continue
                                if not _snapshot_matches(req, followup_request):
                                    _finalize_stale(
                                        session, req, followup_request, response
                                    )
                                    continue
                                _record_transaction(
                                    session,
                                    req,
                                    followup_request,
                                    TRANSACTION_APPLIED,
                                    response,
                                )
                                req.status = TRANSACTION_REQUEST_COMPLETE
                                session.add(req)
                                session.commit()
                                log(f"Job with ID {req.id} completed")
                        elif "Error: database is busy; try again a minute later." in (
                            response.text
                        ):
                            status = "In progress: forced photometry database is busy; trying again in 2 minutes."
                            outcome = _apply_status(
                                session, req, followup_request, status, response
                            )
                            if outcome == "stale":
                                _finalize_stale(
                                    session, req, followup_request, response
                                )
                                continue
                            req.last_query = utcnow_naive()
                            session.add(req)
                            session.commit()
                        else:
                            outcome = _apply_status(
                                session,
                                req,
                                followup_request,
                                f"error: {response.text}",
                                response,
                            )
                            if outcome == "stale":
                                _finalize_stale(
                                    session, req, followup_request, response
                                )
                                continue
                            req.last_query = utcnow_naive()
                            session.add(req)
                            session.commit()
                    else:
                        log(f"Job {req.id}: API for {instrument.name} unknown")

                except Exception as e:  # noqa: BLE001 - requeue below
                    log(f"Error processing follow-up request {req_id}: {e}")
                    try:
                        session.rollback()
                        # Anything unexpected at the transport/parse layer
                        # stays queryable and retried, never an error
                        # terminal for the follow-up request.
                        with DBSession() as cleanup_session:
                            req = cleanup_session.scalars(
                                sa.select(FacilityTransactionRequest).where(
                                    FacilityTransactionRequest.id == req_id
                                )
                            ).first()
                            followup_request = None
                            if req is not None and req.followup_request_id:
                                followup_request = cleanup_session.scalars(
                                    sa.select(FollowupRequest).where(
                                        FollowupRequest.id == req.followup_request_id
                                    )
                                ).first()
                            if req is not None:
                                _release_indeterminate(
                                    cleanup_session,
                                    req,
                                    followup_request,
                                    note=str(e),
                                )
                    except Exception as cleanup_error:  # noqa: BLE001 - logged
                        log(
                            f"Could not requeue request {req_id} after error: "
                            f"{cleanup_error}"
                        )


if __name__ == "__main__":
    try:
        service()
    except Exception as e:
        log(f"Error occurred in service: {e}")
        raise
