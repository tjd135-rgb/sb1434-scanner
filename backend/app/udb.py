"""Miami-Dade Urban Development Boundary ingest (Phase C2).

Populates udb_boundary from one of:
  1. UDB_SOURCE_URL env var pointing at an ArcGIS REST FeatureServer/MapServer
     layer that serves the UDB polygon (fetched as GeoJSON via f=geojson).
  2. Any *.geojson file under data/udb/ (loaded first).

Miami-Dade Open Data (gis-mdc.opendata.arcgis.com) hosts the UDB but the
exact endpoint has rotated across service tenants over the years and can't
be reliably auto-discovered. The recommended workflow is:
  - Search the MDC hub for "Urban Development Boundary",
  - Grab the FeatureServer URL from the layer's "View API Resources" panel,
  - Set UDB_SOURCE_URL in the Render environment,
  OR
  - Download the layer as GeoJSON and drop it at data/udb/udb.geojson.

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


def _fetch_from_url(url: str) -> Dict[str, Any]:
    """Fetch a GeoJSON FeatureCollection from an ArcGIS REST layer URL.

    If the URL already ends in /query, use it as-is; otherwise append the
    standard /query?where=1=1&outFields=*&outSR=4326&f=geojson tail.
    """
    if "/query" not in url:
        params = {
            "where": "1=1",
            "outFields": "*",
            "outSR": "4326",
            "f": "geojson",
        }
        url = f"{url}/query?{urllib.parse.urlencode(params)}"
    log.info("Fetching UDB from %s", url)
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

_UPSERT = text(
    """
    INSERT INTO udb_boundary (name, source, geom)
    VALUES (
        :name, :source,
        ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:geom_json), 4326))
    )
    """
)


def _feature_name(feat: Dict[str, Any], fallback_idx: int) -> str:
    props = feat.get("properties") or {}
    for key in ("NAME", "Name", "name", "UDB_NAME", "BOUNDARY_NAME"):
        v = props.get(key)
        if v:
            return str(v)[:100]
    return f"UDB polygon {fallback_idx}"


def ingest() -> Dict[str, Any]:
    """Replace udb_boundary contents from the configured source."""
    log.info("=== UDB ingest starting ===")
    geojson, source_label = _load_geojson()

    feats = geojson.get("features", [])
    if not feats:
        raise RuntimeError("UDB source returned no features")

    inserted = 0
    with engine.begin() as conn:
        # Full replace — the UDB is not versioned per-row, so treat every
        # ingest as authoritative-and-current.
        conn.execute(text("DELETE FROM udb_boundary"))
        for idx, feat in enumerate(feats, start=1):
            geom = feat.get("geometry")
            if not geom:
                log.warning("Feature %d missing geometry, skipping", idx)
                continue
            conn.execute(
                _UPSERT,
                {
                    "name": _feature_name(feat, idx),
                    "source": source_label[:200],
                    "geom_json": json.dumps(geom),
                },
            )
            inserted += 1

    log.info("UDB ingest done: %d polygon(s) inserted from %s", inserted, source_label)
    return {"inserted": inserted, "source": source_label}


if __name__ == "__main__":  # pragma: no cover
    ingest()
