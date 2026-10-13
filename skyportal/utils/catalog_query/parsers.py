"""Pure catalog response parsers shared by the queue adapters and the
legacy standalone catalog endpoints. No database or network access here."""

import numpy as np
from astropy.time import Time, TimeDelta

GAIA_LIMITING_MAG = 20.7
TESS_LIMITING_MAG = 18.4
TESS_ZP = 20.5
TESS_EXP_TIME = 2.0


def gaia_lightcurve_to_df(lc, ra, dec):
    """Convert a Gaia photometric alert lightcurve table to the shared layout."""
    lc["mjd"] = Time(lc["JD(TCB)"], format="jd").mjd
    lc["ra"] = ra
    lc["dec"] = dec
    lc["limiting_mag"] = GAIA_LIMITING_MAG
    lc["filter"] = "gaia::g"
    lc["magsys"] = "ab"

    df = lc.to_pandas()
    df.rename(columns={"averagemag": "mag"}, inplace=True)
    df = df.replace({"null": np.nan})
    df = df.replace({"untrusted": np.nan})
    df = df.astype({"mag": float})
    df["magerr"] = (
        3.43779
        - (df["mag"] / 1.13759)
        + (df["mag"] / 3.44123) ** 2
        - (df["mag"] / 6.51996) ** 3
        + (df["mag"] / 11.45922) ** 4
    )
    columns_to_keep = {
        "mjd",
        "ra",
        "dec",
        "mag",
        "magerr",
        "limiting_mag",
        "filter",
        "magsys",
    }
    df.drop(columns=[c for c in df.columns if c not in columns_to_keep], inplace=True)
    return df


def tess_lightcurve_to_df(lc, ra, dec):
    """Convert a TESS cleaned lightcurve table to the photometric-series layout."""
    lc["mjd"] = (
        Time(2457000, format="jd") + TimeDelta(lc["BTJD"], format="jd")
    ).mjd
    lc["ra"] = ra
    lc["dec"] = dec
    lc["limiting_mag"] = TESS_LIMITING_MAG
    lc["zp"] = TESS_ZP
    lc["filter"] = "tess"
    lc["magsys"] = "ab"

    df = lc.to_pandas()
    df.rename(
        columns={"e_mag": "magerr", "cts_per_s": "flux", "e_cts_per_s": "fluxerr"},
        inplace=True,
    )

    magerr_none = df["magerr"] == None  # noqa: E711
    df.loc[magerr_none, "mag"] = None
    isnan = np.isnan(df["magerr"])
    df.loc[isnan, "mag"] = None
    df.loc[isnan, "magerr"] = None
    is99 = np.isclose(df["magerr"], 99.9)
    df.loc[is99, "mag"] = None
    df.loc[is99, "magerr"] = None

    columns_to_keep = {
        "mjd",
        "ra",
        "dec",
        "mag",
        "magerr",
        "flux",
        "fluxerr",
        "zp",
        "limiting_mag",
        "filter",
        "magsys",
    }
    df.drop(columns=[c for c in df.columns if c not in columns_to_keep], inplace=True)
    return df


def swift_lightcurves_to_df(lightcurves):
    """Merge Swift LSXPS PC/PCUL rate tables into the shared photometry layout."""
    dfs = []
    if "PC_incbad" in lightcurves:
        df1 = lightcurves["PC_incbad"]
        df1.rename(
            columns={"Time": "mjd", "Rate": "flux", "RatePos": "fluxerr"},
            inplace=True,
        )
        dfs.append(df1)
    if "PCUL_incbad" in lightcurves:
        df2 = lightcurves["PCUL_incbad"]
        df2.rename(columns={"Time": "mjd", "Rate": "fluxerr"}, inplace=True)
        dfs.append(df2)
    if not dfs:
        return None
    import pandas as pd

    df = pd.concat(dfs)
    columns_to_keep = {
        "mjd",
        "ra",
        "dec",
        "flux",
        "fluxerr",
        "limiting_mag",
        "filter",
    }
    df.drop(columns=[c for c in df.columns if c not in columns_to_keep], inplace=True)
    return df
