"""FastAPI app for sb1434-scanner (Phase B).

Endpoints:
- GET  /health
- GET  /brownfield-areas               (list, filterable by county)
- GET  /brownfield-areas/{area_id}     (detail + GeoJSON geometry)
- GET  /qualifying-parcels             (list, filterable by county/pathway/etc)
- GET  /qualifying-parcels/{parcel_id} (detail)
- GET  /stats                          (aggregate counts + totals)
- POST /admin/ingest-brownfields       (fires FDEP ingest; long-running)
- POST /admin/run-screening            (fires the SB 1434 screen)
"""
from __future__ import annotations

import logging
from decimal import Decimal
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import brownfields as bf
from . import screening as scr
from .db import engine, get_db

log = logging.getLogger("sb1434")

app = FastAPI(title="SB 1434 Scanner", version="0.1.0")

# Read-only public API; no auth. Wildcard CORS matches lla-scanner.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- helpers ----------

_TRI_COUNTY_FIPS = {"23", "16", "60"}
_COUNTY_NAMES = {"23": "Miami-Dade", "16": "Broward", "60": "Palm Beach"}
_PATHWAYS = {
    "golf_course", "industrial", "auto_fuel", "office",
    "commercial_retail", "institutional", "utility",
    "residential_redev", "other",
}


def _jsonify_one(row: Dict[str, Any]) -> Dict[str, Any]:
    """Convert Decimal / datetime to JSON-safe primitives."""
    out: Dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, (datetime, date)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _jsonify(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [_jsonify_one(r) for r in rows]


# ---------- health ----------

@app.get("/health")
def health() -> Dict[str, Any]:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            postgis = conn.execute(text("SELECT PostGIS_Version()")).scalar_one_or_none()
        return {"status": "ok", "db": "connected", "postgis": postgis}
    except Exception as e:  # pragma: no cover
        return {"status": "degraded", "db": f"error: {e}"}


# ---------- brownfield areas ----------

_AREA_LIST_COLS = (
    "area_id, area_name, city, county, district, resolution_number, "
    "resolution_date, acreage, latitude, longitude"
)


@app.get("/brownfield-areas")
def list_brownfield_areas(
    county: Optional[str] = Query(
        None,
        description="Case-insensitive substring match on county (e.g. 'DADE', 'BROWARD', 'PALM BEACH')",
    ),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> List[Dict[str, Any]]:
    sql = (
        f"SELECT {_AREA_LIST_COLS} FROM brownfield_areas "
        + ("WHERE county ILIKE :c " if county else "")
        + "ORDER BY acreage DESC NULLS LAST, area_id "
        + "LIMIT :lim OFFSET :off"
    )
    params: Dict[str, Any] = {"lim": limit, "off": offset}
    if county:
        params["c"] = f"%{county}%"
    rows = db.execute(text(sql), params).mappings().all()
    return _jsonify([dict(r) for r in rows])


@app.get("/brownfield-areas/{area_id}")
def get_brownfield_area(area_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    row = db.execute(
        text(
            f"""
            SELECT {_AREA_LIST_COLS}, documents_url,
                   ST_AsGeoJSON(geom)::json AS geometry
              FROM brownfield_areas
             WHERE area_id = :aid
            """
        ),
        {"aid": area_id},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="brownfield area not found")
    return _jsonify_one(dict(row))


# ---------- qualifying parcels ----------

_QP_COLS = (
    "parcel_id, county_fips, acres, env_trigger, brownfield_area_id, "
    "brownfield_area_name, adjacent_residential, ag_exclusion, park_exclusion, "
    "utility_flag, dor_uc, own_name, pathway_hint, latitude, longitude"
)


@app.get("/qualifying-parcels")
def list_qualifying_parcels(
    county: Optional[str] = Query(
        None, description="county_fips: 23 (Miami-Dade), 16 (Broward), 60 (Palm Beach)"
    ),
    pathway: Optional[str] = Query(None, description="pathway_hint value"),
    env_trigger: Optional[str] = Query(
        None, description="'brownfield_area', 'cleanup_proximity', 'both'"
    ),
    min_acres: float = Query(5.0, ge=0),
    adjacent_only: bool = Query(
        False, description="If true, restrict to parcels that pass Gate 4"
    ),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> List[Dict[str, Any]]:
    if county is not None and county not in _TRI_COUNTY_FIPS:
        raise HTTPException(status_code=400, detail=f"county must be one of {sorted(_TRI_COUNTY_FIPS)}")
    if pathway is not None and pathway not in _PATHWAYS:
        raise HTTPException(status_code=400, detail=f"pathway must be one of {sorted(_PATHWAYS)}")

    where = ["acres >= :min_acres"]
    params: Dict[str, Any] = {"min_acres": min_acres, "lim": limit, "off": offset}
    if county:
        where.append("county_fips = :cf")
        params["cf"] = county
    if pathway:
        where.append("pathway_hint = :pw")
        params["pw"] = pathway
    if env_trigger:
        where.append("env_trigger = :et")
        params["et"] = env_trigger
    if adjacent_only:
        where.append("adjacent_residential = true")

    sql = (
        f"SELECT {_QP_COLS} FROM qualifying_parcels "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY acres DESC NULLS LAST "
        "LIMIT :lim OFFSET :off"
    )
    rows = db.execute(text(sql), params).mappings().all()
    return _jsonify([dict(r) for r in rows])


@app.get("/qualifying-parcels/{parcel_id}")
def get_qualifying_parcel(parcel_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    row = db.execute(
        text(f"SELECT {_QP_COLS} FROM qualifying_parcels WHERE parcel_id = :pid"),
        {"pid": parcel_id},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="qualifying parcel not found")
    out = _jsonify_one(dict(row))
    out["county_name"] = _COUNTY_NAMES.get(out.get("county_fips"))
    return out


# ---------- stats ----------

@app.get("/stats")
def stats() -> Dict[str, Any]:
    summary = scr.summary()
    # Attach human-readable county names.
    for row in summary["by_county"]:
        row["county_name"] = _COUNTY_NAMES.get(row["county_fips"])
    with engine.connect() as conn:
        n_areas = conn.execute(text("SELECT COUNT(*) FROM brownfield_areas")).scalar_one()
        n_sites = conn.execute(text("SELECT COUNT(*) FROM brownfield_sites")).scalar_one()
        n_parcels = conn.execute(text("SELECT COUNT(*) FROM parcels")).scalar_one()
    summary["source_counts"] = {
        "parcels": int(n_parcels),
        "brownfield_areas": int(n_areas),
        "brownfield_sites": int(n_sites),
    }
    return summary


# ---------- admin ----------

class ScreenRequest(BaseModel):
    update_adjacency: bool = True


@app.post("/admin/ingest-brownfields")
def admin_ingest_brownfields() -> Dict[str, Any]:
    """Synchronous FDEP ingest. Takes a few minutes; consider background
    execution if the frontend calls this on demand."""
    try:
        return bf.ingest_all()
    except Exception as e:
        log.exception("brownfield ingest failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/run-screening")
def admin_run_screening(payload: ScreenRequest | None = None) -> Dict[str, Any]:
    """Fire the qualifying-parcel screen. Requires parcels + brownfield_areas
    to be populated first."""
    update_adjacency = payload.update_adjacency if payload else True
    try:
        return scr.run_screen(update_adjacency=update_adjacency)
    except Exception as e:
        log.exception("screening failed")
        raise HTTPException(status_code=500, detail=str(e))
