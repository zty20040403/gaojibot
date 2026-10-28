"""Real PG, immutable file export and Unix RPC; no live QQ, model or Docker."""

import asyncio
from dataclasses import replace
import hashlib
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.project_autosave import ProjectAutosave
from src.plugins.ai_chat.project_runtime import ProjectRuntime
from src.plugins.ai_chat.project_access import project_access
from src.plugins.ai_chat.project_commands import command_result
from src.plugins.ai_chat.project_io import ProjectIOStore
from src.plugins.ai_chat.project_usage import ProjectUsageStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.turn_journal import TurnJournal
from src.plugins.ai_chat.projects import ProjectConflict
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker
from tests import test_project_file_outbox as fixture
from tests import test_project_vm_executor as vm_fixture
from tests.project_turn_fixture import start_project_turn


class ProjectAutosaveTests(unittest.IsolatedAsyncioTestCase):
    call = fixture.ProjectFileOutboxTests.call

    async def asyncSetUp(self):
        await fixture.ProjectFileOutboxTests.asyncSetUp(self)
        self.files = self.volumes.binding(self.scope.key, self.pid)
        self.now = int(time.time())

        async def write(owner, sandbox_id, path, content, **kwargs):
            self.effects.append(("write", owner, sandbox_id, path))
            target = self.files / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            return len(content)

        async def read(owner, sandbox_id, path, **kwargs):
            return (self.files / path).read_bytes()

        self.driver.write_file.side_effect = write
        self.driver.read_file.side_effect = read

    async def write(self, text="retained text"):
        result = await self.call(
            "sandbox_write_file", {"path": "report.txt", "content": text}
        )
        self.assertTrue(result["ok"], result)
        return result

    def finish(self, status="succeeded"):
        self.journal.finish_turn(
            self.context.journal_turn_id, status=status, finished_at=self.now
        )

    def saves(self):
        return self.runtime.autosave.saves.status(self.access, self.pid)

    async def scan(self):
        self.now += 31
        return await self.runtime.autosave.run_once(now=self.now)

    async def run_saves(self):
        with patch("time.time", return_value=self.now):
            await self.worker.run_once()

    async def test_terminal_turn_enqueues_once_and_publishes_actual_file_version(self):
        await self.write()
        self.assertEqual(await self.scan(), 0)
        self.assertFalse(self.saves()[0]["saved"])
        self.finish()
        await self.scan()
        self.assertEqual(self.saves()[0]["status"], "queued")
        text = await command_result(self.handlers, self.context.event, "状态")
        self.assertIn("自动保存", text)
        self.assertIn("尚未完成", text)
        with self.runtime.store._transaction() as cursor:
            tracker = dict(
                cursor.execute("SELECT * FROM project_turn_saves").fetchone()
            )
        self.assertEqual(tracker["state"], "queued")
        self.assertIsNotNone(tracker["job_id"])
        self.assertEqual(await self.scan(), 0)
        await self.run_saves()
        self.assertTrue(self.saves()[0]["saved"])
        self.assertEqual(self.saves()[0]["version"], "v1")
        self.assertIn("已保存为 v1", await command_result(self.handlers, self.context.event, "状态"))
        self.assertEqual(
            self.runtime.store.read_file(self.access, self.pid, 1, "report.txt"),
            b"retained text",
        )
        version = self.runtime.store.versions(self.access, self.pid)[0]
        self.assertEqual(version["source_turn_id"], self.context.journal_turn_id)
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(
            self.runtime.store.get(self.access, self.pid)["status"], "active"
        )

    async def test_process_loss_recovers_without_rerunning_write(self):
        await self.write()
        await self.runtime.close()
        self.journal.recover_interrupted()
        self.worker = DurableJobWorker(
            self.jobs, logger=self.logger, worker_id="replacement"
        )
        self.app.job_worker = self.worker
        self.runtime = ProjectRuntime(self.handlers)
        self.app.project_runtime = self.runtime
        self.addAsyncCleanup(self.runtime.close)
        await self.runtime.start()
        await self.scan()
        await self.run_saves()
        self.assertTrue(self.saves()[0]["saved"])
        self.assertEqual(len(self.effects), 1)

    async def test_unknown_command_never_reexecutes_or_unlocks_after_terminal_turn(
        self,
    ):
        with patch.object(
            self.client, "exec", side_effect=TimeoutError("private detail")
        ) as execute:
            result = await self.call("sandbox_exec", {"command": "build"})
            self.assertFalse(result["ok"])
            self.finish("crashed")
            await self.scan()
            self.runtime.autosave = ProjectAutosave(self.runtime)
            await self.scan()
            execute.assert_awaited_once()
        self.assertEqual(self.saves()[0]["reason"], "unknown_execution")
        self.assertFalse(self.saves()[0]["saved"])
        self.assertEqual(
            self.runtime.io.status(self.access, self.pid)["state"], "needs_attention"
        )
        self.assertEqual(self.runtime.store.versions(self.access, self.pid), [])

    async def test_returned_result_waits_for_writers_then_saves_even_failed_command(
        self,
    ):
        original = self.driver.exec.side_effect

        async def busy(*args, **kwargs):
            result = await original(*args, **kwargs)
            (self.files / "partial.txt").write_text("partial result")
            self.docker.processes = "PID\n1001\n2002\n"
            return replace(result, returncode=1)

        with patch.object(self.driver, "exec", side_effect=busy):
            await self.call("sandbox_exec", {"command": "failed build"})
        self.finish("crashed")
        await self.scan()
        self.assertFalse(self.saves()[0]["saved"])
        self.assertEqual(
            self.runtime.io.status(self.access, self.pid)["state"], "needs_attention"
        )
        self.docker.processes = None
        await self.scan()
        await self.run_saves()
        self.assertTrue(self.saves()[0]["saved"])
        self.assertEqual(
            self.runtime.store.read_file(self.access, self.pid, 1, "partial.txt"),
            b"partial result",
        )
        self.assertEqual(len(self.effects), 1)

    async def test_reserved_only_terminal_turn_is_aborted_and_skipped_not_executed(
        self,
    ):
        grant = self.runtime.io.admit(
            self.access,
            self.pid,
            "sandbox_exec",
            request_key=hashlib.sha256(b"reserved").hexdigest(),
            arguments_sha256="a" * 64,
            expected_version=self.runtime.store.get(self.access, self.pid)["version"],
            journal_turn_id=self.context.journal_turn_id,
            native_message_id=5,
        )
        self.finish("aborted")
        with self.assertRaises(ProjectConflict):
            self.runtime.io.dispatch(self.access, grant)
        await self.scan()
        self.assertEqual(
            self.runtime.io.receipt(self.access, grant)["state"], "aborted"
        )
        self.assertEqual(self.saves()[0]["status"], "skipped")
        self.assertEqual(self.effects, [])
        self.assertEqual(self.runtime.store.versions(self.access, self.pid), [])

    async def test_other_running_turn_delays_autosave_and_identical_content_deduplicates(
        self,
    ):
        await self.write()
        self.finish()
        other = start_project_turn(self.ledger, self.journal, self.scope, message_id=6)
        self.context = replace(
            self.context,
            journal_turn_id=other.turn_id,
            event=self.context.event.model_copy(update={"message_id": 6}),
        )
        await self.write("same final content")
        await self.scan()
        self.assertEqual(self.runtime.store.versions(self.access, self.pid), [])
        self.assertTrue(all(not s["saved"] for s in self.saves()))
        self.finish()
        for _ in range(2):
            await self.scan()
            await self.run_saves()
        self.assertTrue(all(s["saved"] for s in self.saves()))
        self.assertEqual(len(self.runtime.store.versions(self.access, self.pid)), 1)

    async def test_pending_file_delivery_blocks_save_and_idle_transition(self):
        await self.write()
        sent = await self.call("send_file_from_sandbox", {"path": "report.txt"})
        self.assertTrue(sent["ok"], sent)
        self.finish()
        await self.scan()
        self.assertEqual(self.saves()[0]["status"], "pending")
        with self.assertRaises(ProjectConflict):
            self.runtime.lifecycle.request(
                self.access,
                self.pid,
                "stop",
                request_key="stop",
                expected_version=self.runtime.store.get(self.access, self.pid)[
                    "version"
                ],
            )
        # Model an independently confirmed transport receipt, not an upload attempt.
        with self.runtime.store._transaction() as cursor:
            cursor.execute(
                "UPDATE deliveries SET status='committed' WHERE delivery_id=?",
                (sent["delivery_id"],),
            )
        await self.scan()
        await self.run_saves()
        self.assertTrue(self.saves()[0]["saved"])

    async def test_missing_or_foreign_turn_fails_before_effect_and_no_tracker(self):
        other = start_project_turn(
            self.ledger, self.journal, self.scope.with_actor("202"), message_id=19
        )
        wrong_message = start_project_turn(
            self.ledger, self.journal, self.scope, message_id=20
        )
        for turn_id in (None, other.turn_id, wrong_message.turn_id):
            result = await self.call(
                "sandbox_write_file",
                {"path": "bad", "content": "no"},
                context=replace(self.context, journal_turn_id=turn_id),
            )
            self.assertEqual(result["code"], "project_conflict", result)
        self.assertEqual(self.effects, [])
        self.assertEqual(self.saves(), [])

    async def test_revoked_membership_keeps_pending_and_does_not_substitute_owner(self):
        await self.write()
        self.finish()
        self.bot.get_group_member_list.return_value = [{"user_id": 301}]
        await self.scan()
        self.assertEqual(self.saves()[0]["reason"], "permission_unavailable")
        self.assertEqual(self.runtime.store.versions(self.access, self.pid), [])
        self.bot.get_group_member_list.return_value = [
            {"user_id": 201},
            {"user_id": 301},
        ]
        await self.scan()
        await self.run_saves()
        self.assertTrue(self.saves()[0]["saved"])

    async def test_concurrent_scans_and_lost_enqueue_reply_create_one_job(self):
        await self.write()
        self.finish()
        request = self.runtime.lifecycle.request

        def lose_reply(*args, **kwargs):
            request(*args, **kwargs)
            raise ConnectionError("lost reply after commit")

        with patch.object(self.runtime.lifecycle, "request", side_effect=lose_reply):
            await asyncio.gather(
                self.scan(), ProjectAutosave(self.runtime).run_once(now=self.now)
            )
        self.runtime.autosave = ProjectAutosave(self.runtime)
        await self.scan()
        with self.runtime.store._transaction() as cursor:
            count = cursor.execute(
                "SELECT count(*) FROM project_lifecycle_runs WHERE action='snapshot'"
            ).fetchone()[0]
        self.assertEqual(count, 1)
        await self.run_saves()
        self.assertTrue(self.saves()[0]["saved"])

    async def test_enqueue_rollback_leaves_no_half_queued_save(self):
        await self.write()
        self.finish()
        with patch.object(
            self.runtime.store, "_event", side_effect=RuntimeError("rollback fixture")
        ):
            await self.scan()
        with self.runtime.store._transaction() as cursor:
            row = cursor.execute(
                "SELECT state,job_id FROM project_turn_saves"
            ).fetchone()
            self.assertEqual((row["state"], row["job_id"]), ("pending", None))
            self.assertEqual(
                cursor.execute(
                    "SELECT count(*) FROM project_lifecycle_runs WHERE action='snapshot'"
                ).fetchone()[0],
                0,
            )
        self.assertIsNone(
            self.runtime.store.get(self.access, self.pid)["lifecycle_job_id"]
        )
        await self.scan()
        await self.run_saves()
        self.assertTrue(self.saves()[0]["saved"])

    async def test_revoked_editor_does_not_save_as_still_authorized_owner(self):
        self.app.settings = replace(self.app.settings, sandbox_allowed_users={201, 202})
        self.bot.get_group_member_list.return_value = [
            {"user_id": 201},
            {"user_id": 202},
            {"user_id": 301},
        ]
        editor = start_project_turn(
            self.ledger, self.journal, self.scope.with_actor("202"), message_id=6
        )
        editor_event = self.context.event.model_copy(
            update={"message_id": 6, "user_id": 202}
        )
        self.context = replace(
            self.context, event=editor_event, journal_turn_id=editor.turn_id
        )
        access = await project_access(self.context)
        project = self.runtime.store.get(self.access, self.pid)
        self.runtime.store.set_member(
            self.access,
            self.pid,
            access.principal_id,
            "editor",
            expected_version=project["version"],
        )
        self.runtime.store.open(access, self.pid)
        await self.write()
        self.finish()
        project = self.runtime.store.get(self.access, self.pid)
        self.runtime.store.set_member(
            self.access,
            self.pid,
            access.principal_id,
            None,
            expected_version=project["version"],
        )
        await self.scan()
        self.assertEqual(self.saves()[0]["reason"], "permission_unavailable")
        self.assertEqual(self.runtime.store.versions(self.access, self.pid), [])


class ProjectVmAutosaveTests(
    vm_fixture.VmExecutorFixture, unittest.IsolatedAsyncioTestCase
):
    async def test_automatic_vm_save_records_turn_without_rebooting_guest(self):
        await self.transition("wake")
        project = self.project()
        ledger, journal = MessageLedger(self.db), TurnJournal(self.db)
        self.addCleanup(ledger.close)
        self.addCleanup(journal.close)
        turn = start_project_turn(ledger, journal, self.scope)
        usage = ProjectUsageStore(self.projects)
        usage.record(
            self.access,
            self.pid,
            await self.backend.sample_usage(project),
            expected_version=project["version"],
        )
        io = ProjectIOStore(self.projects, usage)
        grant = io.admit(
            self.access,
            self.pid,
            "sandbox_write_file",
            request_key="1" * 64,
            arguments_sha256="2" * 64,
            expected_version=project["version"],
            journal_turn_id=turn.turn_id,
            native_message_id=5,
            expected_bytes=11,
        )
        io.dispatch(self.access, grant)
        (self.guests[project["sandbox_id"]].workspace / "result.txt").write_bytes(
            b"VM retained"
        )
        io.complete(grant, {"ok": True}, await self.backend.io_evidence(project))
        journal.finish_turn(turn.turn_id, status="succeeded")

        async def access(candidate, actor):
            self.assertEqual(actor, self.access.principal_id)
            return self.access

        autosave = ProjectAutosave(
            SimpleNamespace(
                store=self.projects,
                lifecycle=self.lifecycle,
                io=io,
                backends={"vm": self.backend},
                _execution_access=access,
            )
        )
        starts = self.effects.count("start")
        await autosave.run_once()
        result = await self.executor(self.claim())
        self.assertEqual(result["snapshot"]["version_no"], 1)
        self.assertEqual(self.effects.count("start"), starts)
        self.assertTrue(autosave.saves.status(self.access, self.pid)[0]["saved"])
        self.assertEqual(
            self.projects.versions(self.access, self.pid)[0]["source_turn_id"],
            turn.turn_id,
        )
        self.assertEqual(
            self.projects.read_file(self.access, self.pid, 1, "result.txt"),
            b"VM retained",
        )
