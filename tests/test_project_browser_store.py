"""Actual PostgreSQL recovery/ACL invariants; runner observations are explicit fixtures."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
import threading
import unittest
from unittest.mock import patch

import nonebot
from alembic import command
from alembic.config import Config

nonebot.init()

from src.bot_storage import DatabaseError
from src.bot_storage.schema import HEAD_REVISION
from src.plugins.ai_chat.project_browser_store import ProjectBrowserStore, digest
from src.plugins.ai_chat.project_io import BROWSER_SESSION_TOOL, ProjectIOStore
from src.plugins.ai_chat.project_usage import ProjectUsageStore
from src.plugins.ai_chat.projects import ProjectConflict, ProjectStore
from tests import test_project_io as fixture


def key(text):
    return hashlib.sha256(text.encode()).hexdigest()


class ProjectBrowserStoreTests(fixture.IOFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = ProjectBrowserStore(self.io)
        # The lifecycle fixture does not create a real runtime.
        self.sql.execute("UPDATE projects SET sandbox_id='s123abc' WHERE project_id=?", (self.pid,))
        self.grant = self.io.admit(
            self.access, self.pid, BROWSER_SESSION_TOOL, request_key=key("session"),
            arguments_sha256=key("session parameters"), expected_version=self.project()["version"],
            journal_turn_id=self.turns[self.principal].turn_id, native_message_id=self.principal,
        )
        self.session = self.store.bind(self.access, self.grant, "1" * 32)
        self.io.dispatch(self.access, self.grant)
        self.remote = {"schema": 1, "binding": {
            "instance_id": "1" * 32, "owner": self.scope.key, "project_id": self.pid,
            "generation": self.project()["workspace_generation"], "fence": self.project()["lifecycle_fence"] - 1,
        }, "runtime": {"backend": "oci", "sandbox_id": "s123abc", "runtime_id": "2" * 64},
            "launch_id": self.session["launch_id"], "session_id": "3" * 32, "worker_id": "4" * 32,
            "config_sha256": "5" * 64, "sequence": 0, "last": None, "state": "ready", "updated_at": self.now}
        self.store.opened(self.grant, self.remote)

    def call(self, method="browser_click", args=None, *, text="first", store=None):
        return (store or self.store).admit(self.access, self.grant, key(text), method, {"ref": "b1"} if args is None else args)

    def dispatch(self, call, method="browser_click", args=None):
        return self.store.dispatch(self.access, call, method, {"ref": "b1"} if args is None else args)

    def reply(self, call, *, result=None, unknown=False):
        row = self.store.receipt(self.access, call)
        reply = {"schema": 1, "binding": self.remote["binding"], "worker_id": self.remote["worker_id"],
                 "sequence": call.sequence, "arguments_sha256": row["arguments_sha256"], "state": "completed",
                 "result": {"ok": True} if result is None else result}
        if unknown:
            reply.pop("result")
            reply.update(state="needs_attention", error="browser_outcome_unknown")
        return reply

    def ack(self, call, *, store=None):
        store = store or self.store
        row = store.receipt(self.access, call)
        remote = deepcopy(self.remote)
        remote.update(sequence=call.sequence, last={"method": row["method"],
            "arguments_sha256": row["arguments_sha256"], "state": "completed", "acknowledged": True,
            "response": row["response"]})
        store.acknowledged(call, remote)
        self.remote = remote

    def test_commit_then_ack_survives_store_recreation_and_never_reexecutes(self):
        call = self.call()
        dispatch = self.dispatch(call)
        self.assertEqual(dispatch["expected_instance_id"], "1" * 32)
        response = self.reply(call)
        self.store.observe(call, response)
        peer_projects = self.owned(ProjectStore(self.db, self.root))
        peer = ProjectBrowserStore(ProjectIOStore(peer_projects, ProjectUsageStore(peer_projects)))
        recovered = peer.find(self.access, key("first"), "browser_click", {"ref": "b1"})
        self.assertEqual(recovered, call)
        self.assertEqual(peer.receipt(self.access, call)["response"], response)
        with self.assertRaises(ProjectConflict):
            peer.dispatch(self.access, call, "browser_click", {"ref": "b1"})
        with self.assertRaises(ProjectConflict):
            self.call(text="second", store=peer)
        self.assertEqual(peer.ack_request(self.access, call)["response_sha256"], digest(response))
        self.ack(call, store=peer)
        self.assertEqual(self.call(text="second", store=peer).sequence, 2)
        self.assertEqual(self.io.receipt(self.access, self.grant)["state"], "dispatched")

    def test_drain_and_physical_quiescence_are_both_required_for_completion(self):
        call = self.call()
        self.dispatch(call)
        response = self.reply(call)
        self.store.observe(call, response)
        before = self.project()["version"]
        with self.assertRaises(ProjectConflict):
            self.store.start_drain(self.access, self.grant)
        self.ack(call)
        with self.assertRaises(ProjectConflict):
            self.io.complete(self.grant, {"ok": True}, self.observation(self.grant))
        self.store.start_drain(self.access, self.grant)
        with self.assertRaises(ProjectConflict):
            self.call(text="after drain intent")
        with self.assertRaises(ProjectConflict):
            self.store.drained(self.grant, self.remote)
        self.remote["state"] = "closed"
        self.store.drained(self.grant, self.remote)
        with self.assertRaises(ProjectConflict):
            self.io.complete(self.grant, {"ok": True}, {**self.observation(self.grant), "quiescent": False})
        self.io.complete(self.grant, {"ok": True}, self.observation(self.grant))
        self.io.complete(self.grant, {"ok": True}, self.observation(self.grant))
        self.assertEqual(self.project()["version"], before + 1)
        self.assertEqual(self.store.receipt(self.access, call)["response"], response)
        self.assertFalse(self.usage.status(self.access, self.pid)["complete"])

    def test_unknown_reply_or_transport_loss_never_becomes_permission_to_retry(self):
        call = self.call()
        self.dispatch(call)
        with self.assertRaises(ProjectConflict):
            self.dispatch(call)
        with self.assertRaises(ProjectConflict):
            self.store.ack_request(self.access, call)
        self.store.observe(call, self.reply(call, unknown=True))
        for operation in (
            lambda: self.call(text="next"), lambda: self.store.ack_request(self.access, call),
            lambda: self.store.start_drain(self.access, self.grant),
            lambda: self.store.observe(call, self.reply(call)),
            lambda: self.io.abort(self.grant),
        ):
            with self.assertRaises(ProjectConflict):
                operation()

    def test_typed_input_is_digest_only_and_changed_args_or_replies_are_rejected(self):
        args = {"ref": "b2", "text": "fixture-private-password", "submit": True}
        call = self.call("browser_type", args)
        row = self.store.receipt(self.access, call)
        self.assertNotIn("fixture-private-password", json.dumps(row))
        with self.assertRaises(ProjectConflict):
            self.dispatch(call, "browser_type", {**args, "text": "changed"})
        self.dispatch(call, "browser_type", args)
        response = self.reply(call)
        for changes in ({"sequence": True}, {"sequence": 2}, {"worker_id": "6" * 32},
                        {"arguments_sha256": "0" * 64}, {"state": "unknown"},
                        {"binding": {**self.remote["binding"], "fence": True}},
                        {"binding": {**self.remote["binding"], "instance_id": "9" * 32}}):
            with self.subTest(changes=changes), self.assertRaises((ProjectConflict, ValueError)):
                self.store.observe(call, {**response, **changes})
        self.store.observe(call, response)
        with self.assertRaises(ProjectConflict):
            self.store.observe(call, {**response, "result": "changed"})
        with self.assertRaises(ProjectConflict):
            self.store.find(self.access, key("first"), "browser_type", {**args, "text": "changed"})

    def test_scope_actor_bot_membership_and_editor_guard_even_saved_receipts(self):
        call = self.call()
        for access in (self.b, replace(self.access, member=False),
                       replace(self.access, scope=replace(self.scope, bot_native_user_id="302")),
                       replace(self.access, scope=replace(self.scope, native_conversation_id="999")),
                       replace(self.access, scope=replace(self.scope, kind="private", native_conversation_id="201"))):
            with self.subTest(access=access), self.assertRaises(PermissionError):
                self.store.receipt(access, call)
            with self.subTest(access=access), self.assertRaises(PermissionError):
                self.store.dispatch(access, call, "browser_click", {"ref": "b1"})
        self.sql.execute("UPDATE projects SET creator_principal_id=? WHERE project_id=?", (self.other, self.pid))
        with self.assertRaises(PermissionError):
            self.store.receipt(self.access, call)

    def test_find_uses_original_project_not_new_focus(self):
        call = self.call()
        second = self.projects.create(self.access, "Other project")
        self.projects.open(self.access, second["project_id"])
        self.assertEqual(self.store.find(self.access, key("first"), "browser_click", {"ref": "b1"}), call)
        self.assertEqual(self.store.find(self.b, key("first"), "browser_click", {"ref": "b1"}), None)
        with self.assertRaises(ProjectConflict):
            self.call(text="new call on old project")

    def test_finished_turn_can_recover_and_drain_but_not_dispatch(self):
        call = self.call()
        self.dispatch(call)
        self.journal.finish_turn(self.turns[self.principal].turn_id, status="succeeded")
        self.store.observe(call, self.reply(call))
        self.ack(call)
        with self.assertRaises(ProjectConflict):
            self.call(text="too late")
        self.store.start_drain(self.access, self.grant)

    def test_fenced_workspace_cannot_dispatch_and_receipt_cannot_change_runner(self):
        call = self.call()
        with self.assertRaises(ProjectConflict):
            self.store.bind(self.access, self.grant, "f" * 32)
        self.sql.execute("UPDATE projects SET workspace_generation=workspace_generation+1 WHERE project_id=?", (self.pid,))
        with self.assertRaises(ProjectConflict):
            self.dispatch(call)
        with self.assertRaises(ProjectConflict):
            self.store.start_drain(self.access, self.grant)

    def test_lost_ack_replays_identically_and_cannot_admit_next_action_early(self):
        call = self.call()
        self.dispatch(call)
        self.store.observe(call, self.reply(call))
        expected = self.store.ack_request(self.access, call)
        with self.assertRaises(ProjectConflict):
            self.call(text="early")
        self.ack(call)
        self.ack(call)
        self.assertEqual(self.store.ack_request(self.access, call), expected)
        second = self.call(text="second")
        with self.assertRaises(ProjectConflict):
            self.store.ack_request(self.access, call)
        self.assertEqual(second.sequence, 2)

    def test_ready_or_closed_record_cannot_forge_other_binding_or_ack(self):
        call = self.call()
        self.dispatch(call)
        self.store.observe(call, self.reply(call))
        self.ack(call)
        for changes in ({"session_id": "0" * 32}, {"worker_id": "0" * 32},
                        {"runtime": {**self.remote["runtime"], "runtime_id": "0" * 64}},
                        {"launch_id": "0" * 32}, {"sequence": True},
                        {"last": {**self.remote["last"], "acknowledged": 1}},
                        {"last": {**self.remote["last"], "response": None}}):
            with self.subTest(changes=changes), self.assertRaises(ProjectConflict):
                self.store.acknowledged(call, {**self.remote, **changes})
        self.store.start_drain(self.access, self.grant)
        with self.assertRaises(ProjectConflict):
            self.store.drained(self.grant, {**self.remote, "state": "closed", "sequence": 0, "last": None})

    def test_two_connections_cannot_reserve_two_actions(self):
        stores = [self.owned(ProjectStore(self.db, self.root)) for _ in range(2)]
        barrier = threading.Barrier(2)
        def reserve(index):
            store = ProjectBrowserStore(ProjectIOStore(stores[index], ProjectUsageStore(stores[index])))
            barrier.wait(timeout=5)
            try:
                return self.call(text=f"race-{index}", store=store)
            except ProjectConflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(reserve, range(2)))
        self.assertEqual(sum(result is not None for result in outcomes), 1)
        self.assertEqual(self.sql.execute("SELECT count(*) FROM project_browser_calls").fetchone()[0], 1)

    def test_database_blocks_two_pending_calls_and_unsafe_downgrade(self):
        self.call()
        with self.assertRaises(DatabaseError), self.projects._transaction() as cursor:
            cursor.execute("""INSERT INTO project_browser_calls(operation_id,sequence,request_key,method,
            arguments_sha256,state,created_at,updated_at) SELECT operation_id,sequence+1,?,method,
            arguments_sha256,state,created_at,updated_at FROM project_browser_calls""", (key("second"),))
        with patch.dict(os.environ, {"AI_POSTGRES_DSN": os.environ["TEST_POSTGRES_DSN"], "AI_POSTGRES_SCHEMA": self.db.schema}):
            with self.assertRaisesRegex(RuntimeError, "Export project browser"):
                command.downgrade(Config("alembic.ini"), "0053_project_restore")
            self.db.require_revision(HEAD_REVISION)

    def test_abandon_only_never_dispatched_action_and_drain_without_sequence_gap(self):
        first = self.call()
        self.dispatch(first)
        self.store.observe(first, self.reply(first))
        self.ack(first)
        second = self.call(text="never sent")
        self.assertEqual(self.store.pending(self.access, self.grant), second)
        with self.assertRaises(ProjectConflict):
            self.store.abandon_reserved(self.access, first)
        self.store.abandon_reserved(self.access, second)
        self.store.abandon_reserved(self.access, second)
        self.assertIsNone(self.store.pending(self.access, self.grant))
        with self.assertRaises(ProjectConflict):
            self.dispatch(second)
        with self.assertRaises(ProjectConflict):
            self.store.observe(second, self.reply(second))
        with self.assertRaises(ProjectConflict):
            self.call(text="not allowed after abandonment")
        self.store.start_drain(self.access, self.grant)
        self.remote["state"] = "closed"
        self.store.drained(self.grant, self.remote)
        self.io.complete(self.grant, {"ok": True}, self.observation(self.grant))
        self.assertEqual(self.store.receipt(self.access, second)["state"], "aborted")

    def test_abort_first_admission_before_dispatch_allows_empty_session_drain(self):
        call = self.call()
        self.store.abandon_reserved(self.access, call)
        self.store.start_drain(self.access, self.grant)
        self.remote["state"] = "closed"
        self.store.drained(self.grant, self.remote)
        self.io.complete(self.grant, {"ok": True}, self.observation(self.grant))
