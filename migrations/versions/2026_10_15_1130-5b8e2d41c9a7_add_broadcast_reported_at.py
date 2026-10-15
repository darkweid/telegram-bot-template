"""add broadcast reported_at

Revision ID: 5b8e2d41c9a7
Revises: e32a5c7195f8
Create Date: 2026-10-15 11:30:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "5b8e2d41c9a7"
down_revision: str | Sequence[str] | None = "e32a5c7195f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "broadcasts",
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("broadcasts", "reported_at")
