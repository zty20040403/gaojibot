from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.bot_security.service import MobileAuthorization






@dataclass(frozen=True)
class AdminServices:
    version: str
    started_at: int
    delivery_store: Any = None
    usage_store: Any = None
    running_tasks: Any = None
    job_store: Any = None
    job_worker: Any = None
    background_jobs: Any = None
    background_tasks: Any = None
    model_catalog: Any = None
    llm_gateway: Any = None
    local_model: Any = None
    model_preferences: Any = None
    reasoning_preferences: Any = None
    user_profiles: Any = None
    message_ledger: Any = None
    conversations: Any = None
    settings: Any = None
    sandbox_manager: Any = None
    sticker_inventory: Any = None
    media_library: Any = None
    source_store: Any = None
    media_cleanup: Any = None
    vision_worker: Any = None
    turn_journal: Any = None
    database: Any = None
    telemetry: Any = None
    mobile_authorization: MobileAuthorization | None = None
    official_file_service: Any = None  # Live host getter; never a request-supplied route.
    official_identity_service: Any = None  # Exact live account host getter.
    official_observability: Any = None  # Live host getter; read-only aggregate diagnostics.
    official_runtime: Any = None  # Live host getter for committed control notifications.


@dataclass(frozen=True)
class AdminMutationContext:
    expected_version: int | None
    actor: str


class ModelSelectionRequest(BaseModel):
    profile: str | None = None


class LocalModelControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["start", "stop"]
    request_id: UUID


class ReasoningEffortRequest(BaseModel):
    effort: str | None = None


class GroupEnabledRequest(BaseModel):
    enabled: bool


class SandboxActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["start", "stop", "destroy"]
    confirmation: str = Field(default="", max_length=32)


class CleanupConfirmationRequest(BaseModel):
    confirmation_token: str


class ToolEnabledRequest(BaseModel):
    enabled: bool


class MediaReviewRequest(BaseModel):
    state: str


class ConversationControlRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    endpoint_key: str = Field(min_length=1,max_length=1000)
    action: Literal['stop','start','kill']


class ConversationProactiveRequest(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    endpoint_key: str = Field(min_length=1,max_length=1000)
    enabled: bool


class ConversationBudgetRequest(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    endpoint_key: str = Field(min_length=1,max_length=1000)
    daily_budget_usd: str | None = Field(max_length=32)


class BrowserClearRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    endpoint_key: str = Field(min_length=1,max_length=1000)
    fence: str = Field(pattern=r'^[a-f0-9]{64}$')
    confirmation: Literal['clear']


class BackgroundCommandCancelRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    endpoint_key: str = Field(min_length=1,max_length=1000)


class AgentTimeLimitRequest(BackgroundCommandCancelRequest):
    hours: int = Field(ge=1,le=6,strict=True)


class ProgramSettingsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    endpoint_key: str = Field(min_length=1,max_length=1000)
    enabled: bool = Field(strict=True)
    say_limit: int = Field(ge=0,le=10000,strict=True)
    wake_limit: int = Field(ge=0,le=1000,strict=True)


class ProgramResumeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    endpoint_key: str = Field(min_length=1,max_length=1000)
    sandbox_id: str = Field(pattern=r'^s[a-f0-9]{6,32}$')
    recovery_fence: str = Field(pattern=r'^[a-f0-9]{64}$')


class ProgramControlRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    endpoint_key: str = Field(min_length=1,max_length=1000)
    program_name: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.@-]{0,79}$')
    enabled: bool = Field(strict=True)


class ImageCaptionSettingsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scope_key: str = Field(min_length=1, max_length=1000)
    enabled: bool = Field(strict=True)
    daily_limit: int = Field(default=200, ge=0, le=10000, strict=True)
