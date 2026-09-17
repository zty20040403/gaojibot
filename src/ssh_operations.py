"""Fixed SSH target endpoint with durable systemd jobs, not an HTTP daemon.

Only the dedicated operations account may sudo this entry point. Requests arrive
on stdin, never interpolated into a shell command. State is root-owned; historical
receipts are tombstones and never authorize another execution.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from typing import Any

if __package__:
    from . import host_control as checks
    from .ssh_ops_protocol import (LOG_LIMIT, MAX_REQUEST, MAX_RESPONSE, OPERATION_ID,
        PROTOCOL, SERVICE_ACTIONS, TERMINAL, UNIT, handle, parse_handle)
else:
    import host_control as checks
    from ssh_ops_protocol import (LOG_LIMIT, MAX_REQUEST, MAX_RESPONSE, OPERATION_ID,
        PROTOCOL, SERVICE_ACTIONS, TERMINAL, UNIT, handle, parse_handle)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def command(config: dict, name: str, args: list[str], *, timeout: int = 20) -> str:
    argv = [config[name], *args]
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={"PATH": config["path"], "LC_ALL": "C"}, start_new_session=True)
    selector = selectors.DefaultSelector()
    stdout, stderr = bytearray(), bytearray()
    deadline = time.monotonic() + timeout
    try:
        selector.register(process.stdout, selectors.EVENT_READ, stdout)
        selector.register(process.stderr, selectors.EVENT_READ, stderr)
        while selector.get_map():
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _ in selector.select(timeout=0.1):
                chunk = os.read(key.fd, 16384)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                key.data.extend(chunk)
                checks.require(len(key.data) <= MAX_RESPONSE, "response_too_large", "Target query exceeded output limit")
        code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        checks.require(code == 0, "command_failed", stderr.decode(errors="replace")[:1000])
        return stdout.decode(errors="replace")
    finally:
        selector.close()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.stdout.close()
        process.stderr.close()
        process.wait()


def unit_status(config: dict, unit: str) -> dict[str, Any]:
    checks.require(isinstance(unit, str) and UNIT.fullmatch(unit) is not None,
                   "invalid_unit", "An exact .service unit is required")
    raw = command(config, "systemctl", ["show", unit, "--no-pager", "--property=" + ",".join([
        "LoadState", "ActiveState", "SubState", "InvocationID", "MainPID", "NRestarts",
        "ExecMainStatus", "ActiveEnterTimestamp", "ReloadResult", "Result"])])
    values = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    return {"unit": unit, "load_state": values.get("LoadState"), "active_state": values.get("ActiveState"),
        "sub_state": values.get("SubState"), "details": {
            "invocation_id": values.get("InvocationID") or None, "main_pid": int(values.get("MainPID") or 0),
            "restarts": int(values.get("NRestarts") or 0), "exec_main_status": int(values.get("ExecMainStatus") or 0),
            "active_enter_timestamp": values.get("ActiveEnterTimestamp"), "reload_result": values.get("ReloadResult"),
            "service_result": values.get("Result")}}


def state_summary(unit: dict) -> dict:
    return {**unit["details"], "active_state": unit["active_state"], "sub_state": unit["sub_state"]}


def filesystem_metrics(config: dict) -> dict:
    metrics = {"filesystem_size_bytes": {"samples": []}, "filesystem_available_bytes": {"samples": []}}
    for mount in config.get("mounts", ["/"]):
        data = os.statvfs(mount)
        sampled_at = time.time()
        for name, blocks in (("filesystem_size_bytes", data.f_blocks), ("filesystem_available_bytes", data.f_bavail)):
            metrics[name]["samples"].append({"labels": {"mountpoint": mount},
                "value": blocks * data.f_frsize, "state": "available", "sample_at_unix_seconds": sampled_at})
    return metrics


def cpu_counters() -> dict[str, tuple[int, int]]:
    counters = {}
    for line in Path("/proc/stat").read_text().splitlines():
        fields = line.split()
        if fields and re.fullmatch(r"cpu[0-9]+", fields[0]) and len(fields) >= 9:
            # Guest time is already counted in user/nice; do not count it twice.
            ticks = [int(value) for value in fields[1:9]]
            counters[fields[0][3:]] = (sum(ticks), ticks[3])
    return counters


def observation(config: dict, operation: str, params: dict) -> dict:
    host = config["host_id"]
    envelope = {"host": host, "observed_at": now()}
    if operation == "units.status":
        return {**envelope, "unit": unit_status(config, params.get("unit"))}
    if operation == "units.logs":
        unit = params.get("unit")
        checks.require(isinstance(unit, str) and UNIT.fullmatch(unit) is not None, "invalid_unit", "Invalid unit")
        lines, seconds = params.get("lines", 80), params.get("since_seconds", 3600)
        checks.require(type(lines) is int and 1 <= lines <= 500 and type(seconds) is int and 1 <= seconds <= 604800,
                       "invalid_request", "Invalid log bounds")
        raw = command(config, "journalctl", ["--no-pager", "--output=json", "--unit", unit,
            "--lines", str(lines), "--since", f"@{int(time.time()) - seconds}"])
        entries = [json.loads(line) for line in raw.splitlines()]
        return {**envelope, "unit": unit, "entries": entries}
    if operation in {"fleet.overview", "units.failed"}:
        raw = command(config, "systemctl", ["--failed", "--type=service", "--no-legend", "--plain", "--no-pager"])
        failed = []
        for line in raw.splitlines():
            fields = line.split(maxsplit=4)
            if len(fields) >= 4 and UNIT.fullmatch(fields[0]):
                failed.append(dict(zip(("unit", "load_state", "active_state", "sub_state"), fields[:4])))
        result = {**envelope, "status": "online", "state": "available", "units": failed,
            "agent": {"state": "reachable", "failed_units": len(failed), "observed_at": envelope["observed_at"]}}
        if operation == "fleet.overview":
            result["pressure"] = filesystem_metrics(config)
            result["resource_observation"] = {"source": "ssh", "sample_at_unix_seconds": time.time()}
        return result
    if operation == "host.facts":
        closure = Path("/run/current-system")
        return {**envelope, "facts": {"boot_id": Path(config["boot_id_file"]).read_text().strip(),
            "hostname": os.uname().nodename, "kernel": os.uname().release,
            "architecture": os.uname().machine, "uptime_seconds": float(Path("/proc/uptime").read_text().split()[0]),
            "system_closure": str(closure.resolve()) if closure.exists() else None}}
    if operation == "host.metrics":
        metrics = filesystem_metrics(config)
        def scalar(name, value):
            metrics[name] = {"samples": [{"value": value, "labels": {}, "state": "available",
                "sample_at_unix_seconds": time.time()}]}
        memory_names = {"MemTotal": "memory_total_bytes", "MemAvailable": "memory_available_bytes",
            "SwapTotal": "swap_total_bytes", "SwapFree": "swap_free_bytes"}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            if key in memory_names:
                scalar(memory_names[key], int(value.split()[0]) * 1024)
        for name, value in zip(("load1", "load5", "load15"), os.getloadavg()):
            scalar(name, value)
        before, started = cpu_counters(), time.monotonic()
        time.sleep(0.2)
        after = cpu_counters()
        window, sampled_at = time.monotonic() - started, time.time()
        samples = []
        for cpu in sorted(before.keys() & after.keys()):
            total, idle = (after[cpu][i] - before[cpu][i] for i in (0, 1))
            if total > 0 and 0 <= idle <= total:
                samples.append({"labels": {"cpu": cpu}, "value": idle / total, "state": "available",
                    "sample_at_unix_seconds": sampled_at})
        metrics["cpu_idle_seconds_per_second"] = {"samples": samples, "window_seconds": window}
        return {**envelope, "observation": {"source": "ssh", "metrics": metrics}}
    raise checks.CheckError("unsupported", "Unknown read-only operation")


class JobStore:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.root = Path(config["jobs_directory"])
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.root.lstat()
        checks.require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0,
                       "unsafe_state", "SSH job state must be private and owned by the execution identity")

    def directory(self, operation_id: str) -> Path:
        checks.require(isinstance(operation_id, str) and OPERATION_ID.fullmatch(operation_id) is not None,
                       "invalid_identity", "Invalid job identity")
        path = self.root / operation_id
        checks.require(not path.is_symlink(), "unsafe_state", "A job directory cannot be a symlink")
        return path

    def lock(self, operation_id: str):
        path = self.directory(operation_id)
        path.mkdir(exist_ok=True, mode=0o700)
        fd = os.open(path / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        stream = os.fdopen(fd, "w")
        fcntl.flock(stream, fcntl.LOCK_EX)
        return stream

    def read(self, operation_id: str) -> dict | None:
        path = self.directory(operation_id) / "receipt.json"
        if not path.exists():
            return None
        checks.require(not path.is_symlink(), "unsafe_state", "Receipt cannot be a symlink")
        with path.open("rb") as stream:
            raw = stream.read(MAX_REQUEST + 1)
        checks.require(len(raw) <= MAX_REQUEST, "invalid_receipt", "Receipt is too large")
        value = json.loads(raw)
        checks.require(isinstance(value, dict) and value["handle"]["job_id"] == handle(self.config["host_id"], operation_id),
                       "invalid_receipt", "Receipt identity mismatch")
        return value

    def write(self, operation_id: str, receipt: dict) -> None:
        receipt["updated_at"] = now()
        receipt["handle"]["revision"] += 1
        checks.write_receipt(self.directory(operation_id) / "receipt.json", receipt)


def validate_submission(config: dict, operation: str, params: dict) -> None:
    checks.require(params.get("host") == config["host_id"], "wrong_host", "Target does not match intent")
    if operation in SERVICE_ACTIONS:
        checks.require(set(params) <= {"host", "unit", "expected_invocation_id"}, "invalid_request", "Unknown service fields")
        unit = params.get("unit")
        checks.require(isinstance(unit, str) and UNIT.fullmatch(unit) is not None, "invalid_unit", "Invalid service")
        expected = params.get("expected_invocation_id")
        checks.require(expected is None or isinstance(expected, str) and re.fullmatch(r"[a-f0-9]{32}|", expected) is not None,
                       "invalid_baseline", "Invalid expected service instance")
    else:
        checks.require(operation == "exec.run" and params.get("profile") == "operator", "unsupported", "Unsupported execution profile")
        checks.require(set(params) <= {"host", "profile", "command", "timeout_seconds", "cwd", "env"},
                       "invalid_request", "Unknown execution fields")
        intent = {"host": config["host_id"], "action": "exec", "command": params.get("command")}
        checks.command_spec(intent, config)
        if "cwd" in params:
            checks.require(isinstance(params["cwd"], str) and Path(params["cwd"]).is_absolute(), "invalid_cwd", "Use an absolute cwd")
        checks.effective_environment({"env": params.get("env", {})}, config)
    timeout = params.get("timeout_seconds", 1800)
    checks.require(type(timeout) is int and 1 <= timeout <= 3600, "invalid_timeout", "Invalid execution timeout")


def submit(config: dict, operation: str, params: dict, operation_id: str) -> dict:
    validate_submission(config, operation, params)
    store = JobStore(config)
    with store.lock(operation_id):
        prior = store.read(operation_id)
        intent_hash = checks.digest({"operation": operation, "params": params})
        if prior:
            checks.require(prior["intent_hash"] == intent_hash, "operation_conflict", "Identity already belongs to a different command")
            return prior["handle"]
        receipt = {"handle": {"job_id": handle(config["host_id"], operation_id), "host": config["host_id"],
            "operation": operation, "state": "dispatching", "revision": 0}, "spec": params,
            "created_at": now(), "intent_hash": intent_hash, "result": None,
            "boot_id": Path(config["boot_id_file"]).read_text().strip()}
        # Commit before systemd dispatch. Ambiguous dispatch is never retried.
        store.write(operation_id, receipt)
        try:
            command(config, "systemd_run", ["--quiet", "--collect", "--unit", "gaoji-ssh-" + operation_id,
                "--service-type=exec", "--property=Restart=no", "--property=KillMode=control-group",
                "--property=UMask=0077", "--property=StandardOutput=null", "--property=StandardError=journal",
                f"--property=RuntimeMaxSec={params.get('timeout_seconds', 1800) + 30}",
                "--", config["entry"], "--run-job", operation_id])
        except (checks.CheckError, subprocess.TimeoutExpired, OSError) as exc:
            receipt["dispatch_error"] = str(exc)[:1000]
            # The worker may have started even if the submitter lost the ack.
            # Keep dispatching; observation checks the durable job before deciding.
            store.write(operation_id, receipt)
        return receipt["handle"]


def service_action(config: dict, operation: str, params: dict, *, checkpoint=None) -> dict:
    unit, action = params["unit"], operation.removeprefix("units.")
    before = state_summary(unit_status(config, unit))
    if "expected_invocation_id" in params:
        checks.require((before["invocation_id"] or "") == params["expected_invocation_id"],
                       "baseline_changed", "Service instance changed before dispatch")
    if checkpoint:
        checkpoint({"phase": "service_preflight", "before": before, "unit": unit, "action": action})
    # systemctl waits for its actual systemd job and checks the job result.
    # Merely seeing a DBus job disappear cannot distinguish success from failure.
    argv = [config["systemctl"], action, unit]
    command(config, "systemctl", [action, unit], timeout=90)
    after = state_summary(unit_status(config, unit))
    return {"unit": unit, "action": action, "success": True, "argv": argv, "exit_code": 0,
        "attribution": "systemctl_exit_and_target_observed", "before": before, "after": after}


def execute_command(config: dict, params: dict, directory: Path) -> tuple[int, bool, dict]:
    spec = params["command"]
    argv = spec.get("argv") or [config["shell"], "--noprofile", "--norc", "-c", spec["script"]]
    # An operation is already explicitly authorized. The model's environment is
    # passed only to its child, never to SSH, sudo or this receipt-writing helper.
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, cwd=params.get("cwd") or "/",
        env={"PATH": config["path"], "LC_ALL": "C.UTF-8", **params.get("env", {})})
    selector = selectors.DefaultSelector()
    outputs = []
    logs = {}
    timed_out = False
    deadline = time.monotonic() + params.get("timeout_seconds", 1800)
    try:
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            output = (directory / name).open("wb", buffering=0)
            outputs.append(output)
            logs[name] = {"observed_bytes": 0, "stored_bytes": 0, "truncated": False}
            selector.register(pipe, selectors.EVENT_READ, (output, logs[name]))
        while selector.get_map():
            if time.monotonic() >= deadline and not timed_out:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
            for key, _ in selector.select(timeout=0.2):
                chunk = os.read(key.fd, 16384)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                output, summary = key.data
                summary["observed_bytes"] += len(chunk)
                keep = chunk[:max(0, LOG_LIMIT - summary["stored_bytes"])]
                summary["truncated"] |= len(keep) < len(chunk)
                if keep:
                    output.write(keep)
                    os.fsync(output.fileno())
                    summary["stored_bytes"] += len(keep)
        # A command can close both output streams and keep running.
        try:
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            code = process.wait()
        return code, timed_out, logs
    finally:
        selector.close()
        for output in outputs:
            output.close()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def run_job(config: dict, operation_id: str) -> None:
    store = JobStore(config)
    with store.lock(operation_id):
        receipt = store.read(operation_id)
        checks.require(receipt is not None, "missing_receipt", "No submitted job")
        checks.require(receipt["handle"]["state"] == "dispatching", "already_started", "Never restart an existing execution")
        receipt["handle"]["state"] = "running"
        store.write(operation_id, receipt)
    try:
        params, operation = receipt["spec"], receipt["handle"]["operation"]
        if operation in SERVICE_ACTIONS:
            def checkpoint(evidence):
                with store.lock(operation_id):
                    current = store.read(operation_id)
                    current["result"] = evidence
                    store.write(operation_id, current)
            result = service_action(config, operation, params, checkpoint=checkpoint)
            state = "succeeded"
        else:
            code, timed_out, logs = execute_command(config, params, store.directory(operation_id))
            result = {"exit_code": code, "logs": logs}
            state = "timed_out" if timed_out else "succeeded" if code == 0 else "failed"
    except Exception as exc:
        result = {"error": str(exc)[:1000], "code": getattr(exc, "code", "execution_failed")}
        state = "outcome_unknown" if isinstance(exc, subprocess.TimeoutExpired) else "failed"
    with store.lock(operation_id):
        receipt = store.read(operation_id)
        # Cancellation may have killed the command; never claim it undid effects.
        receipt["handle"]["state"] = "cancelled" if receipt.get("cancel_requested") else state
        receipt["result"] = result
        store.write(operation_id, receipt)


def status(config: dict, operation_id: str) -> dict:
    store = JobStore(config)
    with store.lock(operation_id):
        receipt = store.read(operation_id)
        checks.require(receipt is not None, "not_found", "SSH job was not found; do not resubmit automatically")
        if receipt["handle"]["state"] not in TERMINAL:
            unit = unit_status(config, "gaoji-ssh-" + operation_id + ".service")
            live = unit.get("active_state") in {"active", "activating", "deactivating"}
            age = time.time() - datetime.fromisoformat(receipt["created_at"]).timestamp()
            if not live and age > 30:
                # No receipt plus a dead unit cannot prove the command never ran.
                receipt["handle"]["state"] = "cancelled" if receipt.get("cancel_requested") else "outcome_unknown"
                store.write(operation_id, receipt)
        return receipt


def dispatch(config: dict, request: dict) -> Any:
    checks.require(isinstance(request, dict) and request.get("protocol") == PROTOCOL
        and request.get("host") == config["host_id"], "wrong_host", "Wrong host or SSH protocol")
    operation, params = request.get("op"), request.get("params")
    checks.require(isinstance(operation, str) and isinstance(params, dict), "invalid_request", "Invalid operation")
    checks.require(params.get("host", config["host_id"]) == config["host_id"], "wrong_host", "Wrong target")
    if operation == "exec.run" or operation in SERVICE_ACTIONS:
        return submit(config, operation, params, request.get("idempotency_key"))
    if operation == "jobs.list":
        store = JobStore(config)
        limit, cursor = params.get("limit", 200), params.get("cursor", "")
        checks.require(type(limit) is int and 1 <= limit <= 200 and isinstance(cursor, str), "invalid_request", "Invalid page")
        names = sorted(p.name for p in store.root.iterdir() if OPERATION_ID.fullmatch(p.name) and p.name > cursor)
        jobs = [status(config, name) for name in names[:limit]]
        return {"jobs": jobs, "next_cursor": names[limit - 1] if len(names) > limit else None}
    if operation.startswith("jobs."):
        host, operation_id = parse_handle(params.get("job_id"))
        checks.require(host == config["host_id"], "wrong_host", "Job belongs to a different host")
        if operation == "jobs.status":
            return status(config, operation_id)
        store = JobStore(config)
        if operation == "jobs.logs":
            receipt = store.read(operation_id)
            checks.require(receipt is not None, "not_found", "Unknown job")
            limit = params.get("limit", 65536)
            checks.require(type(limit) is int and 1 <= limit <= 65536, "invalid_request", "Invalid log limit")
            result = {"job_id": params["job_id"], "encoding": "base64"}
            for stream in ("stdout", "stderr"):
                path = store.directory(operation_id) / stream
                checks.require(not path.is_symlink(), "unsafe_state", "Invalid log file")
                with path.open("rb") if path.exists() else open(os.devnull, "rb") as source:
                    data = source.read(limit + 1)
                result[stream + "_base64"] = base64.b64encode(data[:limit]).decode()
                saved = receipt.get("result", {}).get("logs", {}).get(stream, {})
                result[stream + "_truncated"] = len(data) > limit or saved.get("truncated", False)
                result[stream + "_capture_finished"] = stream in receipt.get("result", {}).get("logs", {})
            return result
        if operation == "jobs.cancel":
            with store.lock(operation_id):
                receipt = store.read(operation_id)
                checks.require(receipt is not None, "not_found", "Unknown job")
                checks.require(receipt["handle"]["revision"] == params.get("expected_revision"), "revision_conflict", "Job changed")
                if receipt["handle"]["state"] in TERMINAL:
                    return receipt
                receipt["cancel_requested"] = True
                receipt["cancellation_note"] = "Cancellation stops this job, not side effects already accepted by systemd or written to disk. Inspect the target before claiming rollback."
                store.write(operation_id, receipt)
            command(config, "systemctl", ["stop", "gaoji-ssh-" + operation_id + ".service"])
            return status(config, operation_id)
        raise checks.CheckError("unsupported", "Unknown job operation")
    return observation(config, operation, params)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-job")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_bytes())
    try:
        if args.run_job:
            run_job(config, args.run_job)
            return 0
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        checks.require(len(raw) <= MAX_REQUEST, "request_too_large", "Request exceeds 256 KiB")
        result = {"ok": True, "data": dispatch(config, json.loads(raw))}
    except (checks.CheckError, ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as exc:
        result = {"ok": False, "code": getattr(exc, "code", "target_error"), "error": str(exc)[:1000]}
    result.update(protocol=PROTOCOL, host=config["host_id"])
    body = checks.encoded(result)
    if len(body) > MAX_RESPONSE:
        body = checks.encoded({"protocol": PROTOCOL, "host": config["host_id"], "ok": False,
            "code": "response_too_large", "error": "Target response exceeds 2 MiB"})
    print(body.decode(), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
