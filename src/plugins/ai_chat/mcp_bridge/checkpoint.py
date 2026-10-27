"""Freeze the exact tool boundary; never re-interpret an old user request."""

from __future__ import annotations

from copy import deepcopy
import json

from .contracts import canonical, tool_name

MAX_CHECKPOINT_BYTES = 2 * 1024 * 1024


def validate_transcript(messages):
    if not isinstance(messages, list) or not 1 <= len(messages) <= 512:
        raise ValueError("MCP continuation requires a bounded conversation transcript")
    if len(canonical(messages).encode()) > MAX_CHECKPOINT_BYTES:
        raise ValueError("MCP continuation transcript is too large")
    pending, seen = {}, set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "tool"}:
            raise ValueError("MCP continuation cannot persist new system authority")
        role = message["role"]
        if role == "tool":
            identifier = message.get("tool_call_id")
            if not isinstance(identifier, str) or identifier not in pending:
                raise ValueError("MCP transcript has an orphan or duplicate tool result")
            if not isinstance(message.get("content"), str):
                raise ValueError("MCP tool receipts must be text")
            del pending[identifier]
            continue
        if pending:
            raise ValueError("MCP transcript has unfinished earlier tool calls")
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list) or len(calls) > 64 or (calls and role != "assistant"):
            raise ValueError("Invalid MCP transcript tool batch")
        for call in calls:
            if not isinstance(call, dict) or call.get("type") != "function":
                raise ValueError("Invalid MCP transcript tool call")
            identifier, function = call.get("id"), call.get("function")
            if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 256
                    or identifier in seen or not isinstance(function, dict)
                    or not isinstance(function.get("name"), str)
                    or not 1 <= len(function["name"]) <= 128
                    or not isinstance(function.get("arguments"), str)):
                raise ValueError("Invalid or reused MCP transcript tool identity")
            arguments = json.loads(function["arguments"])
            if not isinstance(arguments, dict):
                raise ValueError("MCP tool arguments must be an object")
            canonical(arguments)  # Reject NaN/infinity before recording an effect.
            seen.add(identifier)
            pending[identifier] = (function["name"], arguments)
    return pending


def freeze_checkpoint(messages, call, *, question, profile, message_id, message_time,
                      reply=None, system_context="", history_length=0):
    """Finish the interrupted batch with explicit non-execution receipts.

    Only the current MCP call may resume after approval. Remaining calls from
    that batch have not run and must be reconsidered, not silently replayed.
    """
    messages = deepcopy(messages)
    pending = validate_transcript(messages)
    if not pending:
        raise ValueError("MCP checkpoint has no pending tool call")
    identifier, (name, arguments) = next(iter(pending.items()))
    if type(history_length) is not int or not 0 <= history_length < len(messages):
        raise ValueError("Invalid MCP current-turn boundary")
    if not any(identifier == item.get("id") for message in messages[history_length:]
               for item in message.get("tool_calls", [])):
        raise ValueError("MCP approved call cannot belong to previous conversation history")
    if (name != tool_name(call["server_name"], call["remote_name"])
            or canonical(arguments) != canonical(call["binding"]["arguments"])):
        raise ValueError("MCP checkpoint is not at the approved tool boundary")
    if (not isinstance(question, str) or not 1 <= len(question) <= 64000
            or type(message_id) is not int or type(message_time) is not int or message_time <= 0
            or not isinstance(profile, dict) or set(profile) != {"name", "provider", "model"}
            or any(not isinstance(value, str) or not 1 <= len(value) <= 512 for value in profile.values())):
        raise ValueError("Invalid MCP continuation origin or model profile")
    for remaining in pending:
        result = ({"ok": False, "status": "awaiting_approval", "call_id": call["call_id"]}
                  if remaining == identifier else
                  {"ok": False, "executed": False, "code": "deferred_batch_not_executed"})
        messages.append({"role": "tool", "tool_call_id": remaining, "content": canonical(result)})
    checkpoint = {"messages": messages, "tool_call_id": identifier, "question": question,
                  "profile": deepcopy(profile), "message_id": message_id, "message_time": message_time,
                  "reply": deepcopy(reply or {}), "system_context": system_context, "history_length": history_length}
    if not isinstance(checkpoint["reply"], dict) or not isinstance(system_context, str) or len(system_context) > 64000:
        raise ValueError("Invalid MCP saved reply or host prompt")
    if len(canonical(checkpoint).encode()) > MAX_CHECKPOINT_BYTES:
        raise ValueError("MCP checkpoint is too large")
    return checkpoint


def resume_transcript(checkpoint, call):
    messages = deepcopy(checkpoint["messages"])
    if validate_transcript(messages):
        raise ValueError("Cannot resume an unfinished MCP batch")
    for message in messages:
        if message.get("role") == "tool" and message.get("tool_call_id") == checkpoint["tool_call_id"]:
            message["content"] = canonical({"call_id": call["call_id"], "status": call["status"],
                "ok": call["status"] == "succeeded", "executed": call["status"] == "succeeded",
                "dispatch_started": call["dispatched_at"] is not None,
                "needs_attention": call["status"] == "needs_attention", "result": call["result"]})
            return messages
    raise ValueError("MCP checkpoint lost its pending result slot")
