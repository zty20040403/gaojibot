"""Namespace platform identities and message sources without rewriting legacy IDs."""

import os
import re

from alembic import op
import sqlalchemy as sa


revision = "0061_platform_actor_identity"
down_revision = "0060_project_quota_jobs"
branch_labels = None
depends_on = None


def _schema():
    value = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError("Invalid PostgreSQL schema")
    return value


def upgrade():
    s = _schema()
    for name in ("account_key", "identity_kind", "identity_namespace"):
        op.add_column("principal_identities", sa.Column(
            name, sa.Text(), nullable=False, server_default=""), schema=s)
    op.drop_constraint("uq_principal_identities_platform_native", "principal_identities", schema=s)
    op.create_unique_constraint("uq_principal_identities_actor", "principal_identities", [
        "platform", "account_key", "identity_kind", "identity_namespace", "native_user_id",
    ], schema=s)
    for name in ("sender_actor_key", "source_endpoint_key"):
        op.add_column("messages", sa.Column(
            name, sa.Text(), nullable=False, server_default=""), schema=s)
    op.drop_constraint("uq_messages_conversation_native", "messages", schema=s)
    op.create_unique_constraint("uq_messages_conversation_source_native", "messages", [
        "conversation_id", "source_endpoint_key", "native_message_id",
    ], schema=s)
    op.drop_index("idx_messages_reply_native", table_name="messages", schema=s)
    op.create_index("idx_messages_reply_native", "messages", [
        "conversation_id", "source_endpoint_key", "reply_to_native_message_id",
    ], schema=s)


def downgrade():
    s = _schema()
    # A downgrade must never silently collapse identity or transport namespaces.
    populated = op.get_bind().execute(sa.text(f'''
        SELECT EXISTS(SELECT 1 FROM "{s}".principal_identities
          WHERE account_key<>'' OR identity_kind<>'' OR identity_namespace<>'')
          OR EXISTS(SELECT 1 FROM "{s}".messages
          WHERE sender_actor_key<>'' OR source_endpoint_key<>'')
    ''')).scalar()
    if populated:
        raise RuntimeError("Export structured platform identities and messages before downgrade")
    op.drop_index("idx_messages_reply_native", table_name="messages", schema=s)
    op.create_index("idx_messages_reply_native", "messages", [
        "conversation_id", "reply_to_native_message_id",
    ], schema=s)
    op.drop_constraint("uq_messages_conversation_source_native", "messages", schema=s)
    op.create_unique_constraint("uq_messages_conversation_native", "messages", [
        "conversation_id", "native_message_id",
    ], schema=s)
    for name in ("source_endpoint_key", "sender_actor_key"):
        op.drop_column("messages", name, schema=s)
    op.drop_constraint("uq_principal_identities_actor", "principal_identities", schema=s)
    op.create_unique_constraint("uq_principal_identities_platform_native", "principal_identities", [
        "platform", "native_user_id",
    ], schema=s)
    for name in ("identity_namespace", "identity_kind", "account_key"):
        op.drop_column("principal_identities", name, schema=s)
