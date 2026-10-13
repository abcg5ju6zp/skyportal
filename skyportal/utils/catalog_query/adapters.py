"""Catalog adapters: resumable, timeout-bound fetches that write candidates
into the query-private staging area. Never touch live tables directly."""

import base64
import glob
import io
import tempfile
import urllib.parse

import arrow
import numpy as np
import pandas as pd
import requests
import sqlalchemy as sa
import swifttools.ukssdc.query as uq
from astropy.table import Table
from astropy.time import Time, TimeDelta
from sqlalchemy.orm import selectinload

from baselayer.app.env import load_env
from skyportal.models import Instrument, Localization, Telescope, User
from skyportal.utils.catalog import get_conesearch_centers

from .jobs import AdapterDataError, UncertainResponse, log, queue_config
from .parsers import (
    TESS_EXP_TIME,
    gaia_lightcurve_to_df,
    swift_lightcurves_to_df,
    tess_lightcurve_to_df,
)

_, cfg = load_env()

TESS_URL = cfg["app.tess_endpoint"]
GAIA_ALERT_URL = "http://gsaweb.ast.cam.ac.uk/alerts/alerts.csv"
GAIA_LIGHTCURVE_URL = "https://gsaweb.ast.cam.ac.uk/alerts/alert"
SWIFT_TELESCOPE = "Swift"


def _timeout():
    return queue_config().get("request_timeout_seconds", 60)


def _check_status(response):
    if 500 <= response.status_code < 600:
        raise UncertainResponse(f"HTTP {response.status_code} from catalog")
    if response.status_code != 200:
        raise AdapterDataError(
            f"HTTP {response.status_code}: {response.text[:300]}"
        )


def _get(url, **kwargs):
    try:
        response = requests.get(url, timeout=_timeout(), **kwargs)
    except requests.RequestException as e:
        raise UncertainResponse(f"GET {url} failed: {e}") from e
    _check_status(response)
    return response


def _post(url, **kwargs):
    try:
        response = requests.post(url, timeout=_timeout(), **kwargs)
    except requests.RequestException as e:
        raise UncertainResponse(f"POST {url} failed: {e}") from e
    _check_status(response)
    return response


def _user(session, ctx):
    user = session.scalar(sa.select(User).where(User.id == ctx.requester_id))
    if user is None:
        raise AdapterDataError(f"Requester {ctx.requester_id} not found")
    return user


def _instrument(session, user, name):
    instrument = session.scalars(
        Instrument.select(user).where(Instrument.name == name)
    ).first()
    if instrument is None:
        raise AdapterDataError(f"Expected an Instrument named {name}")
    return instrument


def _telescope_instrument(session, user, nickname):
    telescope = session.scalars(
        Telescope.select(user)
        .options(selectinload(Telescope.instruments))
        .where(Telescope.nickname == nickname)
    ).first()
    if telescope is None or not telescope.instruments:
        raise AdapterDataError(f"Expected a Telescope named {nickname}")
    return telescope.instruments[0]


# --- ZTF-Fink -------------------------------------------------------------

def _fink_photometry(object_id):
    response = _post(
        urllib.parse.urljoin(cfg["app.fink_endpoint"], "api/v1/objects"),
        json={"objectId": object_id, "output-format": "json"},
    )
    try:
        df = pd.DataFrame.from_dict(response.json())
    except ValueError as e:
        raise UncertainResponse(
            f"Unparseable Fink photometry for {object_id}: {e}"
        ) from e

    required = {"i:jd", "i:ra", "i:dec", "i:magpsf", "i:sigmapsf",
                "i:diffmaglim", "i:magzpsci", "i:fid"}
    missing = required - set(df.columns)
    if missing:
        raise AdapterDataError(
            f"Fink photometry for {object_id} missing columns: {sorted(missing)}"
        )

    df.rename(
        columns={
            "i:jd": "jd",
            "i:ra": "ra",
            "i:dec": "dec",
            "i:magpsf": "mag",
            "i:sigmapsf": "magerr",
            "i:diffmaglim": "limiting_mag",
            "i:magzpsci": "zp",
            "i:fid": "filter",
        },
        inplace=True,
    )
    from skyportal.facility_apis.ztf import inv_bands

    df["filter"] = [inv_bands[int(f)] for f in df["filter"]]
    df["mjd"] = [Time(jd, format="jd").mjd for jd in df["jd"]]
    columns_to_keep = ["mjd", "ra", "dec", "mag", "magerr", "limiting_mag", "filter"]
    return df[columns_to_keep]


def run_fink(ctx):
    with ctx.session_factory() as session:
        user = _user(session, ctx)
        instrument = _instrument(session, user, "ZTF")
        localization = session.scalars(
            Localization.select(user).where(
                Localization.dateobs == ctx.payload["localizationDateobs"],
                Localization.localization_name == ctx.payload["localizationName"],
            )
        ).first()
        if localization is None:
            raise AdapterDataError("Localization not accessible to requester")
        start_date = Time(arrow.get(ctx.payload["startDate"].strip()).datetime)
        end_date = Time(arrow.get(ctx.payload["endDate"].strip()).datetime)
        ra_center, dec_center = get_conesearch_centers(
            localization.flat_2d, level=ctx.payload.get("localizationCumprob", 0.95)
        )
        instrument_id = instrument.id

    target_group_ids = ctx.permission_snapshot["target_group_ids"]
    time_min = start_date
    time_max = end_date
    pagination = dict(ctx.pagination or {"tile_index": 0, "seen_ids": []})

    for index in range(pagination["tile_index"], len(ra_center)):
        response = _post(
            urllib.parse.urljoin(cfg["app.fink_endpoint"], "api/v1/explorer"),
            json={
                "ra": ra_center[index],
                "dec": dec_center[index],
                "radius": 3600,
                "startdate_conesearch": Time(
                    time_min.jd, format="jd"
                ).iso,
                "window_days_conesearch": time_max.jd - time_min.jd,
            },
        )
        try:
            objects = pd.DataFrame(response.json())
        except ValueError as e:
            raise UncertainResponse(f"Unparseable Fink tile {index}: {e}") from e

        for _, obj in objects.iterrows():
            object_id = obj["i:objectId"]
            if object_id in pagination["seen_ids"]:
                continue
            jdstarthist = obj["i:jdstarthist"]
            jdendhist = obj["i:jd"]
            if jdstarthist < time_min.jd or jdendhist > time_max.jd:
                continue
            df = _fink_photometry(object_id)
            detections = np.where(
                (~np.isnan(df["mag"]))
                & (df["mjd"] >= time_min.mjd)
                & (df["mjd"] <= time_max.mjd)
            )[0]
            if len(detections) < 2:
                continue
            ctx.stage(
                external_id=object_id,
                ra=obj["i:ra"],
                dec=obj["i:dec"],
                raw_data={"id": object_id, "ra": obj["i:ra"], "dec": obj["i:dec"]},
                photometry={
                    "obj_id": object_id,
                    "instrument_id": instrument_id,
                    "group_ids": target_group_ids,
                    **df.to_dict(orient="list"),
                },
            )
            pagination["seen_ids"].append(object_id)

        pagination["tile_index"] = index + 1
        ctx.heartbeat(
            pagination=pagination,
            response_summary={
                "tiles_completed": index + 1,
                "tiles_total": len(ra_center),
                "objects": len(pagination["seen_ids"]),
            },
        )

    return {"tiles": len(ra_center), "objects": len(pagination["seen_ids"])}


# --- Swift LSXPS ----------------------------------------------------------

def run_swift(ctx):
    with ctx.session_factory() as session:
        user = _user(session, ctx)
        instrument_id = _telescope_instrument(session, user, SWIFT_TELESCOPE).id

    accessible_group_ids = ctx.permission_snapshot.get(
        "accessible_group_ids", ctx.permission_snapshot["target_group_ids"]
    )
    pagination = {"done": list((ctx.pagination or {}).get("done") or [])}

    with tempfile.TemporaryDirectory() as tmpdirname:
        q = uq.SXPSQuery(silent=True, cat="LSXPS", table="transients")
        q.addFilter(("Classification", "=", 1))
        try:
            q.submit()
            q.getDetails(byName=True)
            q.getLightCurves(byName=True, timeFormat="MJD", binning="obs")
            q.getSpectra(byName=True, specType="Discovery")
            q.saveSpectra(destDir=tmpdirname)
        except Exception as e:
            raise UncertainResponse(f"Swift LSXPS query failed: {e}") from e

        for transient in q.transientDetails:
            if transient in pagination["done"]:
                continue
            details = q.transientDetails[transient]
            ra = details.pop("RA")
            dec = details.pop("Decl")
            obj_name = details.pop("IAUName").replace(" ", "-")

            photometry = None
            df = swift_lightcurves_to_df(q.lightCurves.get(transient, {}))
            if df is not None and len(df.index) > 0:
                df["ra"] = ra
                df["dec"] = dec
                df["filter"] = "swiftxrt"
                df["magsys"] = "ab"
                df["zp"] = 0.0
                df = df.replace({np.nan: None})
                photometry = {
                    "obj_id": obj_name,
                    "instrument_id": instrument_id,
                    "group_ids": accessible_group_ids,
                    **df.to_dict(orient="list"),
                }

            spectra = []
            for filename in glob.glob(f"{tmpdirname}/{transient}/interval*"):
                with open(filename, "rb") as f:
                    spectra.append(
                        {
                            "filename": filename.split("/")[-1],
                            "content_b64": base64.b64encode(f.read()).decode(),
                        }
                    )

            ctx.stage(
                external_id=obj_name,
                ra=ra,
                dec=dec,
                raw_data={
                    "id": obj_name,
                    "ra": ra,
                    "dec": dec,
                    "details": details,
                    "spectra": spectra,
                },
                photometry=photometry,
            )
            pagination["done"].append(transient)
            ctx.heartbeat(
                pagination=pagination,
                response_summary={"objects": len(pagination["done"])},
            )

    return {"objects": len(pagination["done"])}


# --- Gaia -----------------------------------------------------------------

def run_gaia(ctx):
    with ctx.session_factory() as session:
        user = _user(session, ctx)
        instrument_id = _telescope_instrument(session, user, "Gaia").id
        start_date = (
            Time(arrow.get(ctx.payload["startDate"].strip()).datetime)
            if ctx.payload.get("startDate")
            else None
        )
        end_date = (
            Time(arrow.get(ctx.payload["endDate"].strip()).datetime)
            if ctx.payload.get("endDate")
            else None
        )

    target_group_ids = ctx.permission_snapshot["target_group_ids"]
    table = Table.read(io.StringIO(_get(GAIA_ALERT_URL).text), format="csv")
    pagination = {"done_names": list((ctx.pagination or {}).get("done_names") or [])}

    for row in table:
        name = row["#Name"]
        if name in pagination["done_names"]:
            continue
        ra, dec, date = row["RaDeg"], row["DecDeg"], Time(row["Date"], format="iso")
        if start_date is not None and date < start_date:
            continue
        if end_date is not None and date > end_date:
            continue

        lightcurve_url = f"{GAIA_LIGHTCURVE_URL}/{name}/lightcurve.csv/"
        try:
            response = _get(lightcurve_url)
        except AdapterDataError as e:
            # A missing per-object lightcurve skips the object, as before.
            log(f"Gaia alert {name} lightcurve unavailable: {e}")
            pagination["done_names"].append(name)
            continue
        try:
            lc = Table.read(
                io.StringIO(response.text), format="csv", header_start=1
            )
            df = gaia_lightcurve_to_df(lc, ra, dec)
        except Exception as e:
            raise AdapterDataError(
                f"Malformed Gaia lightcurve for {name}: {e}"
            ) from e
        df = df.replace({np.nan: None})

        ctx.stage(
            external_id=name,
            ra=float(ra),
            dec=float(dec),
            raw_data={"id": name, "ra": float(ra), "dec": float(dec)},
            photometry={
                "obj_id": name,
                "instrument_id": instrument_id,
                "group_ids": target_group_ids,
                **df.to_dict(orient="list"),
            },
        )
        pagination["done_names"].append(name)
        ctx.heartbeat(
            pagination=pagination,
            response_summary={"objects": len(pagination["done_names"])},
        )

    return {"objects": len(pagination["done_names"])}


# --- TESS -----------------------------------------------------------------

def run_tess(ctx):
    with ctx.session_factory() as session:
        user = _user(session, ctx)
        instrument_id = _telescope_instrument(session, user, "TESS").id
        start_date = (
            Time(arrow.get(ctx.payload["startDate"].strip()).datetime)
            if ctx.payload.get("startDate")
            else None
        )
        end_date = (
            Time(arrow.get(ctx.payload["endDate"].strip()).datetime)
            if ctx.payload.get("endDate")
            else None
        )

    target_group_ids = ctx.permission_snapshot["target_group_ids"]
    count_url = f"{TESS_URL}/lc_bulk/count_transients.txt"
    table = Table.read(io.StringIO(_get(count_url).text), format="ascii")
    pagination = {"done_names": list((ctx.pagination or {}).get("done_names") or [])}

    for row in table:
        name = row["name"]
        if name in pagination["done_names"]:
            continue
        ra, dec = row["ra"], row["dec"]
        date = Time(2457000, format="jd") + TimeDelta(row["disc_tjd"], format="jd")
        if start_date is not None and date < start_date:
            continue
        if end_date is not None and date > end_date:
            continue

        lightcurve_url = f"{TESS_URL}/light_curves/lc_{name}_cleaned"
        try:
            response = _get(lightcurve_url)
            lc = Table.read(
                io.StringIO(response.text), format="ascii", header_start=1
            )
        except AdapterDataError:
            log(f"TESS alert {name} lightcurve unavailable")
            pagination["done_names"].append(name)
            continue
        except Exception as e:
            log(f"TESS alert {name} could not be ingested: {e}")
            pagination["done_names"].append(name)
            continue
        if "BTJD" not in lc.columns:
            log(f"TESS alert {name} missing BTJD column")
            pagination["done_names"].append(name)
            continue

        df = tess_lightcurve_to_df(lc, ra, dec).replace({np.nan: None})
        ctx.stage(
            external_id=name,
            ra=float(ra),
            dec=float(dec),
            raw_data={"id": name, "ra": float(ra), "dec": float(dec)},
            photometric_series={
                "data_out": {
                    "obj_id": name,
                    "series_name": "tesstransients",
                    "series_obj_id": name,
                    "exp_time": TESS_EXP_TIME,
                    "instrument_id": instrument_id,
                    "group_ids": target_group_ids,
                },
                "data": df.to_dict(orient="list"),
            },
        )
        pagination["done_names"].append(name)
        ctx.heartbeat(
            pagination=pagination,
            response_summary={"objects": len(pagination["done_names"])},
        )

    return {"objects": len(pagination["done_names"])}


ADAPTERS = {
    "ZTF-Fink": run_fink,
    "LSXPS": run_swift,
    "Gaia": run_gaia,
    "TESS": run_tess,
}
