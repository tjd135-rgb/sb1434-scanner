"""initial schema: parcels + brownfield_areas + brownfield_sites + qualifying_parcels

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-24 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.create_table(
        "parcels",
        sa.Column("county_fips", sa.String(length=3), nullable=False),
        sa.Column("parcel_id", sa.String(length=30), nullable=False),
        sa.Column("dor_uc", sa.String(length=4), nullable=True),
        sa.Column("jv", sa.Numeric(), nullable=True),
        sa.Column("lnd_val", sa.Numeric(), nullable=True),
        sa.Column("lnd_sqfoot", sa.Numeric(), nullable=True),
        sa.Column("tot_lvg_ar", sa.Numeric(), nullable=True),
        sa.Column("act_yr_blt", sa.Integer(), nullable=True),
        sa.Column("own_name", sa.Text(), nullable=True),
        sa.Column("own_addr1", sa.Text(), nullable=True),
        sa.Column("own_city", sa.Text(), nullable=True),
        sa.Column("own_state", sa.String(length=32), nullable=True),
        sa.Column("own_zipcd", sa.Text(), nullable=True),
        sa.Column("phy_addr1", sa.Text(), nullable=True),
        sa.Column("phy_city", sa.Text(), nullable=True),
        sa.Column("phy_zipcd", sa.Text(), nullable=True),
        sa.Column("s_legal", sa.Text(), nullable=True),
        sa.Column("lat", sa.Numeric(), nullable=True),
        sa.Column("lon", sa.Numeric(), nullable=True),
        sa.Column(
            "geom",
            Geometry(geometry_type="POINT", srid=4326),
            nullable=True,
        ),
        sa.Column(
            "refresh_date",
            sa.DateTime(),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("county_fips", "parcel_id", name="pk_parcels"),
    )
    op.create_index("ix_parcels_dor_uc", "parcels", ["dor_uc"])
    op.create_index("ix_parcels_county_fips", "parcels", ["county_fips"])
    op.create_index("ix_parcels_lnd_sqfoot", "parcels", ["lnd_sqfoot"])
    op.create_index(
        "ix_parcels_geom", "parcels", ["geom"], postgresql_using="gist"
    )

    op.create_table(
        "brownfield_areas",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("area_id", sa.String(length=15), nullable=False),
        sa.Column("area_name", sa.String(length=120), nullable=True),
        sa.Column("city", sa.String(length=50), nullable=True),
        sa.Column("county", sa.String(length=30), nullable=True),
        sa.Column("district", sa.String(length=20), nullable=True),
        sa.Column("resolution_number", sa.String(length=20), nullable=True),
        sa.Column("resolution_date", sa.Date(), nullable=True),
        sa.Column("acreage", sa.Float(), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("documents_url", sa.Text(), nullable=True),
        sa.Column(
            "geom",
            Geometry(geometry_type="MULTIPOLYGON", srid=4326),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("area_id", name="uq_brownfield_areas_area_id"),
    )
    op.create_index(
        "ix_brownfield_areas_geom",
        "brownfield_areas",
        ["geom"],
        postgresql_using="gist",
    )
    op.create_index("ix_brownfield_areas_county", "brownfield_areas", ["county"])

    op.create_table(
        "brownfield_sites",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("site_id", sa.String(length=20), nullable=False),
        sa.Column("area_id", sa.String(length=15), nullable=True),
        sa.Column("site_name", sa.String(length=150), nullable=True),
        sa.Column("area_name", sa.String(length=120), nullable=True),
        sa.Column("county", sa.String(length=30), nullable=True),
        sa.Column("acreage", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=True),
        sa.Column("contaminants", sa.Text(), nullable=True),
        sa.Column(
            "geom",
            Geometry(geometry_type="MULTIPOLYGON", srid=4326),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id", name="uq_brownfield_sites_site_id"),
    )
    op.create_index(
        "ix_brownfield_sites_geom",
        "brownfield_sites",
        ["geom"],
        postgresql_using="gist",
    )

    op.create_table(
        "qualifying_parcels",
        sa.Column("parcel_id", sa.String(length=30), nullable=False),
        sa.Column("county_fips", sa.String(length=3), nullable=True),
        sa.Column("acres", sa.Float(), nullable=True),
        sa.Column("env_trigger", sa.String(length=30), nullable=True),
        sa.Column("brownfield_area_id", sa.String(length=15), nullable=True),
        sa.Column("brownfield_area_name", sa.String(length=120), nullable=True),
        sa.Column(
            "adjacent_residential",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "ag_exclusion",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "park_exclusion",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "utility_flag",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("dor_uc", sa.String(length=4), nullable=True),
        sa.Column("own_name", sa.String(length=100), nullable=True),
        sa.Column("pathway_hint", sa.String(length=50), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column(
            "geom",
            Geometry(geometry_type="POINT", srid=4326),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("parcel_id"),
    )
    op.create_index(
        "ix_qualifying_parcels_county", "qualifying_parcels", ["county_fips"]
    )
    op.create_index(
        "ix_qualifying_parcels_pathway", "qualifying_parcels", ["pathway_hint"]
    )
    op.create_index(
        "ix_qualifying_parcels_env_trigger",
        "qualifying_parcels",
        ["env_trigger"],
    )
    op.create_index(
        "ix_qualifying_parcels_geom",
        "qualifying_parcels",
        ["geom"],
        postgresql_using="gist",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_qualifying_parcels_geom", table_name="qualifying_parcels"
    )
    op.drop_index(
        "ix_qualifying_parcels_env_trigger", table_name="qualifying_parcels"
    )
    op.drop_index(
        "ix_qualifying_parcels_pathway", table_name="qualifying_parcels"
    )
    op.drop_index(
        "ix_qualifying_parcels_county", table_name="qualifying_parcels"
    )
    op.drop_table("qualifying_parcels")

    op.drop_index("ix_brownfield_sites_geom", table_name="brownfield_sites")
    op.drop_table("brownfield_sites")

    op.drop_index("ix_brownfield_areas_county", table_name="brownfield_areas")
    op.drop_index("ix_brownfield_areas_geom", table_name="brownfield_areas")
    op.drop_table("brownfield_areas")

    op.drop_index("ix_parcels_geom", table_name="parcels")
    op.drop_index("ix_parcels_lnd_sqfoot", table_name="parcels")
    op.drop_index("ix_parcels_county_fips", table_name="parcels")
    op.drop_index("ix_parcels_dor_uc", table_name="parcels")
    op.drop_table("parcels")
