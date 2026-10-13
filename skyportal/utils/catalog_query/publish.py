"""Publish validated, staged catalog-query candidates into live tables.

Idempotent and re-entrant: already-published rows are skipped, so a worker
crash during publication never duplicates sources, photometry or notifications.
"""

import asyncio
import base64

import pandas as pd
import sqlalchemy as sa
from baselayer.log import make_log

from skyportal.models import (
    CatalogQuery,
    CatalogQueryCandidate,
    Comment,
    Group,
    Obj,
    PhotometricSeries,
    User,
    new_session,
)
from skyportal.utils.naive_datetime import utcnow_naive
from skyportal.handlers.api.photometric_series import (
    post_photometric_series,
    update_photometric_series,
)
from skyportal.handlers.api.photometry import commit_external_photometry
from skyportal.handlers.api.source import post_source

from .jobs import PublishError

log = make_log("catalog_query_publish")


def _restricted_group_ids(payload, allowed_group_ids):
    """Drop embedded group ids the requester can no longer use."""
    if not isinstance(payload, dict) or "group_ids" not in payload:
        return payload
    payload = dict(payload)
    payload["group_ids"] = [
        gid for gid in payload["group_ids"] if int(gid) in allowed_group_ids
    ]
    return payload


def _publish_candidate(session, candidate, user, allowed_group_ids, now):
    existing = session.scalars(
        Obj.select(user).where(Obj.id == candidate.external_id)
    ).first()

    if existing is not None:
        obj_id = existing.id
        candidate.resolution = "existing"
    else:
        try:
            obj_id, _, _ = post_source(
                {
                    "id": candidate.external_id,
                    "ra": candidate.ra,
                    "dec": candidate.dec,
                    "group_ids": allowed_group_ids,
                },
                user.id,
                session,
            )
        except Exception as e:
            # Bad ID / missing coordinates / group issue: keep the row staged
            # but never block publication of the remaining candidates.
            candidate.validation_status = "invalid"
            candidate.validation_error = f"publish rejected: {e}"[:1000]
            candidate.resolution = "rejected"
            session.commit()
            return None
        candidate.resolution = "new"

    candidate.resolved_obj_id = obj_id

    for data_row in candidate.data_rows:
        if data_row.published_at is not None:
            continue
        if data_row.validation_status == "invalid":
            continue

        if data_row.kind == "photometry":
            payload = _restricted_group_ids(data_row.payload, allowed_group_ids)
            payload["obj_id"] = obj_id
            asyncio.run(commit_external_photometry(payload, user.id))
        elif data_row.kind == "photometric_series":
            payload = dict(data_row.payload)
            data_out = _restricted_group_ids(
                payload.get("data_out", {}), allowed_group_ids
            )
            data_out["obj_id"] = obj_id
            df = pd.DataFrame(payload.get("data", {}))
            try:
                post_photometric_series(data_out, df, {}, user, session)
            except Exception:
                existing_ps = session.scalars(
                    sa.select(PhotometricSeries).where(
                        PhotometricSeries.series_obj_id == obj_id,
                        PhotometricSeries.obj_id == obj_id,
                    )
                ).first()
                if existing_ps is None:
                    raise
                update_photometric_series(existing_ps, data_out, df, {}, user, session)
        data_row.published_at = now

    # Swift LSXPS detection spectra ride along as attachment comments.
    for spectrum in (candidate.raw_data or {}).get("spectra", []):
        groups = session.scalars(
            Group.select(user).where(Group.id.in_(allowed_group_ids))
        ).all()
        comment = Comment(
            text="Swift Detection Spectrum",
            obj_id=obj_id,
            attachment_name=spectrum["filename"],
            author=user,
            groups=groups,
            bot=True,
        )
        session.add(comment)
        comment.save_data(
            spectrum["filename"], base64.b64decode(spectrum["content_b64"])
        )

    candidate.published_at = now
    session.commit()
    return obj_id


def publish_query(query_id):
    """Publish every valid, unpublished candidate of one query.

    Returns summary lists. Raises PublishError(fatal=True) when the requester
    can no longer publish to any target group. Other exceptions propagate for
    the queue to retry (the query stays in the publishing state).
    """
    session = new_session()
    try:
        query = session.scalar(
            sa.select(CatalogQuery).where(CatalogQuery.id == query_id)
        )
        if query is None:
            raise PublishError(f"query {query_id} not found")

        user = session.scalar(sa.select(User).where(User.id == query.requester_id))
        if user is None:
            raise PublishError(f"requester {query.requester_id} not found")

        target_group_ids = set(
            (query.permission_snapshot or {}).get("target_group_ids") or []
        )
        accessible_group_ids = set()
        if target_group_ids:
            accessible_group_ids = {
                group.id
                for group in session.scalars(
                    Group.select(user).where(Group.id.in_(target_group_ids))
                ).all()
            }
        allowed_group_ids = sorted(target_group_ids & accessible_group_ids)
        if not allowed_group_ids:
            raise PublishError(
                "requester can no longer access any target group; staged "
                "results retained for a later retry"
            )

        candidates = session.scalars(
            sa.select(CatalogQueryCandidate)
            .where(
                CatalogQueryCandidate.query_id == query_id,
                CatalogQueryCandidate.published_at.is_(None),
            )
            .order_by(CatalogQueryCandidate.id)
        ).all()

        new_obj_ids, existing_obj_ids = [], []
        for candidate in candidates:
            if candidate.validation_status == "invalid":
                continue
            now = utcnow_naive()
            obj_id = _publish_candidate(
                session, candidate, user, allowed_group_ids, now
            )
            if obj_id is None:
                continue
            if candidate.resolution == "new":
                new_obj_ids.append(obj_id)
            else:
                existing_obj_ids.append(obj_id)

        return {
            "new_obj_ids": sorted(set(new_obj_ids)),
            "existing_obj_ids": sorted(set(existing_obj_ids)),
        }
    finally:
        session.close()
