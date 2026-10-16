"""task_tokens.task_run_id and .claim: tokens bound to one task attempt.

Revision ID: a6c3e9d1f7b2
Revises: f4b8d2a6c9e1
Create Date: 2026-10-04 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a6c3e9d1f7b2"
down_revision: str | Sequence[str] | None = "f4b8d2a6c9e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds two nullable columns; existing (REST-minted) tokens keep both
    `NULL` and stay unbound. The CHECK keeps them set or unset together.
    """
    with op.batch_alter_table("task_tokens") as batch_op:
        batch_op.add_column(sa.Column("task_run_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("claim", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_task_tokens_task_run_id_task_runs"),
            "task_runs",
            ["task_run_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(op.f("ix_task_tokens_task_run_id"), ["task_run_id"])
        batch_op.create_check_constraint(
            op.f("ck_task_tokens_bound_pair"), "(task_run_id IS NULL) = (claim IS NULL)"
        )


def downgrade() -> None:
    """Downgrade schema.

    Deletes every bound token first. Without the two columns the previous
    release would read one as an ordinary task token: no check that its
    task is still running, and the wider scope of its `asset_scope` strings
    (reading the task's own table, dropping tables).
    """
    op.execute("DELETE FROM task_tokens WHERE task_run_id IS NOT NULL")
    with op.batch_alter_table("task_tokens") as batch_op:
        batch_op.drop_constraint(op.f("ck_task_tokens_bound_pair"), type_="check")
        batch_op.drop_index(op.f("ix_task_tokens_task_run_id"))
        batch_op.drop_constraint(op.f("fk_task_tokens_task_run_id_task_runs"), type_="foreignkey")
        batch_op.drop_column("claim")
        batch_op.drop_column("task_run_id")
