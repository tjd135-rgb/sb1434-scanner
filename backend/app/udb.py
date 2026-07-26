"""Miami-Dade Urban Development Boundary ingest (Phase C2).

Populates udb_boundary from one of:
  1. Any *.geojson file under data/udb/ (loaded first).
  2. UDB_SOURCE_URL env var pointing at an ArcGIS REST FeatureServer/MapServer
     layer that serves the UDB (fetched as GeoJSON via f=geojson).

Miami-Dade publishes the UDB as a **LineString** feature collection (32
segments in the version shipped in-repo), not a closed polygon. We
polygonize at ingest time via a fallback chain, first-hit wins:
  1. ST_Polygonize(ST_Union(g)) → largest polygon by area  (PRIMARY)
     ST_Union noded/dissolves gaps that would trip ST_LineMerge, and
     ST_Polygonize returns every closed region formed by the network.
     The largest is the UDB itself.
  2. ST_MakePolygon(ST_LineMerge(ST_Collect(g)))            — cleanest when
     the merged segments already form a single closed ring.
  3. ST_MakePolygon(ST_LineMerge(ST_Node(ST_Collect(g))))   — same, after
     noding to clean intersections/T-junctions.
  4. ST_BuildArea(ST_Node(ST_Collect(g)))                   — handles
     arbitrary planar topology; returns whatever closed regions form.
  5. ST_ConvexHull(ST_Collect(g))                           — absolute
     last resort. Guaranteed to produce a polygon but WILDLY over-
     estimates the UDB (swallows the west-of-UDB conservation area).
     Only useful so downstream Miami-Dade parcels don't all end up
     udb_status='outside'.

Each strategy runs inside its own SAVEPOINT so a PostGIS error in one
attempt can't poison the outer transaction and block later attempts. The
chosen strategy is recorded in the source label so provenance stays
visible in the DB.

After insert, ST_Area (in acres) and ST_IsValid are logged as a sanity
check — Miami-Dade's UDB should span most of urban MD (~400k acres).

The screen treats parcels inside as normal and parcels outside as *flagged*
(not excluded) — the UDB is a strong developability signal but not a
statutory §163.2525 exclusion.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from sqlalchemy import text

from .db import engine

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(_h)


DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "udb"
UDB_SOURCE_URL = os.getenv("UDB_SOURCE_URL", "").strip()
HTTP_TIMEOUT = 60


# ---------- source resolution ----------

def _local_geojson() -> Optional[Path]:
    if not DATA_DIR.exists():
        return None
    hits = sorted(DATA_DIR.glob("*.geojson"))
    return hits[0] if hits else None


def _is_plain_geojson_url(url: str) -> bool:
    """True if the URL points at a static GeoJSON/JSON file (raw GitHub,
    S3, etc.) rather than an ArcGIS REST endpoint. Ignores query string
    and fragment when inspecting the extension."""
    path = urllib.parse.urlparse(url).path.lower()
    return path.endswith(".geojson") or path.endswith(".json")


def _fetch_from_url(url: str) -> Dict[str, Any]:
    """Fetch a GeoJSON FeatureCollection from either a static file URL
    (e.g. raw.githubusercontent.com/.../udb.geojson) or an ArcGIS REST
    layer URL.

    Static file: simple GET, parse JSON as-is.
    ArcGIS REST: append /query?where=1=1&outFields=*&outSR=4326&f=geojson
    (unless the URL already ends in /query, in which case use as-is).
    """
    if _is_plain_geojson_url(url):
        log.info("Fetching UDB (plain GeoJSON) from %s", url)
    else:
        if "/query" not in url:
            params = {
                "where": "1=1",
                "outFields": "*",
                "outSR": "4326",
                "f": "geojson",
            }
            url = f"{url}/query?{urllib.parse.urlencode(params)}"
        log.info("Fetching UDB (ArcGIS REST) from %s", url)

    r = requests.get(url, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} from UDB source: {r.text[:400]}")
    return r.json()


def _load_geojson() -> tuple[Dict[str, Any], str]:
    """Return (geojson, source_label). Prefers local file over URL."""
    local = _local_geojson()
    if local is not None:
        log.info("Loading UDB from local file: %s", local)
        with local.open("r", encoding="utf-8") as f:
            return json.load(f), f"file:{local.name}"

    if UDB_SOURCE_URL:
        return _fetch_from_url(UDB_SOURCE_URL), f"url:{UDB_SOURCE_URL}"

    raise RuntimeError(
        "No UDB source configured. Either drop a *.geojson under "
        f"{DATA_DIR} or set UDB_SOURCE_URL to a Miami-Dade UDB "
        "ArcGIS REST layer."
    )


# ---------- upsert ----------

# Direct-insert path for features that already arrive as (Multi)Polygons.
_UPSERT_POLYGON = text(
    """
    INSERT INTO udb_boundary (name, source, geom)
    VALUES (
        :name, :source,
        ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:geom_json), 4326))
    )
    """
)


# Polygonization strategies, tried in order. Each is an INSERT ... SELECT
# that produces zero rows when its strategy can't build a valid non-empty
# polygon; the first strategy to insert a row wins. Working over a TEMP
# TABLE udb_lines populated up-front so PostGIS does all the geometry work
# server-side.
#
# Each strategy runs inside its own SAVEPOINT (see _polygonize_lines) so
# that a PostGIS error in one attempt doesn't poison the outer transaction
# and block later attempts.
_POLYGONIZE_STRATEGIES = [
    # PRIMARY: ST_Union nodes/dissolves the linework across all inputs, and
    # ST_Polygonize builds every polygon that the noded network describes.
    # In practice the Miami-Dade UDB comes back as several polygons (the
    # main boundary plus a handful of small inclusions/holes). Take the
    # largest by geographic area — that's the UDB itself.
    (
        "polygonize_union_largest",
        text(
            """
            INSERT INTO udb_boundary (name, source, geom)
            WITH parts AS (
                SELECT (ST_Dump(ST_Polygonize(ST_Union(g)))).geom AS poly
                  FROM udb_lines
            )
            SELECT :name, :source, ST_Multi(poly)
              FROM parts
             WHERE poly IS NOT NULL
               AND GeometryType(poly) IN ('POLYGON', 'MULTIPOLYGON')
               AND NOT ST_IsEmpty(poly)
             ORDER BY ST_Area(poly::geography) DESC
             LIMIT 1
            RETURNING id
            """
        ),
    ),
    (
        "linemerge_makepolygon",
        text(
            """
            INSERT INTO udb_boundary (name, source, geom)
            SELECT :name, :source, ST_Multi(ST_MakePolygon(m))
              FROM (
                SELECT ST_LineMerge(ST_Collect(g)) AS m FROM udb_lines
              ) t
             WHERE m IS NOT NULL
               AND GeometryType(m) = 'LINESTRING'
               AND ST_IsClosed(m)
               AND ST_NPoints(m) >= 4
            RETURNING id
            """
        ),
    ),
    (
        "noded_linemerge_makepolygon",
        text(
            """
            INSERT INTO udb_boundary (name, source, geom)
            SELECT :name, :source, ST_Multi(ST_MakePolygon(m))
              FROM (
                SELECT ST_LineMerge(ST_Node(ST_Collect(g))) AS m FROM udb_lines
              ) t
             WHERE m IS NOT NULL
               AND GeometryType(m) = 'LINESTRING'
               AND ST_IsClosed(m)
               AND ST_NPoints(m) >= 4
            RETURNING id
            """
        ),
    ),
    (
        "noded_buildarea",
        text(
            """
            INSERT INTO udb_boundary (name, source, geom)
            SELECT :name, :source, ST_Multi(poly)
              FROM (
                SELECT ST_BuildArea(ST_Node(ST_Collect(g))) AS poly FROM udb_lines
              ) t
             WHERE poly IS NOT NULL
               AND NOT ST_IsEmpty(poly)
            RETURNING id
            """
        ),
    ),
    # ABSOLUTE LAST RESORT: convex hull of every input vertex. Guaranteed
    # to produce a valid polygon but WILDLY over-estimates the UDB
    # footprint (it'd swallow all of Miami-Dade including the west-of-UDB
    # conservation area). Only useful as a "the ingest didn't crash" fallback
    # so downstream Miami-Dade parcels don't all end up with
    # udb_status='outside'. Source label makes the imprecision obvious.
    (
        "convex_hull_fallback",
        text(
            """
            INSERT INTO udb_boundary (name, source, geom)
            SELECT :name, :source, ST_Multi(hull)
              FROM (
                SELECT ST_ConvexHull(ST_Collect(g)) AS hull FROM udb_lines
              ) t
             WHERE hull IS NOT NULL
               AND GeometryType(hull) IN ('POLYGON', 'MULTIPOLYGON')
            RETURNING id
            """
        ),
    ),
]


_SANITY_CHECK = text(
    """
    SELECT id,
           name,
           source,
           ST_IsValid(geom) AS is_valid,
           ST_NumGeometries(geom) AS n_polygons,
           ST_Area(geom::geography) / 4046.8564224 AS acres
      FROM udb_boundary
     ORDER BY id
    """
)


def _feature_name(feat: Dict[str, Any], fallback_idx: int) -> str:
    props = feat.get("properties") or {}
    for key in ("NAME", "Name", "name", "UDB_NAME", "BOUNDARY_NAME"):
        v = props.get(key)
        if v:
            return str(v)[:100]
    return f"UDB polygon {fallback_idx}"


def _split_features(feats: List[Dict[str, Any]]) -> tuple[list, list]:
    """Return (polygon_features, line_geoms) split by geometry type."""
    poly_feats: List[Dict[str, Any]] = []
    line_geoms: List[Dict[str, Any]] = []
    for idx, feat in enumerate(feats, start=1):
        geom = feat.get("geometry")
        if not geom:
            log.warning("Feature %d missing geometry, skipping", idx)
            continue
        gt = (geom.get("type") or "").lower()
        if gt in ("polygon", "multipolygon"):
            poly_feats.append(feat)
        elif gt in ("linestring", "multilinestring"):
            line_geoms.append(geom)
        else:
            log.warning("Feature %d has unsupported geometry type %r, skipping", idx, gt)
    return poly_feats, line_geoms


def _polygonize_lines(conn, line_geoms: List[Dict[str, Any]], base_source: str) -> Optional[str]:
    """Load all line geoms into a temp table and try each polygonization
    strategy. Returns the name of the strategy that succeeded, or None if
    all failed."""
    conn.execute(text("DROP TABLE IF EXISTS udb_lines"))
    conn.execute(
        text(
            """
            CREATE TEMP TABLE udb_lines (
                g geometry(Geometry, 4326)
            ) ON COMMIT DROP
            """
        )
    )
    for geom in line_geoms:
        conn.execute(
            text(
                "INSERT INTO udb_lines (g) "
                "VALUES (ST_SetSRID(ST_GeomFromGeoJSON(:g), 4326))"
            ),
            {"g": json.dumps(geom)},
        )
    log.info("Staged %d LineString(s) for polygonization", len(line_geoms))

    for strat, stmt in _POLYGONIZE_STRATEGIES:
        # Each strategy runs inside its own SAVEPOINT. Without this, the
        # first PostGIS error would abort the outer transaction and every
        # subsequent execute() would fail with "current transaction is
        # aborted, commands ignored until end of transaction block" — the
        # fallback strategies would never actually get to run.
        savepoint = conn.begin_nested()
        try:
            result = conn.execute(
                stmt,
                {
                    "name": "Miami-Dade UDB",
                    "source": f"{base_source} [polygonized:{strat}]"[:200],
                },
            )
            inserted_ids = [row[0] for row in result]
        except Exception as e:  # noqa: BLE001 — try next strategy on any PostGIS error
            savepoint.rollback()
            log.warning("Polygonization strategy %r raised: %s", strat, e)
            continue

        if inserted_ids:
            savepoint.commit()
            log.info("Polygonization strategy %r succeeded (id=%s)", strat, inserted_ids[0])
            return strat

        # Strategy ran cleanly but produced 0 rows (the WHERE clause
        # filtered everything out). Undo whatever the statement did and
        # try the next strategy.
        savepoint.rollback()
        log.info("Polygonization strategy %r produced no polygon; trying next", strat)

    return None


def _log_sanity_checks(conn) -> List[Dict[str, Any]]:
    """Log ST_Area (acres), ST_IsValid, and part count for every UDB row.
    Returns the mapping list so callers can include it in the summary."""
    rows = conn.execute(_SANITY_CHECK).mappings().all()
    out = []
    for r in rows:
        info = {
            "id": r["id"],
            "name": r["name"],
            "source": r["source"],
            "acres": float(r["acres"]) if r["acres"] is not None else None,
            "is_valid": bool(r["is_valid"]),
            "n_polygons": int(r["n_polygons"]) if r["n_polygons"] is not None else None,
        }
        log.info(
            "UDB row id=%s: %s acres, valid=%s, %s polygon(s), source=%s",
            info["id"],
            f"{info['acres']:,.1f}" if info["acres"] is not None else "n/a",
            info["is_valid"],
            info["n_polygons"],
            info["source"],
        )
        # Sanity: Miami-Dade UDB should span roughly ~400k acres. Warn on
        # anything wildly off — likely a botched polygonization.
        if info["acres"] is not None and (info["acres"] < 50_000 or info["acres"] > 1_500_000):
            log.warning(
                "UDB polygon acreage %s is outside the expected 50k..1.5M range — "
                "polygonization may have failed to close the boundary correctly.",
                f"{info['acres']:,.1f}",
            )
        out.append(info)
    return out


def ingest() -> Dict[str, Any]:
    """Replace udb_boundary contents from the configured source."""
    log.info("=== UDB ingest starting ===")
    geojson, source_label = _load_geojson()

    feats = geojson.get("features", [])
    if not feats:
        raise RuntimeError("UDB source returned no features")

    poly_feats, line_geoms = _split_features(feats)
    log.info(
        "Source has %d polygon feature(s) and %d line feature(s)",
        len(poly_feats), len(line_geoms),
    )

    inserted = 0
    used_strategy: Optional[str] = None

    with engine.begin() as conn:
        # Full replace — the UDB is not versioned per-row, so treat every
        # ingest as authoritative-and-current.
        conn.execute(text("DELETE FROM udb_boundary"))

        # Direct insert for any pre-polygonized features.
        for idx, feat in enumerate(poly_feats, start=1):
            conn.execute(
                _UPSERT_POLYGON,
                {
                    "name": _feature_name(feat, idx),
                    "source": source_label[:200],
                    "geom_json": json.dumps(feat["geometry"]),
                },
            )
            inserted += 1

        # Polygonize any LineStrings into a single boundary polygon.
        if line_geoms:
            used_strategy = _polygonize_lines(conn, line_geoms, source_label)
            if used_strategy is None:
                raise RuntimeError(
                    "All polygonization strategies failed — the UDB LineStrings "
                    "did not form a closable boundary. Inspect the source file."
                )
            inserted += 1

        sanity = _log_sanity_checks(conn)

    if not sanity:
        raise RuntimeError("UDB ingest produced no rows")

    log.info(
        "UDB ingest done: %d row(s) inserted from %s (polygonization=%s)",
        inserted, source_label, used_strategy or "none",
    )
    return {
        "inserted": inserted,
        "source": source_label,
        "polygonization_strategy": used_strategy,
        "rows": sanity,
    }


if __name__ == "__main__":  # pragma: no cover
    ingest()
