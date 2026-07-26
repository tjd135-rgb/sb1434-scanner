"""Phase C1: cleanup_sites table + functional geography index

Revision ID: 0002_cleanup_sites
Revises: 0001_initial
Create Date: 2026-07-25 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002_cleanup_sites"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cleanup_sites",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("site_id", sa.String(length=40), nullable=False),
        sa.Column("site_name", sa.String(length=200), nullable=True),
        sa.Column("site_status", sa.String(length=80), nullable=True),
        sa.Column("county", sa.String(length=30), nullable=True),
        sa.Column("address", sa.String(length=200), nullable=True),
        sa.Column("city", sa.String(length=80), nullable=True),
        sa.Column("zip", sa.String(length=15), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column(
            "geom",
            Geometry(geometry_type="POINT", srid=4326),
            nullable=True,
        ),
        sa.Column("raw_json", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id", name="uq_cleanup_sites_site_id"),
    )
    op.create_index(
        "ix_cleanup_sites_geom",
        "cleanup_sites",
        ["geom"],
        postgresql_using="gist",
    )
    op.create_index("ix_cleanup_sites_county", "cleanup_sites", ["county"])
    # Functional geography index — screening query casts geom::geography for
    # the ST_DWithin proximity check; the plain GIST(geom) index isn't usable
    # through that cast.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_cleanup_sites_geom_geog "
        "ON cleanup_sites USING GIST ((geom::geography))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_cleanup_sites_geom_geog")
    op.drop_index("ix_cleanup_sites_county", table_name="cleanup_sites")
    op.drop_index("ix_cleanup_sites_geom", table_name="cleanup_sites")
    op.drop_table("cleanup_sites")
