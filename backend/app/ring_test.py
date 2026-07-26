"""Golf-course ring test (Phase D).

For every qualifying_parcels row that is a golf course (DOR_UC = '038'),
sample points around a ring 200 ft outside the parcel's approximate
boundary and record what percentage of the neighboring parcels are
single-family residential (DOR 001-009).

We only have the parcel centroid and its acreage — not a real polygon —
so the boundary is approximated as a circle of equivalent area:
    radius_m = sqrt(acres * 4046.8564 / pi)
Sample points sit at radius_m + 61 m (200 ft) from the centroid, spaced
every 30 degrees (12 samples). At each sample point we KNN-lookup the
nearest OTHER parcel (excluding the golf parcel itself), read its DOR
code, and count it as SF-residential iff dor_uc BETWEEN '001' AND '009'.

Result buckets (per pathways.classify_ring_pct):
    >= 80 %          → 'ringed'            → pathway_1_golf_ringed
    40 % .. < 80 %   → 'partially_ringed'  → pathway_1b_golf_partial
    <  40 %          → 'not_ringed'        → pathway_2_golf_not_ringed

The full per-sample results (bearing, nearest DOR, distance) are archived
in ring_test_samples (JSONB) so a downstream UI can visualize which side
of a course is the "unringed" edge.

Caveats:
- Circular buffer is an approximation. Some courses are long/narrow
  (fits around lakes) and 12-bearing sampling will miss shape detail.
  A ring pct near a threshold should be spot-checked visually.
- If the KNN lookup returns the golf parcel itself (large course, tiny
  buffer), we filter it out via (county_fips, parcel_id) != self.
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from . import pathways
from .db import engine

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(_h)


# 200 ft in meters — the buffer outside the parcel boundary at which we
# sample. Small enough to catch the immediate neighbor, large enough not
# to fall inside the golf course itself on realistic footprints.
BUFFER_FT = 200
BUFFER_METERS = BUFFER_FT * 0.3048  # 60.96

# 12 bearings every 30 degrees.
SAMPLE_BEARINGS: List[int] = list(range(0, 360, 30))

# 1 acre = 4046.8564224 m^2
SQM_PER_ACRE = 4046.8564224


# ---------- SQL ----------

_GOLF_PARCELS_SQL = text(
    """
    SELECT parcel_id,
           county_fips,
           acres,
           latitude,
           longitude
      FROM qualifying_parcels
     WHERE dor_uc = '038'
       AND geom IS NOT NULL
       AND acres IS NOT NULL
       AND acres > 0
     ORDER BY parcel_id
    """
)

# One query returns all 12 sample results for a single golf parcel. Uses
# ST_Project (a geography function) to walk metric distances around the
# centroid, then a LATERAL KNN join to find the nearest non-self parcel.
_SAMPLE_SQL = text(
    """
    WITH samples AS (
        SELECT b.bearing,
               ST_Project(
                   ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
                   :dist_m,
                   radians(b.bearing)
               )::geometry AS pt
          FROM unnest(CAST(:bearings AS integer[])) AS b(bearing)
    )
    SELECT s.bearing,
           n.county_fips        AS nearest_county_fips,
           n.parcel_id          AS nearest_parcel_id,
           n.dor_uc             AS nearest_dor_uc,
           ST_Distance(n.geom::geography, s.pt::geography) AS nearest_distance_m,
           (n.dor_uc BETWEEN '001' AND '009') AS is_residential
      FROM samples s
      CROSS JOIN LATERAL (
          SELECT county_fips, parcel_id, dor_uc, geom
            FROM parcels
           WHERE geom IS NOT NULL
             AND NOT (county_fips = :self_county AND parcel_id = :self_pid)
           ORDER BY geom <-> s.pt
           LIMIT 1
      ) n
     ORDER BY s.bearing
    """
)


_UPDATE_RESULT = text(
    """
    UPDATE qualifying_parcels
       SET ring_test_pct     = :pct,
           ring_test_result  = :result,
           ring_test_samples = CAST(:samples AS jsonb),
           pathway_hint      = :pathway
     WHERE parcel_id = :parcel_id
    """
)


# ---------- ring test ----------

def _ring_distance_m(acres: float) -> float:
    """Radius of an equivalent-area circle + BUFFER_METERS."""
    area_m2 = float(acres) * SQM_PER_ACRE
    r_parcel = math.sqrt(area_m2 / math.pi)
    return r_parcel + BUFFER_METERS


def _process_parcel(conn, parcel: Dict[str, Any]) -> Dict[str, Any]:
    """Run the 12-sample ring test on one golf parcel; update its row.
    Returns a summary dict for aggregate logging."""
    pid = parcel["parcel_id"]
    dist_m = _ring_distance_m(parcel["acres"])

    rows = conn.execute(
        _SAMPLE_SQL,
        {
            "lat": float(parcel["latitude"]),
            "lon": float(parcel["longitude"]),
            "dist_m": dist_m,
            "bearings": SAMPLE_BEARINGS,
            "self_county": parcel["county_fips"],
            "self_pid": pid,
        },
    ).mappings().all()

    samples: List[Dict[str, Any]] = []
    residential_hits = 0
    for r in rows:
        is_res = bool(r["is_residential"])
        if is_res:
            residential_hits += 1
        samples.append(
            {
                "bearing_deg": int(r["bearing"]),
                "nearest_county_fips": r["nearest_county_fips"],
                "nearest_parcel_id": r["nearest_parcel_id"],
                "nearest_dor_uc": r["nearest_dor_uc"],
                "nearest_distance_m": (
                    float(r["nearest_distance_m"])
                    if r["nearest_distance_m"] is not None
                    else None
                ),
                "is_residential": is_res,
            }
        )

    total = len(samples)
    pct = (residential_hits / total * 100.0) if total else 0.0
    result = pathways.classify_ring_pct(pct)
    pathway = pathways.pathway_from_ring_result(result)

    conn.execute(
        _UPDATE_RESULT,
        {
            "parcel_id": pid,
            "pct": pct,
            "result": result,
            "samples": json.dumps(
                {
                    "buffer_ft": BUFFER_FT,
                    "ring_radius_m": round(dist_m, 2),
                    "residential_hits": residential_hits,
                    "total_samples": total,
                    "samples": samples,
                }
            ),
            "pathway": pathway,
        },
    )

    log.info(
        "ring_test parcel=%s acres=%.1f dist=%.0fm hits=%d/%d pct=%.1f%% → %s (%s)",
        pid, float(parcel["acres"]), dist_m, residential_hits, total, pct, result, pathway,
    )

    return {
        "parcel_id": pid,
        "county_fips": parcel["county_fips"],
        "pct": pct,
        "result": result,
        "pathway": pathway,
    }


def run_all() -> Dict[str, Any]:
    """Ring-test every dor_uc='038' qualifying parcel and update in place."""
    log.info("=== Golf-course ring test starting ===")

    with engine.begin() as conn:
        golfs = conn.execute(_GOLF_PARCELS_SQL).mappings().all()
        log.info("Found %d golf-course qualifying parcels", len(golfs))

        summaries: List[Dict[str, Any]] = []
        for parcel in golfs:
            try:
                summaries.append(_process_parcel(conn, dict(parcel)))
            except Exception as e:  # noqa: BLE001 — keep processing the rest
                log.exception("Ring test failed for parcel_id=%s: %s", parcel["parcel_id"], e)

    counts: Dict[str, int] = {"ringed": 0, "partially_ringed": 0, "not_ringed": 0}
    for s in summaries:
        counts[s["result"]] = counts.get(s["result"], 0) + 1

    log.info(
        "Ring test complete: %d ringed, %d partial, %d not_ringed (%d processed)",
        counts.get("ringed", 0),
        counts.get("partially_ringed", 0),
        counts.get("not_ringed", 0),
        len(summaries),
    )

    return {
        "processed": len(summaries),
        "by_result": counts,
        "parcels": summaries,
    }


if __name__ == "__main__":  # pragma: no cover
    run_all()
