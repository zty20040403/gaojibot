"""Private one-attempt question reducer; no QQ/history/VM/model side effects."""
import hashlib
import json
import re
import secrets

from ...storage.jobs import DurableJob
from .keys import account_job_kind
from .lease import assert_epoch
from .turn_platform import ExecutionBinding

TERMINAL = frozenset({'answered','refused','cancelled','expired','unknown'})


class Questions:
    def __init__(self,authority,jobs):self.authority,self.jobs = authority,jobs

    def get(self,cursor,token,question_id):
        assert_epoch(cursor,token)
        if type(question_id) is not str or re.fullmatch('CQ-[0-9a-f]{32}',question_id) is None:raise ValueError('question_reference_invalid')
        row = cursor.execute('SELECT * FROM qq_official_conversation_questions WHERE account_key=? AND question_id=? FOR UPDATE',
            (token.account_key,question_id)).fetchone()
        if row is None:raise PermissionError('question_unavailable')
        return dict(row)

    @staticmethod
    def lane(endpoint_key):return 'qqq:'+hashlib.sha256(endpoint_key.encode()).hexdigest()

    def settle(self,cursor,row,now):
        status = 'succeeded' if row['state'] in {'answered','refused'} else 'failed' if row['state']=='unknown' else 'cancelled'
        cursor.execute('''UPDATE durable_jobs SET status=?,lease_owner='',lease_until=NULL,finished_at=?,updated_at=?,
            result_json=?,last_error=? WHERE job_id=? AND kind=?''',(status,now,now,json.dumps({'question_id':row['question_id']},separators=(',',':')),
                row['reason_code'],row['job_id'],account_job_kind('question',row['account_key'])))

    def close(self,cursor,token,question_id,*,reason='cancelled'):
        row = self.get(cursor,token,question_id)
        if row['state'] in TERMINAL:return row
        if reason not in {'cancelled','source_unavailable','target_unavailable','model_unknown','restart'}:raise ValueError('question_outcome_invalid')
        now = self.authority.now()
        state = 'expired' if now>=row['deadline_at'] else 'unknown' if row['state']=='running' and reason in {'model_unknown','restart'} else 'cancelled'
        if state=='expired':reason = 'expired'
        row = dict(cursor.execute('''UPDATE qq_official_conversation_questions SET state=?,reason_code=?,finished_at=?
            WHERE question_id=? RETURNING *''',(state,reason,now,question_id)).fetchone())
        self.settle(cursor,row,now); return row

    def retire(self,cursor,token,row):
        if row['state'] not in TERMINAL:
            try:self.authority.current(cursor,token,row)
            except PermissionError as error:
                reason = 'target_unavailable' if str(error).startswith(('question_target','question_link','conversation_daily_budget')) else 'source_unavailable'
                return self.close(cursor,token,row['question_id'],reason=reason)
            actual = cursor.execute('SELECT * FROM durable_jobs WHERE job_id=?',(row['job_id'],)).fetchone()
            pending = (actual is not None and row['state']=='queued' and actual['status']=='pending'
                and actual['lease_generation']==0 and actual['attempts']==0)
            if not pending:
                try:
                    if actual is None:raise PermissionError('question_worker_replaced')
                    self.job(cursor,token,self.jobs._row(actual),row)
                except PermissionError:
                    return self.close(cursor,token,row['question_id'],reason='model_unknown' if row['state']=='running' else 'cancelled')
        return row

    def submit(self,cursor,token,binding,reference,question):
        fields = self.authority.capture(cursor,token,binding,reference,question)
        old = cursor.execute('''SELECT * FROM qq_official_conversation_questions WHERE account_key=?
            AND source_execution_id=? AND request_hmac=?''',(token.account_key,fields['source_execution_id'],fields['request_hmac'])).fetchone()
        if old is not None:return self.retire(cursor,token,dict(old)),False
        live = cursor.execute('''SELECT target_endpoint_key,source_execution_id FROM qq_official_conversation_questions
            WHERE account_key=? AND state IN ('queued','running') LIMIT 129''',(token.account_key,)).fetchall()
        if len(live)>=128 or sum(item['target_endpoint_key']==fields['target_endpoint_key'] for item in live)>=16 or sum(item['source_execution_id']==fields['source_execution_id'] for item in live)>=4:
            raise PermissionError('question_queue_full')
        # A dedup/limit lookup may consume the original deadline. Capture again
        # before queue insertion; the intent key is stable, not renewed consent.
        fields = self.authority.capture(cursor,token,binding,reference,question)
        fields['question_id'] = 'CQ-'+secrets.token_hex(16)
        job,_ = self.jobs.enqueue(kind=account_job_kind('question',token.account_key),scope_key=self.lane(fields['target_endpoint_key']),
            payload={'question_id':fields['question_id']},idempotency_key='qq-question:'+fields['question_id'],
            max_attempts=1,resume_on_lease_loss=False,now=fields['created_at'],transaction_cursor=cursor)
        fields['job_id'] = job.job_id
        row = dict(cursor.execute('INSERT INTO qq_official_conversation_questions ('+','.join(fields)+') VALUES ('+
            ','.join('?' for _ in fields)+') RETURNING *',tuple(fields.values())).fetchone())
        return self.retire(cursor,token,row),True

    def job(self,cursor,token,job,row):
        now = self.authority.now()
        actual = cursor.execute('SELECT * FROM durable_jobs WHERE job_id=? FOR UPDATE',(row['job_id'],)).fetchone()
        if (not isinstance(job,DurableJob) or actual is None or actual['status']!='running'
            or (job.job_id,job.kind,job.scope_key,job.payload)!=(row['job_id'],account_job_kind('question',token.account_key),self.lane(row['target_endpoint_key']),{'question_id':row['question_id']})
            or (actual['kind'],actual['scope_key'],json.loads(actual['payload_json']))!=(job.kind,job.scope_key,job.payload)
            or (actual['lease_owner'],actual['lease_generation'])!=(job.lease_owner,job.lease_generation)
            or actual['lease_generation']!=1 or actual['attempts']!=1 or actual['max_attempts']!=1 or actual['resume_on_lease_loss']!=0
            or actual['lease_until'] is None or actual['lease_until']<=now):raise PermissionError('question_worker_replaced')
        return now

    def bound(self,cursor,token,job):
        if not isinstance(job,DurableJob):raise PermissionError('question_worker_replaced')
        row = self.get(cursor,token,job.payload.get('question_id')); self.job(cursor,token,job,row)
        return row

    def claim(self,cursor,token,job):
        row = self.bound(cursor,token,job)
        if row['state']!='queued':return row,False
        row = self.retire(cursor,token,row)
        if row['state'] in TERMINAL:return row,False
        now = self.job(cursor,token,job,row)
        if now>=row['deadline_at']:return self.close(cursor,token,row['question_id']),False
        return dict(cursor.execute("UPDATE qq_official_conversation_questions SET state='running',started_at=? WHERE question_id=? RETURNING *",
            (now,row['question_id'])).fetchone()),True

    def current(self,cursor,token,job):
        row = self.bound(cursor,token,job)
        if row['state']!='running':raise PermissionError('question_not_running')
        row = self.retire(cursor,token,row)
        if row['state'] not in TERMINAL:
            now = self.job(cursor,token,job,row)
            if now>=row['deadline_at']:row = self.close(cursor,token,row['question_id'])
        return row

    def respond(self,cursor,token,job,*,answer=None,refuse=False):
        if type(refuse) is not bool or (refuse and answer is not None) or (not refuse and (type(answer) is not str or not 0<len(answer.strip())<=16000)):
            raise ValueError('question_response_invalid')
        row = self.current(cursor,token,job)
        if row['state'] in TERMINAL:return row
        now = self.job(cursor,token,job,row)
        if now>=row['deadline_at']:return self.close(cursor,token,row['question_id'])
        row = dict(cursor.execute('''UPDATE qq_official_conversation_questions SET state=?,answer=?,reason_code=?,finished_at=?
            WHERE question_id=? RETURNING *''',('refused' if refuse else 'answered','' if refuse else answer.strip(),'declined' if refuse else '',now,row['question_id'])).fetchone())
        self.settle(cursor,row,now); return row

    def consume(self,cursor,token,question_id,binding):
        row = self.get(cursor,token,question_id)
        if row['state'] not in {'answered','refused'} or row['consumed_at'] is not None:return None
        if (not isinstance(binding,ExecutionBinding) or binding.token!=token or (binding.handle.execution_id,binding.handle.endpoint.key,binding.handle.actor.key,
            binding.handle.scope_key,binding.handle.execution_revision,binding.handle.deadline_at)!=(row['source_execution_id'],row['source_endpoint_key'],row['source_actor_key'],
            row['source_scope_key'],row['source_execution_revision'],row['source_deadline_at'])):raise PermissionError('question_source_unavailable')
        binding.current(cursor,self.authority.now())
        try:self.authority.current(cursor,token,row)
        except PermissionError:return None
        now = self.authority.now()
        if now>=row['deadline_at']:return None
        return dict(cursor.execute('UPDATE qq_official_conversation_questions SET consumed_at=? WHERE question_id=? RETURNING *',(now,question_id)).fetchone())

    def recover(self,cursor,token):
        assert_epoch(cursor,token)
        rows = cursor.execute("SELECT question_id FROM qq_official_conversation_questions WHERE account_key=? AND state IN ('queued','running') LIMIT 129",
            (token.account_key,)).fetchall()
        if len(rows)>128:raise RuntimeError('question_live_limit_exceeded')
        for row in rows:self.close(cursor,token,row['question_id'],reason='restart')
        return len(rows)
