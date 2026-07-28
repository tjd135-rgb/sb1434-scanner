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

from . import pathways
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


_INSERT_QUALIFIERS_SQL = """
    INSERT INTO qualifying_parcels (
        parcel_id, county_fips, acres, jv, lnd_val, land_to_improvement_ratio,
        env_trigger, brownfield_area_id, brownfield_area_name,
        ag_exclusion, park_exclusion, utility_flag,
        dor_uc, own_name, phy_addr1, phy_city, phy_zipcd,
        pathway_hint, latitude, longitude, geom
    )
    SELECT
        p.parcel_id,
        p.county_fips,
        p.lnd_sqfoot / 43560.0 AS acres,

        -- Value context copied from parcels so filters + display don't
        -- need a JOIN back to parcels every request.
        p.jv,
        p.lnd_val,
        CASE
            WHEN p.jv IS NULL OR p.lnd_val IS NULL THEN NULL
            WHEN p.jv - p.lnd_val > 0 THEN (p.lnd_val / (p.jv - p.lnd_val))::float
            -- Vacant / near-vacant: no measurable improvement value.
            -- Use a sentinel of 999 so land-heavy filters still catch it.
            WHEN p.lnd_val > 0 THEN 999.0
            ELSE NULL
        END AS land_to_improvement_ratio,

        -- Gate 3 trigger classification. A parcel is here because ba OR cs
        -- matched; label which.
        CASE
            WHEN ba.area_id IS NOT NULL AND cs.site_id IS NOT NULL THEN 'both'
            WHEN ba.area_id IS NOT NULL                             THEN 'brownfield_area'
            ELSE 'cleanup_site'
        END AS env_trigger,
        ba.area_id   AS brownfield_area_id,
        ba.area_name AS brownfield_area_name,

        -- Exclusion flags on the surviving row (all FALSE by definition
        -- since the WHERE filtered them out); kept for schema parity.
        FALSE AS ag_exclusion,
        FALSE AS park_exclusion,
        FALSE AS utility_flag,

        p.dor_uc,
        LEFT(p.own_name, 100) AS own_name,
        p.phy_addr1,
        p.phy_city,
        p.phy_zipcd,

        -- Pathway hint. Sourced from pathways.PATHWAY_CASE_SQL so the
        -- pathway mapping lives in ONE place; golf parcels emit
        -- 'pathway_golf_pending' here and the ring test refines them
        -- into pathway_1 / _1b / _2 later.
        __PATHWAY_CASE__ AS pathway_hint,

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
        -- Gate 5A: exclude agricultural (DOR 050-069).
        AND NOT (p.dor_uc BETWEEN '050' AND '069')
        -- Gate 5B: exclude government-owned public parks.
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
        -- Gate 5E: exclude institutional uses (DOR 070-079). Covers
        -- private schools, homes for the aged, orphanages, mortuaries,
        -- clubs, sanitariums, cultural orgs. Not actionable as
        -- redevelopment targets at scale.
        AND NOT (p.dor_uc BETWEEN '070' AND '079')
        -- Gate 5F: exclude the additional school DOR codes not already
        -- caught by 070-079: 084 colleges. Also 020 airports/bus/marine
        -- terminals — infrastructure, not redevelopable land.
        AND p.dor_uc NOT IN ('020', '084')
        -- Gate 5G: exclude utility parcels (DOR 091-097). Removed
        -- from pathways because the 15-year title lookback required
        -- to confirm qualification isn't actionable without manual
        -- title research.
        AND NOT (p.dor_uc BETWEEN '091' AND '097')
        -- Gate 5H: exclude explicit military/airport owner patterns
        -- (catches "DADE COUNTY HOMESTEAD AIR BASE" and similar false
        -- positives that snuck through the geometric ¼-mile buffer).
        AND NOT (
            p.own_name ILIKE '%AIR BASE%'
            OR p.own_name ILIKE '%AIR FORCE%'
            OR p.own_name ILIKE '%MILITARY%'
            OR p.own_name ILIKE '% NAVY%'
            OR p.own_name ILIKE '% ARMY%'
            OR p.own_name ILIKE '%NATIONAL GUARD%'
            OR p.own_name ILIKE '%COAST GUARD%'
            OR p.own_name ILIKE '%HOMESTEAD AIR%'
        )
        -- Gate 5I: broad government-ownership exclusion.
        --
        -- The escape hatch (keep parcels in the qualifying set even when
        -- govt-owned) applies ONLY when:
        --   (a) DOR code is commercial or industrial (010-049), AND
        --   (b) owner name does NOT flag transportation infrastructure
        --       (AIRPORT / AIR PORT / TRANSIT / BUS TERMINAL — those
        --       are dedicated infrastructure and never developable
        --       regardless of the DOR bucket, e.g. an airport parking
        --       lot classifies as DOR 028 but isn't actionable).
        AND NOT (
            (
                NOT (p.dor_uc BETWEEN '010' AND '049')
                OR p.own_name ILIKE '%AIRPORT%'
                OR p.own_name ILIKE '%AIR PORT%'
                OR p.own_name ILIKE '%TRANSIT%'
                OR p.own_name ILIKE '%BUS TERMINAL%'
            )
            AND (
                p.own_name ILIKE '%CITY OF%'
                OR p.own_name ILIKE '%COUNTY%'
                OR p.own_name ILIKE '%STATE OF%'
                OR p.own_name ILIKE '%UNITED STATES%'
                OR p.own_name ILIKE '%TOWN OF%'
                OR p.own_name ILIKE '%VILLAGE OF%'
                OR p.own_name ILIKE '%DISTRICT%'
                OR p.own_name ILIKE '%MUNICIPAL%'
                OR p.own_name ILIKE '%GOVERNMENT%'
                OR p.own_name ILIKE '%SCHOOL BOARD%'
                OR p.own_name ILIKE '%WATER MANAGEMENT%'
                OR p.own_name ILIKE '%SOUTH FLORIDA WATER%'
                OR p.own_name ILIKE '%DEPARTMENT OF%'
                OR p.own_name ILIKE '%AUTHORITY%'
            )
        )
    ON CONFLICT (parcel_id) DO UPDATE SET
        county_fips = EXCLUDED.county_fips,
        acres = EXCLUDED.acres,
        jv = EXCLUDED.jv,
        lnd_val = EXCLUDED.lnd_val,
        land_to_improvement_ratio = EXCLUDED.land_to_improvement_ratio,
        env_trigger = EXCLUDED.env_trigger,
        brownfield_area_id = EXCLUDED.brownfield_area_id,
        brownfield_area_name = EXCLUDED.brownfield_area_name,
        ag_exclusion = EXCLUDED.ag_exclusion,
        park_exclusion = EXCLUDED.park_exclusion,
        utility_flag = EXCLUDED.utility_flag,
        dor_uc = EXCLUDED.dor_uc,
        own_name = EXCLUDED.own_name,
        phy_addr1 = EXCLUDED.phy_addr1,
        phy_city = EXCLUDED.phy_city,
        phy_zipcd = EXCLUDED.phy_zipcd,
        pathway_hint = EXCLUDED.pathway_hint,
        latitude = EXCLUDED.latitude,
        longitude = EXCLUDED.longitude,
        geom = EXCLUDED.geom,
        -- Re-screening clears the golf ring-test outputs so stale
        -- classifications don't survive; re-run /admin/run-ring-test.
        ring_test_pct = NULL,
        ring_test_result = NULL,
        ring_test_samples = NULL
    """

# Splice the shared 13-pathway CASE fragment into the INSERT template.
_INSERT_QUALIFIERS = text(
    _INSERT_QUALIFIERS_SQL.replace("__PATHWAY_CASE__", pathways.PATHWAY_CASE_SQL)
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

_STATS_BY_RING_TEST = text(
    """
    SELECT ring_test_result, COUNT(*) AS n, COALESCE(SUM(acres), 0) AS acres
      FROM qualifying_parcels
     WHERE dor_uc = '038'
     GROUP BY ring_test_result
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

        # Full-replace pattern: TRUNCATE first so rows that no longer
        # pass the new exclusion gates don't linger. The INSERT below is
        # authoritative — every re-screen is a fresh classification.
        log.info("Truncating qualifying_parcels for fresh full re-screen")
        conn.execute(text("TRUNCATE qualifying_parcels"))

        log.info(
            "Applying Gates 1, 2, 3 (brownfield OR cleanup within %.1fm), 5A/5B/5D "
            "(military at %.1fm), 5E/5F (institutions), 5G (utilities), "
            "5H (military-owner names), 5I (broad-gov-not-commercial) "
            "(INSERT ... SELECT)",
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
        by_ring = [dict(r) for r in conn.execute(_STATS_BY_RING_TEST).mappings()]

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
        "by_ring_test_result": [
            {
                "ring_test_result": r["ring_test_result"],
                "n": int(r["n"]),
                "acres": float(r["acres"]),
            }
            for r in by_ring
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
