"""Military installation ingest (Phase C3).

Populates military_installations from one of, in order:
  1. Any *.geojson under data/military/ (loaded first).
  2. MILITARY_SOURCE_URL env var — an ArcGIS REST FeatureServer/MapServer
     layer that serves DoD/USCG installation polygons (e.g. HIFLD's
     "Military Installations"). Fetched as GeoJSON via f=geojson with a
     Florida filter appended when possible.
  3. Hardcoded tri-county fallback — installation centroids buffered to
     approximate footprints. Ensures the statutory ¼-mile exclusion still
     runs when no shapefile is at hand; source is labeled 'fallback:hardcoded'
     so downstream analysts can see the data provenance.

§163.2525 excludes parcels within ¼ mile of a military installation. The
screening query does the ¼-mile buffer via ST_DWithin at query time; this
module stores the raw installation boundary (or an approximate footprint
for fallback entries).
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


DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "military"
MILITARY_SOURCE_URL = os.getenv("MILITARY_SOURCE_URL", "").strip()
HTTP_TIMEOUT = 60


# Hardcoded fallback set — the SB 1434-relevant installations in the tri-
# county footprint per §163.3175(2). Coordinates are installation centroids
# in WGS84; footprint_m is the buffer radius that approximates each
# installation's boundary (later expanded by ¼ mile at screening time).
FALLBACK_INSTALLATIONS: List[Dict[str, Any]] = [
    {
        "name": "Homestead Air Reserve Base",
        "branch": "USAF Reserve",
        "lat": 25.4867, "lon": -80.3833, "footprint_m": 1500,
    },
    {
        "name": "US Southern Command (SOUTHCOM) HQ",
        "branch": "DoD",
        "lat": 25.8203, "lon": -80.3820, "footprint_m": 400,
    },
    {
        "name": "USCG Base Miami Beach",
        "branch": "USCG",
        "lat": 25.7752, "lon": -80.1656, "footprint_m": 250,
    },
    {
        "name": "USCG Sector Miami / Station Miami Beach",
        "branch": "USCG",
        "lat": 25.7690, "lon": -80.1350, "footprint_m": 200,
    },
    {
        "name": "USCG Station Fort Lauderdale",
        "branch": "USCG",
        "lat": 26.0997, "lon": -80.1122, "footprint_m": 200,
    },
    {
        "name": "Homestead Armory (FL Army National Guard)",
        "branch": "FL Army NG",
        "lat": 25.5300, "lon": -80.3866, "footprint_m": 150,
    },
]


# ---------- source resolution ----------

def _local_geojson() -> Optional[Path]:
    if not DATA_DIR.exists():
        return None
    hits = sorted(DATA_DIR.glob("*.geojson"))
    return hits[0] if hits else None


def _fetch_from_url(url: str) -> Dict[str, Any]:
    if "/query" not in url:
        params = {
            # Narrow to Florida if the layer has STATE_TERR or STATE; if not,
            # the where='1=1' fallback is fine — we filter tri-county via the
            # spatial join downstream.
            "where": "STATE_TERR='FL' OR STATE='FL' OR 1=1",
            "outFields": "*",
            "outSR": "4326",
            "f": "geojson",
        }
        url = f"{url}/query?{urllib.parse.urlencode(params)}"
    log.info("Fetching military installations from %s", url)
    r = requests.get(url, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} from military source: {r.text[:400]}")
    return r.json()


# ---------- upsert ----------

_UPSERT_POLYGON = text(
    """
    INSERT INTO military_installations (name, branch, state, source, geom)
    VALUES (
        :name, :branch, :state, :source,
        ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:geom_json), 4326))
    )
    ON CONFLICT (name) DO UPDATE SET
        branch = EXCLUDED.branch,
        state = EXCLUDED.state,
        source = EXCLUDED.source,
        geom = EXCLUDED.geom
    """
)


# For fallback records we synthesize a polygon by buffering the centroid to
# footprint_m meters (using geography for accuracy), then storing as
# MULTIPOLYGON geometry in SRID 4326.
_UPSERT_BUFFERED = text(
    """
    INSERT INTO military_installations (name, branch, state, source, geom)
    VALUES (
        :name, :branch, 'FL', :source,
        ST_Multi(
          ST_SetSRID(
            ST_Buffer(
              ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
              :footprint_m
            )::geometry,
            4326
          )
        )
    )
    ON CONFLICT (name) DO UPDATE SET
        branch = EXCLUDED.branch,
        state = EXCLUDED.state,
        source = EXCLUDED.source,
        geom = EXCLUDED.geom
    """
)


# ---------- ingest paths ----------

def _extract_state(props: Dict[str, Any]) -> Optional[str]:
    for key in ("STATE_TERR", "STATE", "STATE_ABBR", "STUSPS"):
        v = props.get(key)
        if v:
            return str(v)[:2]
    return None


def _extract_name(props: Dict[str, Any], fallback_idx: int) -> str:
    for key in ("SITE_NAME", "INSTALLATION", "NAME", "INSTALLATIONNAME"):
        v = props.get(key)
        if v:
            return str(v)[:150]
    return f"military installation {fallback_idx}"


def _extract_branch(props: Dict[str, Any]) -> Optional[str]:
    for key in ("BRANCH", "COMPONENT", "SERVICE", "OWNER"):
        v = props.get(key)
        if v:
            return str(v)[:50]
    return None


def _ingest_from_geojson(geojson: Dict[str, Any], source_label: str) -> int:
    feats = geojson.get("features", [])
    if not feats:
        raise RuntimeError("military source returned no features")

    inserted = 0
    with engine.begin() as conn:
        for idx, feat in enumerate(feats, start=1):
            props = feat.get("properties") or {}
            state = _extract_state(props)
            # If the layer is nationwide, keep only Florida rows. If the
            # layer has no state field, keep everything — the ¼-mile
            # spatial join is cheap enough that non-FL rows are harmless.
            if state is not None and state.upper() != "FL":
                continue
            geom = feat.get("geometry")
            if not geom:
                continue
            conn.execute(
                _UPSERT_POLYGON,
                {
                    "name": _extract_name(props, idx),
                    "branch": _extract_branch(props),
                    "state": state,
                    "source": source_label[:200],
                    "geom_json": json.dumps(geom),
                },
            )
            inserted += 1

    return inserted


def _ingest_from_fallback() -> int:
    log.warning(
        "Using hardcoded fallback military installations. Set MILITARY_SOURCE_URL "
        "or drop a *.geojson under %s for authoritative data.",
        DATA_DIR,
    )
    inserted = 0
    with engine.begin() as conn:
        for row in FALLBACK_INSTALLATIONS:
            conn.execute(
                _UPSERT_BUFFERED,
                {
                    "name": row["name"],
                    "branch": row["branch"],
                    "source": "fallback:hardcoded",
                    "lat": row["lat"],
                    "lon": row["lon"],
                    "footprint_m": row["footprint_m"],
                },
            )
            inserted += 1
    return inserted


def ingest() -> Dict[str, Any]:
    """Replace military_installations from the best available source."""
    log.info("=== Military installations ingest starting ===")

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM military_installations"))

    inserted = 0
    source: str

    local = _local_geojson()
    if local is not None:
        log.info("Loading military installations from local file: %s", local)
        with local.open("r", encoding="utf-8") as f:
            geojson = json.load(f)
        inserted = _ingest_from_geojson(geojson, f"file:{local.name}")
        source = f"file:{local.name}"
    elif MILITARY_SOURCE_URL:
        try:
            geojson = _fetch_from_url(MILITARY_SOURCE_URL)
            inserted = _ingest_from_geojson(geojson, f"url:{MILITARY_SOURCE_URL}")
            source = f"url:{MILITARY_SOURCE_URL}"
        except Exception as e:
            log.error("URL ingest failed (%s); falling back to hardcoded set", e)
            inserted = _ingest_from_fallback()
            source = "fallback:hardcoded"
    else:
        inserted = _ingest_from_fallback()
        source = "fallback:hardcoded"

    log.info("Military ingest done: %d installation(s) from %s", inserted, source)
    return {"inserted": inserted, "source": source}


if __name__ == "__main__":  # pragma: no cover
    ingest()
