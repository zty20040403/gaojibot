"""Gaoji SSH operation contracts; also installed on managed hosts."""
from __future__ import annotations

import re
from typing import Any

PROTOCOL = "gaoji-ssh-v1"
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
OPERATION_ID = re.compile(r"op_[a-f0-9]{32}")
UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,240}\.service")
SERVICE_ACTIONS = {"units.start", "units.stop", "units.restart", "units.reload"}
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "outcome_unknown"}
MAX_REQUEST = 256 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
LOG_LIMIT = 1024 * 1024


def handle(host: str, operation_id: str) -> str:
    if not HOST.fullmatch(host) or not OPERATION_ID.fullmatch(operation_id):
        raise ValueError("Invalid SSH operation identity")
    return f"ssh:{host}:{operation_id}"


def parse_handle(value: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ValueError("Invalid SSH job handle")
    parts = value.split(":")
    if len(parts) != 3 or parts[0] != "ssh" or handle(parts[1], parts[2]) != value:
        raise ValueError("Not a Gaoji SSH job; legacy jobs must not be replayed")
    return parts[1], parts[2]


def catalog() -> dict[str, Any]:
    string = {"type": "string"}
    host = {"type": "string", "pattern": HOST.pattern}
    unit = {"type": "string", "pattern": UNIT.pattern}
    job = {"type": "string", "pattern": r"^ssh:[A-Za-z0-9_-]+:op_[a-f0-9]{32}$"}
    positive = {"type": "integer", "minimum": 1}
    result = []

    def add(name: str, properties: dict, required: list[str], summary: str, write: bool = False) -> None:
        result.append({"name": name, "summary": summary, "read_only": not write,
            "kind": "job_submission" if write else "observation",
            "idempotency": "required" if write else "none", "provider": PROTOCOL,
            "params_schema": {"type": "object", "properties": properties,
                              "required": required, "additionalProperties": False}})

    add("fleet.overview", {}, [], "Fresh SSH observations of configured hosts; unavailable hosts are unknown")
    add("units.failed", {}, [], "List failed systemd units on configured hosts")
    for name in ("host.facts", "host.metrics"):
        add(name, {"host": host}, ["host"], "Read target identity, boot, resource and filesystem evidence")
    add("units.status", {"host": host, "unit": unit}, ["host", "unit"], "Read current systemd unit state")
    add("units.logs", {"host": host, "unit": unit,
        "lines": {**positive, "maximum": 500}, "since_seconds": {**positive, "maximum": 604800}},
        ["host", "unit"], "Read bounded systemd journal entries")
    for name in sorted(SERVICE_ACTIONS):
        add(name, {"host": host, "unit": unit, "expected_invocation_id": string},
            ["host", "unit"], "Authorized service action with durable before/after evidence", True)
    add("exec.run", {"host": host, "profile": {"type": "string", "enum": ["operator"]},
        "command": {"oneOf": [
            {"type": "object", "properties": {"argv": {"type": "array", "items": string,
                "minItems": 1, "maxItems": 256}}, "required": ["argv"], "additionalProperties": False},
            {"type": "object", "properties": {"script": {**string, "minLength": 1, "maxLength": 65536}},
                "required": ["script"], "additionalProperties": False}]},
        "cwd": string, "env": {"type": "object", "additionalProperties": string, "maxProperties": 128},
        "timeout_seconds": {**positive, "maximum": 3600}}, ["host", "profile", "command"],
        "Run an explicitly authorized command as a durable target systemd job", True)
    add("jobs.status", {"job_id": job}, ["job_id"], "Observe one existing job; never re-execute it")
    add("jobs.logs", {"job_id": job, "limit": {**positive, "maximum": 65536}}, ["job_id"], "Read persisted bounded output")
    add("jobs.list", {"host": host, "limit": {**positive, "maximum": 200}, "cursor": string}, ["host"], "Recover existing job handles")
    add("jobs.cancel", {"job_id": job, "expected_revision": positive, "reason": string},
        ["job_id", "expected_revision", "reason"], "Stop an existing job, not its already committed effects")
    result[-1].update(read_only=False, kind="job_control")
    return {"version": 2, "backend": PROTOCOL, "operations": result}
