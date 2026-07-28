"""SB 1434 pathway classification.

Central home for the 13-pathway mapping. Screening's INSERT ... SELECT
uses PATHWAY_CASE_SQL to compute pathway_hint in one shot; the API
validates ?pathway filters against PATHWAY_VALUES; the golf-course ring
test uses pathway_from_ring_result to refine 'pathway_golf_pending' into
one of the three golf variants.

Priority ordering matters — the first WHEN in the CASE that matches wins.
The order below disambiguates natural DOR-code overlaps (038 golf vs
generic 011-029 commercial, 028/048 auto/fuel vs industrial, 091-097
utility overriding everything).
"""
from __future__ import annotations

from typing import Dict

# Every pathway_hint value that qualifying_parcels can carry.
# 'pathway_golf_pending' is transient — it appears between the main
# screen and the ring test, then gets replaced by one of the three
# golf pathways below.
PATHWAY_VALUES: frozenset[str] = frozenset(
    {
        "pathway_1_golf_ringed",
        "pathway_1b_golf_partial",
        "pathway_2_golf_not_ringed",
        "pathway_3_industrial",
        "pathway_4_commercial",
        "pathway_5_office",
        # pathway_6_institutional REMOVED — institutional parcels (DOR
        # 070-079) are now excluded upstream in screening.py.
        "pathway_7_residential_redev",
        # pathway_8_utility REMOVED — utility parcels (DOR 091-097 or
        # utility-owner names) are excluded upstream because the required
        # 15-year title lookback isn't actionable without manual research.
        "pathway_9_auto_fuel",
        "pathway_10_hospitality",
        "pathway_11_vacant_commercial",
        "pathway_12_mixed_use",
        "pathway_13_other",
        "pathway_golf_pending",
    }
)

RING_TEST_VALUES: frozenset[str] = frozenset(
    {"ringed", "partially_ringed", "not_ringed"}
)

# Thresholds for the ring test — see ring_test.py.
RING_TEST_RINGED_THRESHOLD_PCT: float = 80.0
RING_TEST_PARTIAL_THRESHOLD_PCT: float = 40.0


# SQL CASE fragment. Expects `p.dor_uc` (String) to be in scope. Emitted
# into screening.py's INSERT ... SELECT verbatim. Comments inside the
# SQL explain the priority ordering.
PATHWAY_CASE_SQL: str = """
CASE
    -- Auto/fuel is more specific than commercial (028) and industrial (048).
    WHEN p.dor_uc IN ('028', '048')       THEN 'pathway_9_auto_fuel'
    -- Golf gets a placeholder until the ring test refines it.
    WHEN p.dor_uc = '038'                 THEN 'pathway_golf_pending'
    WHEN p.dor_uc = '039'                 THEN 'pathway_10_hospitality'
    WHEN p.dor_uc = '010'                 THEN 'pathway_11_vacant_commercial'
    WHEN p.dor_uc BETWEEN '030' AND '035' THEN 'pathway_12_mixed_use'
    -- Office is a narrower slice of the 011-029 commercial band.
    WHEN p.dor_uc BETWEEN '017' AND '019' THEN 'pathway_5_office'
    WHEN p.dor_uc BETWEEN '041' AND '049' THEN 'pathway_3_industrial'
    WHEN p.dor_uc BETWEEN '011' AND '029' THEN 'pathway_4_commercial'
    WHEN p.dor_uc BETWEEN '001' AND '009' THEN 'pathway_7_residential_redev'
    -- pathway_6_institutional (070-079) and pathway_8_utility (091-097)
    -- entries removed — those DOR ranges are excluded upstream in
    -- screening.py's WHERE clause so they never reach this CASE.
    ELSE 'pathway_13_other'
END
"""


_RING_TO_PATHWAY: Dict[str, str] = {
    "ringed": "pathway_1_golf_ringed",
    "partially_ringed": "pathway_1b_golf_partial",
    "not_ringed": "pathway_2_golf_not_ringed",
}


def pathway_from_ring_result(result: str) -> str:
    """Map a ring_test_result value to the corresponding pathway_hint."""
    try:
        return _RING_TO_PATHWAY[result]
    except KeyError:
        raise ValueError(
            f"unknown ring_test_result {result!r}; must be one of {sorted(_RING_TO_PATHWAY)}"
        )


def classify_ring_pct(pct: float) -> str:
    """Bucket a ring-test percentage into 'ringed' / 'partially_ringed' /
    'not_ringed' per the Phase D spec."""
    if pct >= RING_TEST_RINGED_THRESHOLD_PCT:
        return "ringed"
    if pct >= RING_TEST_PARTIAL_THRESHOLD_PCT:
        return "partially_ringed"
    return "not_ringed"
