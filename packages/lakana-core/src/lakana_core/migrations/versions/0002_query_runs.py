"""query runs.

Revision ID: 7c957aaa4637
Revises: f9a7c4390732
Create Date: 2026-09-14 20:50:20.465385

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c957aaa4637"
down_revision: str | Sequence[str] | None = "f9a7c4390732"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "query_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=True),
        sa.Column("sql_text", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", name="queryrunstatus", native_enum=False
            ),
            nullable=False,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("lineage_json", sa.JSON(), nullable=True),
        sa.Column("result_location", sa.String(length=1024), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_query_runs_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_query_runs_principal_id_principals"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_query_runs")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("query_runs")
