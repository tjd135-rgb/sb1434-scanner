"""SB 1434 (Section 163.2525) qualifying-parcel screen.

Runs one INSERT ... SELECT that applies Gates 1, 2, 3 (both triggers), 5A,
5B, 5D and computes every flag column + a preliminary pathway_hint. Gate 4
(residential adjacency) and Gate 5C (UDB flag) are expensive per-row
lookups, so they run as follow-up UPDATEs against qualifying_parcels only.

Gate 3 supports BOTH environmental triggers as of Phase C1:
  - Trigger B: parcel centroid inside a designated FDEP brownfield area
  - Trigger A: parcel centroid within 1,500 ft of a DEP cleanup site point
A parcel qualifies if EITHER trigger matches; env_trigger records which
(brownfield_area / cleanup_site / both).

Phase C2 (UDB): Miami-Dade parcels get udb_status='inside'/'outside'; other
counties get NULL. This is a FLAG, not an exclusion.

Phase C3 (military): Parcels within 1/4 mile (1,320 ft) of a military
installation are EXCLUDED from qualifying_parcels — statutory exclusion
under §163.2525.
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

# 500 feet expressed in meters — Gate 4 residential adjacency.
ADJACENCY_METERS = 152.4
# 1,500 feet expressed in meters — Gate 3 Trigger A cleanup-site proximity.
CLEANUP_PROXIMITY_METERS = 457.2
# 1,320 feet (¼ mile) in meters — Gate 5D military-installation exclusion.
MILITARY_EXCLUSION_METERS = 402.336


# Indexes we ensure at the top of every screen run. All are idempotent — the
# alembic migrations also create equivalent indexes, but we belt-and-suspenders
# them here because production has been observed running without them
# (Gate 4's ST_DWithin on ~2M parcels timed out until the functional
# geography index went in). The critical ones are the functional geography
# indexes: every proximity check casts `geom::geography`, and the plain
# GIST on `geom` is not usable through that cast.
_ENSURE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_parcels_centroid "
    "ON parcels USING GIST (geom)",
    "CREATE INDEX IF NOT EXISTS idx_parcels_centroid_geog "
    "ON parcels USING GIST ((geom::geography))",
    "CREATE INDEX IF NOT EXISTS idx_parcels_dor_uc "
    "ON parcels (dor_uc)",
    "CREATE INDEX IF NOT EXISTS idx_cleanup_sites_geom "
    "ON cleanup_sites USING GIST (geom)",
    "CREATE INDEX IF NOT EXISTS idx_cleanup_sites_geom_geog "
    "ON cleanup_sites USING GIST ((geom::geography))",
    "CREATE INDEX IF NOT EXISTS idx_udb_boundary_geom "
    "ON udb_boundary USING GIST (geom)",
    "CREATE INDEX IF NOT EXISTS idx_military_installations_geom "
    "ON military_installations USING GIST (geom)",
    "CREATE INDEX IF NOT EXISTS idx_military_installations_geom_geog "
    "ON military_installations USING GIST ((geom::geography))",
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

        -- Gate 3 trigger classification. A parcel is here because ba OR cs
        -- matched; label which.
        CASE
            WHEN ba.area_id IS NOT NULL AND cs.site_id IS NOT NULL THEN 'both'
            WHEN ba.area_id IS NOT NULL                             THEN 'brownfield_area'
            ELSE 'cleanup_site'
        END AS env_trigger,
        ba.area_id   AS brownfield_area_id,
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
    -- LATERAL LIMIT-1 lookups so each trigger short-circuits at the first
    -- match rather than exploding the row-count via multi-match joins. Both
    -- lookups produce at most one row per parcel; either can be NULL.
    LEFT JOIN LATERAL (
        SELECT area_id, area_name
          FROM brownfield_areas
         WHERE ST_Contains(geom, p.geom)
         LIMIT 1
    ) ba ON true
    LEFT JOIN LATERAL (
        SELECT site_id
          FROM cleanup_sites
         WHERE geom IS NOT NULL
           AND ST_DWithin(
                 geom::geography,
                 p.geom::geography,
                 :cleanup_radius_meters
               )
         LIMIT 1
    ) cs ON true
    WHERE
        p.lnd_sqfoot >= :min_sqfoot
        AND p.county_fips IN ('23','16','60')
        AND p.geom IS NOT NULL
        -- Gate 3: at least one environmental trigger present.
        AND (ba.area_id IS NOT NULL OR cs.site_id IS NOT NULL)
        AND NOT (p.dor_uc BETWEEN '050' AND '069')
        AND NOT (
            p.dor_uc = '082' AND (
                p.own_name ILIKE '%CITY OF%' OR p.own_name ILIKE '%COUNTY OF%'
                OR p.own_name ILIKE '%TOWN OF%' OR p.own_name ILIKE '%VILLAGE OF%'
                OR p.own_name ILIKE '%MIAMI-DADE%' OR p.own_name ILIKE '%BROWARD%'
                OR p.own_name ILIKE '%PALM BEACH%'
            )
        )
        -- Gate 5D: exclude parcels within ¼ mile of any military installation.
        AND NOT EXISTS (
            SELECT 1 FROM military_installations mi
             WHERE mi.geom IS NOT NULL
               AND ST_DWithin(
                     mi.geom::geography,
                     p.geom::geography,
                     :military_meters
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


# Gate 5C. Miami-Dade only — no UDB applies elsewhere. 'inside' if any UDB
# polygon contains the centroid; 'outside' if not; NULL for non-MD parcels.
_UPDATE_UDB_STATUS = text(
    """
    UPDATE qualifying_parcels qp
       SET udb_status = CASE
           WHEN qp.county_fips <> '23' THEN NULL
           WHEN EXISTS (
               SELECT 1 FROM udb_boundary u
                WHERE u.geom IS NOT NULL
                  AND ST_Contains(u.geom, qp.geom)
           ) THEN 'inside'
           ELSE 'outside'
       END
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

_STATS_BY_TRIGGER = text(
    """
    SELECT env_trigger, COUNT(*) AS n, COALESCE(SUM(acres), 0) AS acres
      FROM qualifying_parcels
     GROUP BY env_trigger
     ORDER BY n DESC
    """
)

_STATS_BY_UDB = text(
    """
    SELECT udb_status, COUNT(*) AS n, COALESCE(SUM(acres), 0) AS acres
      FROM qualifying_parcels
     GROUP BY udb_status
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

        n_cleanup = conn.execute(text("SELECT COUNT(*) FROM cleanup_sites")).scalar_one()
        n_udb = conn.execute(text("SELECT COUNT(*) FROM udb_boundary")).scalar_one()
        n_mil = conn.execute(text("SELECT COUNT(*) FROM military_installations")).scalar_one()
        log.info(
            "Input: %d cleanup sites, %d UDB polygons, %d military installations",
            n_cleanup, n_udb, n_mil,
        )
        if n_cleanup == 0:
            log.warning(
                "cleanup_sites is empty; only brownfield-area triggers will match. "
                "Run ingest-cleanup-sites for full Gate 3 coverage."
            )
        if n_mil == 0:
            log.warning(
                "military_installations is empty; Gate 5D exclusion won't remove anything. "
                "Run ingest-military first."
            )
        if n_udb == 0:
            log.warning(
                "udb_boundary is empty; udb_status will remain NULL for all Miami-Dade rows. "
                "Run ingest-udb to populate."
            )

        log.info(
            "Applying Gates 1, 2, 3 (brownfield OR cleanup within %.1fm), 5A, 5B, "
            "5D (military exclusion at %.1fm) (INSERT ... SELECT)",
            CLEANUP_PROXIMITY_METERS,
            MILITARY_EXCLUSION_METERS,
        )
        result = conn.execute(
            _INSERT_QUALIFIERS,
            {
                "min_sqfoot": MIN_SQFOOT,
                "cleanup_radius_meters": CLEANUP_PROXIMITY_METERS,
                "military_meters": MILITARY_EXCLUSION_METERS,
            },
        )
        log.info("Screen touched %s rows", result.rowcount)

        if update_adjacency:
            log.info("Computing Gate 4 (residential adjacency, 500ft)")
            adj = conn.execute(_UPDATE_ADJACENCY, {"radius_meters": ADJACENCY_METERS})
            log.info("Adjacency updated on %s qualifying parcels", adj.rowcount)

        log.info("Computing Gate 5C (UDB flag, Miami-Dade only)")
        udb_res = conn.execute(_UPDATE_UDB_STATUS)
        log.info("UDB status updated on %s qualifying parcels", udb_res.rowcount)

    return summary()


def summary() -> Dict[str, Any]:
    """Report counts + totals over qualifying_parcels."""
    with engine.connect() as conn:
        totals = conn.execute(_STATS_SQL).mappings().one()
        by_county = [dict(r) for r in conn.execute(_STATS_BY_COUNTY).mappings()]
        by_pathway = [dict(r) for r in conn.execute(_STATS_BY_PATHWAY).mappings()]
        by_trigger = [dict(r) for r in conn.execute(_STATS_BY_TRIGGER).mappings()]
        by_udb = [dict(r) for r in conn.execute(_STATS_BY_UDB).mappings()]

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
        "by_env_trigger": [
            {"env_trigger": r["env_trigger"], "n": int(r["n"]), "acres": float(r["acres"])}
            for r in by_trigger
        ],
        "by_udb_status": [
            {"udb_status": r["udb_status"], "n": int(r["n"]), "acres": float(r["acres"])}
            for r in by_udb
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
