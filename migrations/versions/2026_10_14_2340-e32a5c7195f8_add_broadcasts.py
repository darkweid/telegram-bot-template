"""add broadcasts

Revision ID: e32a5c7195f8
Revises: 722c7847da4b
Create Date: 2026-10-14 23:40:16.859088

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e32a5c7195f8"
down_revision: str | Sequence[str] | None = "722c7847da4b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    sa.Enum(
        "PENDING", "RUNNING", "COMPLETED", "CANCELLED", "FAILED", name="broadcaststatus"
    ).create(op.get_bind())
    op.create_table(
        "broadcasts",
        sa.Column("admin_id", sa.BigInteger(), nullable=False),
        sa.Column("source_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.Integer(), nullable=False),
        sa.Column("progress_message_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "PENDING",
                "RUNNING",
                "COMPLETED",
                "CANCELLED",
                "FAILED",
                name="broadcaststatus",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("sent", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "unreachable", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "cursor_user_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "stop_requested",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("checkpoint_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["admin_id"], ["users.id"], name=op.f("fk_broadcasts_admin_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_broadcasts")),
    )


def downgrade() -> None:
    op.drop_table("broadcasts")
    sa.Enum(
        "PENDING", "RUNNING", "COMPLETED", "CANCELLED", "FAILED", name="broadcaststatus"
    ).drop(op.get_bind())
