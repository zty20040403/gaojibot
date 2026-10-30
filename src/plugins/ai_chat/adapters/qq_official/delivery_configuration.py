"""Trusted startup delivery limits; never loaded from a chat or model tool."""
from dataclasses import dataclass, fields
import re

from .rate_policy import RateCaps
from .turn_platform import TurnDeliveryPolicy, PlatformBoundaryError


def _integer(env, name, default, *, optional=False):
    value = env.get('AI_QQ_OFFICIAL_'+name, '').strip()
    if not value:
        return None if optional else default
    if not re.fullmatch(r'[0-9]{1,9}', value):
        raise ValueError('invalid_official_delivery_configuration')
    return int(value)


def _boolean(env, name, default):
    value = env.get('AI_QQ_OFFICIAL_'+name, str(default)).strip().lower()
    if value in {'true', '1', 'yes', 'on'}:
        return True
    if value in {'false', '0', 'no', 'off', ''}:
        return False
    raise ValueError('invalid_official_delivery_configuration')


@dataclass(frozen=True)
class DeliveryConfiguration:
    markdown: bool = True
    max_text_chars: int = 2000
    max_text_parts: int = 4
    max_text_bytes: int | None = None
    defer_max_hours: int = 24
    allow_newer_windows: bool = True
    certification: str = 'unknown'
    rate_caps: RateCaps = RateCaps()
    project_grant_ttl_seconds: int = 86400
    file_link_ttl_seconds: int = 3600

    def __post_init__(self):
        if (self.certification not in {'unknown', 'unverified', 'certified'}
                or type(self.defer_max_hours) is not int or not 1 <= self.defer_max_hours <= 24
                or type(self.project_grant_ttl_seconds) is not int
                or not 60 <= self.project_grant_ttl_seconds <= 86400
                or type(self.file_link_ttl_seconds) is not int or not 60 <= self.file_link_ttl_seconds <= 3600
                or type(self.rate_caps) is not RateCaps):
            raise ValueError('invalid_official_delivery_configuration')
        try:
            self.turn_policy()
        except PlatformBoundaryError:
            raise ValueError('invalid_official_delivery_configuration') from None

    @classmethod
    def from_env(cls, env):
        return cls(
            markdown=_boolean(env, 'MARKDOWN', True),
            max_text_chars=_integer(env, 'MAX_TEXT_CHARS', 2000),
            max_text_parts=_integer(env, 'MAX_TEXT_PARTS', 4),
            max_text_bytes=_integer(env, 'MAX_TEXT_BYTES', None, optional=True),
            defer_max_hours=_integer(env, 'DEFER_MAX_HOURS', 24),
            allow_newer_windows=_boolean(env, 'ALLOW_NEWER_WINDOWS', True),
            certification=env.get('AI_QQ_OFFICIAL_CERTIFICATION', 'unknown').strip(),
            project_grant_ttl_seconds=_integer(env, 'PROJECT_GRANT_TTL_SECONDS', 86400),
            file_link_ttl_seconds=_integer(env, 'FILE_LINK_TTL_SECONDS', 3600),
            rate_caps=RateCaps(**{field.name: _integer(env, 'RATE_'+field.name.upper(), None, optional=True)
                                 for field in fields(RateCaps)}),
        )

    @property
    def max_wait_seconds(self):
        return self.defer_max_hours * 3600

    def turn_policy(self):
        return TurnDeliveryPolicy(markdown=self.markdown, max_chars=self.max_text_chars,
            max_parts=self.max_text_parts, max_bytes=self.max_text_bytes,
            allow_newer_windows=self.allow_newer_windows)
