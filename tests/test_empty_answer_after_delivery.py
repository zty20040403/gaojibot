"""Empty final text is allowed when a tool already delivered content in the turn."""
from __future__ import annotations

from dataclasses import replace
import json
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.media_refs import MediaDenied
from src.plugins.ai_chat.adapters.qq_official.media_tools import media_registry
from src.plugins.ai_chat.adapters.qq_official.progress import ProgressEmitter
from src.plugins.ai_chat.application import ChatFailure
from src.plugins.ai_chat.application.owned_sandbox import OwnedSandboxExecutor
from src.plugins.ai_chat.application.platform_chat import PlatformChatService
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile
from src.plugins.ai_chat.platform_types import PlatformFeatures
from src.plugins.ai_chat.tools import neutral_registry_for
from src.plugins.ai_chat.tools.owned_sandbox import HANDLERS as SANDBOX_HANDLERS
from src.plugins.ai_chat.tools.registry import ToolRegistry
from src.plugins.ai_chat.tools.state import ToolContext
from tests.test_platform_types import inbound as make_inbound


class EmptyAnswerAfterDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.calls = DatabaseCalls()
        self.addAsyncCleanup(self.calls.close)
        self.inbound = make_inbound()
        self.profile = ModelProfile(
            name="TEST_MODEL",
            provider="test",
            protocol="openai-chat",
            model="TEST_MODEL",
            base_url="http://127.0.0.1",
            api_key_required=False,
        )
        self.app = SimpleNamespace(
            logger=logging.getLogger(__name__),
            db_call=self.calls,
            model_catalog=ModelCatalog({"TEST_MODEL": self.profile}, default_profile="TEST_MODEL"),
            settings=replace(settings, search_enabled=False),
            message_ledger=None,
            conversations=None,
            skill_registry=SimpleNamespace(prompt_index=lambda: "", get=lambda _: None),
            model_preferences=SimpleNamespace(get_explicit=lambda _: None, get_scope_default=lambda _: None),
            reasoning_preferences=SimpleNamespace(get_explicit=lambda _: None, get_scope_member_default=lambda _: None),
            media_library=None,
        )

    def _service(self, *, llm, media_session=None, generation_factory=None, registry_factory=None) -> PlatformChatService:
        async def _media_factory(_inbound, _platform):
            return media_session

        return PlatformChatService(
            self.app,
            scope_enabled=lambda _: True,
            registry_factory=registry_factory or neutral_registry_for,
            llm=llm,
            media_factory=_media_factory if media_session is not None else None,
            generation_factory=generation_factory,
        )

    def _platform(self, *, progress=None) -> SimpleNamespace:
        return SimpleNamespace(
            account=self.inbound.endpoint.account,
            features=PlatformFeatures(),
            progress=progress,
            progress_enabled=progress is not None,
        )

    async def test_no_delivery_empty_answer_raises_empty_response(self) -> None:
        async def llm(*_args, **kwargs) -> str:
            self.assertEqual((kwargs['max_tool_rounds'], kwargs['foreground_quiet_calls']), (40, 12))
            return ""

        service = self._service(llm=llm)
        with self.assertRaises(ChatFailure) as raised:
            await service.ask(
                self.inbound,
                self._platform(),
                "hello",
                selected_profile_override=self.profile,
            )
        self.assertEqual(raised.exception.code, "empty_response")

    async def test_reply_with_voice_success_allows_empty_answer(self) -> None:
        media_session = SimpleNamespace(
            frontend_vision=False,
            service=SimpleNamespace(
                image_fetcher=SimpleNamespace(max_bytes=1024),
                config=SimpleNamespace(max_download_bytes=1024),
                asr=None,
            ),
            enabled=lambda name: name == "reply_with_voice",
            input_images=AsyncMock(return_value=((), "")),
            current=AsyncMock(return_value=None),
            finish=lambda answer: answer,
            reply_with_voice=AsyncMock(
                return_value={"state": "committed", "message_accepted": True, "artifact_id": "art-1"}
            ),
        )

        async def llm(user_text, tools, execute_tool, **_kwargs) -> str:
            res = json.loads(await execute_tool("reply_with_voice", {"text": "你好"}))
            self.assertTrue(res["ok"])
            return ""

        service = self._service(
            llm=llm,
            media_session=media_session,
            registry_factory=lambda ctx: media_registry(neutral_registry_for(ctx), ctx),
        )
        reply = await service.ask(
            self.inbound,
            self._platform(),
            "发个语音",
            selected_profile_override=self.profile,
        )
        self.assertEqual(reply, "[silence]")
        media_session.reply_with_voice.return_value = {"state": "unknown", "artifact_id": "art-1"}
        with self.assertRaises(ChatFailure) as raised:
            await service.ask(self.inbound, self._platform(), "发个语音", selected_profile_override=self.profile)
        self.assertEqual(raised.exception.code, "empty_response")

    async def test_reply_with_voice_failure_still_raises_empty_response(self) -> None:
        media_session = SimpleNamespace(
            frontend_vision=False,
            service=SimpleNamespace(
                image_fetcher=SimpleNamespace(max_bytes=1024),
                config=SimpleNamespace(max_download_bytes=1024),
                asr=None,
            ),
            enabled=lambda name: name == "reply_with_voice",
            input_images=AsyncMock(return_value=((), "")),
            current=AsyncMock(return_value=None),
            finish=lambda answer: answer,
            reply_with_voice=AsyncMock(side_effect=MediaDenied("voice_failed")),
        )

        async def llm(user_text, tools, execute_tool, **_kwargs) -> str:
            res = json.loads(await execute_tool("reply_with_voice", {"text": "你好"}))
            self.assertFalse(res["ok"])
            return ""

        service = self._service(
            llm=llm,
            media_session=media_session,
            registry_factory=lambda ctx: media_registry(neutral_registry_for(ctx), ctx),
        )
        with self.assertRaises(ChatFailure) as raised:
            await service.ask(
                self.inbound,
                self._platform(),
                "发个语音",
                selected_profile_override=self.profile,
            )
        self.assertEqual(raised.exception.code, "empty_response")

    async def test_say_allows_empty_answer(self) -> None:
        class _FakeProgress(ProgressEmitter):
            def __init__(self) -> None:
                pass

            async def say(self, text):
                return {"ok": True, "status": "enqueued"}

        async def llm_say(_user_text, _tools, execute_tool, **_kwargs) -> str:
            res = json.loads(await execute_tool("say", {"text": "正在发"}))
            self.assertTrue(res["ok"])
            return ""

        service = self._service(llm=llm_say)
        reply = await service.ask(
            self.inbound,
            self._platform(progress=_FakeProgress()),
            "说话",
            selected_profile_override=self.profile,
        )
        self.assertEqual(reply, "[silence]")

    async def test_send_sticker_success_allows_empty_answer(self) -> None:
        media_session = SimpleNamespace(
            frontend_vision=False,
            service=SimpleNamespace(
                image_fetcher=SimpleNamespace(max_bytes=1024),
                config=SimpleNamespace(max_download_bytes=1024),
                asr=None,
            ),
            enabled=lambda name: name == "send_sticker",
            input_images=AsyncMock(return_value=((), "")),
            current=AsyncMock(return_value=None),
            finish=lambda answer: answer,
            sticker=AsyncMock(return_value={"state": "stored", "artifact_id": "art-s1"}),
        )
        self.app.media_library = SimpleNamespace(
            search_stickers=AsyncMock(
                return_value=[SimpleNamespace(handle="media#1", mime_type="image/png")]
            ),
            mark_sent=Mock(),
        )

        async def llm(_user_text, _tools, execute_tool, **_kwargs) -> str:
            res = json.loads(await execute_tool("send_sticker", {"query": "笑死"}))
            self.assertTrue(res["ok"])
            return ""

        service = self._service(
            llm=llm,
            media_session=media_session,
            registry_factory=lambda ctx: media_registry(neutral_registry_for(ctx), ctx),
        )
        reply = await service.ask(
            self.inbound,
            self._platform(),
            "发个表情包",
            selected_profile_override=self.profile,
        )
        self.assertEqual(reply, "[silence]")
        self.app.media_library.mark_sent.assert_called_once_with(1)
        media_session.sticker.return_value = {"state": "failed", "artifact_id": "art-s1"}
        with self.assertRaises(ChatFailure) as raised:
            await service.ask(self.inbound, self._platform(), "发个表情包", selected_profile_override=self.profile)
        self.assertEqual(raised.exception.code, "empty_response")

    async def test_delivered_progress_preserves_prepared_media_content(self) -> None:
        class FakeProgress(ProgressEmitter):
            def __init__(self):
                pass

            async def say(self, text):
                return {"ok": True, "status": "enqueued"}

        media_session = SimpleNamespace(
            frontend_vision=False,
            service=SimpleNamespace(image_fetcher=SimpleNamespace(max_bytes=1024),
                                    config=SimpleNamespace(max_download_bytes=1024), asr=None),
            enabled=lambda _name: False,
            input_images=AsyncMock(return_value=((), "")), current=AsyncMock(),
            finish=lambda answer: f"{answer}\n\n待发送的转发摘要",
        )

        async def llm(_user_text, _tools, execute_tool, **_kwargs):
            await execute_tool("say", {"text": "正在处理"})
            return ""

        reply = await self._service(llm=llm, media_session=media_session).ask(
            self.inbound, self._platform(progress=FakeProgress()), "请转发",
            selected_profile_override=self.profile,
        )
        self.assertEqual(reply.strip(), "待发送的转发摘要")
        media_session.current.assert_awaited()

    async def test_forward_registered_file_allows_silence_but_failed_file_does_not(self) -> None:
        media = SimpleNamespace(
            frontend_vision=False,
            service=SimpleNamespace(image_fetcher=SimpleNamespace(max_bytes=1024),
                                    config=SimpleNamespace(max_download_bytes=1024), asr=None),
            enabled=lambda name: name == 'send_forward_summary',
            input_images=AsyncMock(return_value=((), '')), current=AsyncMock(), finish=lambda answer: answer,
            forward=AsyncMock(return_value={'state': 'stored', 'artifact_id': 'art-forward'}),
        )
        async def llm(_user_text, _tools, execute_tool, **_kwargs):
            result = json.loads(await execute_tool('send_forward_summary', {}))
            self.assertTrue(result['ok'])
            return ''
        service = self._service(llm=llm, media_session=media,
            registry_factory=lambda ctx: media_registry(neutral_registry_for(ctx), ctx))
        for state in ('stored', 'uploading', 'uploaded', 'publishing', 'published', 'handed_off', 'message_accepted'):
            media.forward.return_value = {'state': state, 'artifact_id': 'art-forward'}
            try:
                answer = await service.ask(self.inbound, self._platform(), '转发',
                                           selected_profile_override=self.profile)
            except ChatFailure as error:
                self.fail(f'registered forward {state} must not produce {error.code}')
            self.assertEqual(answer, '[silence]')
        for state in ('failed', 'ambiguous', 'prepared_content'):
            media.forward.return_value = {'state': state, 'artifact_id': 'art-forward'}
            with self.assertRaises(ChatFailure) as raised:
                await service.ask(self.inbound, self._platform(), '转发', selected_profile_override=self.profile)
            self.assertEqual(raised.exception.code, 'empty_response')

    async def test_forward_prepared_text_is_sent_without_other_tool_delivery(self) -> None:
        pending = []
        async def forward(_arguments, *, send):
            self.assertTrue(send)
            pending.append('已准备的转发摘要')
            return {'state': 'prepared_content'}
        media = SimpleNamespace(
            frontend_vision=False,
            service=SimpleNamespace(image_fetcher=SimpleNamespace(max_bytes=1024),
                                    config=SimpleNamespace(max_download_bytes=1024), asr=None),
            enabled=lambda name: name == 'send_forward_summary',
            input_images=AsyncMock(return_value=((), '')), current=AsyncMock(), forward=forward,
            finish=lambda answer: '\n\n'.join([answer, *pending]),
        )
        async def llm(_user_text, _tools, execute_tool, **_kwargs):
            self.assertTrue(json.loads(await execute_tool('send_forward_summary', {}))['ok'])
            return ''
        service = self._service(llm=llm, media_session=media,
            registry_factory=lambda ctx: media_registry(neutral_registry_for(ctx), ctx))
        try:
            answer = await service.ask(self.inbound, self._platform(), '转发', selected_profile_override=self.profile)
        except ChatFailure as error:
            self.fail(f'prepared forward content must be returned, not {error.code}')
        self.assertEqual(answer.strip(), '已准备的转发摘要')
        media.current.assert_awaited_once()

    async def test_send_file_from_sandbox_allows_empty_answer(self) -> None:
        class _FakeExecutor(OwnedSandboxExecutor):
            def __init__(self) -> None:
                self.generation_enabled = True
                self.state = 'committed'

            def matches(self, source) -> bool:
                return True

            async def execute(self, name, arguments):
                return json.dumps({"ok": True, "stored": True, "delivery_state": self.state,
                                   "message_accepted": self.state == 'committed'})

        executor = _FakeExecutor()

        async def _generation_factory(_inbound, _platform):
            return SimpleNamespace(
                context=lambda ctx: ToolContext(
                    ctx.services,
                    event=ctx.event,
                    capabilities=ctx.capabilities | {"sandbox"},
                    agent_executor=executor,
                    journal_turn_id=ctx.journal_turn_id,
                    inbound=ctx.inbound,
                    platform=ctx.platform,
                ),
                close=AsyncMock(),
            )

        async def llm_file(_user_text, _tools, execute_tool, **_kwargs) -> str:
            res = json.loads(await execute_tool("send_file_from_sandbox", {"path": "out.png"}))
            self.assertTrue(res["ok"])
            return ""

        service = self._service(
            llm=llm_file,
            generation_factory=_generation_factory,
            registry_factory=lambda _ctx: ToolRegistry(SANDBOX_HANDLERS),
        )
        for state in ('stored', 'waiting', 'committed'):
            executor.state = state
            reply = await service.ask(
                self.inbound,
                self._platform(),
                "发文件",
                selected_profile_override=self.profile,
            )
            self.assertEqual(reply, "[silence]")

        for state in ('failed', 'ambiguous', None):
            executor.state = state
            with self.assertRaises(ChatFailure) as raised:
                await service.ask(self.inbound, self._platform(), "发文件", selected_profile_override=self.profile)
            self.assertEqual(raised.exception.code, 'empty_response')


if __name__ == "__main__":
    unittest.main()


class MustReplyTests(unittest.IsolatedAsyncioTestCase):
    """A direct @ or private message never ends in silence unless the person asked for quiet."""

    asyncSetUp = EmptyAnswerAfterDeliveryTests.asyncSetUp
    _service = EmptyAnswerAfterDeliveryTests._service
    _platform = EmptyAnswerAfterDeliveryTests._platform

    async def _ask(self, answers, *, inbound=None, text="你现在代码规模多少行"):
        calls = []

        async def llm(user_text, tools, execute_tool, **_kwargs):
            calls.append((user_text, len(tools)))
            return answers[len(calls) - 1]

        reply = await self._service(llm=llm).ask(
            inbound or self.inbound, self._platform(), text, selected_profile_override=self.profile)
        return reply, calls

    async def test_mention_silence_retries_without_tools(self) -> None:
        reply, calls = await self._ask(["[silence]", "没查到具体行数"])
        self.assertEqual(reply, "没查到具体行数")
        self.assertEqual(len(calls), 2)
        self.assertIn("必须回复", calls[1][0])
        self.assertEqual(calls[1][1], 0)

    async def test_second_silence_falls_back_to_plain_reply(self) -> None:
        from src.plugins.ai_chat.application.platform_chat import _MUST_REPLY_FALLBACK
        reply, calls = await self._ask(["[silence]", "[silence]"])
        self.assertEqual((reply, len(calls)), (_MUST_REPLY_FALLBACK, 2))

    async def test_quiet_request_bot_sender_and_plain_group_message_stay_silent(self) -> None:
        cases = {
            "shush": dict(text="闭嘴吧你"),
            "bot": dict(inbound=make_inbound(sender_display="别的机器人（机器人）")),
            "not_mentioned": dict(inbound=make_inbound(mentions_bot=False)),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                reply, calls = await self._ask(["[silence]"], **kwargs)
                self.assertEqual((reply, len(calls)), ("[silence]", 1))
