from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.bot_security.service import MobileAuthorization


class SubAgentModelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    policy: dict[str, Any]


class SubAgentRevisionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    instruction: str = Field(min_length=1, max_length=12000)
    step_keys: list[str] = Field(min_length=1, max_length=14)
    file_delivery_required: bool | None = None


@dataclass(frozen=True)
class AdminServices:
    version: str
    started_at: int
    delivery_store: Any = None
    usage_store: Any = None
    running_tasks: Any = None
    job_store: Any = None
    job_worker: Any = None
    subagent_store: Any = None
    subagent_coordinator: Any = None
    bridge_router: Any = None
    bridge_state: Any = None
    browser_manager: Any = None
    background_tasks: Any = None
    model_catalog: Any = None
    llm_gateway: Any = None
    local_model: Any = None
    model_preferences: Any = None
    reasoning_preferences: Any = None
    user_profiles: Any = None
    message_ledger: Any = None
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
    alert_store: Any = None
    alert_preferences: Any = None
    fleet_client: Any = None
    mobile_authorization: MobileAuthorization | None = None


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


class AlertNotificationControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
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


class ContextFeedbackRequest(BaseModel):
    verdict: str
    note: str = ""
