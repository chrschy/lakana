"""secret use: a CHECK that the use columns are set together.

Revision ID: a3d7c9e1f502
Revises: f1c4a8e2b6d9
Create Date: 2026-10-18 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "a3d7c9e1f502"
down_revision: str | Sequence[str] | None = "f1c4a8e2b6d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out here, not imported, so that a later change to the model cannot change this revision.
# `use_kind IS NOT NULL` keeps a NULL kind from making a branch NULL: a CHECK passes on NULL.
_RULE = (
    "(use_kind IS NULL AND use_target_id IS NULL)"
    " OR (use_kind IS NOT NULL AND use_kind = 'ENVIRONMENT' AND use_target_id IS NULL)"
    " OR (use_kind IS NOT NULL AND use_kind IN ('PACKAGE_INDEX', 'REPOSITORY')"
    " AND use_target_id IS NOT NULL)"
)
_NAME = "ck_secrets_use_consistent"


def upgrade() -> None:
    """Upgrade schema.

    Adds the CHECK: a secret is unbound (both columns unset), bound to its environment
    (`use_kind` only), or bound to an index or repository (both set). Revision 0034 and
    `require_use` never write anything else, so no row should break it. If one does, the
    upgrade stops and names the secrets; it never rewrites a binding.

    Raises
    ------
    RuntimeError
        If an existing secret breaks the rule. The message names the secret ids.
    """
    broken = [
        str(row[0])
        for row in op.get_bind().execute(
            text(f"SELECT id FROM secrets WHERE NOT ({_RULE})")  # nosec B608 -- module constant only
        )
    ]
    if broken:
        raise RuntimeError(
            f"Cannot add the check on secrets.use_kind and use_target_id: {len(broken)} "
            f"secret(s) have an inconsistent use (secret ids: {', '.join(sorted(broken))}). "
            "A use is either both columns unset, the environment alone, or an index or "
            "repository with its id. Nothing was changed. Bind each of these secrets again "
            "(`lakana secret bind <name> --index <name>`, `--repository <name>` or "
            "`--own-env`) on the release you are upgrading from, then run the upgrade again."
        )
    with op.batch_alter_table("secrets") as batch_op:
        batch_op.create_check_constraint(op.f(_NAME), _RULE)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("secrets") as batch_op:
        batch_op.drop_constraint(op.f(_NAME), type_="check")
