"""Per-test handler wiring without opening databases or starting workers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from src.plugins.ai_chat.alert_notifier import AlertNotificationPreferences
from src.plugins.ai_chat.bridges import MirrorRouter
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.identity import GroupUserProfileStore
from src.plugins.ai_chat.lifecycle import BackgroundTaskSupervisor
from src.plugins.ai_chat.llm_gateway import LLMGateway
from src.plugins.ai_chat.long_term_memory import LongTermMemoryStore
from src.plugins.ai_chat.memory import ConversationMemory, GroupContextMemory
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.model_preferences import ModelPreferenceStore
from src.plugins.ai_chat.ocr import RecentImageStore
from src.plugins.ai_chat.onebot_runtime import assemble_handlers
from src.plugins.ai_chat.runtime import AppContext
from src.plugins.ai_chat.sandbox import DockerSandboxManager
from src.plugins.ai_chat.self_source import SelfSource
from src.plugins.ai_chat.skills import SkillRegistry
from src.plugins.ai_chat.tasks import RunningTaskRegistry
from src.plugins.ai_chat.video import RecentVideoStore
from src.plugins.ai_chat.voice import RecentVoiceStore


class HandlerTestCaseMixin:
    def setUp(self):
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        logger = Mock()
        self.app_context = AppContext(
            settings=settings,
            state_dir=root,
            project_root=root,
            started_at=1,
            logger=logger,
            background_tasks=BackgroundTaskSupervisor(logger),
            database=None,
            memory=ConversationMemory(settings.max_context_turns),
            group_context=GroupContextMemory(
                settings.group_context_messages, settings.group_context_chars
            ),
            long_term_memory=LongTermMemoryStore(None),
            running_tasks=RunningTaskRegistry(),
            user_profiles=GroupUserProfileStore(None),
            model_preferences=ModelPreferenceStore(None),
            reasoning_preferences=ModelPreferenceStore(None),
            alert_preferences=AlertNotificationPreferences(None),
            model_catalog=ModelCatalog.from_settings(settings),
            llm_gateway=LLMGateway(),
            self_source=SelfSource(root),
            skill_registry=SkillRegistry(root / "skills"),
            recent_images=RecentImageStore(300),
            recent_voices=RecentVoiceStore(300),
            recent_videos=RecentVideoStore(300),
            sandbox_manager=DockerSandboxManager(image="test-only:never-started"),
            bridge_router=MirrorRouter(),
        )
        self.handlers = assemble_handlers(self.app_context)
        if hasattr(self, "addAsyncCleanup"):
            self.addAsyncCleanup(self.app_context.shutdown)
        else:
            self.addCleanup(lambda: asyncio.run(self.app_context.shutdown()))
