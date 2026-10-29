"""Platform selection before importing either transport or reading credentials."""
from dataclasses import dataclass
from collections.abc import Mapping

class PlatformModeError(ValueError):
    pass


def _switch(value, default):
    if value is None:return default
    if type(value) is not str:raise PlatformModeError('invalid_platform_switch')
    value=value.strip().lower()
    if value in {'true','1','yes','on'}:return True
    if value in {'','false','0','no','off'}:return False
    raise PlatformModeError('invalid_platform_switch')


@dataclass(frozen=True)
class PlatformMode:
    onebot: bool = True
    official: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str,str]):
        mode=cls(_switch(env.get('AI_ONEBOT_ENABLED'),True),_switch(env.get('AI_QQ_OFFICIAL_ENABLED'),False))
        if not mode.onebot and not mode.official:raise PlatformModeError('no_platform_enabled')
        return mode
