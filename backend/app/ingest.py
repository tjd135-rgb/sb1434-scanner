"""Phase A: DOR NAL ingest for the tri-county footprint.

Ports the LLA scanner pattern but trims the persisted column set to what
SB 1434 screening actually needs. Loads into the shared `parcels` table
defined in app.models.

Source resolution order (per county):
  1. Local file at data/nal/nal-{fips:02d}.csv or .zip.
  2. GitHub release download at
     {NAL_RELEASE_URL}/{NAL_ASSET_PATTERN}    (defaults documented below).
  3. FDR DOR portal — best-effort URL patterns.
  4. Explicit manual instructions on failure.

Env vars (all optional):
  NAL_RELEASE_URL   Base URL for release assets.
                    Default: https://github.com/tjd135-rgb/sb1434-scanner/releases/download/nal-2025
  NAL_ASSET_PATTERN Filename pattern, {fips} interpolated in (county_fips
                    is always 2 digits for our tri-county set).
                    Default: nal-{fips}.zip
  DOR_PORTAL_URL    DOR data portal root, used only for the pattern fallback.

Usage:
    python -m app.ingest --county miami-dade
    python -m app.ingest --county all

Behavior mirrors the LLA scanner:
  - Detects | vs , delimiter.
  - Streams a projected CSV of only the columns we persist.
  - Bulk COPYs into an unlogged staging table, then transactionally swaps
    per-county rows (DELETE + INSERT SELECT). Prior data survives failures.
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import requests

from .db import raw_connection

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(_h)


# --------- county setup ---------

COUNTIES: Dict[str, int] = {
    "miami-dade": 23,
    "broward": 16,
    "palm-beach": 60,
}
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "nal"
NAL_RELEASE_URL = os.getenv(
    "NAL_RELEASE_URL",
    "https://github.com/tjd135-rgb/sb1434-scanner/releases/download/nal-2025",
)
NAL_ASSET_PATTERN = os.getenv("NAL_ASSET_PATTERN", "nal-{fips}.zip")
DOR_PORTAL_URL = os.getenv("DOR_PORTAL_URL", "https://floridarevenue.com/property/dataportal/")

# Persisted columns, in load order. Must match the staging DDL.
TARGET_COLS: List[str] = [
    "county_fips",
    "parcel_id",
    "dor_uc",
    "jv",
    "lnd_val",
    "act_yr_blt",
    "tot_lvg_ar",
    "lnd_sqfoot",
    "own_name",
    "own_addr1",
    "own_city",
    "own_state",
    "own_zipcd",
    "phy_addr1",
    "phy_city",
    "phy_zipcd",
    "s_legal",
]

# Map target col -> source NAL col (uppercase). county_fips is filled from
# the county being loaded, not read from CSV.
SOURCE_MAP: Dict[str, Optional[str]] = {
    "county_fips": None,
    "parcel_id": "PARCEL_ID",
    "dor_uc": "DOR_UC",
    "jv": "JV",
    "lnd_val": "LND_VAL",
    "act_yr_blt": "ACT_YR_BLT",
    "tot_lvg_ar": "TOT_LVG_AREA",
    "lnd_sqfoot": "LND_SQFOOT",
    "own_name": "OWN_NAME",
    "own_addr1": "OWN_ADDR1",
    "own_city": "OWN_CITY",
    "own_state": "OWN_STATE",
    "own_zipcd": "OWN_ZIPCD",
    "phy_addr1": "PHY_ADDR1",
    "phy_city": "PHY_CITY",
    "phy_zipcd": "PHY_ZIPCD",
    "s_legal": "S_LEGAL",
}

NUMERIC_COLS = {"jv", "lnd_val", "tot_lvg_ar", "lnd_sqfoot"}
INT_COLS = {"act_yr_blt"}

REQUIRED_SOURCE_COLS = {
    v for v in SOURCE_MAP.values() if v is not None and v in {"PARCEL_ID", "DOR_UC", "LND_SQFOOT"}
}


# --------- file resolution ---------

@dataclass
class Source:
    path: Path
    delimiter: str  # ',' or '|'


def resolve_source(county_code: int) -> Source:
    """Locate the NAL file for the county. Downloads from the GitHub release
    if a local copy isn't already sitting under data/nal/."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    candidates = [
        DATA_DIR / f"nal-{county_code:02d}.csv",
        DATA_DIR / f"nal-{county_code:02d}.zip",
        DATA_DIR / f"NAL{county_code:02d}.csv",
        DATA_DIR / f"NAL{county_code:02d}F.csv",
    ]
    for c in candidates:
        if c.exists():
            log.info("Using cached NAL: %s", c)
            return _open(c)

    downloaded = _try_download_release(county_code)
    if downloaded is None:
        downloaded = _try_download_dor(county_code)
    if downloaded is not None:
        return _open(downloaded)

    raise FileNotFoundError(_manual_instructions(county_code))


def _open(path: Path) -> Source:
    if path.suffix.lower() == ".zip":
        path = _extract_zip(path)
    return Source(path=path, delimiter=_sniff_delimiter(path))


def _try_download_release(county_code: int) -> Optional[Path]:
    """Fetch the NAL asset from the GitHub release. Returns local path on success."""
    filename = NAL_ASSET_PATTERN.format(fips=county_code)
    url = f"{NAL_RELEASE_URL}/{filename}"
    dest = DATA_DIR / filename
    log.info("Trying release download: %s", url)
    try:
        r = requests.get(url, timeout=120, stream=True, allow_redirects=True)
        if r.status_code != 200:
            log.warning("Release download HTTP %d for %s", r.status_code, url)
            return None
        # Guard against tiny/error responses masquerading as 200s.
        clen = int(r.headers.get("Content-Length", "0") or 0)
        if clen and clen < 1024:
            log.warning("Release download too small (%d bytes) for %s", clen, url)
            return None
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
        log.info("Downloaded %s (%s bytes)", dest, dest.stat().st_size)
        return dest
    except requests.RequestException as e:
        log.warning("Release download failed for %s: %s", url, e)
        return None


def _try_download_dor(county_code: int) -> Optional[Path]:
    """Best-effort DOR portal patterns. Kept as a last resort."""
    year_hint = time.strftime("%Y")
    patterns = [
        f"{DOR_PORTAL_URL}NAL/NAL{county_code:02d}F{year_hint}.zip",
        f"{DOR_PORTAL_URL}nal/NAL{county_code:02d}F.zip",
        f"{DOR_PORTAL_URL}NAL{county_code:02d}F.zip",
    ]
    for url in patterns:
        log.info("Trying DOR portal: %s", url)
        try:
            r = requests.get(url, timeout=60, stream=True)
            if r.status_code == 200 and int(r.headers.get("Content-Length", "0") or 0) > 1024:
                dest = DATA_DIR / f"nal-{county_code:02d}.zip"
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        f.write(chunk)
                log.info("Downloaded %s", dest)
                return dest
        except requests.RequestException:
            continue
    return None


def _manual_instructions(county_code: int) -> str:
    dest = DATA_DIR / NAL_ASSET_PATTERN.format(fips=county_code)
    return (
        f"\nCould not locate or download NAL for county_fips={county_code}.\n\n"
        f"Manual steps:\n"
        f"  1. Grab the NAL asset from the LLA scanner release ({NAL_RELEASE_URL})\n"
        f"     or from {DOR_PORTAL_URL}\n"
        f"  2. Save it at: {dest}\n"
        f"  3. Re-run the ingest for this county.\n"
        f"\n"
        f"If the asset lives at a different name, set NAL_ASSET_PATTERN\n"
        f"(currently {NAL_ASSET_PATTERN!r}) or NAL_RELEASE_URL and retry.\n"
    )


def _extract_zip(zpath: Path) -> Path:
    with zipfile.ZipFile(zpath) as zf:
        csvs = [n for n in zf.namelist() if n.lower().endswith((".csv", ".txt"))]
        if not csvs:
            raise RuntimeError(f"{zpath}: no CSV inside zip")
        target = max(csvs, key=lambda n: zf.getinfo(n).file_size)
        out = zpath.with_suffix(".csv")
        log.info("Extracting %s from %s -> %s", target, zpath.name, out)
        with zf.open(target) as src, open(out, "wb") as dst:
            while True:
                buf = src.read(1 << 20)
                if not buf:
                    break
                dst.write(buf)
        return out


def _sniff_delimiter(path: Path) -> str:
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        sample = f.readline() + f.readline()
    return "|" if sample.count("|") > sample.count(",") else ","


# --------- transform ---------

def _project_row(
    row: Sequence[str],
    idx_map: Dict[str, int],
    county_code: int,
) -> Optional[List[str]]:
    """Return a projected row in TARGET_COLS order, or None if unusable."""
    parcel_id = row[idx_map["parcel_id"]].strip() if "parcel_id" in idx_map else ""
    if not parcel_id:
        return None

    out: List[str] = []
    for col in TARGET_COLS:
        if col == "county_fips":
            out.append(f"{county_code:02d}")
            continue

        try:
            raw = row[idx_map[col]]
        except (KeyError, IndexError):
            out.append("")
            continue

        if col in INT_COLS:
            v = raw.strip()
            if not v:
                out.append("")
            else:
                try:
                    out.append(str(int(float(v))))
                except ValueError:
                    out.append("")
        elif col in NUMERIC_COLS:
            out.append(raw.strip())
        else:
            out.append(raw.strip())
    return out


# --------- load ---------

STAGING_DDL = """
DROP TABLE IF EXISTS parcels_stage;
CREATE UNLOGGED TABLE parcels_stage (
    county_fips VARCHAR(3),
    parcel_id VARCHAR(30),
    dor_uc VARCHAR(4),
    jv NUMERIC,
    lnd_val NUMERIC,
    act_yr_blt INTEGER,
    tot_lvg_ar NUMERIC,
    lnd_sqfoot NUMERIC,
    own_name TEXT,
    own_addr1 TEXT,
    own_city TEXT,
    own_state VARCHAR(32),
    own_zipcd TEXT,
    phy_addr1 TEXT,
    phy_city TEXT,
    phy_zipcd TEXT,
    s_legal TEXT
);
"""


def _bulk_load_county(source: Source, county_code: int, header: List[str]) -> int:
    """Transactional load. Returns final row count for the county."""
    idx_map: Dict[str, int] = {}
    for target, src in SOURCE_MAP.items():
        if src is None:
            continue
        try:
            idx_map[target] = header.index(src)
        except ValueError:
            if src in REQUIRED_SOURCE_COLS:
                raise RuntimeError(f"schema drift: required column {src!r} missing from NAL header")
            log.warning("optional column %s missing from NAL header; will insert NULL", src)

    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", suffix=".csv", delete=False
    )
    try:
        writer = csv.writer(tmp, quoting=csv.QUOTE_MINIMAL)
        rows_written = 0
        with open(source.path, "r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.reader(f, delimiter=source.delimiter)
            next(reader)  # skip header
            for row in reader:
                projected = _project_row(row, idx_map, county_code)
                if projected is None:
                    continue
                writer.writerow(projected)
                rows_written += 1
                if rows_written % 100_000 == 0:
                    log.info("  transformed %d rows", rows_written)
        tmp.close()
        log.info("Transform done: %d rows staged for county_fips=%02d", rows_written, county_code)

        conn = raw_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(STAGING_DDL)
                copy_sql = (
                    f"COPY parcels_stage ({', '.join(TARGET_COLS)}) "
                    f"FROM STDIN WITH (FORMAT csv, NULL '')"
                )
                with open(tmp.name, "r", encoding="utf-8") as staged:
                    cur.copy_expert(copy_sql, staged)

                cur.execute(
                    "DELETE FROM parcels WHERE county_fips = %s",
                    (f"{county_code:02d}",),
                )
                cur.execute(
                    f"""
                    INSERT INTO parcels ({', '.join(TARGET_COLS)}, refresh_date)
                    SELECT {', '.join(TARGET_COLS)}, NOW() FROM parcels_stage
                    """
                )
                cur.execute("DROP TABLE parcels_stage")
                cur.execute(
                    "SELECT COUNT(*) FROM parcels WHERE county_fips = %s",
                    (f"{county_code:02d}",),
                )
                final_count = cur.fetchone()[0]
            conn.commit()
            return final_count
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# --------- entry points ---------

def run(county: str) -> Dict[str, int]:
    """Ingest one county key ('miami-dade' etc.) or 'all'. Returns per-county counts."""
    if county == "all":
        targets = list(COUNTIES.items())
    else:
        if county not in COUNTIES:
            raise ValueError(f"unknown county {county!r}. valid: {list(COUNTIES) + ['all']}")
        targets = [(county, COUNTIES[county])]

    results: Dict[str, int] = {}
    for name, code in targets:
        log.info("=== NAL ingest: %s (county_fips=%02d) ===", name, code)
        src = resolve_source(code)
        log.info("source: %s (delimiter=%r)", src.path, src.delimiter)

        with open(src.path, "r", encoding="utf-8", errors="replace", newline="") as f:
            header = next(csv.reader(f, delimiter=src.delimiter))
        log.info("header: %d columns", len(header))

        for req in REQUIRED_SOURCE_COLS:
            if req not in header:
                raise RuntimeError(f"required NAL column {req!r} missing from {src.path}")

        rows = _bulk_load_county(src, code, header)
        log.info("loaded %s parcels for %s", f"{rows:,}", name)
        results[name] = rows
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="DOR NAL ingest for sb1434-scanner")
    parser.add_argument(
        "--county", required=True,
        choices=list(COUNTIES) + ["all"],
        help="county key (or 'all')",
    )
    args = parser.parse_args()
    run(args.county)


if __name__ == "__main__":
    main()
