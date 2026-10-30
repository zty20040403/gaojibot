"""Explicit deployment admission/cost policy, not a per-command approval flow."""
from dataclasses import dataclass

from src.plugins.ai_chat.adapters.qq_official.native_schedules import SchedulePolicy, NativeScheduleDenied


@dataclass(frozen=True)
class NativeScheduleConfiguration:
    enabled: bool
    policy_id: str
    revision: int
    authorization_until: int
    execution_seconds: int = 120
    delivery_seconds: int = 86400
    max_tokens: int = 8192
    daily_tokens: int = 65536
    daily_runs: int = 24

    def __post_init__(self):
        if (type(self.enabled) is not bool or not isinstance(self.policy_id,str) or not 1<=len(self.policy_id)<=128
                or any(type(x) is not int or x<=0 for x in (self.revision,self.authorization_until,self.execution_seconds,
                    self.delivery_seconds,self.max_tokens,self.daily_tokens,self.daily_runs))
                or self.execution_seconds>self.delivery_seconds or not 1024<=self.max_tokens<=200000
                or not self.max_tokens<=self.daily_tokens<=2000000 or self.daily_runs>24):
            raise ValueError('invalid_native_schedule_configuration')


class DeploymentSchedulePolicy:
    def __init__(self,configuration):
        if type(configuration) is not NativeScheduleConfiguration:raise ValueError('native_schedule_configuration_required')
        self.configuration=configuration

    def approve(self,cursor,source,spec,now):
        # The domain already checked authenticated source+actor admission. This
        # is an explicit deployment cost bound, with no OTP/operator round-trip.
        config=self.configuration
        if not config.enabled or now>=config.authorization_until:return None
        requested=spec.get('authorization_until')
        until=config.authorization_until if requested is None else requested
        if type(until) is not int or not now<until<=config.authorization_until:
            raise NativeScheduleDenied('authorization_contract_exceeds_deployment')
        if (spec['kind']=='routine' and (spec['max_tokens']>config.max_tokens
                or spec['daily_tokens']>config.daily_tokens or spec['daily_runs']>config.daily_runs)):
            raise NativeScheduleDenied('schedule_cost_limit')
        return SchedulePolicy(config.policy_id,config.revision,until,config.execution_seconds,config.delivery_seconds)

    def current(self,cursor,source,spec,now):
        config=self.configuration
        return (config.enabled and source['policy_id']==config.policy_id and source['policy_revision']==config.revision
            and now<source['authorization_until']<=config.authorization_until
            and source['execution_seconds']==config.execution_seconds and source['delivery_seconds']==config.delivery_seconds)


def build_native_schedules(*,configuration,database,token,jobs,usage,outbox,admission,clock,profile_for,database_calls=None):
    """Explicit host factory: reuse the existing scheduled shared model adapter.

    No environment lookup and no listener/worker start. The account runtime
    installs the returned domain in the existing reminder/routine dispatcher.
    """
    if configuration is None:return None
    if type(configuration) is not NativeScheduleConfiguration:raise ValueError('native_schedule_configuration_required')
    if not configuration.enabled:return None
    from src.plugins.ai_chat.adapters.qq_official.native_schedules import NativeSchedules
    from src.plugins.ai_chat.adapters.qq_official.scheduled_model import SharedScheduledModel
    domain=NativeSchedules(database=database,token=token,jobs=jobs,usage=usage,outbox=outbox,admission=admission,
        clock=clock,policy=DeploymentSchedulePolicy(configuration),
        **({'database_calls':database_calls} if database_calls is not None else {}))
    domain.run_model=SharedScheduledModel(profile_for,usage_callback=domain.settle_model_usage)
    return domain
