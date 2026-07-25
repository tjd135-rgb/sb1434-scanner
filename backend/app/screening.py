"""SB 1434 (Section 163.2525) qualifying-parcel screen.

Runs one INSERT ... SELECT that applies Gates 1, 2, 3B, 5A, 5B and computes
every flag column + a preliminary pathway_hint. Gate 4 (residential adjacency)
is expensive on the full parcel set, so it runs as a follow-up UPDATE against
qualifying_parcels only.

Gates deferred to Phase C: 5C (UDB), 5D (military), Trigger A (cleanup-site
proximity). Their columns exist on qualifying_parcels but are populated later.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from sqlalchemy import text

from .db import engine

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(_h)


# 5 acres = 217,800 sqft
MIN_SQFOOT = 217_800

# 500 feet expressed in meters, used by ST_DWithin's geography path.
ADJACENCY_METERS = 152.4


# Indexes we ensure at the top of every screen run. All are idempotent — the
# alembic migration also creates equivalent indexes, but production has been
# observed running without them (Gate 4's ST_DWithin on ~2M parcels timed
# out) so we belt-and-suspenders it here. The critical one is the functional
# geography index: the Gate 4 query casts `geom::geography` on both sides,
# and the plain GIST on `geom` is not usable through that cast.
_ENSURE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_parcels_centroid "
    "ON parcels USING GIST (geom)",
    "CREATE INDEX IF NOT EXISTS idx_parcels_centroid_geog "
    "ON parcels USING GIST ((geom::geography))",
    "CREATE INDEX IF NOT EXISTS idx_parcels_dor_uc "
    "ON parcels (dor_uc)",
]


def ensure_indexes() -> None:
    """Create the spatial + dor_uc indexes on parcels if missing. Runs in its
    own auto-committing transaction so index state persists even if the
    subsequent screen run fails partway through."""
    log.info("Verifying spatial + dor_uc indexes on parcels")
    with engine.begin() as conn:
        for stmt in _ENSURE_INDEXES:
            log.info("  %s", stmt)
            conn.execute(text(stmt))
    log.info("Indexes ready")


_INSERT_QUALIFIERS = text(
    """
    INSERT INTO qualifying_parcels (
        parcel_id, county_fips, acres, env_trigger, brownfield_area_id,
        brownfield_area_name, ag_exclusion, park_exclusion, utility_flag,
        dor_uc, own_name, pathway_hint, latitude, longitude, geom
    )
    SELECT
        p.parcel_id,
        p.county_fips,
        p.lnd_sqfoot / 43560.0 AS acres,

        -- Gate 3B satisfied; Trigger A (cleanup-site proximity) is Phase C.
        'brownfield_area' AS env_trigger,
        ba.area_id  AS brownfield_area_id,
        ba.area_name AS brownfield_area_name,

        -- Exclusion flags. Ag / park exclusions filter in the WHERE, so these
        -- come out FALSE for rows that pass. Utility flag is informational.
        (p.dor_uc BETWEEN '050' AND '069') AS ag_exclusion,
        (p.dor_uc = '082' AND (
            p.own_name ILIKE '%CITY OF%' OR p.own_name ILIKE '%COUNTY OF%'
            OR p.own_name ILIKE '%TOWN OF%' OR p.own_name ILIKE '%VILLAGE OF%'
            OR p.own_name ILIKE '%MIAMI-DADE%' OR p.own_name ILIKE '%BROWARD%'
            OR p.own_name ILIKE '%PALM BEACH%'
        )) AS park_exclusion,
        (
            p.dor_uc BETWEEN '091' AND '097'
            OR p.own_name ILIKE '%FPL%'
            OR p.own_name ILIKE '%FLORIDA POWER%'
            OR p.own_name ILIKE '%NEXTERA%'
            OR p.own_name ILIKE '%DUKE ENERGY%'
            OR p.own_name ILIKE '%TECO%'
            OR p.own_name ILIKE '%PEOPLES GAS%'
            OR p.own_name ILIKE '%FLORIDA CITY GAS%'
        ) AS utility_flag,

        p.dor_uc,
        LEFT(p.own_name, 100) AS own_name,

        -- Pathway hint. Ordered so more-specific codes win over the broad
        -- 011-029 commercial/retail bucket.
        CASE
            WHEN p.dor_uc = '038'                       THEN 'golf_course'
            WHEN p.dor_uc BETWEEN '041' AND '049'        THEN 'industrial'
            WHEN p.dor_uc IN ('025','026','027')         THEN 'auto_fuel'
            WHEN p.dor_uc BETWEEN '017' AND '019'        THEN 'office'
            WHEN p.dor_uc BETWEEN '011' AND '029'        THEN 'commercial_retail'
            WHEN p.dor_uc BETWEEN '071' AND '079'        THEN 'institutional'
            WHEN p.dor_uc BETWEEN '091' AND '097'        THEN 'utility'
            WHEN p.dor_uc BETWEEN '000' AND '009'        THEN 'residential_redev'
            ELSE 'other'
        END AS pathway_hint,

        ST_Y(p.geom) AS latitude,
        ST_X(p.geom) AS longitude,
        p.geom
    FROM parcels p
    JOIN brownfield_areas ba
        ON ST_Contains(ba.geom, p.geom)
    WHERE
        p.lnd_sqfoot >= :min_sqfoot
        AND p.county_fips IN ('23','16','60')
        AND p.geom IS NOT NULL
        AND NOT (p.dor_uc BETWEEN '050' AND '069')
        AND NOT (
            p.dor_uc = '082' AND (
                p.own_name ILIKE '%CITY OF%' OR p.own_name ILIKE '%COUNTY OF%'
                OR p.own_name ILIKE '%TOWN OF%' OR p.own_name ILIKE '%VILLAGE OF%'
                OR p.own_name ILIKE '%MIAMI-DADE%' OR p.own_name ILIKE '%BROWARD%'
                OR p.own_name ILIKE '%PALM BEACH%'
            )
        )
    ON CONFLICT (parcel_id) DO UPDATE SET
        county_fips = EXCLUDED.county_fips,
        acres = EXCLUDED.acres,
        env_trigger = EXCLUDED.env_trigger,
        brownfield_area_id = EXCLUDED.brownfield_area_id,
        brownfield_area_name = EXCLUDED.brownfield_area_name,
        ag_exclusion = EXCLUDED.ag_exclusion,
        park_exclusion = EXCLUDED.park_exclusion,
        utility_flag = EXCLUDED.utility_flag,
        dor_uc = EXCLUDED.dor_uc,
        own_name = EXCLUDED.own_name,
        pathway_hint = EXCLUDED.pathway_hint,
        latitude = EXCLUDED.latitude,
        longitude = EXCLUDED.longitude,
        geom = EXCLUDED.geom
    """
)


_UPDATE_ADJACENCY = text(
    """
    UPDATE qualifying_parcels qp
       SET adjacent_residential = EXISTS (
           SELECT 1
             FROM parcels r
            WHERE r.dor_uc BETWEEN '001' AND '009'
              AND r.parcel_id != qp.parcel_id
              AND ST_DWithin(
                    r.geom::geography,
                    qp.geom::geography,
                    :radius_meters
                  )
       )
     WHERE qp.geom IS NOT NULL
    """
)


_STATS_SQL = text(
    """
    SELECT
        COUNT(*)                                     AS total,
        COALESCE(SUM(acres), 0)                       AS total_acres,
        COUNT(*) FILTER (WHERE adjacent_residential)  AS with_adjacency,
        COUNT(*) FILTER (WHERE utility_flag)          AS utility_flagged
      FROM qualifying_parcels
    """
)

_STATS_BY_COUNTY = text(
    """
    SELECT county_fips, COUNT(*) AS n, COALESCE(SUM(acres), 0) AS acres
      FROM qualifying_parcels
     GROUP BY county_fips
     ORDER BY n DESC
    """
)

_STATS_BY_PATHWAY = text(
    """
    SELECT pathway_hint, COUNT(*) AS n, COALESCE(SUM(acres), 0) AS acres
      FROM qualifying_parcels
     GROUP BY pathway_hint
     ORDER BY n DESC
    """
)


def run_screen(update_adjacency: bool = True) -> Dict[str, Any]:
    """Run the qualifying-parcel screen end-to-end and return summary stats."""
    log.info("=== SB 1434 screening starting ===")

    # Ensure Gate 4 has an index to lean on. Committed before the main
    # transaction so the index survives even if the screen crashes.
    ensure_indexes()

    with engine.begin() as conn:
        n_parcels = conn.execute(text("SELECT COUNT(*) FROM parcels")).scalar_one()
        n_areas = conn.execute(text("SELECT COUNT(*) FROM brownfield_areas")).scalar_one()
        log.info("Input: %d parcels, %d brownfield areas", n_parcels, n_areas)

        if n_parcels == 0:
            log.warning("parcels table is empty; screening will produce 0 rows")
        if n_areas == 0:
            log.warning("brownfield_areas is empty; run ingest_all() first")

        log.info("Applying Gates 1, 2, 3B, 5A, 5B (INSERT ... SELECT)")
        result = conn.execute(_INSERT_QUALIFIERS, {"min_sqfoot": MIN_SQFOOT})
        log.info("Screen touched %s rows", result.rowcount)

        if update_adjacency:
            log.info("Computing Gate 4 (residential adjacency, 500ft)")
            adj = conn.execute(_UPDATE_ADJACENCY, {"radius_meters": ADJACENCY_METERS})
            log.info("Adjacency updated on %s qualifying parcels", adj.rowcount)

    return summary()


def summary() -> Dict[str, Any]:
    """Report counts + totals over qualifying_parcels."""
    with engine.connect() as conn:
        totals = conn.execute(_STATS_SQL).mappings().one()
        by_county = [dict(r) for r in conn.execute(_STATS_BY_COUNTY).mappings()]
        by_pathway = [dict(r) for r in conn.execute(_STATS_BY_PATHWAY).mappings()]

    out: Dict[str, Any] = {
        "total_qualifying": int(totals["total"]),
        "total_acres": float(totals["total_acres"]),
        "with_adjacency": int(totals["with_adjacency"]),
        "utility_flagged": int(totals["utility_flagged"]),
        "by_county": [
            {"county_fips": r["county_fips"], "n": int(r["n"]), "acres": float(r["acres"])}
            for r in by_county
        ],
        "by_pathway": [
            {"pathway": r["pathway_hint"], "n": int(r["n"]), "acres": float(r["acres"])}
            for r in by_pathway
        ],
    }
    log.info(
        "Summary: %d qualifying / %.1f acres / %d with adjacency / %d utility-flagged",
        out["total_qualifying"],
        out["total_acres"],
        out["with_adjacency"],
        out["utility_flagged"],
    )
    return out


if __name__ == "__main__":  # pragma: no cover
    run_screen()
