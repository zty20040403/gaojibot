"""Retire the two empty tables used only by the historical offline importer."""
from __future__ import annotations

import os

from alembic import op
import sqlalchemy as sa

revision = "0114_retire_legacy_import"
down_revision = "0113_group_attention_scheduler"
branch_labels = depends_on = None


def _schema() -> str:
    return os.environ.get("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"


def upgrade() -> None:
    quoted = op.get_bind().dialect.identifier_preparer.quote(_schema())
    # Lock both before checking either, closing the check/drop writer race.
    op.execute(f'LOCK TABLE {quoted}.maintenance_state, {quoted}.embeddings IN ACCESS EXCLUSIVE MODE')
    for table in ("maintenance_state", "embeddings"):
        if op.get_bind().execute(sa.text(f'SELECT 1 FROM {quoted}.{table} LIMIT 1')).first():
            raise RuntimeError(
                f"0114 refuses nonempty {table}; preserve the database at 0113, "
                "back up and resolve historical data before retrying"
            )
    op.execute(f'DROP TABLE {quoted}.maintenance_state RESTRICT')
    op.execute(f'DROP TABLE {quoted}.embeddings RESTRICT')


def downgrade() -> None:
    schema = _schema()
    op.create_table(
        "maintenance_state",
        sa.Column("job_name", sa.Text(), primary_key=True),
        sa.Column("last_success_key", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        schema=schema,
    )
    op.create_table(
        "embeddings",
        sa.Column("embedding_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "scope_key", "source_type", "source_id", "model", "content_hash",
            name="uq_embeddings_source_model_hash",
        ),
        schema=schema,
    )
    op.create_index(
        "idx_embeddings_scope_source", "embeddings",
        ["scope_key", "source_type", "source_id"], schema=schema,
    )
