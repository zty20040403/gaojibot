"""Read-only M5 inventory/export. No target writes, credentials, or I/O.

Host callbacks read the snapshot policy for route selection, explicit identity
mapping and project grants using this same cursor. They are NOT the production
effect guards that acquire FOR UPDATE/FOR SHARE locks. Export data is not an
execution grant. The authenticated caller rechecks its admin/session against a
current READ COMMITTED view before opening the snapshot AND before returning
data: RR can retain a role/grant that was concurrently revoked.
Use a REPEATABLE READ READ ONLY transaction for a consistent multi-table view;
conversion must re-read under its own locks and compare the safe export digest.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from typing import Callable, Literal

from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.message_ir import TextNode, body_from_json
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey

Disposition = Literal['confirmed', 'unsent', 'unknown', 'terminal', 'unsupported']
KINDS = frozenset({'reminder', 'routine', 'subagent', 'project_task', 'file'})


class PreviewDenied(ValueError):
    def __init__(self):
        super().__init__('migration_preview_unavailable')


class Unsupported(ValueError):
    pass


@dataclass(frozen=True, repr=False)
class Selection:
    source: EndpointKey
    source_scope: ConversationScope
    target: EndpointKey

    def __post_init__(self):
        if (self.source.account.platform not in {'onebot-v11', 'qq-official'}
                or self.target.account.platform != 'qq-official'
                or self.source_scope.platform != self.source.account.platform
                or self.source_scope.kind != self.source.kind
                or self.source_scope.native_conversation_id != self.source.native_conversation_id
                or (self.source_scope.account is not None and self.source_scope.account != self.source.account)
                or (self.source.account.platform == 'qq-official' and self.source_scope.key != self.source.scope.key)
                or (self.source_scope.bot_native_user_id
                    and self.source_scope.bot_native_user_id != self.source.account.app_id)):
            raise PreviewDenied()


@dataclass(frozen=True, repr=False)
class SourceRef:
    kind: str
    key: str

    def __post_init__(self):
        if (self.kind not in KINDS or not isinstance(self.key, str)
                or not 1 <= len(self.key.encode()) <= 180
                or not re.fullmatch('[a-z0-9:]+', self.key)):
            raise PreviewDenied()


@dataclass(frozen=True, repr=False)
class MappingEvidence:
    mapping_id: str
    revision: int
    source: ActorKey
    target_endpoint: EndpointKey
    target: ActorKey


@dataclass(frozen=True, repr=False)
class Preview:
    source: SourceRef
    disposition: Disposition
    reason: str
    original_deadline: int | None
    # Canonical JSON provides immutability; never contains raw dispatch/response.
    export_json: str | None = field(repr=False)
    safe_digest: str

    @property
    def export(self):
        return json.loads(self.export_json) if self.export_json is not None else None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _obj(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise Unsupported('invalid_structured_source')
    return value


def _integer(value, *, zero=False):
    if type(value) is not int or not (0 if zero else 1) <= value <= 2**63 - 1:
        raise Unsupported('invalid_integer')
    return value


def _deadline(value):
    return None if value is None else _integer(value, zero=True)


def _text(value, limit=16000):
    if (not isinstance(value, str) or len(value.encode()) > limit
            or any(ord(c) < 32 and c not in '\n\t\r' for c in value)
            or '[CQ:' in value):
        raise Unsupported('nonportable_text')
    return value


def _hex(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise Unsupported('invalid_immutable_reference')
    return value


def _body(value):
    body = body_from_json(value)
    if not all(isinstance(node, TextNode) for node in body.nodes):
        raise Unsupported('nonportable_message_body')
    return _text(''.join(node.text for node in body.nodes))


def _row(cursor, sql, args=()):
    row = cursor.execute(sql, args).fetchone()
    return dict(row) if row is not None else None


def _rows(cursor, sql, args=()):
    return [dict(r) for r in cursor.execute(sql, args).fetchall()]


def _snapshot_call(function, *args):
    try:
        return function(*args)
    except Exception:
        raise PreviewDenied() from None


def _aggregate(outcomes):
    priority = {'unsent': 0, 'terminal': 1, 'confirmed': 2, 'unsupported': 3, 'unknown': 4}
    return max(outcomes, key=lambda value: priority[value[0]], default=('unsent', 'no_delivery'))


class Previewer:
    """Callbacks are read-only snapshot ports with no permissive defaults.

    route_guard(cursor, selection, ref_or_none, facts) -> strict True
      facts include stored_bot_id=None for account-unattributed legacy rows.
      The host must explicitly approve those weak rows, never infer the bot.
    actor_mapping(cursor, selection, ref, source_actor) -> MappingEvidence|None
    project_guard(cursor, selection, ref, mapping, project_id, operation) -> True
    """
    def __init__(self, *, route_guard: Callable, actor_mapping: Callable, project_guard: Callable):
        if not all(callable(x) for x in (route_guard, actor_mapping, project_guard)):
            raise PreviewDenied()
        self.route_guard = route_guard
        self.actor_mapping = actor_mapping
        self.project_guard = project_guard

    def _route(self, cursor, selection, ref, *, bot=None, actor=None):
        if bot is not None and str(bot) != selection.source.account.app_id:
            raise PreviewDenied()
        if _snapshot_call(self.route_guard, cursor, selection, ref,
                {'stored_bot_id': bot, 'stored_actor_id': actor,
                 'account_attributed': bot is not None}) is not True:
            raise PreviewDenied()

    def _mapped(self, cursor, selection, ref, native, *, stored_actor=None, project=None, operation='execute'):
        if stored_actor is None:
            if selection.source.account.platform != 'onebot-v11':
                raise Unsupported('source_actor_unavailable')
            # This identifies the explicit source domain only. The callback
            # supplies the independently proven target identity and its version.
            source_actor = ActorKey(selection.source.account, 'user', '', str(native))
        else:
            source_actor = ActorKey.parse(stored_actor)
        if not source_actor.matches_endpoint(selection.source) or source_actor.identity_kind == 'bot':
            raise Unsupported('source_actor_unavailable')
        value = _snapshot_call(self.actor_mapping, cursor, selection, ref, source_actor)
        if (not isinstance(value, MappingEvidence) or value.source != source_actor
                or value.target_endpoint != selection.target
                or not value.target.matches_endpoint(selection.target)
                or value.target.identity_kind == 'bot'
                or not re.fullmatch('[a-f0-9]{32}', value.mapping_id)
                or type(value.revision) is not int or value.revision < 1):
            raise Unsupported('explicit_actor_mapping_required')
        project_grant = None
        if project is not None:
            from .project_authority import ProjectGrantRef
            observed = _snapshot_call(self.project_guard, cursor, selection, ref, value, _integer(project), operation)
            if type(observed) is ProjectGrantRef:
                project_grant = {'grant_id': observed.grant_id, 'revision': observed.revision}
            elif observed is not True:
                raise Unsupported('current_project_grant_required')
        result = {'mapping_id': value.mapping_id, 'revision': value.revision,
                'source_actor': source_actor.key, 'target_actor': value.target.key,
                'target_endpoint': selection.target.key}
        # A legacy boolean can classify a preview, never select a grant during
        # conversion. Executable project domains require this exact reviewed ref.
        if project_grant is not None:
            result['project_grant'] = project_grant
        return result

    def preview(self, cursor, selection, ref, *, now):
        _integer(now, zero=True)
        if not isinstance(selection, Selection) or not isinstance(ref, SourceRef):
            raise PreviewDenied()
        self._route(cursor, selection, None)
        deadline = None
        try:
            state, reason, deadline, exported = getattr(self, '_' + ref.kind)(cursor, selection, ref, now)
            if state != 'unsent':
                exported = None
        except (Unsupported, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            if isinstance(exc, PreviewDenied):
                raise
            state, reason, exported = 'unsupported', 'invalid_or_nonportable_source', None
            if isinstance(exc, Unsupported) and re.fullmatch('[a-z_]{1,80}', str(exc)):
                reason = str(exc)
        safe = {'version': 'migration-preview-v1', 'source': asdict(ref),
                'disposition': state, 'reason': reason, 'original_deadline': deadline,
                'source_endpoint': selection.source.key, 'source_scope': selection.source_scope.key,
                'target_endpoint': selection.target.key, 'export': exported}
        return Preview(ref, state, reason, deadline,
            _json(exported) if exported is not None else None,
            hashlib.sha256(_json(safe).encode()).hexdigest())

    def inventory(self, cursor, selection, kind, *, after='', limit=50):
        """Keyset metadata discovery. preview() performs per-item authority checks."""
        if kind not in KINDS or type(limit) is not int or not 1 <= limit <= 100:
            raise PreviewDenied()
        if not isinstance(after, str) or len(after) > 180:
            raise PreviewDenied()
        self._route(cursor, selection, None)
        scope = selection.source_scope.key
        if kind == 'reminder':
            query = 'SELECT reminder_id::text AS k FROM reminders WHERE scope_key=?'
        elif kind == 'routine':
            query = """SELECT 'spec:'||routine_id::text AS k FROM agent_routines WHERE scope_key=?
                UNION ALL SELECT 'run:'||r.run_id AS k FROM routine_runs r
                JOIN agent_routines a USING(routine_id) WHERE a.scope_key=?"""
        elif kind in {'subagent', 'project_task'}:
            query = ('SELECT task_id::text AS k FROM subagent_tasks WHERE scope_key=? '
                     + ('AND project_id IS NULL' if kind == 'subagent' else 'AND project_id IS NOT NULL'))
        else:
            # A single bounded page for each origin prevents one source hiding
            # the others; callers pass back the returned lexical key unchanged.
            query = """SELECT 'subagent:'||d.task_id::text||':'||d.revision::text||':'||d.delivery_key AS k
                FROM subagent_deliveries d JOIN subagent_tasks t USING(task_id) WHERE t.scope_key=?
                UNION ALL SELECT 'project:'||f.delivery_id::text AS k FROM project_file_deliveries f
                JOIN projects p USING(project_id) WHERE p.scope_key=?
                UNION ALL SELECT 'official:'||artifact_id AS k FROM qq_official_file_artifacts WHERE source_scope_key=?"""
        args = (scope, scope, scope) if kind == 'file' else (scope, scope) if kind == 'routine' else (scope,)
        rows = _rows(cursor, 'SELECT k FROM (' + query + ') AS inventory WHERE k>? ORDER BY k LIMIT ?',
                     (*args, after, limit))
        return tuple(SourceRef(kind, row['k']) for row in rows)

    def _scope(self, selection, row, field='scope_key'):
        if row is None or row[field] != selection.source_scope.key:
            raise PreviewDenied()

    def _delivery(self, cursor, delivery_id, selection):
        if delivery_id is None:
            return 'unsent', 'no_delivery'
        row = _row(cursor, 'SELECT * FROM deliveries WHERE delivery_id=?', (delivery_id,))
        if row is None:
            return 'unknown', 'missing_delivery_evidence'
        if (row['source_scope_key'] != selection.source_scope.key
                or row['target_platform'] != selection.source.account.platform
                or row['target_kind'] != selection.source.kind
                or row['target_native_conversation_id'] != selection.source.native_conversation_id
                or (row['account_key'] is not None and
                    (row['account_key'] != selection.source.account.key or row['endpoint_key'] != selection.source.key))):
            raise PreviewDenied()
        attempts = _rows(cursor, 'SELECT state FROM delivery_attempts WHERE delivery_id=? ORDER BY attempt', (delivery_id,))
        states = {r['state'] for r in attempts}
        if row['status'] == 'committed' and not row['native_message_id'] and row['account_key'] is None:
            return 'terminal', 'legacy_terminal_acceptance_unproven'
        if row['status'] in {'committed', 'accepted'} or states & {'committed', 'accepted'}:
            return 'confirmed', 'prior_message_accepted'
        if row['status'] in {'sending', 'ambiguous'} or states & {'sending', 'dispatching', 'ambiguous', 'unknown'}:
            return 'unknown', 'prior_message_unresolved'
        if attempts or row['attempts']:
            # Legacy failed attempts don't carry the M3 confirmed-not-accepted
            # proof. A mere failure string is not a safe retry decision.
            if states and states <= {'proven_unsent', 'confirmed_unaccepted'}:
                return 'unsent', 'confirmed_not_accepted'
            return 'unknown', 'legacy_attempt_outcome_unproven'
        if row['status'] == 'cancelled':
            return 'terminal', 'cancelled'
        return 'unsent', 'never_dispatched'

    def _reminder(self, cursor, s, ref, now):
        row = _row(cursor, 'SELECT * FROM reminders WHERE reminder_id=?', (_integer(int(ref.key)),))
        self._scope(s, row)
        self._route(cursor, s, ref, actor=row['creator_native_user_id'])
        if row['status'] == 'sent':
            return 'terminal', 'legacy_terminal_acceptance_unproven', None, None
        if row['status'] == 'sending' or row['attempts']:
            return 'unknown', 'reminder_attempt_unproven', None, None
        if row['status'] != 'pending':
            return 'terminal', 'cancelled', None, None
        mapping = self._mapped(cursor, s, ref, row['creator_native_user_id'])
        return 'unsent', 'never_dispatched', None, {'type': 'reminder', 'mapping': mapping,
            'text': _text(row['message']), 'scheduled_for': row['scheduled_for'],
            'next_attempt_at': row['next_attempt_at'], 'created_at': row['created_at'],
            'deadline_policy_required': True}

    def _routine(self, cursor, s, ref, now):
        prefix, key = ref.key.split(':', 1)
        run = None
        if prefix == 'run':
            run = _row(cursor, 'SELECT * FROM routine_runs WHERE run_id=?', (key,))
            if run is None:
                raise PreviewDenied()
            key = run['routine_id']
        elif prefix != 'spec':
            raise PreviewDenied()
        row = _row(cursor, 'SELECT * FROM agent_routines WHERE routine_id=?', (_integer(int(key)),))
        self._scope(s, row)
        self._route(cursor, s, ref, bot=row['bot_id'], actor=row['creator_id'])
        if row['routine_kind'] != 'schedule':
            return 'unsupported', 'watch_trigger_not_implemented', None, None
        deadline = _deadline(run['deadline_at']) if run else None
        if run:
            status, reason = self._delivery(cursor, run['delivery_id'], s)
            if status != 'unsent':
                return status, reason, deadline, None
            pending_effect = _row(cursor, "SELECT 1 FROM routine_effects WHERE run_id=? AND state<>'completed' LIMIT 1", (run['run_id'],))
            pending_child = _row(cursor, """SELECT 1 FROM routine_children c JOIN subagent_tasks t ON t.task_id=c.task_id
                WHERE c.run_id=? AND t.status NOT IN ('completed','partial','failed','cancelled') LIMIT 1""", (run['run_id'],))
            if pending_effect or pending_child or run['status'] == 'running':
                return 'unknown', 'routine_execution_unresolved', deadline, None
            if run['version'] != row['version'] or run['status'] in {'cancelled', 'failed', 'skipped', 'quiet'}:
                return 'terminal', 'routine_terminal_or_superseded', deadline, None
            if deadline is not None and now >= deadline and run['status'] not in {'generated', 'enqueued'}:
                return 'terminal', 'execution_deadline_expired', deadline, None
            spec = _obj(run['spec_json'])
            if any(spec[k] != row[k] for k in ('scope_key', 'bot_id', 'creator_id', 'version')):
                raise PreviewDenied()
        else:
            spec = row
        if not row['enabled'] or row['state'] != 'active':
            return 'terminal', 'routine_disabled', deadline, None
        mapping = self._mapped(cursor, s, ref, row['creator_id'], project=row['project_id'])
        data = {k: spec[k] for k in ('name', 'cron', 'timezone', 'max_tokens', 'daily_tokens', 'daily_runs', 'next_due_at', 'version')}
        data.update(type='routine_run' if run else 'routine_schedule', prompt=_text(spec['prompt']), mapping=mapping)
        if run:
            data.update(run_id=run['run_id'], scheduled_at=run['scheduled_at'], missed_from=run['missed_from'],
                        execution_deadline=deadline, generated=run['status'] in {'generated', 'enqueued'})
            if data['generated']:
                data['result_text'] = _body(run['body_json'])
        return 'unsent', 'structured_routine', deadline, data

    def _task_data(self, cursor, s, ref, now, *, project):
        task = _row(cursor, 'SELECT * FROM subagent_tasks WHERE task_id=?', (_integer(int(ref.key)),))
        self._scope(s, task)
        if (task['project_id'] is not None) != project or task['account_key'] is not None:
            raise PreviewDenied()
        control = _row(cursor, 'SELECT * FROM subagent_controls WHERE task_id=?', (task['task_id'],))
        if control is None:
            raise Unsupported('missing_task_control')
        dispatch = _obj(control['dispatch_json'])
        self._route(cursor, s, ref, bot=dispatch.get('bot_id'), actor=task['requester_user_id'])
        if control['revision'] != 1:
            return 'unsupported', 'revision_actor_export_not_implemented', None, None
        deadline = _deadline(dispatch.get('deadline'))
        finals = _rows(cursor, "SELECT delivery_id FROM deliveries WHERE idempotency_key=? OR idempotency_key LIKE ?",
            (f"subagent-final:{task['task_id']}:{control['revision']}", f"subagent-final:{task['task_id']}:{control['revision']}:project:%"))
        final_state, final_reason = _aggregate(self._delivery(cursor, final['delivery_id'], s) for final in finals)
        if final_state == 'unknown':
            return final_state, final_reason, deadline, None
        files = _rows(cursor, 'SELECT state,delivery_key FROM subagent_deliveries WHERE task_id=? AND revision=?', (task['task_id'], control['revision']))
        if any(f['state'] in {'sending', 'unknown'} for f in files):
            return 'unknown', 'task_file_unresolved', deadline, None
        external = _rows(cursor, 'SELECT status,response_json FROM subagent_external_calls WHERE task_id=?', (task['task_id'],))
        for call in external:
            response = _obj(call['response_json'])
            operation = response.get('operation', response)
            if (call['status'] != 'resolved' or response.get('pending') or not isinstance(operation, dict)
                    or operation.get('pending') or operation.get('status') not in {'succeeded', 'completed', 'failed', 'cancelled', 'expired'}):
                return 'unknown', 'task_external_unresolved', deadline, None
        runs = _rows(cursor, 'SELECT * FROM subagent_runs WHERE task_id=? ORDER BY run_id', (task['task_id'],))
        # A manually requeued pending run can still carry old effects. Do not
        # equate its present status with a never-started tool invocation.
        for run in runs:
            if run['status'] == 'pending' and (run['attempt'] > 0 or _row(cursor, """SELECT 1 FROM subagent_events
                    WHERE run_id=? AND event_type='agent.tool_started' LIMIT 1""", (run['run_id'],))):
                return 'unknown', 'task_effect_history_requires_review', deadline, None
        if task['status'] in {'planning', 'running', 'verifying', 'cancelling', 'waiting_external', 'revising'} or any(r['status'] in {'running', 'interrupted', 'waiting_external'} for r in runs):
            return 'unknown', 'task_execution_unresolved', deadline, None
        if final_state != 'unsent':
            return final_state, final_reason, deadline, None
        if task['cancel_requested'] or task['status'] in {'cancelled', 'failed'}:
            return 'terminal', 'task_cancelled_or_failed', deadline, None
        if deadline is not None and now >= deadline and task['status'] not in {'completed', 'partial'}:
            return 'terminal', 'execution_deadline_expired', deadline, None
        mapping = self._mapped(cursor, s, ref, task['requester_user_id'], project=task['project_id'])
        # Refuse in-step recovery in this preview version. Completion evidence
        # points to the original run; it is never reissued as a target tool call.
        source_plan = _obj(task['plan_json'])
        if source_plan.get('contract_refresh'):
            raise Unsupported('task_contract_refresh_unresolved')
        raw_contract = source_plan.get('contract')
        contract = None
        if raw_contract is not None:
            if not isinstance(raw_contract, dict) or set(raw_contract)-{'version','objective','deliverables','constraints','acceptance','delivery_required','outcome_checks'}:
                raise Unsupported('task_contract_unportable')
            if type(raw_contract.get('version',1)) is not int or raw_contract.get('version',1) not in {1,2,3}:
                raise Unsupported('task_contract_unportable')
            contract = {'version':raw_contract.get('version',1),'objective':_text(raw_contract.get('objective',''))}
            for key in ('deliverables','constraints','acceptance'):
                values=raw_contract.get(key,[])
                if not isinstance(values,list) or len(values)>16:
                    raise Unsupported('task_contract_unportable')
                contract[key]=[_text(value,2000) for value in values]
            if type(raw_contract.get('delivery_required',False)) is not bool:
                raise Unsupported('task_contract_unportable')
            contract['delivery_required']=raw_contract.get('delivery_required',False)
            from src.plugins.ai_chat.agent.outcomes import normalize_checks, validate_check_targets
            try:
                checks=normalize_checks(raw_contract.get('outcome_checks',[]),contract['acceptance'])
                if contract['version']>=3:
                    validate_check_targets(checks,contract['acceptance'])
            except ValueError:
                raise Unsupported('task_contract_unportable') from None
            contract['outcome_checks']=list(checks)
        plan_mode=source_plan.get('mode','workflow')
        if plan_mode not in {'workflow','delegate'}:
            raise Unsupported('task_plan_mode_unportable')
        optional={step['id'] for step in source_plan.get('steps',[]) if isinstance(step,dict) and step.get('optional') is True and isinstance(step.get('id'),str)}
        safe_runs = []
        for run in runs:
            result = _obj(run['result_json'])
            artifacts=result.get('artifacts',[])
            if not isinstance(artifacts,list) or len(artifacts)>32:
                raise Unsupported('task_artifact_reference_unportable')
            immutable_refs=[]
            for artifact in artifacts:
                if not isinstance(artifact,dict):
                    raise Unsupported('task_artifact_reference_unportable')
                name=artifact.get('name','')
                size=artifact.get('size')
                if (not isinstance(name,str) or not name or any(c in name for c in '/\\')
                        or type(size) is not int or not 1<=size<=104857600):
                    raise Unsupported('task_artifact_reference_unportable')
                immutable_refs.append({'source_task_id':task['task_id'],'source_run_id':run['run_id'],
                    'sha256':_hex(artifact.get('snapshot')),'filename':_text(name,180),'byte_size':size})
            safe_runs.append({'source_run_id': run['run_id'], 'step_key': _text(run['step_key'], 256),
                'role': _text(run['role'], 64), 'objective': _text(run['objective']),
                'deliverable': _text(run['deliverable']), 'state': run['status'], 'optional': run['step_key'] in optional,
                'dependencies': [_text(v, 256) for v in json.loads(run['dependencies_json'])],
                'required_tools': [_text(v, 128) for v in json.loads(run['allowed_tools_json'])],
                'completed_summary': _text(result.get('summary', '')), 'immutable_artifact_refs': immutable_refs})
        final_text = None
        if task['status'] in {'completed', 'partial'}:
            final = _obj(task['result_json'])
            nested = final.get('result', {})
            if not isinstance(nested, dict):
                raise Unsupported('invalid_terminal_result')
            final_text = _text(final.get('answer') or nested.get('summary') or '')
            if not final_text:
                raise Unsupported('terminal_result_unavailable')
        return 'unsent', 'structured_task', deadline, {'type': 'project_task' if project else 'subagent',
            'source_task_id': task['task_id'], 'source_revision': control['revision'],
            'control_version': control['version'], 'mapping': mapping,
            'source_task_status': task['status'], 'source_finished_at': _deadline(task['finished_at']),
            'frozen_final_text': final_text, 'task_contract': contract, 'plan_mode': plan_mode,
            'objective': _text(task['objective']), 'execution_deadline': deadline,
            'original_trigger_canonical_id': task['trigger_message_id'],
            'steps': safe_runs, 'already_acknowledged_files': sum(f['state'] == 'acknowledged' for f in files),
            'acknowledged_file_refs': [{'task_id': task['task_id'], 'revision': control['revision'],
                'delivery_key': _hex(f['delivery_key'])} for f in files if f['state'] == 'acknowledged'],
            'source_context_requires_rebuild': True}

    def _subagent(self, cursor, s, ref, now):
        return self._task_data(cursor, s, ref, now, project=False)

    def _project_task(self, cursor, s, ref, now):
        result = self._task_data(cursor, s, ref, now, project=True)
        if result[0] != 'unsent':
            return result
        binding = _row(cursor, 'SELECT * FROM project_task_bindings WHERE task_id=?', (int(ref.key),))
        if binding is None:
            raise Unsupported('missing_project_binding')
        project = _row(cursor, 'SELECT * FROM projects WHERE project_id=?', (binding['project_id'],))
        self._scope(s, project)
        self._route(cursor, s, ref, bot=project['bot_native_user_id'])
        pending = _row(cursor, "SELECT 1 FROM project_io WHERE project_id=? AND state IN ('reserved','dispatched','needs_attention') LIMIT 1", (binding['project_id'],))
        if (pending or project['lifecycle_job_id'] is not None or project['locked_task_id'] != int(ref.key)
                or project['workspace_generation'] != binding['generation'] or project['lifecycle_fence'] != binding['fence']):
            return 'unknown', 'project_physical_state_unresolved', result[2], None
        if project['status'] not in {'stopped', 'hibernated', 'archived'}:
            return 'unsupported', 'project_quiescent_evidence_required', result[2], None
        data = {**result[3], 'project_id': project['project_id'], 'base_version': binding['base_version'],
                'manifest_sha256': _hex(binding['manifest_sha256']), 'generation': binding['generation'], 'fence': binding['fence']}
        return result[0], result[1], result[2], data

    def _file(self, cursor, s, ref, now):
        origin, key = ref.key.split(':', 1)
        if origin == 'subagent':
            task_id, revision, digest = key.split(':')
            task_id, revision = _integer(int(task_id)), _integer(int(revision))
            digest = _hex(digest)
            row = _row(cursor, 'SELECT * FROM subagent_deliveries WHERE task_id=? AND revision=? AND delivery_key=?', (task_id, revision, digest))
            task = _row(cursor, 'SELECT * FROM subagent_tasks WHERE task_id=?', (task_id,))
            self._scope(s, task)
            if row is None or task['account_key'] is not None:
                raise PreviewDenied()
            control = _row(cursor, 'SELECT revision,dispatch_json FROM subagent_controls WHERE task_id=?', (task_id,))
            self._route(cursor, s, ref, bot=_obj(control['dispatch_json']).get('bot_id'))
            if row['state'] == 'acknowledged':
                return 'confirmed', 'prior_file_acknowledged', None, None
            if row['state'] in {'sending', 'unknown'}:
                return 'unknown', 'prior_file_unresolved', None, None
            if control['revision'] != revision or task['cancel_requested']:
                return 'terminal', 'file_source_superseded', None, None
            payload = _obj(row['payload_json'])
            if row['state'] != 'queued' or (payload.get('attempts', 0) and payload.get('not_sent') is not True):
                return 'unsupported', 'file_retry_evidence_required', None, None
            artifact = payload['artifact']
            if artifact.get('snapshot') != digest:
                raise Unsupported('invalid_immutable_reference')
            mapping = self._mapped(cursor, s, ref, task['requester_user_id'], project=task['project_id'], operation='deliver')
            return self._file_export(mapping, origin, {'task_id': task_id, 'revision': revision, 'sha256': digest},
                payload['filename'], artifact['size'], None, draft=payload.get('draft', False),
                draft_reason=payload.get('draft_reason', ''))
        if origin == 'project':
            row = _row(cursor, 'SELECT * FROM project_file_deliveries WHERE delivery_id=?', (_integer(int(key)),))
            if row is None:
                raise PreviewDenied()
            project = _row(cursor, 'SELECT * FROM projects WHERE project_id=?', (row['project_id'],))
            self._scope(s, project)
            self._route(cursor, s, ref, bot=project['bot_native_user_id'])
            state, reason = self._delivery(cursor, row['delivery_id'], s)
            if state != 'unsent':
                return state, reason, None, None
            if row['upload_started_at'] is not None:
                return 'unknown', 'prior_upload_unresolved', None, None
            operation = _row(cursor, 'SELECT actor_principal_id,state,tool_name FROM project_io WHERE operation_id=?', (row['operation_id'],))
            if (operation is None or operation['state'] != 'completed'
                    or row['blob_sha256'] != row['sha256']
                    or operation['tool_name'] != 'send_file_from_sandbox'):
                return 'unsupported', 'project_file_source_not_settled', None, None
            identities = _rows(cursor, "SELECT native_user_id FROM principal_identities WHERE principal_id=? AND platform='onebot-v11'", (operation['actor_principal_id'],))
            if len(identities) != 1:
                raise Unsupported('source_actor_unavailable')
            mapping = self._mapped(cursor, s, ref, identities[0]['native_user_id'], project=row['project_id'], operation='deliver')
            return self._file_export(mapping, origin, {'project_id': row['project_id'], 'operation_id': row['operation_id'],
                'sha256': _hex(row['sha256'])}, row['filename'], row['byte_size'], None)
        if origin == 'official':
            row = _row(cursor, '''SELECT a.*,d.state AS delivery_state,d.active_plan_id FROM qq_official_file_artifacts a
                JOIN qq_official_file_deliveries d USING(artifact_id) WHERE a.artifact_id=?''', (key,))
            self._scope(s, row, 'source_scope_key')
            if row['endpoint_key'] != s.source.key or row['account_key'] != s.source.account.key:
                raise PreviewDenied()
            self._route(cursor, s, ref, bot=s.source.account.app_id)
            deadline = min(row['pending_deadline_at'], row['retain_until'])
            plan_outcomes = []
            if row['active_plan_id'] is not None:
                deliveries = _rows(cursor, 'SELECT delivery_id FROM deliveries WHERE plan_id=?', (row['active_plan_id'],))
                for delivery in deliveries:
                    plan_outcomes.append(self._delivery(cursor, delivery['delivery_id'], s))
            if row['delivery_state'] in {'upload_unknown', 'publication_unknown', 'delivery_unknown', 'uploading', 'publishing'}:
                return 'unknown', 'prior_file_stage_unresolved', deadline, None
            if _row(cursor, "SELECT 1 FROM qq_official_file_stage_attempts WHERE artifact_id=? AND state IN ('dispatched','unknown') LIMIT 1", (key,)):
                return 'unknown', 'prior_file_stage_unresolved', deadline, None
            if row['delivery_state'] == 'message_accepted':
                plan_outcomes.append(('confirmed', 'prior_message_accepted'))
            state, reason = _aggregate(plan_outcomes)
            if state != 'unsent':
                return state, reason, deadline, None
            if row['bytes_state'] == 'deleted' or now >= deadline or row['delivery_state'] in {'cancelled', 'expired'}:
                return 'terminal', 'file_expired_or_cancelled', deadline, None
            # Parent/source authority is not reconstructed by a preview. Its
            # current trusted route callback must inspect exact artifact source.
            mapping = self._mapped(cursor, s, ref, None, stored_actor=row['requester_actor_key'])
            return self._file_export(mapping, origin, {'artifact_id': key, 'sha256': _hex(row['sha256'])},
                row['filename'], row['byte_size'], deadline, draft=row['draft'], draft_reason=row['draft_reason'])
        raise PreviewDenied()

    @staticmethod
    def _file_export(mapping, origin, immutable_ref, filename, size, deadline, *, draft=False, draft_reason=''):
        filename = _text(filename, 180)
        if '/' in filename or '\\' in filename or not filename.lower().endswith(('.pdf', '.zip')):
            return 'unsupported', 'target_file_type_unavailable', deadline, None
        size = _integer(size)
        if size > 100 * 1024 * 1024:
            return 'unsupported', 'target_file_size_unavailable', deadline, None
        if type(draft) is not bool or (not draft and draft_reason):
            raise Unsupported('invalid_draft_metadata')
        draft_reason = _text(draft_reason, 1000)
        return 'unsent', 'immutable_reference_only', deadline, {'type': 'artifact_reference', 'origin': origin,
            'mapping': mapping, 'immutable_ref': immutable_ref, 'filename': filename,
            'byte_size': size, 'original_deadline': deadline, 'draft': draft, 'draft_reason': draft_reason,
            'bytes_and_source_revalidation_required': True, 'deadline_policy_required': deadline is None}
