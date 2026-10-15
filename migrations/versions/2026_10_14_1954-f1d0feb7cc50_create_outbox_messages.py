"""create outbox messages

Revision ID: f1d0feb7cc50
Revises: 9c3bf90033ee
Create Date: 2026-10-14 19:54:46.692211

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f1d0feb7cc50"
down_revision: str | Sequence[str] | None = "9c3bf90033ee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    sa.Enum("PENDING", "PUBLISHED", "FAILED", name="outboxmessagestatus").create(
        op.get_bind()
    )
    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task_name", sa.String(length=255), nullable=False),
        sa.Column("args", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("kwargs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "PENDING",
                "PUBLISHED",
                "FAILED",
                name="outboxmessagestatus",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_messages")),
    )
    op.create_index(
        "ix_outbox_messages_pending_created_at",
        "outbox_messages",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outbox_messages_pending_created_at",
        table_name="outbox_messages",
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.drop_table("outbox_messages")
    sa.Enum("PENDING", "PUBLISHED", "FAILED", name="outboxmessagestatus").drop(
        op.get_bind()
    )
