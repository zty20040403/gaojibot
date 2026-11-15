"""A host-bound source wait; no new actor, task deadline or QQ delivery grant."""
import asyncio
from dataclasses import dataclass,field

from ...billing_context import drain
from .lease import LeaseLost
from .question_runtime import QuestionRuntime
from .questions import TERMINAL
from .turn_platform import ExecutionBinding


@dataclass(frozen=True)
class QuestionResult:
    state: str
    reason: str = ''
    question_id: str = ''
    answer: str = field(default='',repr=False)

    def payload(self):
        return dict(ok=self.state=='answered',state=self.state,reason=self.reason,
            task_id=self.question_id,answer=self.answer)


class QuestionAsks:
    def __init__(self,service,binding):
        if (not isinstance(service,QuestionRuntime) or not isinstance(binding,ExecutionBinding)
            or binding.token is not service.token):raise ValueError('question_source_binding_required')
        self.service,self.binding = service,binding

    async def current(self):
        if not self.service.available:raise PermissionError('question_model_unavailable')
        await self.service.runtime._ensure_current(self.service.token)
        if not self.service.available:raise PermissionError('question_model_unavailable')

    def original(self,row):
        handle = self.binding.handle
        if ((row['account_epoch'],row['source_execution_id'],row['source_endpoint_key'],row['source_actor_key'],
            row['source_scope_key'],row['source_execution_revision'],row['source_deadline_at'])!=
            (self.binding.token.epoch,handle.execution_id,handle.endpoint.key,handle.actor.key,
                handle.scope_key,handle.execution_revision,handle.deadline_at)):
            raise PermissionError('question_source_unavailable')

    def response_current(self,question_id):
        service = self.service
        with service.runtime.store.transaction() as cursor:
            row = service.questions.get(cursor,service.token,question_id); self.original(row)
            if row['state'] not in {'answered','refused'} or row['consumed_at'] is None:
                raise PermissionError('question_source_unavailable')
            authority = service.questions.authority
            self.binding.current(cursor,authority.now())
            authority._origin(cursor,service.token,row); authority._target(cursor,service.token,row)
            if not row['created_at']<=authority.now()<row['deadline_at']:raise PermissionError('question_source_expired')

    def poll(self,question_id):
        service,binding = self.service,self.binding
        with service.runtime.store.transaction() as cursor:
            binding.current(cursor,service.questions.authority.now())
            row = service.questions.get(cursor,service.token,question_id)
            self.original(row)
            row = service.questions.retire(cursor,service.token,row)
            if row['state'] in {'answered','refused'}:
                consumed = service.questions.consume(cursor,service.token,question_id,binding)
                if consumed is None:
                    return QuestionResult('unavailable','already_consumed_or_source_closed',question_id)
                return QuestionResult(consumed['state'],consumed['reason_code'],question_id,consumed['answer'])
            if row['state'] in TERMINAL:return QuestionResult(row['state'],row['reason_code'],question_id)
            return None

    def close(self,question_id):
        with self.service.runtime.store.transaction() as cursor:
            row = self.service.questions.get(cursor,self.service.token,question_id); self.original(row)
            return self.service.questions.close(cursor,self.service.token,question_id,reason='source_unavailable')

    @staticmethod
    def rejection(error):
        # Only fixed diagnostics escape. No endpoint, provider URL, SQL or body.
        reason = str(error)
        if reason=='question_link_unavailable':return 'not_linked'
        if reason=='question_target_stopped':return 'target_stopped'
        if reason.startswith('conversation_daily_budget'):return 'target_budget'
        if reason in {'question_queue_full','question_model_unavailable'}:return reason
        if reason=='question_request_invalid':return 'invalid_request'
        return 'source_or_service_unavailable'

    async def ask(self,target,question):
        issued = None
        async def begin():
            nonlocal issued
            row,_ = await self.service.runtime._calls(self.service.submit,self.binding,target,question)
            issued = row['question_id']

        finished,closed = False,None
        failure = 'source_or_service_unavailable'
        try:
            await self.current()
            # Cancellation of a DB await must not lose the committed CQ-ID.
            # Drain this same begin task; its assignment survives double cancel.
            await drain(asyncio.create_task(begin()))
            while True:
                await self.current()
                result = await self.service.runtime._calls(self.poll,issued)
                await self.current()
                if result is not None:
                    if result.state in {'answered','refused'}:
                        await self.service.runtime._calls(self.response_current,issued)
                        await self.current()
                    finished = True
                    return result
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:raise
        except Exception as error:failure = self.rejection(error)
        finally:
            if issued is not None and not finished:
                try:closed = await drain(asyncio.create_task(self.service.runtime._calls(self.close,issued)))
                except LeaseLost:pass  # The next account recovery owns old-epoch closure.
                except asyncio.CancelledError:raise
                except Exception as error:
                    self.service.runtime.logger.warning('Question source settlement unavailable error=%s',type(error).__name__)
        if closed is not None and closed['state']=='expired':return QuestionResult('expired','expired',issued)
        return QuestionResult('unavailable',failure,issued or '')
