"""audit_view_index: the index the window lookup of `ACTIVITY_VIEWED` reads.

Revision ID: 00591a77722d
Revises: ca4f68b6e89a
Create Date: 2026-10-20 15:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "00591a77722d"
down_revision: str | Sequence[str] | None = "ca4f68b6e89a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds a composite index on `audit_events (event_type, principal_id, occurred_at)`: before a
    view of other people's activity writes its `ACTIVITY_VIEWED` row, the server looks for one
    of the same viewer within the window, and `audit_events` only grows. No row changes.
    """
    op.create_index(
        "ix_audit_events_event_type_principal_id_occurred_at",
        "audit_events",
        ["event_type", "principal_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops the index. The previous release never writes `ACTIVITY_VIEWED`; rows written since the
    upgrade stay and read as an unknown event type there.
    """
    op.drop_index("ix_audit_events_event_type_principal_id_occurred_at", table_name="audit_events")
