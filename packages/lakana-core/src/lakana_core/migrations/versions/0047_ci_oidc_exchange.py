"""ci_oidc_exchange: CI trust rules, the tokens a CI job receives, the ID tokens already used.

Revision ID: e0d9e6185736
Revises: 00591a77722d
Create Date: 2026-10-20 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e0d9e6185736"
down_revision: str | Sequence[str] | None = "00591a77722d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Creates `ci_trust_rules`, `ci_tokens` and `ci_oidc_jtis`, all empty, and adds
    `deploy_runs.ci_token_id`, empty on every existing run: no run was submitted with a CI
    token before. Nothing else changes; with no rule, no ID token can be exchanged.
    """
    op.create_table(
        "ci_trust_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.String(length=255), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("kinds", sa.JSON(), nullable=False),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_ci_trust_rules_principal_id_principals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_ci_trust_rules_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_ci_trust_rules_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["principals.id"],
            name=op.f("fk_ci_trust_rules_created_by_principals"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["principals.id"],
            name=op.f("fk_ci_trust_rules_updated_by_principals"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ci_trust_rules")),
    )
    op.create_index(
        op.f("ix_ci_trust_rules_issuer"),
        "ci_trust_rules",
        ["issuer", "environment_id", "repository_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ci_trust_rules_principal_id"), "ci_trust_rules", ["principal_id"], unique=False
    )
    op.create_index(
        op.f("ix_ci_trust_rules_repository_id"), "ci_trust_rules", ["repository_id"], unique=False
    )
    op.create_table(
        "ci_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=True),
        sa.Column("rule_revision", sa.Integer(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("kinds", sa.JSON(), nullable=False),
        sa.Column("sha", sa.String(length=40), nullable=True),
        sa.Column("jti_sha256", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["ci_trust_rules.id"],
            name=op.f("fk_ci_tokens_rule_id_ci_trust_rules"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_ci_tokens_principal_id_principals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_ci_tokens_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_ci_tokens_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ci_tokens")),
    )
    op.create_index(op.f("ix_ci_tokens_key_id"), "ci_tokens", ["key_id"], unique=True)
    for column in ("rule_id", "principal_id", "environment_id", "repository_id"):
        op.create_index(op.f(f"ix_ci_tokens_{column}"), "ci_tokens", [column], unique=False)
    op.create_table(
        "ci_oidc_jtis",
        sa.Column("jti_sha256", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("jti_sha256", name=op.f("pk_ci_oidc_jtis")),
    )
    op.create_index(
        op.f("ix_ci_oidc_jtis_expires_at"), "ci_oidc_jtis", ["expires_at"], unique=False
    )
    with op.batch_alter_table("deploy_runs") as batch:
        batch.add_column(sa.Column("ci_token_id", sa.Uuid(), nullable=True))
        batch.create_index(batch.f("ix_deploy_runs_ci_token_id"), ["ci_token_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema.

    Drops the three tables and the column. Every CI token stops working at once, and a deploy
    queued with one and not yet claimed runs in the previous release as an ordinary deploy of
    its service account, which still needs its `DEPLOY` grant there; the previous release has
    no CI tokens, so no new one can be queued. The rules are gone: recreate them after an
    upgrade.
    """
    with op.batch_alter_table("deploy_runs") as batch:
        batch.drop_index(batch.f("ix_deploy_runs_ci_token_id"))
        batch.drop_column("ci_token_id")
    op.drop_index(op.f("ix_ci_oidc_jtis_expires_at"), table_name="ci_oidc_jtis")
    op.drop_table("ci_oidc_jtis")
    for column in ("repository_id", "environment_id", "principal_id", "rule_id"):
        op.drop_index(op.f(f"ix_ci_tokens_{column}"), table_name="ci_tokens")
    op.drop_index(op.f("ix_ci_tokens_key_id"), table_name="ci_tokens")
    op.drop_table("ci_tokens")
    op.drop_index(op.f("ix_ci_trust_rules_repository_id"), table_name="ci_trust_rules")
    op.drop_index(op.f("ix_ci_trust_rules_principal_id"), table_name="ci_trust_rules")
    op.drop_index(op.f("ix_ci_trust_rules_issuer"), table_name="ci_trust_rules")
    op.drop_table("ci_trust_rules")
