"""SQLAlchemy ORM models for SB 1434 scanner.

Four tables:
- parcels: DOR NAL rows for Miami-Dade / Broward / Palm Beach (populated in
  Phase A; scaffolded here so alembic and the screening query stay coherent).
- brownfield_areas: FDEP Layer 0 designated brownfield-area polygons.
- brownfield_sites: FDEP Layer 1 individual site polygons.
- qualifying_parcels: output of the Section 163.2525 screening query — the
  parcels that survive all statutory gates.
"""
from __future__ import annotations

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Parcel(Base):
    """DOR NAL row + Phase A centroid geometry. Populated by NAL ingest."""

    __tablename__ = "parcels"
    __table_args__ = (
        PrimaryKeyConstraint("county_fips", "parcel_id", name="pk_parcels"),
        Index("ix_parcels_dor_uc", "dor_uc"),
        Index("ix_parcels_county_fips", "county_fips"),
        Index("ix_parcels_lnd_sqfoot", "lnd_sqfoot"),
        # GIST on geom for spatial joins (brownfield containment, adjacency).
        Index("ix_parcels_geom", "geom", postgresql_using="gist"),
    )

    county_fips: Mapped[str] = mapped_column(String(3), nullable=False)
    parcel_id: Mapped[str] = mapped_column(String(30), nullable=False)
    dor_uc: Mapped[str | None] = mapped_column(String(4))
    jv: Mapped[float | None] = mapped_column(Numeric)
    lnd_val: Mapped[float | None] = mapped_column(Numeric)
    lnd_sqfoot: Mapped[float | None] = mapped_column(Numeric)
    tot_lvg_ar: Mapped[float | None] = mapped_column(Numeric)
    act_yr_blt: Mapped[int | None] = mapped_column(Integer)
    own_name: Mapped[str | None] = mapped_column(Text)
    own_addr1: Mapped[str | None] = mapped_column(Text)
    own_city: Mapped[str | None] = mapped_column(Text)
    own_state: Mapped[str | None] = mapped_column(String(32))
    own_zipcd: Mapped[str | None] = mapped_column(Text)
    phy_addr1: Mapped[str | None] = mapped_column(Text)
    phy_city: Mapped[str | None] = mapped_column(Text)
    phy_zipcd: Mapped[str | None] = mapped_column(Text)
    s_legal: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float | None] = mapped_column(Numeric)
    lon: Mapped[float | None] = mapped_column(Numeric)
    geom = mapped_column(Geometry(geometry_type="POINT", srid=4326))
    refresh_date: Mapped[DateTime] = mapped_column(
        DateTime, server_default=text("NOW()"), nullable=False
    )


class BrownfieldArea(Base):
    """FDEP Layer 0. Contains the polygon boundary of every designated
    brownfield area statewide (~531 records). Triggers Gate 3B automatically."""

    __tablename__ = "brownfield_areas"
    __table_args__ = (
        Index("ix_brownfield_areas_geom", "geom", postgresql_using="gist"),
        Index("ix_brownfield_areas_county", "county"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    area_id: Mapped[str] = mapped_column(String(15), unique=True, nullable=False)
    area_name: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(50))
    county: Mapped[str | None] = mapped_column(String(30))
    district: Mapped[str | None] = mapped_column(String(20))
    resolution_number: Mapped[str | None] = mapped_column(String(20))
    resolution_date: Mapped[Date | None] = mapped_column(Date)
    acreage: Mapped[float | None] = mapped_column(Float)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    documents_url: Mapped[str | None] = mapped_column(Text)
    geom = mapped_column(Geometry(geometry_type="MULTIPOLYGON", srid=4326))
    created_at: Mapped[DateTime] = mapped_column(
        DateTime, server_default=text("NOW()"), nullable=False
    )


class BrownfieldSite(Base):
    """FDEP Layer 1. Individual site polygons inside brownfield areas — used
    for pathway enrichment and drill-down, not for the base qualifying screen."""

    __tablename__ = "brownfield_sites"
    __table_args__ = (
        Index("ix_brownfield_sites_geom", "geom", postgresql_using="gist"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_id: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    area_id: Mapped[str | None] = mapped_column(String(15))
    site_name: Mapped[str | None] = mapped_column(String(150))
    area_name: Mapped[str | None] = mapped_column(String(120))
    county: Mapped[str | None] = mapped_column(String(30))
    acreage: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str | None] = mapped_column(String(50))
    contaminants: Mapped[str | None] = mapped_column(Text)
    geom = mapped_column(Geometry(geometry_type="MULTIPOLYGON", srid=4326))
    created_at: Mapped[DateTime] = mapped_column(
        DateTime, server_default=text("NOW()"), nullable=False
    )


class QualifyingParcel(Base):
    """Output of the Section 163.2525 screening. One row per parcel that
    passes Gates 1, 2, 3, 5A, 5B (Phase B). Gates 4 (adjacency) and 5C-D
    (UDB, military) are populated by follow-up updates in Phase B and Phase C."""

    __tablename__ = "qualifying_parcels"
    __table_args__ = (
        Index("ix_qualifying_parcels_county", "county_fips"),
        Index("ix_qualifying_parcels_pathway", "pathway_hint"),
        Index("ix_qualifying_parcels_env_trigger", "env_trigger"),
        Index("ix_qualifying_parcels_geom", "geom", postgresql_using="gist"),
    )

    parcel_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    county_fips: Mapped[str | None] = mapped_column(String(3))
    acres: Mapped[float | None] = mapped_column(Float)
    env_trigger: Mapped[str | None] = mapped_column(String(30))
    brownfield_area_id: Mapped[str | None] = mapped_column(String(15))
    brownfield_area_name: Mapped[str | None] = mapped_column(String(120))
    adjacent_residential: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false")
    )
    ag_exclusion: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    park_exclusion: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    utility_flag: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    dor_uc: Mapped[str | None] = mapped_column(String(4))
    own_name: Mapped[str | None] = mapped_column(String(100))
    pathway_hint: Mapped[str | None] = mapped_column(String(50))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    geom = mapped_column(Geometry(geometry_type="POINT", srid=4326))
    created_at: Mapped[DateTime] = mapped_column(
        DateTime, server_default=text("NOW()"), nullable=False
    )
