"""Real PG and Unix RPC; QQ membership and sandbox processes are fixtures."""

import asyncio
from dataclasses import replace
import json
import unittest
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init()

from src.sandbox_runtime.docker import SandboxError, SandboxResult
from src.plugins.ai_chat.agent.execution import active_agent_step, EntryDecision
from src.plugins.ai_chat.project_access import project_access
from src.plugins.ai_chat.project_io import ProjectIOStore
from src.plugins.ai_chat.tool_invocation import active_tool_call_id
from src.plugins.ai_chat.tools import domain_registry
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from tests import test_project_runtime as fixture
from tests.test_routine_tools import event


class ProjectToolExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.ProjectRuntimeTests.asyncSetUp(self)
        await self.runtime.start()
        self.app.project_runtime = self.runtime
        self.context = ToolContext(self.handlers, event(), frozenset({"projects"}))
        self.counter, self.effects = 0, []
        self.runtime.store.open(self.access, self.pid)
        project = self.runtime.store.get(self.access, self.pid)
        self.runtime.lifecycle.request(
            self.access,
            self.pid,
            "wake",
            request_key="wake",
            expected_version=project["version"],
        )
        await self.worker.run_once()
        self.assertEqual(
            self.runtime.store.get(self.access, self.pid)["status"], "active"
        )
        await self.runtime.sampler.run_once()

        async def write(owner, sandbox_id, path, content, **kwargs):
            sandbox = sandbox_id
            self.effects.append(("write", owner, sandbox, path))
            file = self.root / "tool-files" / sandbox / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(content)
            return len(content)

        async def read(owner, sandbox_id, path, **kwargs):
            sandbox = sandbox_id
            self.effects.append(("read", owner, sandbox, path))
            try:
                return (self.root / "tool-files" / sandbox / path).read_bytes()
            except FileNotFoundError:
                raise SandboxError("fixture missing file") from None

        async def execute(owner, sandbox_id, command, *args, **kwargs):
            sandbox = sandbox_id
            self.effects.append(("exec", owner, sandbox, command))
            return SandboxResult("fixture output", "", 0)

        for name, implementation in (
            ("write_file", write),
            ("read_file", read),
            ("exec", execute),
        ):
            self.enterContext(
                patch.object(self.driver, name, side_effect=implementation)
            )

    async def call(self, name, args=None, *, call_id=None, context=None):
        self.counter += 1
        token = active_tool_call_id.set(call_id or f"call-{self.counter}")
        try:
            return json.loads(
                await domain_registry.execute(
                    TurnState(context or self.context), name, args or {}
                )
            )
        finally:
            active_tool_call_id.reset(token)

    async def test_write_read_execute_use_project_owner_without_task_cleanup(self):
        args = {"path": "src/hello.txt", "content": "你好 project"}
        written = await self.call("sandbox_write_file", args)
        self.assertTrue(written["ok"], written)
        self.assertEqual(written["bytes_written"], len(args["content"].encode()))
        read = await self.call("sandbox_read_file", {"path": args["path"]})
        self.assertEqual(read["content"], args["content"])
        run = await self.call("sandbox_exec", {"command": "printf checked"})
        self.assertTrue(run["ok"], run)
        self.assertEqual(run["stdout"], "fixture output")
        project = self.runtime.store.get(self.access, self.pid)
        self.assertTrue(
            all(
                row[1:3] == (self.scope.key, project["sandbox_id"])
                for row in self.effects
            )
        )
        self.assertEqual([row[0] for row in self.effects], ["write", "read", "exec"])
        self.assertTrue(next(iter(self.docker.containers.values()))["State"]["Running"])
        self.assertIsNone(self.runtime.io.status(self.access, self.pid))
        self.assertTrue(self.runtime.usage.status(self.access, self.pid)["complete"])
        self.assertIsNone(active_tool_call_id.get())

    async def test_replay_after_focus_change_reuses_receipt_not_new_project(self):
        args = {"path": "old.txt", "content": "first"}
        first = await self.call("sandbox_write_file", args, call_id="stable")
        self.assertTrue(first["ok"], first)
        new = self.runtime.store.create(self.access, "Other", quota_bytes=64 * 1024**2)
        self.runtime.io = ProjectIOStore(self.runtime.store, self.runtime.usage)
        replay = await self.call("sandbox_write_file", args, call_id="stable")
        self.assertEqual(replay, first)
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(
            self.runtime.store.current(self.access)["project_id"], new["project_id"]
        )
        changed = await self.call(
            "sandbox_write_file", {**args, "content": "changed"}, call_id="stable"
        )
        self.assertEqual(changed["code"], "project_conflict")
        self.assertEqual(len(self.effects), 1)

    async def test_identical_reads_with_distinct_call_ids_are_not_stale(self):
        args = {"path": "data.txt", "content": "before"}
        await self.call("sandbox_write_file", args)
        before = await self.call("sandbox_read_file", {"path": "data.txt"})
        await self.call("sandbox_write_file", {**args, "content": "after"})
        after = await self.call("sandbox_read_file", {"path": "data.txt"})
        self.assertEqual((before["content"], after["content"]), ("before", "after"))

    async def test_busy_receipt_is_settled_on_next_use_without_reexecution(self):
        execute = self.driver.exec.side_effect

        async def orphan(*args, **kwargs):
            result = await execute(*args, **kwargs)
            self.docker.processes = "PID\n1001\n2002\n"
            return result

        with patch.object(self.driver, "exec", side_effect=orphan):
            result = await self.call(
                "sandbox_exec", {"command": "build"}, call_id="build"
            )
        self.assertFalse(result["ok"])
        status = await self.call("project_status")
        self.assertEqual(status["tool_operation"]["state"], "needs_attention")
        self.assertTrue(status["tool_operation"]["result_recorded"])
        blocked = await self.call(
            "sandbox_write_file", {"path": "other", "content": "no"}
        )
        self.assertFalse(blocked["ok"])
        self.assertEqual(len(self.effects), 1)
        self.docker.processes = None
        result = await self.call(
            "sandbox_write_file", {"path": "done", "content": "yes"}
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual([row[0] for row in self.effects], ["exec", "write"])
        self.assertIsNone(self.runtime.io.status(self.access, self.pid))

    async def test_unknown_effects_never_rerun_even_when_processes_are_idle(self):
        with patch.object(
            self.client, "exec", side_effect=TimeoutError("private endpoint")
        ) as run:
            failed = await self.call(
                "sandbox_exec", {"command": "build"}, call_id="lost"
            )
            self.assertFalse(failed["ok"])
            self.assertEqual(
                self.runtime.io.status(self.access, self.pid)["state"],
                "needs_attention",
            )
            replay = await self.call(
                "sandbox_exec", {"command": "build"}, call_id="lost"
            )
            self.assertEqual(replay["code"], "project_io_pending")
            again = await self.call(
                "sandbox_exec", {"command": "build"}, call_id="new-id"
            )
            self.assertFalse(again["ok"])
            self.assertEqual(run.await_count, 1)

    async def test_cancel_keeps_dispatch_record_and_blocks_new_writes(self):
        started = asyncio.Event()

        async def hang(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        with patch.object(self.client, "exec", side_effect=hang):
            task = asyncio.create_task(self.call("sandbox_exec", {"command": "build"}))
            try:
                await asyncio.wait_for(started.wait(), 5)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        status = self.runtime.io.status(self.access, self.pid)
        self.assertEqual(status["state"], "needs_attention")
        self.assertFalse(status["result_recorded"])

    async def test_live_membership_rechecked_before_dispatch_and_receipt_disclosure(
        self,
    ):
        backend = self.runtime.backends["oci"]
        observe = backend.io_evidence

        async def revoke(project):
            result = await observe(project)
            self.bot.get_group_member_list.return_value = [{"user_id": 301}]
            return result

        with patch.object(backend, "io_evidence", side_effect=revoke):
            result = await self.call(
                "sandbox_write_file", {"path": "bad", "content": "no"}
            )
        self.assertEqual(result["code"], "project_forbidden")
        self.assertEqual(self.effects, [])
        self.assertIsNone(self.runtime.io.status(self.access, self.pid))

    async def test_group_viewer_can_read_but_cannot_write_or_execute(self):
        await self.call("sandbox_write_file", {"path": "visible", "content": "group"})
        self.bot.get_group_member_list.return_value.append({"user_id": 202})
        context = replace(self.context, event=event(actor=202))
        access = await project_access(context)
        self.runtime.store.open(access, self.pid)
        self.assertEqual(
            (
                await self.call(
                    "sandbox_read_file", {"path": "visible"}, context=context
                )
            )["content"],
            "group",
        )
        self.app.settings = replace(self.app.settings, sandbox_allowed_users={201, 202})
        for name, args in (
            ("sandbox_write_file", {"path": "visible", "content": "no"}),
            ("sandbox_exec", {"command": "no"}),
        ):
            self.assertEqual(
                (await self.call(name, args, context=context))["code"],
                "project_forbidden",
            )

    async def test_cross_scope_and_bot_never_fall_back_to_current_project(self):
        for changed in (
            event(group=102),
            event(bot=302),
            event(group=None),
            event(actor=202, group=None),
        ):
            result = await self.call(
                "sandbox_read_file",
                {"path": "secret"},
                context=replace(self.context, event=changed),
            )
            self.assertFalse(result["ok"])
        self.assertEqual(self.effects, [])

    async def test_private_project_tools_need_no_group_executor(self):
        context = replace(self.context, event=event(group=None))
        created = await self.call(
            "project_create", {"title": "Private"}, context=context
        )
        self.assertTrue(created["ok"], created)
        await self.worker.run_once()
        await self.runtime.sampler.run_once()
        result = await self.call(
            "sandbox_write_file",
            {"path": "private", "content": "mine"},
            context=context,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.effects[-1][1], "onebot-v11:private:201")

    async def test_invalid_and_background_requests_have_no_effect_or_reservation(self):
        for args in (
            {"path": "../escape", "content": "no"},
            {"path": "/etc/passwd", "content": "no"},
            {"path": "ok", "content": 123},
        ):
            self.assertFalse((await self.call("sandbox_write_file", args))["ok"])
        result = await self.call(
            "sandbox_exec", {"command": "build", "background": True}
        )
        self.assertEqual(result["code"], "project_background_unavailable")
        missing_id = json.loads(
            await domain_registry.execute(
                TurnState(self.context), "sandbox_exec", {"command": "build"}
            )
        )
        self.assertFalse(missing_id["ok"])
        self.assertEqual(self.effects, [])
        self.assertIsNone(self.runtime.io.status(self.access, self.pid))

    async def test_missing_read_is_reported_without_permanent_lock(self):
        result = await self.call("sandbox_read_file", {"path": "absent"})
        self.assertEqual(result["code"], "project_read_failed")
        self.assertIsNone(self.runtime.io.status(self.access, self.pid))
        self.assertTrue(
            (await self.call("sandbox_write_file", {"path": "new", "content": "ok"}))[
                "ok"
            ]
        )

    async def test_nonzero_exit_is_settled_but_not_reported_as_success(self):
        with patch.object(
            self.driver, "exec", return_value=SandboxResult("", "build failed", 2)
        ):
            result = await self.call("sandbox_exec", {"command": "build"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["returncode"], 2)
        self.assertIsNone(self.runtime.io.status(self.access, self.pid))

    async def test_concurrent_same_call_cannot_dispatch_twice(self):
        results = await asyncio.gather(
            *(
                self.call(
                    "sandbox_write_file",
                    {"path": "once", "content": "yes"},
                    call_id="same",
                )
                for _ in range(2)
            )
        )
        self.assertTrue(any(result["ok"] for result in results), results)
        self.assertEqual(len(self.effects), 1)
        self.assertIsNone(self.runtime.io.status(self.access, self.pid))

    async def test_malformed_process_inventory_fails_closed_before_dispatch(self):
        self.docker.processes = "untrusted process list"
        result = await self.call(
            "sandbox_write_file", {"path": "never", "content": "no"}
        )
        self.assertFalse(result["ok"])
        self.assertEqual(self.effects, [])
        self.assertIsNone(self.runtime.io.status(self.access, self.pid))

    async def test_explicit_sandbox_and_subagent_routing_stay_unchanged(self):
        ordinary = AsyncMock()
        ordinary.sandbox_manager = self.client
        ordinary.execute.return_value = '{"ok":true,"ordinary":true}'
        context = replace(
            self.context,
            capabilities=frozenset({"sandbox", "projects"}),
            agent_executor=ordinary,
        )
        result = await self.call(
            "sandbox_exec",
            {"sandbox_id": "explicit", "command": "run"},
            context=context,
        )
        self.assertTrue(result["ordinary"])
        token = active_agent_step.set("step-1")
        try:
            result = await self.call(
                "sandbox_exec", {"command": "run"}, context=context
            )
        finally:
            active_agent_step.reset(token)
        self.assertTrue(result["ordinary"])
        self.assertEqual(ordinary.execute.await_count, 2)
        self.assertEqual(self.effects, [])

    async def test_schema_projection_is_optional_only_for_project_capable_callers(self):
        tools = {
            d["function"]["name"]: d for d in domain_registry.definitions(self.context)
        }
        self.assertNotIn(
            "sandbox_id", tools["sandbox_exec"]["function"]["parameters"]["required"]
        )
        ordinary = AsyncMock()
        ordinary.sandbox_manager = self.client
        context = replace(
            self.context, capabilities=frozenset({"sandbox"}), agent_executor=ordinary
        )
        tools = {d["function"]["name"]: d for d in domain_registry.definitions(context)}
        self.assertIn(
            "sandbox_id", tools["sandbox_exec"]["function"]["parameters"]["required"]
        )


class ProjectEntryTests(unittest.TestCase):
    def test_direct_execution_requires_host_project_gate_and_cannot_pretend_done(self):
        payload = {
            "mode": "direct",
            "task_type": "execution",
            "reason": "edit selected project",
            "answer": "",
            "objective": "",
            "deliverables": [],
            "constraints": [],
            "acceptance": [],
            "outcome_checks": [],
            "steps": [],
        }
        with self.assertRaises(ValueError):
            EntryDecision.parse(payload)
        self.assertEqual(
            EntryDecision.parse(payload, project_execution=True).mode, "direct"
        )
        with self.assertRaises(ValueError):
            EntryDecision.parse({**payload, "answer": "done"}, project_execution=True)
        with self.assertRaises(ValueError):
            EntryDecision.parse(
                {**payload, "task_type": "project"}, project_execution=True
            )


if __name__ == "__main__":
    unittest.main()
