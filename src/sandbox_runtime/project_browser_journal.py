"""Host-private browser intents; never mounted into a project workspace.

One unacknowledged result is retained until the application has durably saved it.
A lease or result does not expire merely because a process/connection vanished.
"""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import stat
import time
import uuid

from src.local_runtime.files import file_lock, read_json, write_json
from .project_browser_worker import FRAME_LIMIT, arguments, binding
from .wire import ProtocolError, encoded

HEX_ID = re.compile(r"[a-f0-9]{32}")
STATES = {"starting", "ready", "needs_attention", "closing", "closed"}


def _digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def _private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077):
        raise ProtocolError("Browser journal directory must be private and host-owned")
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _runtime(value):
    if (not isinstance(value, dict) or set(value) != {"backend", "sandbox_id", "runtime_id"}
            or value["backend"] not in {"oci", "vm"}
            or not isinstance(value["sandbox_id"], str)
            or re.fullmatch(r"s[a-f0-9]{6}", value["sandbox_id"]) is None
            or not isinstance(value["runtime_id"], str)):
        raise ProtocolError("Invalid browser runtime binding")
    identity = value["runtime_id"]
    if value["backend"] == "oci":
        valid = re.fullmatch(r"[a-f0-9]{64}", identity) is not None
    else:
        try:
            valid = str(uuid.UUID(identity)) == identity
        except ValueError:
            valid = False
    if not valid:
        raise ProtocolError("Browser runtime needs an immutable container/domain identity")
    return dict(value)


class ProjectBrowserJournal:
    def __init__(self, root, instance_id):
        if not isinstance(instance_id, str) or HEX_ID.fullmatch(instance_id) is None:
            raise ValueError("Invalid browser journal instance")
        self.root, self.instance_id = Path(root).absolute(), instance_id
        _private_directory(self.root)
        self.root = self.root.resolve()

    @contextmanager
    def _locked_project(self, project_id):
        if type(project_id) is not int or not 1 <= project_id < 2**53:
            raise ProtocolError("Invalid browser project identity")
        lock = self.root / ".browser.lock"
        if lock.exists() or lock.is_symlink():
            self._private_file(lock, limit=1)
        with file_lock(lock, timeout=10):
            path = self.root / f"p{project_id}.json"
            record = None
            if path.exists() or path.is_symlink():
                self._private_file(path)
                record = read_json(path)
                if (record.get("schema") != 1 or record.get("state") not in STATES
                        or not isinstance(record.get("session_id"), str)
                        or HEX_ID.fullmatch(record["session_id"]) is None
                        or not isinstance(record.get("config_sha256"), str)
                        or re.fullmatch(r"[a-f0-9]{64}", record["config_sha256"]) is None
                        or type(record.get("sequence")) is not int or record["sequence"] < 0):
                    raise ProtocolError("Invalid browser session journal")
                if record.get("launch_id") is not None and (
                        not isinstance(record["launch_id"], str)
                        or HEX_ID.fullmatch(record["launch_id"]) is None):
                    raise ProtocolError("Invalid browser launch identity")
                bound = binding(record["binding"])
                _runtime(record["runtime"])
                if bound["instance_id"] != self.instance_id or bound["project_id"] != project_id:
                    raise ProtocolError("Browser journal belongs to another instance or project")
                last = record.get("last")
                if record["sequence"] == 0:
                    if last is not None:
                        raise ProtocolError("Unexpected browser action receipt")
                elif (not isinstance(last, dict)
                        or set(last) != {"arguments_sha256", "method", "state", "acknowledged", "response"}
                        or not isinstance(last["arguments_sha256"], str)
                        or re.fullmatch(r"[a-f0-9]{64}", last["arguments_sha256"]) is None
                        or last["state"] not in {"dispatched", "completed", "needs_attention"}
                        or type(last["acknowledged"]) is not bool
                        or (last["acknowledged"] and (last["state"] != "completed"
                                                     or not isinstance(last["response"], dict)))):
                    raise ProtocolError("Invalid browser action receipt")
            yield path, record

    @contextmanager
    def _locked(self, identity, session_id=None):
        identity = binding(identity)
        if identity["instance_id"] != self.instance_id:
            raise ProtocolError("Browser journal belongs to another instance")
        if session_id is not None and (not isinstance(session_id, str)
                                      or HEX_ID.fullmatch(session_id) is None):
            raise ProtocolError("Invalid browser session identity")
        with self._locked_project(identity["project_id"]) as (path, record):
            if record is not None:
                if record["binding"]["owner"] != identity["owner"]:
                    raise ProtocolError("Browser journal belongs to another project owner")
            if session_id is not None and (
                    record is None or record["session_id"] != session_id or record["binding"] != identity):
                raise ProtocolError("Browser session was replaced or fenced")
            yield path, record

    @staticmethod
    def _private_file(path, *, limit=FRAME_LIMIT + 16384):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & 0o077
                or info.st_size > limit):
            raise ProtocolError("Unsafe browser journal file")

    @staticmethod
    def _save(path, record):
        record["updated_at"] = int(time.time())
        if len(encoded(record)) > FRAME_LIMIT + 8192:
            raise ProtocolError("Browser journal exceeds its bounded receipt size")
        write_json(path, record, compact=True)

    def begin(self, config, runtime, *, launch_id=None):
        identity, runtime = binding(config["binding"]), _runtime(runtime)
        if launch_id is not None and (not isinstance(launch_id, str) or HEX_ID.fullmatch(launch_id) is None):
            raise ProtocolError("Invalid browser launch identity")
        if len(encoded(config)) > FRAME_LIMIT:
            raise ProtocolError("Browser bootstrap exceeds its limit")
        digest = _digest(config)
        with self._locked(identity) as (path, old):
            if old is not None:
                if launch_id is not None and old.get("launch_id") == launch_id:
                    raise ProtocolError("Browser launch already exists; inspect its original session")
                if old["state"] != "closed":
                    raise ProtocolError("Previous browser still owns this workspace; reconcile it first")
                if old["last"] and not old["last"]["acknowledged"]:
                    raise ProtocolError("Previous browser result has not been durably acknowledged")
                if (identity["generation"] < old["binding"]["generation"]
                        or identity["fence"] < old["binding"]["fence"]):
                    raise ProtocolError("Stale browser workspace generation or fence")
            record = {"schema": 1, "binding": identity, "runtime": runtime,
                      "session_id": uuid.uuid4().hex, "config_sha256": digest,
                      "state": "starting", "worker_id": None, "sequence": 0, "last": None}
            if launch_id is not None:
                record["launch_id"] = launch_id
            self._save(path, record)
            return deepcopy(record)

    def current(self, identity, session_id=None):
        with self._locked(identity, session_id) as (_, record):
            return deepcopy(record)

    def attach(self, identity, session_id, ready):
        with self._locked(identity, session_id) as (path, record):
            if (record["state"] != "starting" or not isinstance(ready, dict)
                    or set(ready) != {"schema", "binding", "worker_id", "state"}
                    or type(ready["schema"]) is not int or ready["schema"] != 1
                    or ready["state"] != "ready" or binding(ready["binding"]) != identity
                    or not isinstance(ready["worker_id"], str)
                    or HEX_ID.fullmatch(ready["worker_id"]) is None):
                raise ProtocolError("Browser handshake does not match its launch intent")
            record.update(state="ready", worker_id=ready["worker_id"])
            self._save(path, record)

    def prepare(self, identity, session_id, sequence, method, args):
        args = arguments(method, args)
        digest = _digest({"method": method, "args": args})
        if type(sequence) is not int or not 1 <= sequence < 2**53:
            raise ProtocolError("Invalid browser operation sequence")
        with self._locked(identity, session_id) as (path, record):
            last = record["last"]
            if last and sequence == record["sequence"]:
                if last["arguments_sha256"] != digest:
                    raise ProtocolError("Browser operation identity was reused")
                return None, deepcopy(last["response"])
            if (record["state"] != "ready" or sequence != record["sequence"] + 1
                    or (last and not last["acknowledged"])):
                raise ProtocolError("Browser is busy, uncertain or has an unacknowledged result")
            record["sequence"] = sequence
            record["last"] = {"arguments_sha256": digest, "method": method,
                              "state": "dispatched", "acknowledged": False, "response": None}
            self._save(path, record)
            return {"schema": 1, "worker_id": record["worker_id"], "sequence": sequence,
                    "method": method, "args": args}, None

    def observe(self, identity, session_id, sequence, reply):
        with self._locked(identity, session_id) as (path, record):
            last = record["last"]
            expected = {"schema": 1, "binding": identity, "worker_id": record["worker_id"],
                        "sequence": sequence, "arguments_sha256": last["arguments_sha256"] if last else None}
            if (not last or type(sequence) is not int or sequence != record["sequence"]
                    or not isinstance(reply, dict)
                    or any(type(reply.get(k)) is not type(v) or reply[k] != v for k, v in expected.items())
                    or binding(reply["binding"]) != identity
                    or reply.get("state") not in {"completed", "needs_attention"}
                    or set(reply) != {*expected, "state", "result" if reply["state"] == "completed" else "error"}
                    or (reply["state"] == "needs_attention" and reply["error"] != "browser_outcome_unknown")
                    or len(encoded(reply)) > FRAME_LIMIT):
                raise ProtocolError("Browser response does not match the persisted intent")
            if last["response"] is not None:
                if last["response"] != reply:
                    raise ProtocolError("Browser response changed after observation")
                return
            if record["state"] not in {"ready", "needs_attention"}:
                raise ProtocolError("Browser is closing; reconcile its pending action separately")
            last.update(state=reply["state"], response=deepcopy(reply))
            if reply["state"] == "needs_attention":
                record["state"] = "needs_attention"
            self._save(path, record)

    def acknowledge(self, identity, session_id, sequence, response_sha256):
        """The caller must commit the application receipt before this acknowledgement."""
        with self._locked(identity, session_id) as (path, record):
            last = record["last"]
            if (type(sequence) is not int or sequence != record["sequence"] or not last
                    or last["state"] != "completed" or last["response"] is None
                    or _digest(last["response"]) != response_sha256):
                raise ProtocolError("Cannot acknowledge an unknown or changed browser outcome")
            last["acknowledged"] = True
            self._save(path, record)

    def uncertain(self, identity, session_id):
        with self._locked(identity, session_id) as (path, record):
            if record["state"] == "closed":
                return
            record["state"] = "needs_attention"
            if record["last"] and record["last"]["response"] is None:
                record["last"]["state"] = "needs_attention"
            self._save(path, record)

    def closing(self, identity, session_id, *, cancel_pending=False):
        if type(cancel_pending) is not bool:
            raise ProtocolError("Browser cancellation must be explicit")
        with self._locked(identity, session_id) as (path, record):
            if record["state"] != "closed":
                if not cancel_pending and (
                        record["worker_id"] is None
                        or (record["last"] and record["last"]["state"] != "completed")):
                    raise ProtocolError("Browser launch/action is unfinished; explicit cancellation required")
                record["state"] = "closing"
                self._save(path, record)
            return deepcopy(record)

    def closed(self, identity, session_id, evidence):
        """Only a host runtime observer may supply evidence, never worker stdout."""
        with self._locked(identity, session_id) as (path, record):
            expected = {"binding": identity, "runtime": record["runtime"],
                        "session_id": session_id, "processes_stopped": True}
            if (not isinstance(evidence, dict) or evidence != expected
                    or binding(evidence["binding"]) != identity
                    or _runtime(evidence["runtime"]) != record["runtime"]
                    or record["state"] not in {"closing", "closed"}
                    or type(evidence.get("processes_stopped")) is not bool):
                raise ProtocolError("Browser process shutdown has not been independently verified")
            record["state"] = "closed"
            self._save(path, record)

    def maintenance_ready(self, identity):
        with self._locked(identity) as (_, record):
            return self._maintenance_ready(record)

    @staticmethod
    def _maintenance_ready(record):
        return record is None or (
            record["state"] == "closed"
            and (record["last"] is None or record["last"]["acknowledged"] is True)
        )
