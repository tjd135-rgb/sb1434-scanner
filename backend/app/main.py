"""FastAPI app for sb1434-scanner (Phases A + B).

Endpoints:
- GET  /health
- GET  /brownfield-areas               (list, filterable by county)
- GET  /brownfield-areas/{area_id}     (detail + GeoJSON geometry)
- GET  /qualifying-parcels             (list, filterable by county/pathway/etc)
- GET  /qualifying-parcels/{parcel_id} (detail)
- GET  /stats                          (aggregate counts + totals)
- POST /admin/ingest-brownfields       (FDEP ingest)
- POST /admin/ingest-nal               (Phase A: NAL parcel ingest)
- POST /admin/ingest-centroids         (Phase A: parcel-centroid backfill)
- POST /admin/run-screening            (SB 1434 screen)
- GET  /admin/status                   (which long-running jobs are active)
"""
from __future__ import annotations

import logging
import threading
import traceback
from decimal import Decimal
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import brownfields as bf
from . import centroids as cent
from . import ingest as nal
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
        description="Case-insensitive substring match on county (e.g. 'MIAMI-DADE', 'BROWARD', 'PALM BEACH')",
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


class NalIngestRequest(BaseModel):
    # 'miami-dade' | 'broward' | 'palm-beach' | 'all'
    county: str = "all"


class CentroidIngestRequest(BaseModel):
    # '23' | '16' | '60' | 'all'
    county: str = "all"


# In-memory job registry for the long-running admin endpoints. Not persisted
# across restarts — fine for a single-worker Render web service.
_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _start_job(name: str, target, *args, **kwargs) -> Dict[str, Any]:
    """Start a background thread. Refuses to start if a same-named job is
    already running so we don't accidentally double-load the DB."""
    with _JOBS_LOCK:
        existing = _JOBS.get(name)
        if existing and existing.get("status") == "running":
            raise HTTPException(
                status_code=409,
                detail=f"job {name!r} already running since {existing.get('started_at')}",
            )
        job = {
            "name": name,
            "status": "running",
            "started_at": _now_iso(),
            "finished_at": None,
            "result": None,
            "error": None,
        }
        _JOBS[name] = job

    def _run():
        try:
            result = target(*args, **kwargs)
            with _JOBS_LOCK:
                job["status"] = "ok"
                job["result"] = result
        except Exception as e:
            with _JOBS_LOCK:
                job["status"] = "error"
                job["error"] = f"{type(e).__name__}: {e}"
                job["traceback"] = traceback.format_exc()
            log.exception("job %s failed", name)
        finally:
            with _JOBS_LOCK:
                job["finished_at"] = _now_iso()

    threading.Thread(target=_run, name=f"job-{name}", daemon=True).start()
    return {"job": name, "status": "started", "started_at": job["started_at"]}


@app.get("/admin/status")
def admin_status() -> Dict[str, Any]:
    """Snapshot of every background job started this process lifetime."""
    with _JOBS_LOCK:
        # Copy so callers don't see mutations mid-serialize.
        return {"jobs": {k: dict(v) for k, v in _JOBS.items()}}


@app.post("/admin/ingest-brownfields")
def admin_ingest_brownfields() -> Dict[str, Any]:
    """Kick off FDEP ingest in a background thread. Poll /admin/status."""
    return _start_job("ingest-brownfields", bf.ingest_all)


@app.post("/admin/ingest-nal")
def admin_ingest_nal(payload: NalIngestRequest | None = None) -> Dict[str, Any]:
    """Kick off NAL parcel ingest. Loads DOR NAL for the given county (or 'all')
    from the LLA scanner's GitHub release. Runs in background — this is a
    multi-minute operation."""
    county = (payload.county if payload else "all").lower()
    if county != "all" and county not in nal.COUNTIES:
        raise HTTPException(
            status_code=400,
            detail=f"county must be one of {list(nal.COUNTIES) + ['all']}",
        )
    return _start_job(f"ingest-nal:{county}", nal.run, county)


@app.post("/admin/ingest-centroids")
def admin_ingest_centroids(payload: CentroidIngestRequest | None = None) -> Dict[str, Any]:
    """Kick off parcel-centroid backfill against the FL Statewide FeatureServer.
    Runs in background — the full tri-county sweep takes ~30-60 minutes."""
    county = (payload.county if payload else "all").lower()
    if county == "all":
        return _start_job("ingest-centroids:all", cent.run_all)
    try:
        fips = int(county)
    except ValueError:
        raise HTTPException(status_code=400, detail="county must be 23, 16, 60, or 'all'")
    if fips not in cent.SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"county_fips must be one of {sorted(cent.SOURCES)} or 'all'",
        )
    return _start_job(f"ingest-centroids:{fips}", cent.load_county, cent.SOURCES[fips])


@app.post("/admin/run-screening")
def admin_run_screening(payload: ScreenRequest | None = None) -> Dict[str, Any]:
    """Kick off the qualifying-parcel screen. Requires parcels + brownfield_areas
    populated first."""
    update_adjacency = payload.update_adjacency if payload else True
    return _start_job(
        "run-screening",
        scr.run_screen,
        update_adjacency=update_adjacency,
    )
