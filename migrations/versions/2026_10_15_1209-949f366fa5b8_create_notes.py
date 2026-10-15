"""create notes

Revision ID: 949f366fa5b8
Revises: 5b8e2d41c9a7
Create Date: 2026-10-15 12:09:05.916789

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "949f366fa5b8"
down_revision: str | Sequence[str] | None = "5b8e2d41c9a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notes",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "char_length(text) BETWEEN 1 AND 2000", name=op.f("ck_notes_text_length")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notes")),
    )
    op.create_index("ix_notes_user_id_id", "notes", ["user_id", "id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_notes_user_id_id", table_name="notes")
    op.drop_table("notes")
