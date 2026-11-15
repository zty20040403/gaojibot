"""Account-owned private question lifecycle; source ask is a separate boundary."""
import asyncio

from .lease import LeaseLost
from .question_authority import QuestionAuthority
from .question_model import QuestionModel
from .question_notices import QuestionNotices
from .question_worker import QuestionWorker
from .questions import Questions


class QuestionRuntime:
    def __init__(self,runtime,token,ingress):
        self.runtime,self.token = runtime,token
        self.questions = Questions(QuestionAuthority(ingress,admission=runtime.store.admitted),runtime.jobs)
        self.notices = QuestionNotices(self.questions,runtime.outbox) if getattr(runtime,'outbox',None) is not None else None
        self.worker,self.model = None,None
        self.closing,self.next_sweep = False,0.0

    @property
    def available(self):return not self.closing and self.worker is not None

    def _recover(self):
        with self.runtime.store.transaction() as cursor:
            closed = self.questions.recover(cursor,self.token)
            if self.notices is not None:self.notices.recover(cursor,self.token)
            return closed

    async def attach(self):
        # Even an account now running without AI must retire its old questions.
        # Recovery precedes workers and inbound frames; no old RAM/epoch resumes.
        if self.notices is not None:self.runtime.outbox.question_notice_authority = self.notices
        await self.runtime._calls(self._recover)
        app = self.runtime.application
        if app is None:return False
        if (any(getattr(app,name,None) is None for name in ('model_billing','usage_store','model_catalog','model_preferences','message_ledger','conversations'))
            or app.model_billing.database!=self.runtime.database):raise RuntimeError('question_model_or_accounting_unavailable')
        self.model = QuestionModel(self.runtime,self.token,self.questions,notices=self.notices)
        self.worker = QuestionWorker(self.runtime,self.token,self.questions,self.model,notices=self.notices,
            ensure_current=lambda:self.runtime._ensure_current(self.token))
        return True

    def _references(self,binding):
        from ... import daily_budgets
        from ...platform_types import EndpointKey
        from .conversation_control import permitted
        authority = self.questions.authority
        with self.runtime.store.transaction() as cursor:
            row = binding.current(cursor,authority.now()); handle = binding.handle
            expected = dict(account_key=self.token.account_key,endpoint_key=handle.endpoint.key,requester_actor_key=handle.actor.key,
                scope_key=handle.scope_key,route_epoch=handle.route_epoch,endpoint_generation=handle.endpoint_generation,
                account_fencing_epoch=self.token.epoch,execution_revision=handle.execution_revision,deadline_at=handle.deadline_at,
                canonical_message_id=handle.canonical_message_id,source_native_message_id=handle.source_native_message_id)
            route = cursor.execute('SELECT * FROM qq_official_endpoints WHERE account_key=? AND endpoint_key=?',
                (self.token.account_key,handle.endpoint.key)).fetchone()
            if (row['state']!='executing' or row['execution_kind']!='chat' or any(row[key]!=value for key,value in expected.items())
                or not route or (route['canonical_scope_key'],route['route_epoch'],route['generation'])!=
                    (handle.scope_key,handle.route_epoch,handle.endpoint_generation)):
                raise PermissionError('question_source_unavailable')
            rows = cursor.execute('''SELECT * FROM qq_official_conversation_links WHERE account_key=? AND source_endpoint_key=?
                AND state='active' ORDER BY created_at DESC,link_id DESC LIMIT 33''',(self.token.account_key,handle.endpoint.key)).fetchall()
            if len(rows)>32:raise PermissionError('question_directory_limit_exceeded')
            entries = []
            for edge in rows:
                if not authority.ingress.management.current(cursor,self.token,edge):continue
                target = EndpointKey.parse(edge['target_endpoint_key'])
                if not permitted(cursor,target):continue
                try:daily_budgets.require(cursor,target,now=authority.now())
                except PermissionError:continue
                view = authority.ingress.management.view(edge,handle.endpoint)
                entries.append(dict(target=view.link_id,peer=view.peer))
            binding.current(cursor,authority.now())
            if authority.now()>=handle.deadline_at:raise PermissionError('question_source_expired')
            return tuple(entries)

    async def frontend(self,inbound,platform):
        from ...platform_types import InboundMessage
        from . import turn_source
        from .question_asks import QuestionAsks
        from .turn_platform import ExecutionBinding,OfficialTurnPlatform
        if (not isinstance(inbound,InboundMessage) or not isinstance(platform,OfficialTurnPlatform)
            or platform.inbound!=inbound or not isinstance(platform.execution,ExecutionBinding)
            or platform.execution.token is not self.token or platform.execution.api is not turn_source
            or platform.execution.handle.execution_kind!='chat' or platform.execution.handle.result_purpose!='final'
            or (inbound.endpoint,inbound.actor,inbound.scope.key,inbound.route_epoch,inbound.native_message_id)!=
                (platform.execution.handle.endpoint,platform.execution.handle.actor,platform.execution.handle.scope_key,
                    platform.execution.handle.route_epoch,platform.execution.handle.source_native_message_id)):
            raise PermissionError('question_source_binding_required')
        session = QuestionAsks(self,platform.execution)
        await session.current()
        session.references = await self.runtime._calls(self._references,session.binding)
        await session.current()
        return session

    def submit(self,binding,reference,question):
        if not self.available:raise RuntimeError('question_model_unavailable')
        with self.runtime.store.transaction() as cursor:return self.questions.submit(cursor,self.token,binding,reference,question)

    def _sweep(self):
        with self.runtime.store.transaction() as cursor:
            self.runtime.store.epoch_guard(cursor,self.token)
            rows = cursor.execute("SELECT * FROM qq_official_conversation_questions WHERE account_key=? AND state IN ('queued','running') LIMIT 129 FOR UPDATE",
                (self.token.account_key,)).fetchall()
            if len(rows)>128:raise RuntimeError('question_live_limit_exceeded')
            for row in rows:self.questions.retire(cursor,self.token,dict(row))
            if self.notices is not None:
                notices = cursor.execute("SELECT * FROM qq_official_question_notices WHERE account_key=? AND state IN ('pending','sending') LIMIT 129 FOR UPDATE",
                    (self.token.account_key,)).fetchall()
                if len(notices)>128:raise RuntimeError('question_notice_live_limit_exceeded')
                for item in notices:
                    source,notice = self.notices.get(cursor,self.token,item['notice_id'])
                    try:self.notices.source_current(cursor,self.token,source,notice)
                    except PermissionError:self.notices.close(cursor,self.token,item['notice_id'],reason='cancelled')

    async def maintain(self):
        if self.closing or asyncio.get_running_loop().time()<self.next_sweep:return
        await self.runtime._ensure_current(self.token); await self.runtime._calls(self._sweep)
        self.next_sweep = asyncio.get_running_loop().time()+5

    async def run(self):
        if not self.available:raise RuntimeError('question_model_unavailable')
        await self.worker.worker.run_forever()

    async def close(self):
        self.closing = True
        # _serve drains/cancels the account's worker/model/socket/billing tasks
        # first. Then close pending leftovers without rewriting finished facts.
        try:await self.runtime._calls(self._recover)
        except LeaseLost:pass
        finally:
            if getattr(self.runtime,'outbox',None) is not None and self.runtime.outbox.question_notice_authority is self.notices:
                self.runtime.outbox.question_notice_authority = None
            if self.runtime.question_service is self:self.runtime.question_service = None
