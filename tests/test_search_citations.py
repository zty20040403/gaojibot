"""Citation numbering must remain correct through the actual streaming boundary."""
import json
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.plugins.ai_chat.agent_loop import FinalStreamState, _create_streaming_completion, ready_stream_prefix
from src.plugins.ai_chat.application.platform_chat import PlatformChatService, _citation_safe_stream
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.message_ir import MessageBody
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage
from src.plugins.ai_chat.web_search import SearchResult, clean_source_url, render_search_sources


class CitationBoundaryTests(unittest.TestCase):
    results = [SearchResult(str(i), f'https://example.test/{i}', '') for i in range(1, 4)]

    def test_code_fences_close_only_on_a_matching_line(self):
        blocks = [
            '```python\ns = "```"\nx = items[2]\n```\n',
            '````python\n```\nx = items[2]\n````\n',
            '~~~python\nx = items[2]\n~~~\n',
            '  ```python\r\nx = items[2]\r\n  ```\r\n',
        ]
        for block in blocks:
            with self.subTest(block=block):
                answer, sources = render_search_sources(self.results, block + '正文[3]。')
                self.assertEqual(answer, block + '正文[1]。')
                self.assertEqual(sources, '来源：\n1. 3\nhttps://example.test/3')
        block = '```python\nx = items[2]\n'
        answer, sources = render_search_sources(self.results, '正文[3]\n' + block)
        self.assertEqual(answer, '正文[1]\n' + block)
        self.assertEqual(sources, '来源：\n1. 3\nhttps://example.test/3')

    def test_inline_triple_backtick_code_is_not_rewritten(self):
        answer, sources = render_search_sources(self.results, '代码 ```items[2]```，结论[3]。')
        self.assertEqual(answer, '代码 ```items[2]```，结论[1]。')
        self.assertEqual(sources, '来源：\n1. 3\nhttps://example.test/3')
        answer, sources = render_search_sources(self.results, '代码 ```python\nitems[2]\n``` 后面[3]。')
        self.assertEqual(answer, '代码 ```python\nitems[2]\n``` 后面[1]。')
        self.assertEqual(sources, '来源：\n1. 3\nhttps://example.test/3')

    def test_very_long_invalid_and_zero_prefixed_citations_do_not_crash(self):
        invalid = '[' + '9' * 5000 + ']'
        leading_zero = '[' + '0' * 5000 + '1]'
        try:
            answer, sources = render_search_sources(self.results, f'{invalid} [3] {leading_zero}')
        except ValueError as error:
            self.fail(f'long citation must not abort the answer: {error}')
        self.assertEqual(answer, f'{invalid} [1] [2]')
        self.assertEqual(sources, '来源：\n1. 3\nhttps://example.test/3\n2. 1\nhttps://example.test/1')

    def test_root_dotted_social_domain_keeps_only_page_and_time(self):
        url = 'https://WWW.BILIBILI.COM.:8443/video/TEST?buvid=TEST&mid=TEST&share_id=TEST&p=2&t=3#TEST'
        self.assertEqual(clean_source_url(url), 'https://WWW.BILIBILI.COM.:8443/video/TEST?p=2&t=3#TEST')
        self.assertEqual(clean_source_url('https://bilibili.com.evil.test/?buvid=TEST'),
                         'https://bilibili.com.evil.test/?buvid=TEST')


class _Sink:
    ready_prefix = staticmethod(ready_stream_prefix)

    def __init__(self):
        self.sections = []

    async def __call__(self, text):
        self.sections.append(text)


class CitationStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def ask(self, with_sources):
        account = AccountKey("qq-official", "TEST_APP", "test")
        endpoint = EndpointKey(account, "private", "TEST_USER")
        actor = ActorKey(account, "c2c_user", "", "TEST_USER")
        inbound = InboundMessage(endpoint, actor, None, "TEST_MESSAGE", "TEST_USER", "",
                                 MessageBody(()), False, None, 1, None)
        platform = SimpleNamespace(account=account)
        profile = SimpleNamespace(provider="test", name="TEST_PROFILE")
        sink = _Sink()
        state = FinalStreamState()
        app = SimpleNamespace(
            settings=replace(settings, search_enabled=True, search_auto_enabled=True),
            message_ledger=None, conversations=None, logger=Mock(),
            skill_registry=SimpleNamespace(prompt_index=lambda: ""),
            reasoning_preferences=SimpleNamespace(get_explicit=lambda _: None),
        )

        async def db_call(func, *args):
            return func(*args)

        app.db_call = db_call
        registry = Mock()
        registry.definitions.return_value = []

        async def execute(turn, name, arguments):
            turn.search_results.extend([
                SearchResult("One", "https://example.test/1", ""),
                SearchResult("Two", "https://example.test/2", ""),
                SearchResult("Three", "https://example.test/3", ""),
            ])
            return json.dumps({"ok": True})

        registry.execute = execute

        async def llm(_text, _tools, execute_tool, **kwargs):
            if with_sources:
                await execute_tool("web_search", {"queries": ["TEST"]})
            content = "第一段引用[3]。\n\n第二段引用[1]。" if with_sources else "普通第一段。\n\n普通第二段。"

            async def chunks():
                for part in (content.split("\n\n")[0] + "\n\n", content.split("\n\n")[1]):
                    yield SimpleNamespace(choices=[SimpleNamespace(
                        delta=SimpleNamespace(content=part, tool_calls=[]), finish_reason=None)])

            with patch("src.plugins.ai_chat.agent_loop._invoke_completion", AsyncMock(return_value=chunks())):
                response, emitted = await _create_streaming_completion(kwargs["final_text_sink"], profile)
            kwargs["final_stream_state"].sent_prefix = emitted
            return response.choices[0].message.content.strip()

        service = PlatformChatService(app, registry_factory=lambda _: registry, llm=llm)
        with patch("src.plugins.ai_chat.application.platform_chat.conversation_key", return_value="TEST"), \
                patch("src.plugins.ai_chat.application.platform_chat.speaker", return_value="TEST"), \
                patch("src.plugins.ai_chat.application.platform_chat.turn_window", return_value=("", "", "")), \
                patch("src.plugins.ai_chat.application.platform_chat.background_task_context", AsyncMock(return_value="")), \
                patch("src.plugins.ai_chat.application.platform_chat.assert_job_owned"), \
                patch("src.storage_admission.require_tool_capacity"):
            result = await service.ask(inbound, platform, "TEST", selected_profile_override=profile,
                                       final_stream_sink=sink, final_stream_state=state)
        return sink.sections, result

    async def test_cited_stream_is_held_until_renumbering(self):
        sections, result = await self.ask(True)
        self.assertEqual(sections, [])
        self.assertEqual(result, "第一段引用[1]。\n\n第二段引用[2]。\n\n"
                         "来源：\n1. Three\nhttps://example.test/3\n2. One\nhttps://example.test/1")

    async def test_no_search_keeps_normal_streaming(self):
        sections, result = await self.ask(False)
        self.assertEqual(sections, ["普通第一段。\n\n"])
        self.assertEqual(result, "普通第二段。")

    async def test_citation_before_a_search_tool_call_is_never_streamed(self):
        # Providers can emit a complete paragraph before announcing a tool
        # call; at that instant the search-result list is still empty.
        sink = _Sink()
        turn = SimpleNamespace(search_results=[])
        profile = SimpleNamespace(provider="test", name="TEST_PROFILE")
        call = SimpleNamespace(index=0, id="call_TEST", type="function",
                               function=SimpleNamespace(name="web_search", arguments='{"queries":["test"]}'))

        async def chunks():
            for delta in (SimpleNamespace(content="猜测引用[3]。\n\n", tool_calls=[]),
                          SimpleNamespace(content=None, tool_calls=[call])):
                yield SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)])

        with patch("src.plugins.ai_chat.agent_loop._invoke_completion", AsyncMock(return_value=chunks())):
            response, emitted = await _create_streaming_completion(_citation_safe_stream(sink, turn), profile)
        self.assertEqual(response.choices[0].message.tool_calls[0].function.name, "web_search")
        self.assertEqual(emitted, "")
        self.assertEqual(sink.sections, [])

    def test_citations_beyond_limit_do_not_impersonate_listed_sources(self):
        results = [SearchResult(str(i), f"https://example.test/{i}", "") for i in range(1, 7)]
        answer, sources = render_search_sources(results, "[6] [1] [2] [3] [4] [5]")
        self.assertEqual(answer, "[1] [2] [3] [4] [5] ")
        self.assertEqual(len(sources.splitlines()), 11)
        self.assertNotIn("https://example.test/5", sources)
