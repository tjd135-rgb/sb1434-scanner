"""Phase D: golf-course ring test columns on qualifying_parcels

Revision ID: 0004_ring_test
Revises: 0003_udb_and_military
Create Date: 2026-07-26 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004_ring_test"
down_revision: Union[str, None] = "0003_udb_and_military"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "qualifying_parcels",
        sa.Column("ring_test_pct", sa.Float(), nullable=True),
    )
    op.add_column(
        "qualifying_parcels",
        sa.Column("ring_test_result", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "qualifying_parcels",
        sa.Column("ring_test_samples", JSONB(), nullable=True),
    )
    op.create_index(
        "ix_qualifying_parcels_ring_test_result",
        "qualifying_parcels",
        ["ring_test_result"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_qualifying_parcels_ring_test_result",
        table_name="qualifying_parcels",
    )
    op.drop_column("qualifying_parcels", "ring_test_samples")
    op.drop_column("qualifying_parcels", "ring_test_result")
    op.drop_column("qualifying_parcels", "ring_test_pct")
