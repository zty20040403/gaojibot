"""Retire removed bot features; live cluster-control tables and chat data stay."""
import os
from alembic import op
import sqlalchemy as sa

revision = '0077_retired_features'
down_revision = '0076_schedules'
branch_labels = depends_on = None

RETIRED_TABLES = (
    'agent_routines',
    'bridge_cursors',
    'bridge_deliveries',
    'bridge_sources',
    'context_compartments',
    'context_state',
    'group_wiki_generations',
    'group_wiki_pages',
    'group_wiki_publications',
    'group_wiki_settings',
    'mcp_calls',
    'mcp_continuation_effects',
    'mcp_continuations',
    'mcp_group_tokens',
    'mcp_servers',
    'mcp_tools',
    'migration_activity_observations',
    'migration_actor_mapping_revisions',
    'migration_actor_mappings',
    'migration_artifact_plans',
    'migration_control_audit',
    'migration_dispatch_activities',
    'migration_events',
    'migration_file_continuations',
    'migration_item_plans',
    'migration_items',
    'migration_journals',
    'migration_preview_revisions',
    'migration_project_grants',
    'migration_routes',
    'migration_runtime_baselines',
    'migration_scope_aliases',
    'migration_subagent_continuations',
    'migration_subagent_run_provenance',
    'migration_verification_intents',
    'migration_verification_receipts',
    'migration_verification_sources',
    'migration_work_sources',
    'pinned_messages',
    'proactive_arms',
    'proactive_feedback',
    'proactive_trials',
    'project_blobs',
    'project_browser_calls',
    'project_browser_sessions',
    'project_events',
    'project_file_deliveries',
    'project_focus',
    'project_io',
    'project_lifecycle_runs',
    'project_lifecycle_steps',
    'project_members',
    'project_previews',
    'project_quota_authorizations',
    'project_scope_limits',
    'project_snapshot_staging',
    'project_task_bindings',
    'project_task_materializations',
    'project_task_publications',
    'project_task_releases',
    'project_turn_saves',
    'project_usage',
    'project_version_blobs',
    'project_versions',
    'projects',
    'qq_migration_native_plan_sources',
    'qq_migration_native_sources',
    'qq_migration_public_history_grants',
    'qq_migration_public_history_snapshots',
    'qq_official_migration_artifacts',
    'qq_official_native_schedule_occurrences',
    'qq_official_native_schedule_sources',
    'qq_official_project_artifacts',
    'qq_official_project_focus',
    'qq_official_project_grants',
    'qq_official_project_io_authority',
    'qq_official_project_lifecycle_authority',
    'qq_official_project_resource_grants',
    'qq_official_project_resource_origins',
    'qq_official_project_task_authority',
    'qq_official_schedule_bindings',
    'qq_official_subagent_dispatches',
    'qq_official_wiki_configurations',
    'reminders',
    'routine_budget_reservations',
    'routine_children',
    'routine_effects',
    'routine_runs',
    'subagent_artifacts',
    'subagent_checkpoints',
    'subagent_collaborators',
    'subagent_controls',
    'subagent_deliveries',
    'subagent_events',
    'subagent_evidence',
    'subagent_external_calls',
    'subagent_revision_feedback',
    'subagent_run_contexts',
    'subagent_runs',
    'subagent_sessions',
    'subagent_tasks',
    'watch_state',
    'workbench_login_codes',
    'workbench_sessions',
)

RETIRED_FUNCTIONS = (
    'migration_work_source_parent_check',
    'qq_project_artifact_immutable',
    'migration_mapping_source_immutable',
    'migration_mapping_revision_immutable',
    'qq_migration_native_source_immutable',
    'migration_file_binding_immutable',
    'migration_project_grant_immutable',
    'qq_official_project_io_immutable',
    'qq_official_task_profile_valid',
    'qq_project_resource_origin_immutable',
    'qq_project_resource_grant_validate',
    'qq_wiki_source_immutable',
    'guard_native_schedule_source',
    'guard_native_schedule_occurrence',
)


def upgrade():
    schema = os.getenv('AI_POSTGRES_SCHEMA', 'qq_bot').strip() or 'qq_bot'
    quote = op.get_bind().dialect.identifier_preparer.quote
    tables = ','.join(quote(schema) + '.' + quote(table) for table in RETIRED_TABLES)
    # Only obsolete foreign keys point into these tables; histories, files and
    # account bindings remain. The deployment requires a fresh database backup.
    op.execute(sa.text('DROP TABLE ' + tables + ' CASCADE'))
    for function in RETIRED_FUNCTIONS:
        op.execute(sa.text(f'DROP FUNCTION {quote(schema)}.{quote(function)}()'))


def downgrade():
    raise RuntimeError('retired_feature_data_requires_backup_restore')
