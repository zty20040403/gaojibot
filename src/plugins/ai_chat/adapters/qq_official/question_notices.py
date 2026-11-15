"""One target-bot ephemeral public notice per question, settled only by real proactive receipts."""
import hashlib
import hmac
import re
import secrets

from ...message_ir import MessageBody,TextNode
from ...platform_types import ActorKey,EndpointKey
from .identity_store import snapshot
from .lease import assert_epoch
from .questions import Questions
from .renderer import render_plan
from .send_types import OutboxInvariantError,stable_json


def render_notice(endpoint,text):
    if type(text) is not str:raise ValueError('question_notice_text_invalid')
    cleaned = text.strip()
    if not 0<len(cleaned)<=2000:raise ValueError('question_notice_text_invalid')
    plan = render_plan(MessageBody((TextNode(0,cleaned),)),endpoint,markdown=False,max_parts=1)
    if plan.status!='ready' or len(plan.parts)!=1:raise ValueError('question_notice_render_unavailable')
    return plan


class QuestionNotices:
    def __init__(self,questions,outbox):
        if not isinstance(questions,Questions) or outbox is None:raise ValueError('question_notice_dependencies_required')
        self.questions,self.outbox = questions,outbox; self.authority = questions.authority

    def now(self):return self.authority.now()

    def content_hash(self,source,text):
        return self.authority.authority._hash('qq-question-notice:v1',[source['account_key'],source['question_id'],
            source['target_endpoint_key'],source['bot_native_id'],text])

    def _bot_and_route(self,cursor,token,target,row):
        if target.kind!='group' or target.account.key!=token.account_key:raise PermissionError('question_notice_group_required')
        account = cursor.execute('SELECT * FROM qq_official_accounts WHERE account_key=?',(token.account_key,)).fetchone()
        if (account is None or account['fencing_epoch']!=token.epoch or account['holder_id']!=token.holder_id
            or account['lock_backend_pid'] is None or not account['enabled'] or account['gateway_state']=='paused'):
            raise PermissionError('question_notice_account_unavailable')
        route = cursor.execute('SELECT * FROM qq_official_endpoints WHERE account_key=? AND endpoint_key=?',
            (token.account_key,target.key)).fetchone()
        if (route is None or route['kind']!='group' or route['binding_state']!='active'
            or route['membership_state']!='present' or route['proactive_state']!='allowed'
            or (route['canonical_scope_key'],route['generation'],route['route_epoch'])
                !=(row['target_scope_key'],row['target_generation'],row['target_route_epoch'])):
            raise PermissionError('question_notice_proactive_unavailable')
        author = snapshot(cursor,target.account)
        if (author is None or author.source not in {'ready','self_api'} or author.revision<1
            or author.bot_actor.identity_kind!='bot' or not author.bot_actor.matches_endpoint(target)):
            raise PermissionError('question_notice_identity_unavailable')
        return account,route,author

    def get(self,cursor,token,notice_id):
        assert_epoch(cursor,token)
        if type(notice_id) is not str or re.fullmatch('QN-[0-9a-f]{32}',notice_id) is None:raise ValueError('question_notice_reference_invalid')
        source = cursor.execute('SELECT * FROM qq_official_question_notice_sources WHERE account_key=? AND notice_id=?',
            (token.account_key,notice_id)).fetchone()
        notice = cursor.execute('SELECT * FROM qq_official_question_notices WHERE account_key=? AND notice_id=? FOR UPDATE',
            (token.account_key,notice_id)).fetchone()
        if source is None or notice is None:raise PermissionError('question_notice_unavailable')
        return dict(source),dict(notice)

    def source_current(self,cursor,token,source,notice,*,require_pending=False):
        assert_epoch(cursor,token)
        if source['account_key']!=token.account_key or source['account_epoch']!=token.epoch or source['account_holder_id']!=token.holder_id:
            raise PermissionError('question_notice_source_unavailable')
        if notice['state']!='pending' if require_pending else notice['state'] not in {'pending','sending'}:
            raise PermissionError('question_notice_closed')
        now = self.now()
        if not source['created_at']<=now<source['expires_at']:raise PermissionError('question_notice_expired')
        question = self.questions.get(cursor,token,source['question_id'])
        job = cursor.execute('SELECT * FROM durable_jobs WHERE job_id=? FOR UPDATE',(source['job_id'],)).fetchone()
        if job is None:raise PermissionError('question_worker_replaced')
        self.questions.current(cursor,token,self.questions.jobs._row(job))
        question = self.questions.get(cursor,token,source['question_id'])
        if (question['state']!='running'
            or (question['account_epoch'],question['target_endpoint_key'],question['target_scope_key'],question['target_generation'],
                question['target_route_epoch'],question['target_stop_generation'],question['target_budget_generation'])
                !=(source['account_epoch'],source['target_endpoint_key'],source['target_scope_key'],source['target_generation'],
                    source['target_route_epoch'],source['target_stop_generation'],source['target_budget_generation'])
            or (job['lease_owner'],job['lease_generation'],job['attempts'])!=(source['worker_id'],source['lease_generation'],source['attempts'])
            or job['lease_until'] is None or job['lease_until']<=now):
            raise PermissionError('question_notice_source_unavailable')
        target = EndpointKey.parse(source['target_endpoint_key'])
        _,route,author = self._bot_and_route(cursor,token,target,question)
        if (route['capability_revision']!=source['capability_revision']
            or (author.bot_actor.native_user_id,author.source,author.revision)
                !=(source['bot_native_id'],source['bot_identity_source'],source['bot_identity_revision'])
            or notice['bot_actor_key']!=author.bot_actor.key):
            raise PermissionError('question_notice_source_unavailable')
        if not source['created_at']<=self.now()<source['expires_at']:raise PermissionError('question_notice_expired')
        return target,route,author

    def propose(self,cursor,token,job,text):
        row = self.questions.current(cursor,token,job)
        if row['state']!='running':raise PermissionError('question_not_running')
        target = EndpointKey.parse(row['target_endpoint_key'])
        _,route,author = self._bot_and_route(cursor,token,target,row)
        rendered = render_notice(target,text)
        canonical_text = rendered.parts[0].canonical_text
        existing_source = cursor.execute('''SELECT * FROM qq_official_question_notice_sources
            WHERE account_key=? AND question_id=?''',(token.account_key,row['question_id'])).fetchone()
        if existing_source is not None:raise PermissionError('question_notice_already_used')
        now = self.now(); expires_at = min(now+30,row['deadline_at'])
        if not row['started_at']<=now<expires_at<=row['deadline_at']:raise PermissionError('question_notice_expired')
        source_fields = dict(notice_id='QN-'+secrets.token_hex(16),question_id=row['question_id'],account_key=token.account_key,
            account_epoch=token.epoch,account_holder_id=token.holder_id,target_endpoint_key=target.key,target_scope_key=row['target_scope_key'],
            target_generation=row['target_generation'],target_route_epoch=row['target_route_epoch'],target_stop_generation=row['target_stop_generation'],
            target_budget_generation=row['target_budget_generation'],capability_revision=route['capability_revision'],job_id=job.job_id,
            worker_id=job.lease_owner,lease_generation=job.lease_generation,attempts=1,bot_native_id=author.bot_actor.native_user_id,
            bot_identity_source=author.source,bot_identity_revision=author.revision,created_at=now,expires_at=expires_at)
        source_fields['content_hmac'] = self.content_hash(source_fields,canonical_text)
        source = dict(cursor.execute('INSERT INTO qq_official_question_notice_sources ('+','.join(source_fields)+') VALUES ('+
            ','.join('?' for _ in source_fields)+') RETURNING *',tuple(source_fields.values())).fetchone())
        notice_fields = dict(notice_id=source['notice_id'],question_id=source['question_id'],account_key=source['account_key'],
            target_endpoint_key=source['target_endpoint_key'],bot_native_id=source['bot_native_id'],bot_actor_key=author.bot_actor.key,
            state='pending',plan_id=None,delivery_state='pending',receipt_id=None,delivery_receipt_hash=None,delivery_received_at=None,
            created_at=now,updated_at=now)
        notice = dict(cursor.execute('INSERT INTO qq_official_question_notices ('+','.join(notice_fields)+') VALUES ('+
            ','.join('?' for _ in notice_fields)+') RETURNING *',tuple(notice_fields.values())).fetchone())
        return source,notice,rendered

    def validate_plan(self,cursor,*,token,notice_id,rendered,requester,logical_key,source_scope_key,
            source_native_message_id,source_canonical_message_id,turn_id,expires_at,now):
        source,notice = self.get(cursor,token,notice_id)
        try:self.source_current(cursor,token,source,notice,require_pending=True)
        except PermissionError as error:raise OutboxInvariantError(str(error)) from error
        if (rendered.endpoint.key!=source['target_endpoint_key'] or requester.key!=notice['bot_actor_key']
            or requester.identity_kind!='bot' or requester.native_user_id!=source['bot_native_id']
            or logical_key!='question-notice:'+notice_id or source_scope_key!=source['target_scope_key']
            or source_native_message_id!='' or source_canonical_message_id is not None or turn_id is not None
            or not source['created_at']<=now<expires_at<=min(source['expires_at'],now+30)
            or rendered.status!='ready' or len(rendered.parts)!=1):
            raise OutboxInvariantError('question_notice_source_unavailable')
        canonical_text = rendered.parts[0].canonical_text
        if not hmac.compare_digest(source['content_hmac'],self.content_hash(source,canonical_text)):
            raise OutboxInvariantError('question_notice_content_changed')
        if rendered.plan_hash!=render_notice(rendered.endpoint,canonical_text).plan_hash:
            raise OutboxInvariantError('question_notice_content_changed')

    def bind_plan(self,cursor,token,notice_id,plan_id):
        source,notice = self.get(cursor,token,notice_id)
        try:self.source_current(cursor,token,source,notice,require_pending=notice['plan_id'] is None)
        except PermissionError as error:raise OutboxInvariantError(str(error)) from error
        plan = cursor.execute('SELECT * FROM delivery_plans WHERE plan_id=?',(plan_id,)).fetchone()
        deliveries = cursor.execute('SELECT * FROM deliveries WHERE plan_id=?',(plan_id,)).fetchall()
        if (plan is None or notice['plan_id'] not in (None,plan_id) or plan['question_notice_id']!=notice_id
            or plan['purpose']!='question_notice' or plan['payload_mode']!='ephemeral'
            or (plan['account_key'],plan['endpoint_key'],plan['requester_actor_key'],plan['source_scope_key'],
                plan['endpoint_generation'],plan['route_epoch'],plan['capability_revision'])
                !=(source['account_key'],source['target_endpoint_key'],notice['bot_actor_key'],source['target_scope_key'],
                    source['target_generation'],source['target_route_epoch'],source['capability_revision'])
            or plan['expires_at']>source['expires_at'] or len(deliveries)!=1 or deliveries[0]['reply_to_native_message_id']!=''):
            raise OutboxInvariantError('question_notice_plan_mismatch')
        if notice['plan_id'] is None:
            now = max(self.now(),notice['updated_at'])
            if now>=source['expires_at']:raise OutboxInvariantError('question_notice_expired')
            cursor.execute("UPDATE qq_official_question_notices SET plan_id=?,state='sending',updated_at=? WHERE notice_id=?",
                (plan_id,now,notice_id))

    def delivery_current(self,cursor,token,delivery):
        if delivery['purpose']!='question_notice' or delivery['payload_mode']!='ephemeral':return False
        plan = cursor.execute('SELECT question_notice_id FROM delivery_plans WHERE plan_id=?',(delivery['plan_id'],)).fetchone()
        if plan is None or plan['question_notice_id'] is None:return False
        try:
            source,notice = self.get(cursor,token,plan['question_notice_id'])
            content = self.outbox._ephemeral.get(delivery['delivery_id'])
            if (content is None or len(content.parts)!=1 or content.endpoint.key!=source['target_endpoint_key']
                or not hmac.compare_digest(source['content_hmac'],self.content_hash(source,content.parts[0].canonical_text))
                or content.plan_hash!=render_notice(content.endpoint,content.parts[0].canonical_text).plan_hash):
                return False
            self.source_current(cursor,token,source,notice)
            return ((delivery['account_key'],delivery['endpoint_key'],delivery['endpoint_generation'],delivery['route_epoch'],
                delivery['requester_actor_key'],delivery['reply_to_native_message_id'])
                ==(source['account_key'],source['target_endpoint_key'],source['target_generation'],source['target_route_epoch'],
                    notice['bot_actor_key'],''))
        except (PermissionError,ValueError):return False

    def prepare(self,cursor,token,job,text):
        source,notice,rendered = self.propose(cursor,token,job,text)
        ref = self.outbox.enqueue(token,rendered=rendered,requester=ActorKey.parse(notice['bot_actor_key']),
            logical_key='question-notice:'+source['notice_id'],source_scope_key=source['target_scope_key'],
            purpose='question_notice',question_notice_id=source['notice_id'],expires_at=source['expires_at'],
            source_native_message_id='',transaction_cursor=cursor)
        _,notice = self.get(cursor,token,source['notice_id'])
        return source,notice,ref

    def close(self,cursor,token,notice_id,*,reason='cancelled'):
        source,notice = self.get(cursor,token,notice_id)
        if notice['state'] not in {'pending','sending'}:return notice
        if reason not in {'cancelled','unknown','restart'}:raise ValueError('question_notice_outcome_invalid')
        now = max(self.now(),notice['updated_at'])
        state = 'expired' if now>=source['expires_at'] else ('unknown' if notice['state']=='sending' and reason in {'unknown','restart'} else 'cancelled')
        dstate = notice['delivery_state']
        if dstate=='pending':dstate = 'unknown' if notice['state']=='sending' else 'cancelled'
        return dict(cursor.execute('UPDATE qq_official_question_notices SET state=?,delivery_state=?,updated_at=? WHERE notice_id=? RETURNING *',
            (state,dstate,now,notice_id)).fetchone())

    def settled(self,cursor,token,plan_id):
        assert_epoch(cursor,token)
        notice = cursor.execute('SELECT * FROM qq_official_question_notices WHERE account_key=? AND plan_id=? FOR UPDATE',
            (token.account_key,plan_id)).fetchone()
        if notice is None:return None
        source = cursor.execute('SELECT * FROM qq_official_question_notice_sources WHERE account_key=? AND notice_id=?',
            (token.account_key,notice['notice_id'])).fetchone()
        delivery = cursor.execute('SELECT * FROM deliveries WHERE plan_id=? FOR UPDATE',(plan_id,)).fetchone()
        if delivery is None:return self.close(cursor,token,notice['notice_id'],reason='cancelled')
        receipt = cursor.execute('''SELECT r.receipt_id,r.attempt_id,r.native_message_id,r.observed_at
            FROM delivery_receipts r JOIN delivery_attempts a ON a.attempt_id=r.attempt_id
            WHERE a.delivery_id=? AND r.account_key=? AND r.endpoint_key=? AND r.kind='response'
              AND r.native_message_id=? AND r.applied_at IS NOT NULL AND a.send_mode='proactive'
              AND a.bot_actor_key=? AND a.bot_identity_source=? AND a.bot_identity_revision=?
            ORDER BY r.observed_at,r.receipt_id LIMIT 1''',
            (delivery['delivery_id'],token.account_key,source['target_endpoint_key'],delivery['native_message_id'],
             notice['bot_actor_key'],source['bot_identity_source'],source['bot_identity_revision'])).fetchone()
        if (delivery['status']=='committed' and delivery['reply_to_native_message_id']==''
            and receipt is not None and receipt['observed_at']>=source['created_at']):
            if notice['receipt_id'] is not None:return dict(notice)
            now = max(self.now(),receipt['observed_at'],notice['updated_at'])
            rhash = hashlib.sha256(stable_json(dict(v=1,receipt_id=receipt['receipt_id'],attempt_id=receipt['attempt_id'],
                native_message_id=receipt['native_message_id'],observed_at=receipt['observed_at'])).encode()).hexdigest()
            new_state = 'committed' if notice['state'] in {'pending','sending'} else notice['state']
            return dict(cursor.execute('''UPDATE qq_official_question_notices SET state=?,delivery_state='committed',
                receipt_id=?,delivery_receipt_hash=?,delivery_received_at=?,updated_at=? WHERE notice_id=? RETURNING *''',
                (new_state,receipt['receipt_id'],rhash,receipt['observed_at'],now,notice['notice_id'])).fetchone())
        if delivery['status'] in {'failed','cancelled','ambiguous'} and notice['receipt_id'] is None:
            dstate = 'unknown' if delivery['status']=='ambiguous' else delivery['status']
            now = max(self.now(),notice['updated_at'])
            if notice['state'] in {'pending','sending'}:
                nstate = 'expired' if now>=source['expires_at'] else dstate
                return dict(cursor.execute('''UPDATE qq_official_question_notices SET state=?,delivery_state=?,updated_at=?
                    WHERE notice_id=? RETURNING *''',(nstate,dstate,now,notice['notice_id'])).fetchone())
            if notice['delivery_state']=='pending':
                return dict(cursor.execute('''UPDATE qq_official_question_notices SET delivery_state=?,updated_at=?
                    WHERE notice_id=? RETURNING *''',(dstate,now,notice['notice_id'])).fetchone())
        return dict(notice)

    def recover(self,cursor,token):
        assert_epoch(cursor,token)
        rows = cursor.execute('''SELECT notice_id FROM qq_official_question_notices
            WHERE account_key=? AND state IN ('pending','sending') LIMIT 129''',(token.account_key,)).fetchall()
        if len(rows)>128:raise RuntimeError('question_notice_live_limit_exceeded')
        for row in rows:self.close(cursor,token,row['notice_id'],reason='restart')
        return len(rows)
