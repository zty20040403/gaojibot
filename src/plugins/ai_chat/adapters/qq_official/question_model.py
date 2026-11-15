"""Target-owned private model composition; no inherited sender/VM/owner context."""
import asyncio
import json
from types import SimpleNamespace

from ...agent_loop import ask_deepseek_with_tools,request_sink
from ...application.turn_context import render_memory,tail_budget
from ...background_tools import BackgroundPlatform
from ...billing_context import active_billing,drain
from ...conversation_scope import ConversationScope
from ...conversation_window import build_tail
from ...model_budget import ModelCallBudget,active_model_budget,model_budget_scope
from ...platform_types import QuestionToolSource
from ...prompt import system_prompt
from ...tools import neutral_registry_for
from ...tools.state import ToolContext,TurnState
from .question_worker import QuestionDecision,QuestionTarget

ANSWER_TOOL = dict(type='function',function=dict(name='ask',description='回答或拒绝本轮私有问答；只准备候选，宿主最终检查后提交，不发送QQ。',
    parameters=dict(type='object',properties=dict(action=dict(type='string',enum=['answer','refuse']),answer=dict(type='string',maxLength=16000)),
        required=['action'],additionalProperties=False)))


class QuestionQuota:
    def __init__(self,model,target):
        self.model,self.target,self.receipts = model,target,[]
        self.budget = ModelCallBudget(1_000_000,max_requests=16,before_request=target.current,reserve_request=self.reserve)

    def _reserve(self,reservation):
        scope = self.target.endpoint.scope.key
        with self.model.runtime.store.transaction() as cursor:
            self.model.source(cursor,self.target); now = self.model.questions.authority.now()
            cursor.execute('SELECT pg_advisory_xact_lock(hashtextextended(?,0))',('official-media-usage:'+scope,))
            quota = self.model.app.usage_store.status(scope,now=now); bound = reservation.reserved
            if (not quota.allowed or quota.input_limit>0 and quota.input_tokens+bound>quota.input_limit
                or quota.output_limit>0 and quota.output_tokens+bound>quota.output_limit):raise PermissionError('question_model_quota_exhausted')
            return cursor.execute('''INSERT INTO usage_events(scope_key,source,provider,model,input_tokens,output_tokens,cached_tokens,occurred_at)
                VALUES (?,'conversation_question_bound',?,?,?,?,0,?) RETURNING usage_id''',
                (scope,reservation.provider,reservation.model,bound,bound,now)).fetchone()['usage_id']

    async def reserve(self,reservation):
        async def commit():self.receipts.append((await self.model.calls(self._reserve,reservation),reservation))
        await drain(asyncio.create_task(commit()))

    def settle(self):
        # Accepted usage remains historical even after original source/epoch loss.
        with self.model.runtime.store.transaction() as cursor:
            for usage_id,reservation in self.receipts:
                if reservation.counts is not None:
                    cursor.execute('''UPDATE usage_events SET source='conversation_question',input_tokens=?,output_tokens=?,cached_tokens=?
                        WHERE usage_id=? AND scope_key=? AND source='conversation_question_bound' ''',
                        (*reservation.counts,usage_id,self.target.endpoint.scope.key))


class QuestionModel:
    def __init__(self,runtime,token,questions):
        self.runtime,self.app,self.token,self.questions = runtime,runtime.application,token,questions
        self.calls,self.llm = runtime._calls,ask_deepseek_with_tools

    def source(self,cursor,target):
        row = self.questions.get(cursor,self.token,target.question_id)
        data = self.questions.authority.target_input(cursor,self.token,row)
        if (row['state']!='running' or (target.endpoint.key,target.scope_key,target.question,target.source_label)
            !=(row['target_endpoint_key'],row['target_scope_key'],data['question'],data['source_label'])):
            raise PermissionError('question_target_snapshot_changed')
        return row

    def context(self,target):
        with self.runtime.store.transaction() as cursor:self.source(cursor,target)
        app,physical = self.app,target.endpoint.scope
        preference = (app.model_preferences.get_scope_default(physical) if target.endpoint.kind=='group' else None) or app.settings.model_profile_for_scope(physical)
        if target.endpoint.kind=='group':
            enabled = app.model_preferences.get_scope_enabled_override(physical)
            if not (enabled if enabled is not None else app.settings.is_scope_enabled(physical)):raise PermissionError('question_target_ai_disabled')
        profile = app.model_catalog.resolve_preference(preference)
        if not profile.configured or not profile.capabilities.tools:raise RuntimeError('question_target_model_unavailable')
        source = QuestionToolSource(target.endpoint,target.question_id,ConversationScope.from_key(target.scope_key))
        ledger,store = app.message_ledger,app.conversations
        tail = build_tail(ledger,store,source.scope,high_tokens=tail_budget(profile)) if ledger is not None and store is not None else ''
        memory = render_memory(store.memory(source.scope.key)) if store is not None else ''
        return source,profile,tail,memory

    async def __call__(self,target):
        billing = active_billing()
        if (not isinstance(target,QuestionTarget) or billing is None or billing.store is not self.app.model_billing
            or billing.source.endpoint!=target.endpoint or billing.source.actor is not None
            or billing.source.purpose!='conversation_question' or billing.source.conversation_question_id!=target.question_id
            or billing.current is not target.current or active_model_budget() is not None or self.app.usage_store is None):
            raise PermissionError('question_host_model_scope_required')
        await target.current()
        source,profile,tail,memory = await self.calls(self.context,target)
        await target.current()
        context = ToolContext(SimpleNamespace(context=self.app),question_source=source,platform=BackgroundPlatform(target.endpoint),capabilities=frozenset({'context'}))
        turn,registry = TurnState(context,model_profile=profile),neutral_registry_for(context)
        definitions = registry.definitions(context)
        if not definitions:raise RuntimeError('question_target_history_unavailable')
        tools,decision = [*definitions,ANSWER_TOOL],None
        async def execute(name,arguments):
            nonlocal decision
            await target.current()
            if type(arguments) is not dict:raise ValueError('question_tool_arguments_invalid')
            if name=='history':
                if set(arguments)-{'query','who','limit','from','to','around'}:raise ValueError('question_tool_arguments_invalid')
                result = await registry.execute(turn,name,arguments)
            elif name=='ask':
                if (set(arguments)-{'action','answer'} or arguments.get('action') not in {'answer','refuse'}
                    or arguments['action']=='refuse' and 'answer' in arguments
                    or arguments['action']=='answer' and (type(arguments.get('answer')) is not str or not 0<len(arguments['answer'].strip())<=16000)):
                    raise ValueError('question_response_invalid')
                proposed = QuestionDecision(refuse=True) if arguments['action']=='refuse' else QuestionDecision(arguments['answer'].strip())
                if decision is not None and decision!=proposed:raise ValueError('question_response_conflict')
                decision = proposed
                result = json.dumps(dict(ok=True,state='prepared',hint='私有候选待宿主最终提交，不是QQ发送。'),ensure_ascii=False)
            else:raise ValueError('question_tool_unavailable')
            await target.current()
            return result
        usage,sink = QuestionQuota(self,target),request_sink.set(None)
        try:
            with model_budget_scope(usage.budget):
                try:
                    await self.llm(target.prompt(),[],tools,execute,current_user='私有跨会话问题（不是真人）',profile=profile,
                        group_context=tail,memory_context=memory,max_tool_rounds=8,max_total_calls=16,max_calls_per_round=4,
                        system_prompt=system_prompt(self.app.settings.system_prompt,'本轮仅history与私有ask回答/拒绝；没有QQ发送、递归ask或任何VM/个人授权工具。不回应也必须ask(action=refuse)，不可只写[silence]或普通文本。',self.app.settings.style_examples))
                    await target.current()
                    if decision is None:raise RuntimeError('question_model_no_response')
                    return decision
                finally:await drain(asyncio.create_task(self.calls(usage.settle)))
        finally:request_sink.reset(sink)
