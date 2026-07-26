"""Phase C2 + C3: UDB boundary + military installations tables + qualifying_parcels.udb_status

Revision ID: 0003_udb_and_military
Revises: 0002_cleanup_sites
Create Date: 2026-07-25 00:00:01.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry

revision: str = "0003_udb_and_military"
down_revision: Union[str, None] = "0002_cleanup_sites"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "udb_boundary",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=True),
        sa.Column("source", sa.String(length=200), nullable=True),
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
    )
    op.create_index(
        "ix_udb_boundary_geom",
        "udb_boundary",
        ["geom"],
        postgresql_using="gist",
    )

    op.create_table(
        "military_installations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("branch", sa.String(length=50), nullable=True),
        sa.Column("state", sa.String(length=2), nullable=True),
        sa.Column("source", sa.String(length=200), nullable=True),
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
        sa.UniqueConstraint("name", name="uq_military_installations_name"),
    )
    op.create_index(
        "ix_military_installations_geom",
        "military_installations",
        ["geom"],
        postgresql_using="gist",
    )
    # Functional geography index for the ¼-mile ST_DWithin exclusion.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_military_installations_geom_geog "
        "ON military_installations USING GIST ((geom::geography))"
    )

    op.add_column(
        "qualifying_parcels",
        sa.Column("udb_status", sa.String(length=10), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("qualifying_parcels", "udb_status")

    op.execute("DROP INDEX IF EXISTS ix_military_installations_geom_geog")
    op.drop_index("ix_military_installations_geom", table_name="military_installations")
    op.drop_table("military_installations")

    op.drop_index("ix_udb_boundary_geom", table_name="udb_boundary")
    op.drop_table("udb_boundary")
