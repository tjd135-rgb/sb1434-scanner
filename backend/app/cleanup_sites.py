"""DEP Contamination Locator ingest (Phase C1).

Second environmental trigger for SB 1434 qualification. Pulls every point
feature from the DEP Cleanup Sites layer (Contamination Locator Map),
filters to the tri-county footprint via CC2_COUNTY_ID, and upserts into
cleanup_sites for the screening query's ST_DWithin check.

Layer: https://ca.dep.state.fl.us/arcgis/rest/services/Map_Direct/Environment/MapServer/1

FDEP-specific gotchas (learned from ?f=json on the layer metadata):
  - The OID field is DEP_CLEANUP_SITE_KEY, NOT OBJECTID. Using OBJECTID
    in orderByFields or WHERE returns HTTP 400.
  - County is CC2_COUNTY_ID as a small integer using the FL alphabetical
    county-code encoding: Miami-Dade=13, Broward=6, Palm Beach=50 (NOT the
    DOR CO_NO 23/16/60).
  - Field names are program-code-heavy (BUSINESS_NAME, ADDRESS1, ZIP5,
    RSC2_REMEDIATION_STATUS_KEY). We extract what we can into typed
    columns and archive the full attributes dict in raw_json for later
    enrichment against the FDEP code tables.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional

import requests
from sqlalchemy import text

from .db import engine

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(_h)


CONTAM_LAYER = (
    "https://ca.dep.state.fl.us/arcgis/rest/services/"
    "Map_Direct/Environment/MapServer/1"
)

# FDEP alphabetical county codes → canonical county name we already use
# elsewhere in this project.
COUNTY_ID_TO_NAME: Dict[int, str] = {
    13: "MIAMI-DADE",
    6: "BROWARD",
    50: "PALM BEACH",
}
TRI_COUNTY_IDS: List[int] = list(COUNTY_ID_TO_NAME)

# This layer's OID field. Everything (orderBy, WHERE keyset, etc.) has to
# use this name — OBJECTID does not exist on this layer.
OID_FIELD = "DEP_CLEANUP_SITE_KEY"

# maxRecordCount on the service is 1000. Match it.
BATCH_SIZE = 1_000
HTTP_TIMEOUT = 60

# County WHERE fragment — server-side filter beats hauling ~10K statewide
# rows over the wire when we only want the tri-county subset.
_COUNTY_WHERE = f"CC2_COUNTY_ID IN ({','.join(str(i) for i in TRI_COUNTY_IDS)})"


# ---------- HTTP ----------

def _fetch_page(where: str, batch_size: int) -> List[Dict[str, Any]]:
    """One f=json page ordered by the layer's OID field."""
    params = {
        "where": where,
        "outFields": "*",
        "outSR": "4326",
        "orderByFields": f"{OID_FIELD} ASC",
        "resultRecordCount": batch_size,
        "returnGeometry": "true",
        "f": "json",
    }
    # Log the fully-encoded URL up front — invaluable when the endpoint
    # returns a 400 with the always-generic "Invalid or missing input
    # parameters" and no hint about which param is offending.
    url = f"{CONTAM_LAYER}/query?{urllib.parse.urlencode(params)}"
    log.debug("GET %s", url)
    r = requests.get(f"{CONTAM_LAYER}/query", params=params, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} for {url}: {r.text[:400]}")
    payload = r.json()
    if isinstance(payload, dict) and "error" in payload:
        raise RuntimeError(
            f"FDEP returned error for {url}: {payload['error']}"
        )
    return payload.get("features", [])


def _feature_oid(feat: Dict[str, Any]) -> Optional[int]:
    """Extract the DEP_CLEANUP_SITE_KEY value (case-insensitive)."""
    attrs = feat.get("attributes") or {}
    for key, value in attrs.items():
        if key.upper() == OID_FIELD:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def _paginate() -> Iterable[Dict[str, Any]]:
    """Yield every tri-county feature via keyset pagination on DEP_CLEANUP_SITE_KEY."""
    where = _COUNTY_WHERE
    page = 0
    total = 0
    while True:
        feats = _fetch_page(where, BATCH_SIZE)
        page += 1
        if not feats:
            log.info("Layer done: %d total features fetched", total)
            break
        oids = [oid for oid in (_feature_oid(f) for f in feats) if oid is not None]
        total += len(feats)
        log.info("  page %d: %d features (running total %d)", page, len(feats), total)
        yield from feats

        if len(feats) < BATCH_SIZE:
            log.info("Layer done (short page): %d total", total)
            break
        if not oids:
            log.warning("page %d had features but no OID; bailing", page)
            break
        where = f"({_COUNTY_WHERE}) AND {OID_FIELD}>{max(oids)}"


# ---------- attribute helpers ----------

def _get(attrs: Dict[str, Any], key: str) -> Any:
    """Case-insensitive single-key lookup."""
    for k, v in attrs.items():
        if k.upper() == key.upper():
            return v
    return None


def _clip(attrs: Dict[str, Any], key: str, limit: int) -> Optional[str]:
    v = _get(attrs, key)
    if v is None:
        return None
    s = str(v).strip()
    return s[:limit] if s else None


def _extract_latlon(feat: Dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    g = feat.get("geometry") or {}
    x = g.get("x")
    y = g.get("y")
    try:
        return float(y), float(x)
    except (TypeError, ValueError):
        # Fall back to the layer's X_COORDINATE / Y_COORDINATE / LATITUDE_*
        # attributes if geometry didn't come through for some reason.
        attrs = feat.get("attributes") or {}
        try:
            return float(_get(attrs, "Y_COORDINATE")), float(_get(attrs, "X_COORDINATE"))
        except (TypeError, ValueError):
            return None, None


def _row_from_feature(feat: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    attrs = feat.get("attributes") or {}

    # Prefer the human-readable source-database ID; fall back to the OID.
    site_id = _get(attrs, "SOURCE_DATABASE_ID")
    if site_id in (None, ""):
        oid = _feature_oid(feat)
        if oid is None:
            return None
        site_id = f"OID:{oid}"
    site_id = str(site_id).strip()[:40]

    county_id = _get(attrs, "CC2_COUNTY_ID")
    try:
        county_id_int = int(county_id) if county_id is not None else None
    except (TypeError, ValueError):
        county_id_int = None
    county = COUNTY_ID_TO_NAME.get(county_id_int) if county_id_int is not None else None

    lat, lon = _extract_latlon(feat)

    # ZIP5 is an integer on the wire; render it as zero-padded text so it
    # matches how everyone thinks of ZIPs.
    zip5 = _get(attrs, "ZIP5")
    zip_str: Optional[str] = None
    if zip5 not in (None, "", 0):
        try:
            zip_str = f"{int(zip5):05d}"
        except (TypeError, ValueError):
            zip_str = str(zip5)[:15]

    return {
        "site_id": site_id,
        "site_name": _clip(attrs, "BUSINESS_NAME", 200),
        # RSC2 is a status code (e.g. "N", "C"); good enough for now, raw_json
        # has the full record for later decode.
        "site_status": _clip(attrs, "RSC2_REMEDIATION_STATUS_KEY", 80),
        "county": county[:30] if county else None,
        "address": _clip(attrs, "ADDRESS1", 200),
        "city": _clip(attrs, "CITY", 80),
        "zip": zip_str,
        "latitude": lat,
        "longitude": lon,
        "raw_json": json.dumps(attrs),
    }


# ---------- upsert ----------

_UPSERT = text(
    """
    INSERT INTO cleanup_sites (
        site_id, site_name, site_status, county, address, city, zip,
        latitude, longitude, geom, raw_json
    )
    VALUES (
        :site_id, :site_name, :site_status, :county, :address, :city, :zip,
        :latitude, :longitude,
        CASE WHEN :latitude IS NULL OR :longitude IS NULL THEN NULL
             ELSE ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326) END,
        CAST(:raw_json AS jsonb)
    )
    ON CONFLICT (site_id) DO UPDATE SET
        site_name = EXCLUDED.site_name,
        site_status = EXCLUDED.site_status,
        county = EXCLUDED.county,
        address = EXCLUDED.address,
        city = EXCLUDED.city,
        zip = EXCLUDED.zip,
        latitude = EXCLUDED.latitude,
        longitude = EXCLUDED.longitude,
        geom = EXCLUDED.geom,
        raw_json = EXCLUDED.raw_json
    """
)


# ---------- public entry point ----------

def ingest_all() -> Dict[str, Any]:
    """Server-side filter to tri-county, upsert every row. Returns a summary."""
    log.info("=== DEP Contamination Locator ingest starting ===")
    log.info("Filter: %s (Miami-Dade=13, Broward=6, Palm Beach=50)", _COUNTY_WHERE)

    scanned = 0
    kept = 0
    skipped_unknown_county = 0

    with engine.begin() as conn:
        for feat in _paginate():
            scanned += 1
            row = _row_from_feature(feat)
            if row is None:
                continue
            if row["county"] is None:
                # WHERE filter should prevent this, but guard anyway.
                skipped_unknown_county += 1
                continue
            conn.execute(_UPSERT, row)
            kept += 1

    per_county = _tri_county_summary()
    log.info(
        "Scanned %d features, kept %d (skipped %d unknown-county)",
        scanned, kept, skipped_unknown_county,
    )
    for row in per_county:
        log.info(
            "  cleanup_sites %-12s n=%d (with geom=%d)",
            row["county"], row["n"], row["with_geom"],
        )

    return {
        "scanned": scanned,
        "kept": kept,
        "skipped_unknown_county": skipped_unknown_county,
        "tri_county_summary": per_county,
    }


def _tri_county_summary() -> List[Dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT county,
                       COUNT(*) AS n,
                       COUNT(geom) AS with_geom
                  FROM cleanup_sites
                 GROUP BY county
                 ORDER BY n DESC
                """
            )
        ).mappings().all()
    return [
        {"county": r["county"], "n": int(r["n"]), "with_geom": int(r["with_geom"])}
        for r in rows
    ]


if __name__ == "__main__":  # pragma: no cover
    ingest_all()
