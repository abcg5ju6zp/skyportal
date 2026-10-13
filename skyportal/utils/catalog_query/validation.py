"""Validation for staged catalog-query candidates.

Rules mirror post_source (ID charset, RA/Dec ranges) and the column
requirements already enforced downstream by the photometry/series
entrypoints, so invalid rows never reach the live tables.
"""

import math
import re

# Keep in lock-step with post_source's source ID rule.
RE_SOURCE_ID = re.compile(r"^[a-zA-Z0-9_\-;:+.]+$")

# Column-list photometry payloads always carry time and band information.
PHOTOMETRY_REQUIRED = ("mjd", "filter")
SERIES_REQUIRED = ("mjd", "filter")

# Scalar metadata that happens to be list-shaped but never one-entry-per-row.
META_LIST_KEYS = {"group_ids", "stream_ids", "reduction", "origin", "comments"}


def validate_candidate(external_id, ra, dec):
    """Return (status, error) for a candidate's identity and coordinates."""
    if external_id is None or str(external_id).strip() == "":
        return "invalid", "empty external id"
    external_id = str(external_id)
    if " " in external_id or not RE_SOURCE_ID.match(external_id):
        return "invalid", "illegal characters in source ID"
    if ra is not None:
        try:
            ra = float(ra)
        except (TypeError, ValueError):
            return "invalid", "RA is not numeric"
        if not math.isfinite(ra) or not (0.0 <= ra < 360.0):
            return "invalid", "RA out of [0, 360)"
    if dec is not None:
        try:
            dec = float(dec)
        except (TypeError, ValueError):
            return "invalid", "Dec is not numeric"
        if not math.isfinite(dec) or not (-90.0 <= dec <= 90.0):
            return "invalid", "Dec out of [-90, 90]"
    return "valid", None


def validate_data_payload(kind, payload):
    """Return (status, error) for a staged photometry/series payload."""
    if not isinstance(payload, dict):
        return "invalid", f"{kind} payload is not an object"
    # Photometric series stage as {"data_out": {...}, "data": {columns: rows}}.
    container = payload.get("data") if kind == "photometric_series" else payload
    if not isinstance(container, dict):
        return "invalid", f"{kind} payload data is not an object"
    required = SERIES_REQUIRED if kind == "photometric_series" else PHOTOMETRY_REQUIRED
    for col in required:
        if col not in container:
            return "invalid", f"missing required column: {col}"
        if not isinstance(container[col], (list, tuple)):
            return "invalid", f"column {col} must be a list"
    n = len(container["mjd"])
    if n == 0:
        return "invalid", "no data rows"
    for col in container:
        if col in META_LIST_KEYS:
            continue
        if isinstance(container[col], (list, tuple)) and len(container[col]) != n:
            return "invalid", f"column {col} length differs from mjd"
    return "valid", None
