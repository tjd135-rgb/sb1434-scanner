"""FDEP brownfield ingest.

Fetches every designated brownfield-area polygon (Layer 0, ~531 statewide) and
every individual brownfield-site polygon (Layer 1) from FDEP's public ArcGIS
REST endpoint and upserts them into PostGIS.

FDEP quirks worth knowing:
- County name for Miami-Dade is stored as 'DADE'.
- Field names on Layer 0/1 are UPPERCASE.
- Geometry may come back as either a Polygon or MultiPolygon; PostGIS is
  normalized to MULTIPOLYGON on insert via ST_Multi.
- Dates arrive as epoch-milliseconds; convert to Python date before insert.

The ingest is idempotent via ON CONFLICT (area_id) / (site_id) DO UPDATE.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
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

FDEP_BASE = (
    "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/BROWNFIELD_AREAS/MapServer"
)
AREAS_LAYER = f"{FDEP_BASE}/0"
SITES_LAYER = f"{FDEP_BASE}/1"

# ArcGIS caps single-request features at 1000-2000 depending on service; use a
# conservative batch size. Pagination is done via WHERE OBJECTID > last_seen so
# request URLs stay short (embedding all ObjectIDs blew past the server's URL
# length limit and returned 404).
BATCH_SIZE = 500
HTTP_TIMEOUT = 60
TRI_COUNTIES = ("DADE", "BROWARD", "PALM BEACH")


# ---------- HTTP helpers ----------

def _fetch_page(layer_url: str, where: str, batch_size: int) -> List[Dict[str, Any]]:
    """Fetch one page of GeoJSON features matching `where`, sorted by OBJECTID."""
    r = requests.get(
        f"{layer_url}/query",
        params={
            "where": where,
            "outFields": "*",
            "outSR": "4326",
            "orderByFields": "OBJECTID ASC",
            "resultRecordCount": batch_size,
            "f": "geojson",
        },
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    payload = r.json()
    if isinstance(payload, dict) and "error" in payload:
        raise RuntimeError(f"FDEP returned error: {payload['error']}")
    return payload.get("features", [])


def _feature_oid(feat: Dict[str, Any]) -> Optional[int]:
    """Extract OBJECTID from a GeoJSON feature (properties are case-sensitive)."""
    props = feat.get("properties") or {}
    for key in ("OBJECTID", "ObjectID", "objectid", "OBJECTID_1", "FID"):
        v = props.get(key)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    fid = feat.get("id")
    if fid is not None:
        try:
            return int(fid)
        except (TypeError, ValueError):
            return None
    return None


def _paginate(layer_url: str, batch_size: int = BATCH_SIZE) -> Iterable[Dict[str, Any]]:
    """Yield every GeoJSON feature in the layer.

    Uses WHERE-clause keyset pagination on OBJECTID so each request URL is
    small. The first page uses `1=1`; subsequent pages use
    `OBJECTID > <last_seen>` and always sort by OBJECTID ASC.
    """
    where = "1=1"
    page = 0
    total = 0
    while True:
        feats = _fetch_page(layer_url, where, batch_size)
        page += 1
        if not feats:
            log.info("Layer %s: page %d empty; done (%d total)", layer_url, page, total)
            break

        # Find max OBJECTID in the page to advance the WHERE cursor. If we
        # can't extract OIDs, bail out to avoid an infinite loop.
        oids = [oid for oid in (_feature_oid(f) for f in feats) if oid is not None]
        total += len(feats)
        log.info(
            "  page %d: %d features (running total %d)",
            page, len(feats), total,
        )
        yield from feats

        if len(feats) < batch_size:
            log.info("Layer %s: short page; done (%d total)", layer_url, total)
            break
        if not oids:
            log.warning(
                "Layer %s: page %d had features but no OBJECTID; stopping to avoid loop",
                layer_url, page,
            )
            break

        last_oid = max(oids)
        where = f"OBJECTID>{last_oid}"


# ---------- Field mapping helpers ----------

def _prop(feat: Dict[str, Any], *keys: str) -> Any:
    """Case-insensitive property lookup; returns the first non-None hit."""
    props = feat.get("properties") or {}
    lowered = {k.lower(): v for k, v in props.items()}
    for k in keys:
        v = lowered.get(k.lower())
        if v not in (None, ""):
            return v
    return None


def _to_date(v: Any) -> Optional[date]:
    """ArcGIS emits dates as epoch-milliseconds. Accept ISO strings too."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000.0, tz=timezone.utc).date()
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _to_float(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _geom_json(feat: Dict[str, Any]) -> Optional[str]:
    g = feat.get("geometry")
    return json.dumps(g) if g else None


# ---------- Upsert ----------

_AREAS_UPSERT = text(
    """
    INSERT INTO brownfield_areas (
        area_id, area_name, city, county, district, resolution_number,
        resolution_date, acreage, latitude, longitude, documents_url, geom
    )
    VALUES (
        :area_id, :area_name, :city, :county, :district, :resolution_number,
        :resolution_date, :acreage, :latitude, :longitude, :documents_url,
        CASE WHEN :geom_json IS NULL THEN NULL
             ELSE ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:geom_json), 4326)) END
    )
    ON CONFLICT (area_id) DO UPDATE SET
        area_name = EXCLUDED.area_name,
        city = EXCLUDED.city,
        county = EXCLUDED.county,
        district = EXCLUDED.district,
        resolution_number = EXCLUDED.resolution_number,
        resolution_date = EXCLUDED.resolution_date,
        acreage = EXCLUDED.acreage,
        latitude = EXCLUDED.latitude,
        longitude = EXCLUDED.longitude,
        documents_url = EXCLUDED.documents_url,
        geom = EXCLUDED.geom
    """
)


_SITES_UPSERT = text(
    """
    INSERT INTO brownfield_sites (
        site_id, area_id, site_name, area_name, county,
        acreage, status, contaminants, geom
    )
    VALUES (
        :site_id, :area_id, :site_name, :area_name, :county,
        :acreage, :status, :contaminants,
        CASE WHEN :geom_json IS NULL THEN NULL
             ELSE ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:geom_json), 4326)) END
    )
    ON CONFLICT (site_id) DO UPDATE SET
        area_id = EXCLUDED.area_id,
        site_name = EXCLUDED.site_name,
        area_name = EXCLUDED.area_name,
        county = EXCLUDED.county,
        acreage = EXCLUDED.acreage,
        status = EXCLUDED.status,
        contaminants = EXCLUDED.contaminants,
        geom = EXCLUDED.geom
    """
)


def _row_from_area(feat: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    area_id = _prop(feat, "AREA_ID", "AreaID", "area_id")
    if not area_id:
        return None
    return {
        "area_id": str(area_id)[:15],
        "area_name": (_prop(feat, "AREA_NAME") or "")[:120] or None,
        "city": (_prop(feat, "CITY") or "")[:50] or None,
        "county": (_prop(feat, "COUNTY") or "")[:30] or None,
        "district": (_prop(feat, "DISTRICT") or "")[:20] or None,
        "resolution_number": (_prop(feat, "RESOLUTION_NUMBER") or "")[:20] or None,
        "resolution_date": _to_date(_prop(feat, "RESOLUTION_DATE")),
        "acreage": _to_float(_prop(feat, "ACREAGE")),
        "latitude": _to_float(_prop(feat, "LATITUDE")),
        "longitude": _to_float(_prop(feat, "LONGITUDE")),
        "documents_url": _prop(feat, "DOCUMENTS_URL", "DOCUMENTS"),
        "geom_json": _geom_json(feat),
    }


def _row_from_site(feat: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    site_id = _prop(feat, "SITE_ID", "SiteID", "BROWNFIELD_ID")
    if not site_id:
        return None
    return {
        "site_id": str(site_id)[:20],
        "area_id": (_prop(feat, "AREA_ID") or "")[:15] or None,
        "site_name": (_prop(feat, "SITE_NAME", "NAME") or "")[:150] or None,
        "area_name": (_prop(feat, "AREA_NAME") or "")[:120] or None,
        "county": (_prop(feat, "COUNTY") or "")[:30] or None,
        "acreage": _to_float(_prop(feat, "ACREAGE")),
        "status": (_prop(feat, "STATUS", "CLEANUP_STATUS") or "")[:50] or None,
        "contaminants": _prop(feat, "CONTAMINANTS", "CONTAMINANT"),
        "geom_json": _geom_json(feat),
    }


# ---------- Public entry points ----------

def ingest_areas() -> int:
    """Ingest Layer 0. Returns number of rows upserted."""
    rows = 0
    with engine.begin() as conn:
        for feat in _paginate(AREAS_LAYER):
            row = _row_from_area(feat)
            if row is None:
                continue
            conn.execute(_AREAS_UPSERT, row)
            rows += 1
    log.info("brownfield_areas: %d rows upserted", rows)
    return rows


def ingest_sites() -> int:
    """Ingest Layer 1. Returns number of rows upserted."""
    rows = 0
    with engine.begin() as conn:
        for feat in _paginate(SITES_LAYER):
            row = _row_from_site(feat)
            if row is None:
                continue
            conn.execute(_SITES_UPSERT, row)
            rows += 1
    log.info("brownfield_sites: %d rows upserted", rows)
    return rows


def _tri_county_summary() -> Dict[str, Any]:
    """Ingest QA: counts + total acreage in Miami-Dade / Broward / Palm Beach."""
    with engine.connect() as conn:
        areas = conn.execute(
            text(
                """
                SELECT COALESCE(UPPER(county), '<unknown>') AS c,
                       COUNT(*) AS n,
                       COALESCE(SUM(acreage), 0) AS acres
                  FROM brownfield_areas
                 GROUP BY COALESCE(UPPER(county), '<unknown>')
                 ORDER BY n DESC
                """
            )
        ).all()
        sites = conn.execute(
            text(
                """
                SELECT COALESCE(UPPER(county), '<unknown>') AS c,
                       COUNT(*) AS n,
                       COALESCE(SUM(acreage), 0) AS acres
                  FROM brownfield_sites
                 GROUP BY COALESCE(UPPER(county), '<unknown>')
                 ORDER BY n DESC
                """
            )
        ).all()
    return {
        "areas_all_counties": [
            {"county": r[0], "n": int(r[1]), "acres": float(r[2])} for r in areas
        ],
        "sites_all_counties": [
            {"county": r[0], "n": int(r[1]), "acres": float(r[2])} for r in sites
        ],
        "areas_tri_county": [
            {"county": r[0], "n": int(r[1]), "acres": float(r[2])}
            for r in areas
            if r[0] in TRI_COUNTIES
        ],
        "sites_tri_county": [
            {"county": r[0], "n": int(r[1]), "acres": float(r[2])}
            for r in sites
            if r[0] in TRI_COUNTIES
        ],
    }


def ingest_all() -> Dict[str, Any]:
    """Ingest both layers and return a summary payload."""
    log.info("=== FDEP brownfield ingest starting ===")
    n_areas = ingest_areas()
    n_sites = ingest_sites()
    summary = _tri_county_summary()
    log.info("=== Ingest complete: %d areas, %d sites ===", n_areas, n_sites)
    for row in summary["areas_tri_county"]:
        log.info(
            "  AREAS %-12s n=%4d acres=%s",
            row["county"], row["n"], f"{row['acres']:,.1f}",
        )
    for row in summary["sites_tri_county"]:
        log.info(
            "  SITES %-12s n=%4d acres=%s",
            row["county"], row["n"], f"{row['acres']:,.1f}",
        )
    return {
        "areas_upserted": n_areas,
        "sites_upserted": n_sites,
        "summary": summary,
    }


if __name__ == "__main__":  # pragma: no cover
    ingest_all()
