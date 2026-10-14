"""add user bans

Revision ID: 722c7847da4b
Revises: f1d0feb7cc50
Create Date: 2026-10-14 20:36:12.111946

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "722c7847da4b"
down_revision: str | Sequence[str] | None = "f1d0feb7cc50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_banned", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column(
        "users", sa.Column("banned_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("users", sa.Column("banned_by", sa.BigInteger(), nullable=True))
    op.drop_index(op.f("ix_users_reachable"), table_name="users")
    op.create_index(
        "ix_users_recipients",
        "users",
        ["id"],
        unique=False,
        postgresql_where=sa.text("is_reachable AND NOT is_banned"),
    )
    op.create_index(
        "ix_users_lower_username",
        "users",
        [sa.literal_column("lower(username)")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_users_lower_username", table_name="users")
    op.drop_index("ix_users_recipients", table_name="users")
    op.create_index(
        op.f("ix_users_reachable"),
        "users",
        ["id"],
        unique=False,
        postgresql_where=sa.text("is_reachable"),
    )
    op.drop_column("users", "banned_by")
    op.drop_column("users", "banned_at")
    op.drop_column("users", "is_banned")
