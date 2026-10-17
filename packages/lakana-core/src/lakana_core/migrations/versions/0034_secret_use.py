"""secret use: what a secret is bound to, and the bind of every unambiguous existing secret.

Revision ID: f1c4a8e2b6d9
Revises: e8b1f4c7d0a3
Create Date: 2026-10-17 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1c4a8e2b6d9"
down_revision: str | Sequence[str] | None = "e8b1f4c7d0a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND = sa.Enum(
    "PACKAGE_INDEX", "REPOSITORY", "ENVIRONMENT", name="secretusekind", native_enum=False, length=16
)

_NAMED_BY_INDEXES = (
    "(SELECT count(*) FROM package_indexes p WHERE p.credential_secret = secrets.name)"
)
_NAMED_BY_REPOSITORIES = (
    "(SELECT count(*) FROM repositories r WHERE r.credential_secret = secrets.name)"
)


def upgrade() -> None:
    """Upgrade schema.

    Adds `secrets.use_kind` and `secrets.use_target_id`. A secret whose name exactly one
    package index or repository names, and nothing else, is bound to it: that is the use it
    already has. Every other secret stays unbound, and the worker refuses it to an index or
    a repository until an administrator binds it (`lakana secret bind`).
    """
    with op.batch_alter_table("secrets") as batch_op:
        batch_op.add_column(sa.Column("use_kind", _KIND, nullable=True))
        batch_op.add_column(sa.Column("use_target_id", sa.Uuid(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE secrets SET use_kind = 'PACKAGE_INDEX', use_target_id = "
            "(SELECT p.id FROM package_indexes p WHERE p.credential_secret = secrets.name) "
            f"WHERE {_NAMED_BY_INDEXES} = 1 AND {_NAMED_BY_REPOSITORIES} = 0"
        )
    )
    op.execute(
        sa.text(
            "UPDATE secrets SET use_kind = 'REPOSITORY', use_target_id = "
            "(SELECT r.id FROM repositories r WHERE r.credential_secret = secrets.name) "
            f"WHERE {_NAMED_BY_REPOSITORIES} = 1 AND {_NAMED_BY_INDEXES} = 0"
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("secrets") as batch_op:
        batch_op.drop_column("use_target_id")
        batch_op.drop_column("use_kind")
