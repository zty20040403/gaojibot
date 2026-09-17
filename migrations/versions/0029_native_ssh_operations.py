"""Allow native SSH operations without rewriting historical approvals."""
from __future__ import annotations

import os
import re

from alembic import op

revision = "0029_native_ssh_operations"
down_revision = "0028_task_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise RuntimeError("Invalid PostgreSQL schema")
    op.drop_constraint("ck_fleet_operations_action", "fleet_operations", schema=schema)
    op.create_check_constraint("ck_fleet_operations_action", "fleet_operations",
        "operation IN ('service.start','service.stop','service.restart','maxops.execute','ssh.execute')", schema=schema)


def downgrade() -> None:
    raise RuntimeError("Preserve SSH operation receipts and anti-replay evidence; migration is forward-only")
