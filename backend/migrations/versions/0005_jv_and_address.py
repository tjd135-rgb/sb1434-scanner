"""qualifying_parcels: add jv / lnd_val / land_to_improvement_ratio + address

Revision ID: 0005_jv_and_address
Revises: 0004_ring_test
Create Date: 2026-07-27 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_jv_and_address"
down_revision: Union[str, None] = "0004_ring_test"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("qualifying_parcels", sa.Column("jv", sa.Numeric(), nullable=True))
    op.add_column("qualifying_parcels", sa.Column("lnd_val", sa.Numeric(), nullable=True))
    op.add_column(
        "qualifying_parcels",
        sa.Column("land_to_improvement_ratio", sa.Float(), nullable=True),
    )
    op.add_column("qualifying_parcels", sa.Column("phy_addr1", sa.Text(), nullable=True))
    op.add_column("qualifying_parcels", sa.Column("phy_city", sa.Text(), nullable=True))
    op.add_column("qualifying_parcels", sa.Column("phy_zipcd", sa.Text(), nullable=True))
    op.create_index(
        "ix_qualifying_parcels_jv", "qualifying_parcels", ["jv"]
    )
    op.create_index(
        "ix_qualifying_parcels_land_ratio",
        "qualifying_parcels",
        ["land_to_improvement_ratio"],
    )


def downgrade() -> None:
    op.drop_index("ix_qualifying_parcels_land_ratio", table_name="qualifying_parcels")
    op.drop_index("ix_qualifying_parcels_jv", table_name="qualifying_parcels")
    op.drop_column("qualifying_parcels", "phy_zipcd")
    op.drop_column("qualifying_parcels", "phy_city")
    op.drop_column("qualifying_parcels", "phy_addr1")
    op.drop_column("qualifying_parcels", "land_to_improvement_ratio")
    op.drop_column("qualifying_parcels", "lnd_val")
    op.drop_column("qualifying_parcels", "jv")
