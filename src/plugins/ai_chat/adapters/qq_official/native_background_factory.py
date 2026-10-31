"""Explicit account-runtime composition; no environment or bot instance."""
from dataclasses import dataclass
import random
from src.plugins.ai_chat.adapters.qq_official.proactive_ingress import ProactiveIngress
from src.plugins.ai_chat.application.shared_proactive import SharedProactiveScorer
from src.plugins.ai_chat.adapters.qq_official.native_background import NativeBackgroundService


@dataclass(frozen=True)
class ProactiveConfiguration:
    enabled: bool = False
    percent: int = 0
    max_checks_per_hour: int = 0
    threshold: int = 95
    max_reply_chars: int = 120

    def __post_init__(self):
        if (type(self.enabled) is not bool or any(type(x) is not int for x in
                (self.percent,self.max_checks_per_hour,self.threshold,self.max_reply_chars))
                or not 0<=self.percent<=100 or not 0<=self.max_checks_per_hour<=120
                or not 0<=self.threshold<=100 or not 1<=self.max_reply_chars<=2000):
            raise ValueError('invalid_proactive_configuration')


def background_factory(*,proactive_configuration=ProactiveConfiguration(),proactive_profile=None):
    async def create(runtime,token):
        app=runtime.application
        proactive=None
        if proactive_configuration.enabled:
            if app is None or app.usage_store is None or not proactive_profile:
                raise ValueError('native_proactive_host_unavailable')
            profile=app.model_catalog.resolve(proactive_profile)
            async def usage(endpoint,actor,trace):
                await runtime._calls(app.usage_store.record,scope_key=endpoint.scope.key,source='proactive-interest',
                    provider=trace.provider,model=trace.model,input_tokens=trace.input_tokens,
                    output_tokens=trace.output_tokens,cached_tokens=trace.cached_tokens,occurred_at=int(runtime.store.clock()))
            from src.plugins.ai_chat.prompt import PERSONA
            from src.plugins.ai_chat.runtime_clock import runtime_clock_prompt
            scorer=SharedProactiveScorer(profile=profile,persona=app.settings.system_prompt or PERSONA,clock_prompt=runtime_clock_prompt,
                threshold=proactive_configuration.threshold,max_reply_chars=proactive_configuration.max_reply_chars,record_usage=usage)
            proactive=ProactiveIngress(token=token,transaction=runtime.store.transaction,admission=runtime.store.admitted,
                outbox=runtime.outbox,scorer=scorer,clock=runtime.store.clock,database_calls=runtime._calls,
                threshold=proactive_configuration.threshold,percent=proactive_configuration.percent,
                max_checks_per_hour=proactive_configuration.max_checks_per_hour,random_value=random.random)
        service=NativeBackgroundService(runtime=runtime,token=token,proactive=proactive)
        service.attach()
        return service
    return create
