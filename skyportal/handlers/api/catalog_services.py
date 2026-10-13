import asyncio
import functools
import glob
import hashlib
import json
import tempfile
import time

import arrow
import numpy as np
import sqlalchemy as sa
import swifttools.ukssdc.query as uq
from astropy.table import Table
from astropy.time import Time, TimeDelta
from marshmallow.exceptions import ValidationError
from skyportal_py_models.catalog_queries import (
    CatalogQueryPostBody,
    GaiaPhotometricAlertsQueryPostBody,
    SwiftLSXPSQueryPostBody,
    TessTransientsQueryPostBody,
)
from sqlalchemy.orm import selectinload
from tornado.ioloop import IOLoop

from baselayer.app.access import auth_or_token
from baselayer.app.env import load_env
from baselayer.log import make_log

from ...models import (
    Allocation,
    CatalogQuery,
    CatalogQueryCandidate,
    CatalogQuerySubjob,
    Comment,
    Group,
    Localization,
    Obj,
    PhotometricSeries,
    Telescope,
    User,
    new_session,
)
from ...models.schema import CatalogQueryPost
from ...utils.catalog_query.jobs import (
    QUERY_ACTIVE_STATUSES,
    QUERY_TERMINAL_STATUSES,
    finalize_query,
    request_cancel,
)
from ...utils.catalog_query.parsers import (
    gaia_lightcurve_to_df,
    swift_lightcurves_to_df,
    tess_lightcurve_to_df,
)
from ...utils.data_access import accessible_group_ids_async
from ..base import BaseHandler
from .photometric_series import post_photometric_series, update_photometric_series
from .photometry import commit_external_photometry
from .source import post_source

_, cfg = load_env()


TESS_URL = cfg["app.tess_endpoint"]

log = make_log("api/catalogs")


ALLOWED_CATALOGS = ("ZTF-Fink", "LSXPS", "Gaia", "TESS")


def _normalize_catalogs(payload):
    catalogs = payload.get("catalogNames")
    if catalogs is None:
        single = payload.get("catalogName")
        catalogs = [single] if single else []
    if not catalogs:
        raise ValueError("catalogName required in query payload")
    catalogs = [str(c) for c in catalogs]
    unknown = sorted(set(catalogs) - set(ALLOWED_CATALOGS))
    if unknown:
        raise ValueError(f"Unknown catalog(s): {', '.join(unknown)}")
    return catalogs


def _normalize_geometry(body_data, payload):
    def pick(key):
        value = body_data.get(key)
        return value if value is not None else payload.get(key)

    ra, dec, radius = pick("ra"), pick("dec"), pick("radius")
    if (ra is None) != (dec is None):
        raise ValueError("ra and dec must be provided together")
    if ra is not None:
        if not 0.0 <= float(ra) < 360.0:
            raise ValueError("ra must lie in [0, 360) degrees")
        if not -90.0 <= float(dec) <= 90.0:
            raise ValueError("dec must lie in [-90, 90] degrees")
    if radius is not None and float(radius) <= 0.0:
        raise ValueError("radius must be positive")
    return ra, dec, radius


def _build_request_key(
    requester_id, allocation_id, payload, catalogs, ra, dec, radius, publish_results
):
    material = {
        "requester_id": requester_id,
        "allocation_id": allocation_id,
        "catalogs": sorted(catalogs),
        "geometry": {"ra": ra, "dec": dec, "radius": radius},
        "publish_results": publish_results,
        "dates": {
            key: payload.get(key)
            for key in (
                "startDate",
                "endDate",
                "localizationDateobs",
                "localizationName",
                "localizationCumprob",
                "gcnevent_id",
            )
        },
    }
    encoded = json.dumps(material, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class CatalogQueryHandler(BaseHandler):
    @auth_or_token
    async def post(self, *, body: CatalogQueryPostBody = None):
        """
        ---
        summary: Submit catalog queries
        description: |
          Persist a catalog query job (one sub-request per selected catalog)
          before any remote request is made. The catalog query queue worker
          claims sub-requests, stages validated candidates, and publishes them.
        tags:
          - gcn event catalog queries
        responses:
          200:
            content:
              application/json:
                schema: Success
          400:
            content:
              application/json:
                schema: Error
        """
        body = self.parse_body(CatalogQueryPostBody)
        body_data = body.model_dump(exclude_unset=True)

        try:
            data = CatalogQueryPost.load(
                {
                    key: body_data[key]
                    for key in ("allocation_id", "payload", "status", "target_group_ids")
                    if key in body_data
                }
            )
        except ValidationError as e:
            return self.error(
                f"Invalid / missing parameters: {e.normalized_messages()}"
            )

        payload = data.get("payload") or {}
        try:
            catalogs = _normalize_catalogs(payload)
            ra, dec, radius = _normalize_geometry(body_data, payload)
        except ValueError as e:
            return self.error(str(e))
        publish_results = (
            body_data["publish_results"]
            if body_data.get("publish_results") is not None
            else bool(payload.get("publish_results", True))
        )

        for key in ("startDate", "endDate"):
            if payload.get(key):
                try:
                    arrow.get(payload[key].strip())
                except Exception as e:
                    return self.error(f"Invalid {key}: {e}")

        user_id = self.associated_user_object.id
        allocation_id = int(data["allocation_id"])

        async with self.AsyncSession() as session:
            allocation = await session.scalar(
                sa.select(Allocation).where(Allocation.id == allocation_id)
            )
            if allocation is None:
                return self.error(f"Could not find allocation {allocation_id}")

            # The localization lookup must honor the GcnEvent group restriction:
            # a requester must not drive a fetch in a restricted error region.
            if payload.get("localizationDateobs") and payload.get("localizationName"):
                localization = await session.scalar(
                    Localization.select(self.current_user).where(
                        Localization.dateobs == payload["localizationDateobs"],
                        Localization.localization_name == payload["localizationName"],
                    )
                )
                if localization is None:
                    return self.error(
                        "Inaccessible or unknown localization "
                        f"{payload['localizationName']} @ "
                        f"{payload['localizationDateobs']}"
                    )

            group_ids = list(data.get("target_group_ids") or [])
            group_ids.append(allocation.group_id)
            groups = (
                await session.scalars(
                    Group.select(self.current_user).where(Group.id.in_(group_ids))
                )
            ).all()
            target_group_ids = sorted({group.id for group in groups})

            accessible_group_ids = await accessible_group_ids_async(
                self.current_user, session
            )
            role_ids = (
                await session.execute(
                    sa.text(
                        "SELECT role_id FROM user_roles WHERE user_id = :user_id"
                    ),
                    {"user_id": user_id},
                )
            ).scalars().all()

            request_key = _build_request_key(
                user_id,
                allocation_id,
                payload,
                catalogs,
                ra,
                dec,
                radius,
                publish_results,
            )
            existing = await session.scalar(
                sa.select(CatalogQuery).where(
                    CatalogQuery.request_key == request_key,
                    CatalogQuery.status.in_(QUERY_ACTIVE_STATUSES),
                )
            )
            if existing is not None:
                return self.success(data={"id": existing.id, "deduplicated": True})

            catalog_query = CatalogQuery(
                requester_id=user_id,
                allocation_id=allocation_id,
                payload=payload,
                target_groups=groups,
                status="pending",
                ra=ra,
                dec=dec,
                radius=radius,
                publish_results=publish_results,
                permission_snapshot={
                    "user_id": user_id,
                    "is_admin": bool(self.current_user.is_admin),
                    "role_ids": list(role_ids),
                    "accessible_group_ids": list(accessible_group_ids),
                    "target_group_ids": target_group_ids,
                    "allocation_group_id": allocation.group_id,
                },
                request_key=request_key,
            )
            catalog_query.subjobs = [
                CatalogQuerySubjob(
                    catalog_name=catalog_name,
                    request_key=f"{request_key}:{catalog_name}",
                )
                for catalog_name in catalogs
            ]
            session.add(catalog_query)
            await session.commit()

            try:
                self.push_all(
                    action="skyportal/REFRESH_GCNEVENT_CATALOG_QUERIES",
                    payload={
                        "gcnEvent_dateobs": payload.get("localizationDateobs")
                    },
                )
            except Exception:
                pass

            return self.success(
                data={"id": catalog_query.id, "query_uid": catalog_query.query_uid}
            )


def _serialize_attempt(attempt):
    return {
        "attempt_no": attempt.attempt_no,
        "state": attempt.state,
        "remote_handle": attempt.remote_handle,
        "request_summary": attempt.request_summary,
        "response_summary": attempt.response_summary,
        "error": attempt.error,
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
    }


def _serialize_subjob(subjob):
    return {
        "id": subjob.id,
        "catalog_name": subjob.catalog_name,
        "status": subjob.status,
        "request_key": subjob.request_key,
        "claimed_by": subjob.claimed_by,
        "claimed_at": subjob.claimed_at,
        "last_heartbeat": subjob.last_heartbeat,
        "lease_expires": subjob.lease_expires,
        "attempts_count": subjob.attempts_count,
        "pagination": subjob.pagination,
        "result_summary": subjob.result_summary,
        "error": subjob.error,
        "confirmed_at": subjob.confirmed_at,
        "attempts": [_serialize_attempt(a) for a in subjob.attempts],
    }


def _serialize_catalog_query(query):
    candidates = query.candidates
    published_ids = sorted(
        {
            candidate.resolved_obj_id
            for candidate in candidates
            if candidate.published_at is not None and candidate.resolved_obj_id
        }
    )
    return {
        "id": query.id,
        "query_uid": query.query_uid,
        "status": query.status,
        "payload": query.payload,
        "ra": query.ra,
        "dec": query.dec,
        "radius": query.radius,
        "publish_results": query.publish_results,
        "permission_snapshot": query.permission_snapshot,
        "cancel_requested": query.cancel_requested,
        "terminal_at": query.terminal_at,
        "created_at": query.created_at,
        "allocation_id": query.allocation_id,
        "requester_id": query.requester_id,
        "published_obj_ids": published_ids,
        "candidate_counts": {
            "total": len(candidates),
            "valid": sum(c.validation_status == "valid" for c in candidates),
            "invalid": sum(c.validation_status == "invalid" for c in candidates),
            "published": sum(c.published_at is not None for c in candidates),
        },
        "subjobs": [_serialize_subjob(s) for s in query.subjobs],
    }


class CatalogQueryIDHandler(BaseHandler):
    @auth_or_token
    async def get(self, catalog_query_id):
        """
        ---
        summary: Get a catalog query with sub-requests and attempts
        tags:
          - gcn event catalog queries
        responses:
          200:
            content:
              application/json:
                schema: Success
        """
        async with self.AsyncSession() as session:
            query = await session.scalar(
                CatalogQuery.select(session.user_or_token)
                .where(CatalogQuery.id == int(catalog_query_id))
                .options(
                    selectinload(CatalogQuery.subjobs)
                    .selectinload(CatalogQuerySubjob.attempts),
                    selectinload(CatalogQuery.candidates),
                )
            )
            if query is None:
                return self.error("Catalog query not found", status=404)
            return self.success(data=_serialize_catalog_query(query))


class CatalogQueryCandidatesHandler(BaseHandler):
    @auth_or_token
    async def get(self, catalog_query_id):
        """
        ---
        summary: List staged candidates of a catalog query
        tags:
          - gcn event catalog queries
        responses:
          200:
            content:
              application/json:
                schema: Success
        """
        try:
            page_number = max(1, int(self.get_query_argument("pageNumber", "1")))
            num_per_page = min(
                500, max(1, int(self.get_query_argument("numPerPage", "100")))
            )
        except ValueError:
            return self.error("Invalid pagination parameters", status=400)

        async with self.AsyncSession() as session:
            query = await session.scalar(
                CatalogQuery.select(session.user_or_token)
                .where(CatalogQuery.id == int(catalog_query_id))
                .options(
                    selectinload(CatalogQuery.candidates).selectinload(
                        CatalogQueryCandidate.data_rows
                    )
                )
            )
            if query is None:
                return self.error("Catalog query not found", status=404)

            candidates = sorted(query.candidates, key=lambda c: c.id)
            total = len(candidates)
            page = candidates[
                (page_number - 1)
                * num_per_page : page_number
                * num_per_page
            ]
            return self.success(
                data={
                    "candidates": [
                        {
                            "id": c.id,
                            "subjob_id": c.subjob_id,
                            "catalog_name": c.catalog_name,
                            "external_id": c.external_id,
                            "ra": c.ra,
                            "dec": c.dec,
                            "raw_data": c.raw_data,
                            "validation_status": c.validation_status,
                            "validation_error": c.validation_error,
                            "resolution": c.resolution,
                            "resolved_obj_id": c.resolved_obj_id,
                            "published_at": c.published_at,
                            "data": [
                                {
                                    "id": d.id,
                                    "kind": d.kind,
                                    "validation_status": d.validation_status,
                                    "validation_error": d.validation_error,
                                    "published_at": d.published_at,
                                }
                                for d in c.data_rows
                            ],
                        }
                        for c in page
                    ],
                    "total": total,
                    "pageNumber": page_number,
                    "numPerPage": num_per_page,
                }
            )


class CatalogQueryCancelHandler(BaseHandler):
    @auth_or_token
    async def post(self, catalog_query_id):
        """
        ---
        summary: Cancel a catalog query
        description: |
          Requests cancellation. Only unconfirmed sub-requests and the publish
          phase are affected; confirmed staged results are retained. Terminal
          queries return 409.
        tags:
          - gcn event catalog queries
        responses:
          200:
            content:
              application/json:
                schema: Success
        """
        catalog_query_id = int(catalog_query_id)
        async with self.AsyncSession() as session:
            query = await session.scalar(
                CatalogQuery.select(session.user_or_token, mode="update").where(
                    CatalogQuery.id == catalog_query_id
                )
            )
            if query is None:
                readable = await session.scalar(
                    CatalogQuery.select(session.user_or_token).where(
                        CatalogQuery.id == catalog_query_id
                    )
                )
                if readable is None:
                    return self.error("Catalog query not found", status=404)
                return self.error("Insufficient permissions to cancel", status=403)
            if query.terminal_at is not None or query.status in QUERY_TERMINAL_STATUSES:
                return self.error(
                    f"Catalog query already terminal ({query.status})", status=409
                )

            await session.run_sync(lambda s: request_cancel(s, catalog_query_id))

        # Finalize immediately when no live claim holds the query; claimed
        # sub-requests are settled by the queue worker. Cancellation never
        # reaches the publish branch, so invoking the publisher is safe.
        def _settle():
            from ...utils.catalog_query.publish import publish_query

            with new_session() as sync_session:
                return finalize_query(
                    sync_session,
                    catalog_query_id,
                    "cancel_api",
                    publisher=publish_query,
                )

        IOLoop.current().run_in_executor(None, _settle)
        return self.success(data={"id": catalog_query_id, "cancelled": True})


class SwiftLSXPSQueryHandler(BaseHandler):
    @auth_or_token
    async def post(self, *, body: SwiftLSXPSQueryPostBody = None):
        """
        ---
        summary: Post Swift LSXPS objects as sources
        description: |
            Get Swift LSXPS objects and post them as sources.
            Repeated posting will skip the existing source.
        tags:
            - catalogs
        responses:
          200:
            content:
              application/json:
                schema: Success
          400:
            content:
              application/json:
                schema: Error
        """
        body = self.parse_body(SwiftLSXPSQueryPostBody)

        telescope_name = (
            body.telescope_name if body.telescope_name is not None else "Swift"
        )
        group_ids = body.groupIDs

        async with self.AsyncSession() as session:
            if group_ids is None:
                group_ids = await accessible_group_ids_async(self.current_user, session)

            telescope = await session.scalar(
                Telescope.select(session.user_or_token)
                .options(selectinload(Telescope.instruments))
                .where(Telescope.nickname == telescope_name)
            )
            if telescope is None:
                return self.error(f"Expected a Telescope named {telescope_name}")
            instrument = telescope.instruments[0]

            fetch_tr = functools.partial(
                fetch_swift_transients,
                instrument.id,
                self.associated_user_object.id,
                group_ids,
            )

            IOLoop.current().run_in_executor(None, fetch_tr)

            return self.success()


def fetch_swift_transients(instrument_id, user_id, group_ids):
    """Fetch Swift XRT transients.
    instrument_id : int
        ID of the instrument
    user_id : int
        ID of the User
    group_id : List[int]
        List of group IDs to save to
    """

    session = new_session()

    obj_ids = []

    try:
        user = session.scalar(sa.select(User).where(User.id == user_id))

        groups = session.scalars(
            Group.select(user).where(Group.id.in_(group_ids))
        ).all()

        with tempfile.TemporaryDirectory() as tmpdirname:
            q = uq.SXPSQuery(silent=True, cat="LSXPS", table="transients")
            q.addFilter(("Classification", "=", 1))
            q.submit()
            q.getDetails(byName=True)
            q.getLightCurves(byName=True, timeFormat="MJD", binning="obs")
            q.getSpectra(byName=True, specType="Discovery")
            q.saveSpectra(destDir=tmpdirname)

            obj_ids = []
            for transient in q.transientDetails:
                ra = q.transientDetails[transient].pop("RA")
                dec = q.transientDetails[transient].pop("Decl")
                obj_name = (
                    q.transientDetails[transient].pop("IAUName").replace(" ", "-")
                )
                data = {"ra": ra, "dec": dec, "id": obj_name}
                s = session.scalars(Obj.select(user).where(Obj.id == obj_name)).first()
                if s is None:
                    data["group_ids"] = group_ids
                    (obj_id,) = post_source(data, user_id, session)
                    obj_ids.append(obj_id)
                else:
                    obj_id = s.id

                if transient in q.lightCurves:
                    df = swift_lightcurves_to_df(q.lightCurves[transient])
                    if df is not None:
                        df["ra"] = ra
                        df["dec"] = dec
                        df["filter"] = "swiftxrt"
                        df["magsys"] = "ab"
                        df["zp"] = 0.0
                        df = df.replace({np.nan: None})

                        data_out = {
                            "obj_id": obj_id,
                            "instrument_id": instrument_id,
                            "group_ids": [g.id for g in user.accessible_groups],
                            **df.to_dict(orient="list"),
                        }

                        if len(df.index) > 0:
                            # async add_external_photometry, bridged from this sync executor;
                            # obj already committed above so the bridge's session sees it.
                            asyncio.run(commit_external_photometry(data_out, user_id))
                            log(f"Photometry committed to database for {obj_id}")
                        else:
                            log(f"No photometry to commit to database for {obj_id}")

                if transient in q.spectra:
                    filenames = glob.glob(f"{tmpdirname}/{transient}/interval*")
                    for filename in filenames:
                        attachment_name = filename.split("/")[-1]
                        with open(filename, "rb") as f:
                            data_to_disk = f.read()

                        comment = Comment(
                            text="Swift Detection Spectrum",
                            obj_id=obj_id,
                            attachment_name=attachment_name,
                            author=user,
                            groups=groups,
                            bot=True,
                        )
                        session.add(comment)
                        if data_to_disk is not None:
                            comment.save_data(attachment_name, data_to_disk)

            session.commit()
        return obj_ids

    except Exception as e:
        return log(f"Unable to commit Swift XRT transient catalog: {e}")
    finally:
        session.close()


class GaiaPhotometricAlertsQueryHandler(BaseHandler):
    @auth_or_token
    async def post(self, *, body: GaiaPhotometricAlertsQueryPostBody = None):
        """
        ---
        summary: Post Gaia Photometric Alert as sources
        description: |
            Get Gaia Photometric Alert objects and post them as sources.
            Repeated posting will skip the existing source.
        tags:
            - catalogs
        responses:
          200:
            content:
              application/json:
                schema: Success
          400:
            content:
              application/json:
                schema: Error
        """
        body = self.parse_body(GaiaPhotometricAlertsQueryPostBody)

        telescope_name = (
            body.telescope_name if body.telescope_name is not None else "Gaia"
        )
        start_date = body.startDate
        end_date = body.endDate
        group_ids = body.groupIDs

        if start_date is not None:
            start_date = Time(arrow.get(start_date.strip()).datetime)
        if end_date is not None:
            end_date = Time(arrow.get(end_date.strip()).datetime)

        payload = {"start_date": start_date, "end_date": end_date}

        async with self.AsyncSession() as session:
            if group_ids is None:
                group_ids = await accessible_group_ids_async(self.current_user, session)

            telescope = await session.scalar(
                Telescope.select(session.user_or_token)
                .options(selectinload(Telescope.instruments))
                .where(Telescope.nickname == telescope_name)
            )
            if telescope is None:
                return self.error(f"Expected a Telescope named {telescope_name}")
            instrument = telescope.instruments[0]

            fetch_tr = functools.partial(
                fetch_gaia_transients,
                instrument.id,
                self.associated_user_object.id,
                payload,
            )

            IOLoop.current().run_in_executor(None, fetch_tr)

            return self.success()


def fetch_gaia_transients(instrument_id, user_id, group_ids, payload):
    """Fetch Gaia Photometric Alert transients.
    instrument_id : int
        ID of the instrument
    user_id : int
        ID of the User
    group_id : List[int]
        List of group IDs to save to
    payload : dict
        Dictionary containing filtering parameters
    """

    session = new_session()

    obj_ids = []

    lightcurve_url = "https://gsaweb.ast.cam.ac.uk/alerts/alert"
    alert_url = "http://gsaweb.ast.cam.ac.uk/alerts/alerts.csv"

    try:
        file_read = False
        nretries = 0

        while not file_read and nretries < 10:
            try:
                table = Table.read(alert_url, format="csv")
                file_read = True
            except FileNotFoundError:
                nretries = nretries + 1
                time.sleep(10)
        if not file_read:
            log("Failed to read Gaia alert catalog")
            return

        start_date = payload.get("start_date", None)
        end_date = payload.get("end_date", None)

        user = session.scalar(sa.select(User).where(User.id == user_id))

        for row in table:
            name = row["#Name"]
            ra, dec = row["RaDeg"], row["DecDeg"]
            date = Time(row["Date"], format="iso")

            if start_date is not None:
                if date < start_date:
                    continue
            if end_date is not None:
                if date > end_date:
                    continue

            data = {"ra": ra, "dec": dec, "id": name}
            s = session.scalars(Obj.select(user).where(Obj.id == name)).first()
            if s is None:
                data["group_ids"] = group_ids
                (obj_id,) = post_source(data, user_id, session)
                obj_ids.append(obj_id)
            else:
                obj_id = s.id

            try:
                lc = Table.read(
                    f"{lightcurve_url}/{name}/lightcurve.csv/",
                    format="csv",
                    header_start=1,
                )
            except FileNotFoundError:
                log(f"Gaia alert {name} not found.")
                continue

            df = gaia_lightcurve_to_df(lc, ra, dec)

            data_out = {
                "obj_id": obj_id,
                "instrument_id": instrument_id,
                "group_ids": group_ids,
                **df.to_dict(orient="list"),
            }

            if len(df.index) > 0:
                # async add_external_photometry, bridged from this sync executor;
                # obj already committed above so the bridge's session sees it.
                asyncio.run(commit_external_photometry(data_out, user_id))
                log(f"Photometry committed to database for {obj_id}")
            else:
                log(f"No photometry to commit to database for {obj_id}")

        return obj_ids
    except Exception as e:
        return log(f"Unable to commit Gaia Photometric Alert catalog: {e}")
    finally:
        session.close()


class TessTransientsQueryHandler(BaseHandler):
    @auth_or_token
    async def post(self, *, body: TessTransientsQueryPostBody = None):
        """
        ---
        summary: Post TESS transients as sources
        description: |
            Get TESS transient objects and post them as sources.
            Repeated posting will skip the existing source.
        tags:
            - catalogs
            - tess
        responses:
          200:
            content:
              application/json:
                schema: Success
          400:
            content:
              application/json:
                schema: Error
        """
        body = self.parse_body(TessTransientsQueryPostBody)

        telescope_name = (
            body.telescope_name if body.telescope_name is not None else "TESS"
        )
        start_date = body.startDate
        end_date = body.endDate
        group_ids = body.groupIDs

        if start_date is not None:
            start_date = Time(arrow.get(start_date.strip()).datetime)
        if end_date is not None:
            end_date = Time(arrow.get(end_date.strip()).datetime)

        payload = {"start_date": start_date, "end_date": end_date}

        async with self.AsyncSession() as session:
            if group_ids is None:
                group_ids = await accessible_group_ids_async(self.current_user, session)

            telescope = await session.scalar(
                Telescope.select(session.user_or_token)
                .options(selectinload(Telescope.instruments))
                .where(Telescope.nickname == telescope_name)
            )
            if telescope is None:
                return self.error(f"Expected a Telescope named {telescope_name}")
            instrument = telescope.instruments[0]

            fetch_tr = functools.partial(
                fetch_tess_transients,
                instrument.id,
                self.associated_user_object.id,
                group_ids,
                payload,
            )

            IOLoop.current().run_in_executor(None, fetch_tr)

            return self.success()


def fetch_tess_transients(instrument_id, user_id, group_ids, payload):
    """Fetch TESS transients.
    instrument_id : int
        ID of the instrument
    user_id : int
        ID of the User
    group_id : List[int]
        List of group IDs to save to
    payload : dict
        Dictionary containing filtering parameters
    """

    session = new_session()

    obj_ids = []

    alert_url = f"{TESS_URL}/lc_bulk/count_transients.txt"
    lightcurve_url = f"{TESS_URL}/light_curves/"

    try:
        file_read = False
        nretries = 0

        while not file_read and nretries < 10:
            try:
                table = Table.read(alert_url, format="ascii")
                file_read = True
            except FileNotFoundError:
                nretries = nretries + 1
                time.sleep(10)
        if not file_read:
            log("Failed to read TESS alert catalog")
            return

        start_date = payload.get("start_date", None)
        end_date = payload.get("end_date", None)

        user = session.scalar(sa.select(User).where(User.id == user_id))

        for row in table:
            name = row["name"]
            ra, dec = row["ra"], row["dec"]
            date = Time(2457000, format="jd") + TimeDelta(row["disc_tjd"], format="jd")

            if start_date is not None:
                if date < start_date:
                    continue
            if end_date is not None:
                if date > end_date:
                    continue

            data = {"ra": ra, "dec": dec, "id": name}
            s = session.scalars(Obj.select(user).where(Obj.id == name)).first()
            if s is None:
                data["group_ids"] = group_ids
                (obj_id,) = post_source(data, user_id, session)
                obj_ids.append(obj_id)
            else:
                obj_id = s.id

            try:
                lc = Table.read(
                    f"{lightcurve_url}/lc_{name}_cleaned",
                    format="ascii",
                    header_start=1,
                )
            except FileNotFoundError:
                log(f"TESS alert {name} not found.")
                continue
            except Exception:
                log(
                    f"TESS alert {name} could not be ingested: {lightcurve_url}/lc_{name}_cleaned"
                )
                continue

            if "BTJD" not in list(lc.columns):
                log(
                    f"TESS alert {name} could not be ingested: {lightcurve_url}/lc_{name}_cleaned"
                )
                continue

            df = tess_lightcurve_to_df(lc, ra, dec)

            data_out = {
                "obj_id": obj_id,
                "series_name": "tesstransients",
                "series_obj_id": obj_id,
                "exp_time": 2.0,
                "instrument_id": instrument_id,
                "group_ids": group_ids,
            }

            if len(df.index) > 0:
                try:
                    post_photometric_series(data_out, df, {}, user, session)
                    log(f"Photometry committed to database for {obj_id}")
                except Exception:
                    ps = session.scalars(
                        sa.select(PhotometricSeries).where(
                            PhotometricSeries.series_obj_id == obj_id,
                            PhotometricSeries.obj_id == obj_id,
                        )
                    ).first()
                    if ps is not None:
                        update_photometric_series(ps, data_out, df, {}, user, session)
                        log(f"Photometry updated in database for {obj_id}")
                    else:
                        log(f"No photometry to commit to database for {obj_id}")
            else:
                log(f"No photometry to commit to database for {obj_id}")

        return obj_ids
    except Exception as e:
        return log(f"Unable to commit TESS transient catalog: {e}")
    finally:
        session.close()
