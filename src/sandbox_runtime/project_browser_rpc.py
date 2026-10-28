"""Allowlisted project browser RPC; transport delivery is never a durable ACK."""

import re

from .project_browser_journal import HEX_ID
from .project_browser_worker import arguments, binding
from .project_vm_io import _journal
from .wire import ProtocolError

TARGET = {"owner", "project_id", "generation", "fence"}
FIELDS = {
    "project_browser_open": (TARGET | {"launch_id"}, set()),
    "project_browser_status": (TARGET, {"launch_id", "session_id"}),
    "project_browser_execute": (TARGET | {"session_id", "sequence", "action", "arguments"}, set()),
    "project_browser_receive": (TARGET | {"session_id", "sequence"}, set()),
    "project_browser_ack": (TARGET | {"session_id", "sequence", "response_sha256"}, set()),
    "project_browser_drain": (TARGET | {"session_id"}, {"cancel_pending"}),
}
FIELDS = {method: (required, optional | {"expected_instance_id"})
          for method, (required, optional) in FIELDS.items()}


def validate(method, args, identifier):
    if "expected_instance_id" in args and (
            not isinstance(args["expected_instance_id"], str)
            or HEX_ID.fullmatch(args["expected_instance_id"]) is None):
        raise ProtocolError("Invalid expected browser runner identity")
    for key in ("session_id", "launch_id"):
        if key in args and (not isinstance(args[key], str) or HEX_ID.fullmatch(args[key]) is None):
            raise ProtocolError("Invalid project browser operation identity")
    if method == "project_browser_open" and args["launch_id"] != identifier:
        # The runner's non-expiring mutation tombstone prevents reuse after the
        # per-project journal has advanced to a newer session or generation.
        raise ProtocolError("Browser launch ID must equal its durable RPC request ID")
    if method == "project_browser_status" and len({"session_id", "launch_id"} & args.keys()) != 1:
        raise ProtocolError("Browser status requires exactly one launch or session identity")
    if "sequence" in args and (type(args["sequence"]) is not int or not 1 <= args["sequence"] < 2**53):
        raise ProtocolError("Invalid project browser sequence")
    if "response_sha256" in args and (not isinstance(args["response_sha256"], str)
                                      or re.fullmatch(r"[a-f0-9]{64}", args["response_sha256"]) is None):
        raise ProtocolError("Invalid project browser receipt hash")
    if "cancel_pending" in args and type(args["cancel_pending"]) is not bool:
        raise ProtocolError("Browser cancellation must be explicit")
    if method == "project_browser_execute":
        arguments(args["action"], args["arguments"])


async def dispatch(workspaces, method, args, *, capacity_check=None):
    if workspaces is None:
        raise ProtocolError("Project browser runtime is unavailable")
    return await dispatch_browser(workspaces.browser, workspaces.manager.namespace, method, args,
                                  capacity_check=capacity_check)


async def dispatch_browser(browser, instance_id, method, args, *, capacity_check=None):
    identity = binding({"instance_id": instance_id,
                        **{key: args[key] for key in TARGET}})
    if args.get("expected_instance_id", identity["instance_id"]) != identity["instance_id"]:
        raise ProtocolError("Browser runner instance changed; original receipt recovery is required")
    sid = args.get("session_id")

    def admission():
        if capacity_check is not None:
            from src.storage_admission import CapacityError
            try:
                capacity_check(256 * 1024**2)
            except CapacityError as error:
                raise ProtocolError(str(error)) from None

    if method == "project_browser_open":
        admission()
        return await browser.open(**{key: args[key] for key in TARGET}, launch_id=args["launch_id"])
    if method == "project_browser_status":
        record = await _journal(browser.journal.current, identity, sid)
        if record is not None and (record["binding"] != identity
                or ("launch_id" in args and record.get("launch_id") != args["launch_id"])):
            raise ProtocolError("Browser session belongs to another launch or workspace epoch")
        return record
    if method == "project_browser_execute":
        return await browser.execute(identity, sid, args["sequence"], args["action"],
                                     args["arguments"], admission=admission)
    if method == "project_browser_receive":
        return await browser.receive_pending(identity, sid, sequence=args["sequence"])
    if method == "project_browser_ack":
        return await browser.acknowledge(identity, sid, args["sequence"], args["response_sha256"])
    if method == "project_browser_drain":
        return await browser.drain(identity, sid, cancel_pending=args.get("cancel_pending", False))
    raise ProtocolError("Unsupported project browser operation")
