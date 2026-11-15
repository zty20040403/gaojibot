from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src import host_control as hc


class HostControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.boot = self.root / "boot-id"
        self.boot.write_text("8d526a9e-ee0b-4ae2-ab51-1e5462c3f99d")
        self.config = {"host_id": "h310", "path": str(self.bin) + os.pathsep + os.environ.get("PATH", os.defpath),
            "default_cwd": str(self.root), "boot_id_file": str(self.boot),
            "receipt_directory": str(self.root / "receipts"),
            "shell": shutil.which("bash"), "systemctl": str(self.bin / "systemctl")}
        env = patch.dict(os.environ, {"PATH": self.config["path"], "HOME": str(self.root)}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.op = "op_" + "a" * 32

    def script(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o700)
        return path

    def intent(self, argv=None, **kwargs):
        return {"action": "exec", "host": "h310", "command": {"argv": argv or ["true"]}, **kwargs}

    def assertCheck(self, code, callback):
        with self.assertRaises(hc.CheckError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def execute(self, intent, evidence=None, operation=None):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = hc.execute(intent, evidence or hc.preflight(intent, self.config), operation or self.op, self.config)
        return result, output.getvalue()

    def test_preflight_does_not_execute(self):
        marker = self.root / "effect"
        program = self.script("effect", f"touch '{marker}'\n")
        result = hc.preflight(self.intent([str(program)]), self.config)
        self.assertFalse(marker.exists())
        self.assertTrue(result["ok"])
        self.assertEqual(result["evidence"]["programs"][0]["resolved"], str(program))
        self.assertEqual(result["evidence"]["uid"], os.geteuid())

    def test_missing_explicit_path_only_suggests(self):
        self.script("systemctl", "exit 0\n")
        error = self.assertCheck("program_unavailable", lambda: hc.preflight(
            self.intent(["/definitely-absent/systemctl", "reboot"]), self.config))
        self.assertEqual(error.details["suggested_program"], self.config["systemctl"])
        self.assertFalse(error.details["command_started"])
        self.assertFalse((self.root / "receipts").exists())

    def test_missing_non_executable_and_relative_programs(self):
        path = self.bin / "not-executable"
        path.write_text("data")
        for argv, code in [([str(path)], "program_unavailable"), (["absent"], "program_unavailable"),
                           (["./true"], "relative_program"), ([""], "invalid_program")]:
            with self.subTest(argv=argv):
                self.assertCheck(code, lambda: hc.preflight(self.intent(argv), self.config))

    def test_restricted_executor_path_uses_host_path_for_advice_only(self):
        marker = self.root / "unexpected-effect"
        program = self.script("systemctl", f"touch '{marker}'\n")
        restricted = self.root / "restricted-bin"
        restricted.mkdir()
        intent = self.intent(["/definitely-absent/systemctl", "reboot"], env={"PATH": str(restricted)})
        error = self.assertCheck("program_unavailable", lambda: hc.preflight(intent, self.config))
        self.assertEqual(error.details["suggested_program"], str(program))
        self.assertFalse(error.details["command_started"])
        self.assertEqual(intent["command"]["argv"][0], "/definitely-absent/systemctl")
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / "receipts").exists())
        self.assertCheck("program_unavailable", lambda: hc.preflight(
            self.intent(["systemctl", "reboot"], env={"PATH": str(restricted)}), self.config))
        self.assertFalse(marker.exists())

    def test_non_regular_executable_does_not_block(self):
        path = self.bin / "pipe"
        os.mkfifo(path, 0o700)
        self.assertCheck("invalid_executable", lambda: hc.fingerprint(str(path), self.config["path"]))

    def test_oversized_executable_rejected(self):
        program = self.script("large", "exit 0\n")
        with patch.object(hc, "MAX_EXECUTABLE_BYTES", 4):
            self.assertCheck("invalid_executable", lambda: hc.fingerprint(str(program), self.config["path"]))

    def test_bad_shebangs(self):
        cases = [("#!\n", "invalid_shebang"), ("#!/absent/interpreter\n", "program_unavailable"),
                 ("#!relative\n", "invalid_shebang"), ("#!/usr/bin/env missing-interpreter\n", "program_unavailable"),
                 ("#!/usr/bin/env -i bash\n", "unsupported_shebang")]
        for text, code in cases:
            path = self.script("script", "")
            path.write_text(text)
            with self.subTest(text=text):
                self.assertCheck(code, lambda: hc.preflight(self.intent([str(path)]), self.config))

    def test_env_split_shebang_checks_interpreter(self):
        path = self.script("script", "")
        path.write_text("#!/usr/bin/env -S sh -e\nexit 0\n")
        evidence = hc.preflight(self.intent([str(path)]), self.config)["evidence"]
        self.assertEqual(len(evidence["programs"]), 3)

    def test_invalid_identity_environment_cwd_and_request(self):
        cases = [(self.intent(host="other"), "wrong_host"),
            (self.intent(cwd="relative"), "invalid_cwd"),
            (self.intent(cwd=str(self.root / "missing")), "invalid_cwd"),
            (self.intent(cwd=str(self.boot)), "invalid_cwd"),
            (self.intent(env={"A-B": "x"}), "invalid_environment"),
            (self.intent(env={"X": "a\0b"}), "invalid_environment"),
            (self.intent(env={"PATH": ".:/bin"}), "invalid_path"),
            (self.intent(extra=True), "invalid_intent")]
        for intent, code in cases:
            with self.subTest(code=code, intent=intent):
                self.assertCheck(code, lambda: hc.preflight(intent, self.config))

    def test_script_syntax_and_no_bash_env_execution_in_preflight(self):
        marker = self.root / "injected"
        bash_env = self.root / "bash-env"
        bash_env.write_text(f"touch '{marker}'\n")
        intent = self.intent()
        intent.update(command={"script": "if then"}, env={"BASH_ENV": str(bash_env)})
        self.assertCheck("script_syntax", lambda: hc.preflight(intent, self.config))
        self.assertFalse(marker.exists())

    def test_argument_array_remains_literal(self):
        target = self.root / "arguments.json"
        hostile = ["hello world", "$(touch injected)", "; echo bad", "\"quotes\"", "a\nb", "中文"]
        argv = [sys.executable, "-c", "import json,sys;open(sys.argv[1],'w').write(json.dumps(sys.argv[2:]))", str(target), *hostile]
        code, output = self.execute(self.intent(argv))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(target.read_text()), hostile)
        self.assertFalse((self.root / "injected").exists())
        report = json.loads(output.removeprefix(hc.REPORT_PREFIX))
        self.assertEqual(report["operation_id"], self.op)

    def test_evidence_precedes_effect_and_receipt_is_durable(self):
        target = self.root / "effect"
        self.script("effect", f"touch '{target}'\n")
        intent = self.intent(["effect"])
        original = hc.subprocess.run
        def run(*args, **kwargs):
            receipt = json.loads((self.root / "receipts" / (self.op + ".json")).read_text())
            self.assertEqual(receipt["state"], "dispatching")
            self.assertEqual(receipt["preflight"]["evidence"]["intent_hash"], hc.digest(intent))
            return original(*args, **kwargs)
        evidence = hc.preflight(intent, self.config)
        with patch.object(hc.subprocess, "run", side_effect=run):
            code, _ = self.execute(intent, evidence)
        self.assertTrue(target.exists())
        self.assertEqual(code, 0)
        receipt = json.loads((self.root / "receipts" / (self.op + ".json")).read_text())
        self.assertEqual(receipt["state"], "finished")
        self.assertEqual(receipt["exit_code"], 0)

    def test_finished_operation_is_not_executed_twice(self):
        marker = self.root / "counter"
        self.script("count", f"echo x >> '{marker}'\nexit 7\n")
        intent = self.intent(["count"])
        proof = hc.preflight(intent, self.config)
        self.assertEqual(self.execute(intent, proof)[0], 7)
        code, replay = self.execute(intent, proof)
        self.assertEqual(code, 7)
        self.assertTrue(replay.startswith(hc.REPORT_PREFIX))
        self.assertEqual(marker.read_text(), "x\n")

    def test_selected_symlink_preserves_program_identity(self):
        target = self.root / "invoked-as"
        program = self.script("multicall", f"printf '%s' \"$0\" > '{target}'\n")
        alias = self.bin / "selected-tool"
        alias.symlink_to(program)
        intent = self.intent(["selected-tool"])
        proof = hc.preflight(intent, self.config)
        self.assertEqual(proof["evidence"]["programs"][0]["resolved"], str(program))
        self.assertEqual(self.execute(intent, proof)[0], 0)
        self.assertEqual(target.read_text(), str(alias))

    def test_malformed_receipts_never_reexecute(self):
        intent = self.intent()
        proof = hc.preflight(intent, self.config)
        root = hc.receipt_root(self.config)
        for content in ["[]", "{", json.dumps({"fingerprint": proof["fingerprint"], "state": "finished", "exit_code": 0})]:
            (root / (self.op + ".json")).write_text(content)
            with self.subTest(content=content), patch.object(hc.subprocess, "run") as run:
                error = self.assertCheck("invalid_receipt", lambda: self.execute(intent, proof))
                self.assertIsNone(error.details["command_started"])
                run.assert_not_called()

    def test_lost_receipt_is_unknown_not_repeated(self):
        intent = self.intent()
        proof = hc.preflight(intent, self.config)
        root = hc.receipt_root(self.config)
        hc.write_receipt(root / (self.op + ".json"), {"fingerprint": proof["fingerprint"], "state": "dispatching"})
        with patch.object(hc.subprocess, "run") as run:
            self.assertCheck("outcome_unknown", lambda: self.execute(intent, proof))
        run.assert_not_called()

    def test_changed_evidence_never_starts_command(self):
        mutations = [lambda: self.boot.write_text("9d526a9e-ee0b-4ae2-ab51-1e5462c3f99d"),
            lambda: os.environ.update(HOME="/different"),
            lambda: self.config.update(default_cwd="/"),
            lambda: self.script("mutable", "exit 4\n")]
        for index, mutation in enumerate(mutations):
            self.script("mutable", "exit 0\n")
            intent = self.intent(["mutable"])
            proof = hc.preflight(intent, self.config)
            mutation()
            with self.subTest(index=index), patch.object(hc.subprocess, "run") as run:
                self.assertCheck("preflight_changed", lambda: self.execute(intent, proof, "op_" + str(index) * 32))
                run.assert_not_called()

    def test_changed_intent_and_malformed_evidence_rejected(self):
        intent = self.intent()
        proof = hc.preflight(intent, self.config)
        for malformed in [None, {}, {"ok": True, "evidence": []}, {"ok": True, "evidence": None},
                          {**proof, "fingerprint": "bad"}]:
            self.assertCheck("invalid_evidence", lambda: hc.execute(intent, malformed, self.op, self.config))
        self.assertCheck("invalid_evidence", lambda: hc.execute(self.intent(["false"]), proof, self.op, self.config))
        self.assertCheck("invalid_operation_id", lambda: hc.execute(intent, proof, "../escape", self.config))

    def test_changed_target_identity_rejected(self):
        intent = self.intent()
        proof = hc.preflight(intent, self.config)
        with patch.object(hc.os, "getgroups", return_value=[999999]):
            self.assertCheck("preflight_changed", lambda: self.execute(intent, proof))

    def test_reboot_is_fixed_and_tested_with_a_fake_program(self):
        target = self.root / "reboot-args"
        self.script("systemctl", f"printf '%s' \"$*\" > '{target}'\n")
        intent = {"action": "reboot", "host": "h310", "reason": "test"}
        self.assertEqual(self.execute(intent)[0], 0)
        self.assertEqual(target.read_text(), "reboot")
        for key in ("command", "env", "cwd"):
            self.assertCheck("invalid_reboot", lambda: hc.preflight({**intent, key: "injected"}, self.config))

    def test_symlink_or_public_receipt_directory_rejected(self):
        root = self.root / "receipts"
        root.symlink_to(self.bin)
        self.assertCheck("unsafe_receipt_directory", lambda: hc.receipt_root(self.config))
        root.unlink()
        root.mkdir(mode=0o755)
        root.chmod(0o755)  # mkdir's mode is filtered by the caller's umask.
        self.assertEqual(root.stat().st_mode & 0o777, 0o755)
        self.assertCheck("unsafe_receipt_directory", lambda: hc.receipt_root(self.config))

    def test_systemd_state_directory_is_used_instead_of_private_tmp(self):
        state = self.root / "job-state"
        state.mkdir()
        with patch.dict(os.environ, {"STATE_DIRECTORY": str(state)}):
            self.assertEqual(hc.receipt_root(self.config), state / "gaoji-host-control")

    def test_executor_cgroup_resolves_state_after_environment_is_cleared(self):
        cgroup = self.root / "cgroup"
        job_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        cgroup.write_text(f"0::/system.slice/maxops-job-{job_id}.service\n")
        job_state = self.root / "jobs" / job_id
        job_state.mkdir(parents=True)
        self.config.update(cgroup_file=str(cgroup), job_state_root=str(self.root / "jobs"))
        with patch.dict(os.environ, {"STATE_DIRECTORY": "/untrusted"}):
            self.assertEqual(hc.receipt_root(self.config), job_state / "gaoji-host-control")

    def test_live_duplicate_is_rejected_without_waiting_or_reexecuting(self):
        import fcntl
        intent = self.intent()
        proof = hc.preflight(intent, self.config)
        root = hc.receipt_root(self.config)
        with (root / (self.op + ".lock")).open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with patch.object(hc.subprocess, "run") as run:
                self.assertCheck("outcome_unknown", lambda: self.execute(intent, proof))
                run.assert_not_called()

    def test_program_is_not_retried_after_spawn_failure(self):
        intent = self.intent()
        proof = hc.preflight(intent, self.config)
        with patch.object(hc.subprocess, "run", side_effect=FileNotFoundError("Interpreter vanished")) as run:
            self.assertCheck("spawn_failed", lambda: self.execute(intent, proof))
            self.assertEqual(self.execute(intent, proof)[0], 126)
            self.assertEqual(run.call_count, 1)

    def test_cli_run_and_structured_failure(self):
        config = self.root / "config.json"
        config.write_text(json.dumps(self.config))
        command = [sys.executable, "-I", str(Path(hc.__file__).resolve()), "--config", str(config), "--request-json"]
        request = {"phase": "run", "operation_id": self.op, "intent": self.intent()}
        result = subprocess.run([*command, json.dumps(request)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith(hc.REPORT_PREFIX))
        request["intent"] = self.intent(["/absent/tool"])
        result = subprocess.run([*command, json.dumps(request)], capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stderr)["code"], "program_unavailable")
        self.assertFalse(json.loads(result.stderr)["command_started"])


if __name__ == "__main__":
    unittest.main()
