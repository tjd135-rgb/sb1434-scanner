"""DEP Contamination Locator ingest (Phase C1).

Second environmental trigger for SB 1434 qualification. Pulls every point
feature from the DEP Contamination Locator Map (~10K statewide), filters to
the tri-county footprint, and upserts into cleanup_sites for the screening
query's ST_DWithin check.

Layer: https://ca.dep.state.fl.us/arcgis/rest/services/Map_Direct/Environment/MapServer/1

Uses the same OBJECTID keyset-pagination pattern as brownfields.py so request
URLs stay short and the server's URL-length limit never bites. Format is
f=json (not geojson) because the raw attributes are archived to raw_json for
later pathway enrichment.
"""
from __future__ import annotations

import json
import logging
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

BATCH_SIZE = 1_000
HTTP_TIMEOUT = 60
TRI_COUNTIES = ("MIAMI-DADE", "BROWARD", "PALM BEACH")

# Candidate attribute keys to try in order for each logical field. FDEP
# layers are inconsistent about casing and naming across services, so we
# scan a list rather than hard-coding a single key.
SITE_ID_KEYS = ("FACILITY_ID", "FACID", "SITE_ID", "SITEID", "BROWNFIELD_ID", "PROGRAM_ID")
SITE_NAME_KEYS = ("FACILITY_NAME", "SITE_NAME", "NAME", "FACNAME", "FAC_NAME")
STATUS_KEYS = ("STATUS", "SITE_STATUS", "CLEANUP_STATUS", "SR_STATUS")
COUNTY_KEYS = ("COUNTY", "COUNTY_NAME", "SITE_COUNTY", "CTY")
ADDRESS_KEYS = ("ADDRESS", "SITE_ADDRESS", "STREET_ADDRESS", "ADDR")
CITY_KEYS = ("CITY", "SITE_CITY")
ZIP_KEYS = ("ZIP", "ZIPCODE", "ZIP_CODE", "SITE_ZIP")


# ---------- HTTP ----------

def _fetch_page(where: str, batch_size: int) -> List[Dict[str, Any]]:
    """One f=json page ordered by OBJECTID."""
    r = requests.get(
        f"{CONTAM_LAYER}/query",
        params={
            "where": where,
            "outFields": "*",
            "outSR": "4326",
            "orderByFields": "OBJECTID ASC",
            "resultRecordCount": batch_size,
            "returnGeometry": "true",
            "f": "json",
        },
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    payload = r.json()
    if isinstance(payload, dict) and "error" in payload:
        raise RuntimeError(f"FDEP returned error: {payload['error']}")
    return payload.get("features", [])


def _feature_oid(feat: Dict[str, Any]) -> Optional[int]:
    attrs = feat.get("attributes") or {}
    for key in ("OBJECTID", "ObjectID", "objectid", "OBJECTID_1", "FID"):
        v = attrs.get(key)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


def _paginate() -> Iterable[Dict[str, Any]]:
    """Yield every feature statewide via OBJECTID keyset pagination."""
    where = "1=1"
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
            log.warning("page %d had features but no OBJECTID; bailing", page)
            break
        where = f"OBJECTID>{max(oids)}"


# ---------- attribute helpers ----------

def _first(attrs: Dict[str, Any], keys: Iterable[str]) -> Any:
    """First non-empty value for any of the candidate keys (case-insensitive)."""
    lowered = {k.lower(): v for k, v in attrs.items()}
    for k in keys:
        v = lowered.get(k.lower())
        if v not in (None, ""):
            return v
    return None


def _norm_county(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().upper()
    if not s:
        return None
    # FDEP standardizes on MIAMI-DADE, but a handful of layers still emit
    # the historical 'DADE'. Fold to the canonical form so tri-county
    # matching is stable.
    if s == "DADE":
        return "MIAMI-DADE"
    return s


def _extract_latlon(feat: Dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    g = feat.get("geometry") or {}
    x = g.get("x")
    y = g.get("y")
    try:
        return float(y), float(x)
    except (TypeError, ValueError):
        return None, None


def _clip(attrs: Dict[str, Any], keys: Iterable[str], limit: int) -> Optional[str]:
    v = _first(attrs, keys)
    if v is None:
        return None
    s = str(v).strip()
    return s[:limit] if s else None


def _row_from_feature(feat: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    attrs = feat.get("attributes") or {}

    site_id = _first(attrs, SITE_ID_KEYS)
    if not site_id:
        # Fall back to OBJECTID so we still get a stable unique key.
        oid = _feature_oid(feat)
        if oid is None:
            return None
        site_id = f"OID:{oid}"
    site_id = str(site_id)[:40]

    county = _norm_county(_first(attrs, COUNTY_KEYS))
    lat, lon = _extract_latlon(feat)

    return {
        "site_id": site_id,
        "site_name": _clip(attrs, SITE_NAME_KEYS, 200),
        "site_status": _clip(attrs, STATUS_KEYS, 80),
        "county": county[:30] if county else None,
        "address": _clip(attrs, ADDRESS_KEYS, 200),
        "city": _clip(attrs, CITY_KEYS, 80),
        "zip": _clip(attrs, ZIP_KEYS, 15),
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
    """Fetch statewide, filter to tri-county, upsert. Returns summary payload."""
    log.info("=== DEP Contamination Locator ingest starting ===")

    scanned = 0
    kept = 0
    dropped_no_county = 0
    dropped_non_tri = 0
    county_seen: Dict[str, int] = {}

    with engine.begin() as conn:
        for feat in _paginate():
            scanned += 1
            row = _row_from_feature(feat)
            if row is None:
                continue

            county = row["county"]
            if county:
                county_seen[county] = county_seen.get(county, 0) + 1
            else:
                dropped_no_county += 1
                continue

            if county not in TRI_COUNTIES:
                dropped_non_tri += 1
                continue

            conn.execute(_UPSERT, row)
            kept += 1

    per_county = _tri_county_summary()
    log.info(
        "Scanned %d features, kept %d (dropped %d no-county, %d non-tri-county)",
        scanned, kept, dropped_no_county, dropped_non_tri,
    )
    for row in per_county:
        log.info(
            "  cleanup_sites %-12s n=%d (with geom=%d)",
            row["county"], row["n"], row["with_geom"],
        )

    return {
        "scanned": scanned,
        "kept": kept,
        "dropped_no_county": dropped_no_county,
        "dropped_non_tri_county": dropped_non_tri,
        "county_distribution_all_state": county_seen,
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
