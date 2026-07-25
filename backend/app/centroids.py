"""Phase A gap-filler: backfill parcels.lat / parcels.lon / parcels.geom
from the FL Statewide Parcel Centroid FeatureServer.

Ports the pattern from lla-scanner (OID-window pagination, retry/backoff,
staging table + UPDATE FROM) but adapted for sb1434-scanner's VARCHAR
county_fips and PostGIS geom column.

Usage:
    python -m app.centroids --county 23     # Miami-Dade
    python -m app.centroids --county 16     # Broward
    python -m app.centroids --county 60     # Palm Beach
    python -m app.centroids --county all

Pagination:
    The FL Statewide layer rejects resultOffset combined with a non-indexed
    WHERE (e.g. CO_NO=23) with HTTP 400. We paginate over the OBJECTID
    index instead. Before the main loop we fetch (min_oid, max_oid) for the
    county via returnIdsOnly=true so we iterate only the ~600k OID window
    that holds the county's rows, not the full 10.8M-row layer.
"""
from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from .db import raw_connection

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(_h)


# --------- source configs ---------

FL_STATEWIDE_URL = (
    "https://services9.arcgis.com/Gh9awoU677aKree0/ArcGIS/rest/services/"
    "Florida_Statewide_Parcel_Centroid_Version/FeatureServer/0"
)


@dataclass
class CentroidSource:
    county_fips: int  # numeric CO_NO for the ArcGIS WHERE clause
    url: str = FL_STATEWIDE_URL
    id_field: str = "PARCEL_ID"
    oid_chunk: int = 2_000


# Zero-pad the join key to the county's target parcel_id length, matching
# the LLA scanner convention (Broward centroid data comes in unpadded).
TARGET_LEN: Dict[int, int] = {
    23: 13,  # Miami-Dade FOLIO
    16: 12,  # Broward
    60: 17,  # Palm Beach PCN
}

# Broward's centroid layer sometimes carries PARCELNO instead of PARCEL_ID.
BROWARD_FALLBACK_FIELDS: List[str] = ["PARCEL_ID", "PARCELNO"]


SOURCES: Dict[int, CentroidSource] = {
    23: CentroidSource(county_fips=23),
    16: CentroidSource(county_fips=16),
    60: CentroidSource(county_fips=60),
}


PAGE_SIZE = 2_000
POLITE_SLEEP_S = 0.2
RETRY_DELAYS_S = [2, 4, 8]


# --------- HTTP ---------

def _out_fields(source: CentroidSource) -> str:
    if source.county_fips == 16:
        return ",".join(BROWARD_FALLBACK_FIELDS)
    return source.id_field


def _fetch_oid_window(source: CentroidSource) -> Tuple[int, int]:
    """Return (min_oid, max_oid) for rows matching CO_NO=<county_fips>."""
    where = f"CO_NO={source.county_fips}"
    params = {"where": where, "returnIdsOnly": "true", "f": "json"}
    last_exc: Optional[Exception] = None
    for attempt in range(1 + len(RETRY_DELAYS_S)):
        try:
            r = requests.get(f"{source.url}/query", params=params, timeout=60)
            if r.status_code == 200:
                data = r.json()
                if "error" in data:
                    raise RuntimeError(f"ArcGIS error: {data['error']}")
                ids = data.get("objectIds") or []
                if not ids:
                    raise RuntimeError(f"no OIDs matched WHERE={where!r}")
                return int(min(ids)), int(max(ids))
            last_exc = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        except (requests.RequestException, ValueError, RuntimeError) as e:
            last_exc = e
        if attempt < len(RETRY_DELAYS_S):
            time.sleep(RETRY_DELAYS_S[attempt])
    raise RuntimeError(f"_fetch_oid_window failed after 3 retries: {last_exc}")


def fetch_page(source: CentroidSource, oid_start: int) -> List[Dict[str, Any]]:
    """One /query page. `oid_start` is the starting OBJECTID for this chunk."""
    oid_end = oid_start + source.oid_chunk
    where = f"(CO_NO={source.county_fips}) AND OBJECTID>={oid_start} AND OBJECTID<{oid_end}"
    params: Dict[str, Any] = {
        "where": where,
        "outFields": _out_fields(source),
        "outSR": 4326,
        "f": "json",
        "resultRecordCount": PAGE_SIZE,
        "returnGeometry": "true",
    }
    last_exc: Optional[Exception] = None
    for attempt in range(1 + len(RETRY_DELAYS_S)):
        try:
            r = requests.get(f"{source.url}/query", params=params, timeout=60)
            if r.status_code == 200:
                data = r.json()
                if "error" in data:
                    raise RuntimeError(f"ArcGIS error: {data['error']}")
                return data.get("features", []) or []
            last_exc = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        except (requests.RequestException, ValueError, RuntimeError) as e:
            last_exc = e
        if attempt < len(RETRY_DELAYS_S):
            time.sleep(RETRY_DELAYS_S[attempt])
    raise RuntimeError(f"fetch_page failed after 3 retries at OID {oid_start}: {last_exc}")


# --------- feature -> row ---------

def _extract_join_key(attrs: Dict[str, Any], fips: int) -> Optional[str]:
    target = TARGET_LEN[fips]
    keys = BROWARD_FALLBACK_FIELDS if fips == 16 else ["PARCEL_ID"]
    candidates = [str(attrs.get(k) or "").strip() for k in keys]
    for v in candidates:
        if v and len(v) <= target:
            return v.zfill(target)
    return None


def _extract_latlon(feature: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    g = feature.get("geometry") or {}
    x = g.get("x")
    y = g.get("y")
    try:
        return float(y), float(x)
    except (TypeError, ValueError):
        return None, None


# --------- DB ---------

STAGING_DDL = """
CREATE TABLE IF NOT EXISTS centroid_stage (
    parcel_id VARCHAR(30) PRIMARY KEY,
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION
);
"""

INSERT_SQL = (
    "INSERT INTO centroid_stage (parcel_id, lat, lon) VALUES (%s, %s, %s) "
    "ON CONFLICT (parcel_id) DO UPDATE SET lat = EXCLUDED.lat, lon = EXCLUDED.lon"
)


def load_county(source: CentroidSource, dry_run: bool = False, resume_from_oid: int = 0) -> Dict[str, Any]:
    fips = source.county_fips
    fips_s = f"{fips:02d}"
    log.info("=== centroid backfill: county_fips=%s ===", fips_s)

    conn = raw_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(STAGING_DDL)
            if not dry_run:
                # Clear this county's staged rows only; we can't easily filter
                # since staging has no county column, so TRUNCATE + serial
                # county runs. Load one county at a time from run_all.
                cur.execute("TRUNCATE centroid_stage")
        conn.commit()

        min_oid, max_oid = _fetch_oid_window(source)
        cursor = max(resume_from_oid, min_oid)
        step = source.oid_chunk
        log.info(
            "[fips=%s] OID window %s..%s (%s OIDs), chunks of %s",
            fips_s,
            f"{min_oid:,}",
            f"{max_oid:,}",
            f"{max_oid - min_oid + 1:,}",
            f"{step:,}",
        )

        page_no = 0
        rows_staged = 0
        rows_skipped = 0

        while True:
            page_no += 1
            feats = fetch_page(source, cursor)

            batch: List[Tuple[str, float, float]] = []
            for f in feats:
                attrs = f.get("attributes") or {}
                pid = _extract_join_key(attrs, fips)
                lat, lon = _extract_latlon(f)
                if pid is None or lat is None or lon is None:
                    rows_skipped += 1
                    continue
                batch.append((pid, lat, lon))

            if batch and not dry_run:
                with conn.cursor() as cur:
                    cur.executemany(INSERT_SQL, batch)
                conn.commit()
            rows_staged += len(batch)

            last_page = cursor + step > max_oid
            if page_no % 10 == 0 or last_page:
                log.info(
                    "[fips=%s] page %d, OID %s/%s -> %s staged, %s skipped",
                    fips_s, page_no,
                    f"{cursor:,}", f"{max_oid:,}",
                    f"{rows_staged:,}", f"{rows_skipped:,}",
                )

            if last_page:
                break
            cursor += step
            time.sleep(POLITE_SLEEP_S)

        # --------- push staging into parcels ---------
        matched = 0
        staged_total = 0
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM parcels WHERE county_fips = %s", (fips_s,))
            total = cur.fetchone()[0]

            if not dry_run:
                cur.execute("SELECT COUNT(*) FROM centroid_stage")
                staged_total = cur.fetchone()[0]
                cur.execute(
                    """
                    UPDATE parcels p
                       SET lat = s.lat,
                           lon = s.lon,
                           geom = ST_SetSRID(ST_MakePoint(s.lon, s.lat), 4326)
                      FROM centroid_stage s
                     WHERE p.county_fips = %s
                       AND p.parcel_id = s.parcel_id
                    """,
                    (fips_s,),
                )
                matched = cur.rowcount
                conn.commit()

        pct = (matched / total * 100) if total else 0
        log.info(
            "[fips=%s] coverage: total parcels=%s, staged=%s, matched=%s (%.1f%%)",
            fips_s, f"{total:,}", f"{staged_total:,}", f"{matched:,}", pct,
        )
        return {
            "county_fips": fips_s,
            "total_parcels": int(total),
            "staged": int(staged_total),
            "matched": int(matched),
            "coverage_pct": round(pct, 1),
        }
    finally:
        conn.close()


def run_all(dry_run: bool = False) -> List[Dict[str, Any]]:
    return [load_county(SOURCES[f], dry_run=dry_run) for f in (23, 16, 60)]


# --------- CLI ---------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill parcels.lat/lon/geom from the FL Statewide centroid layer"
    )
    parser.add_argument("--county", required=True, help="23 | 16 | 60 | all")
    parser.add_argument("--resume-from-oid", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.county == "all":
        if args.resume_from_oid:
            raise SystemExit("--resume-from-oid only valid with a single county")
        run_all(dry_run=args.dry_run)
        return

    try:
        fips = int(args.county)
    except ValueError:
        raise SystemExit(f"invalid --county {args.county!r}; use 23, 16, 60, or 'all'")
    if fips not in SOURCES:
        raise SystemExit(f"unknown --county {fips}; valid: {sorted(SOURCES)} or 'all'")

    load_county(SOURCES[fips], dry_run=args.dry_run, resume_from_oid=args.resume_from_oid)


if __name__ == "__main__":
    main()
