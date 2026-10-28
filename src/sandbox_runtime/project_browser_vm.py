"""KVM project browser: root QGA lifecycle plus a private virtio byte channel."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import logging
import os
import stat
import uuid
from xml.etree import ElementTree

from .errors import SandboxError
from .project_browser_host import BrowserPipe, ProjectBrowserHost
from .project_browser_worker import binding
from .project_vm_browser_guest import CHANNEL_NAME, CHROMIUM_PATH, HELPER_PATH, SOCKET_NAME, _unit
from .project_vm_io import _journal
from .wire import ProtocolError


class VmProjectBrowser:
    def __init__(self, runtime):
        self.runtime, self.manager, self.disks = runtime, runtime.manager, runtime.disks
        self.journal = self.disks.browsers
        self.host = ProjectBrowserHost(self.journal)
        self.sessions = {}

    async def health(self):
        return {"channel_ready": True, "project_browser_protocol": 1, "instance_id": self.disks.instance_id}

    async def project_browser(self, operation, *, request_id=None, **args):
        from .project_browser_rpc import FIELDS, dispatch_browser, validate
        method = "project_browser_" + operation
        if method not in FIELDS:
            raise ProtocolError("Unsupported project browser operation")
        required, optional = FIELDS[method]
        if not required <= args.keys() or args.keys() - required - optional:
            raise ProtocolError("Unexpected project browser arguments")
        if operation == "open" and request_id is None:
            request_id = args["launch_id"]
        validate(method, args, request_id)
        return await dispatch_browser(self, self.disks.instance_id, method, args,
                                      capacity_check=self.disks.capacity_check)

    @asynccontextmanager
    async def _workspace(self, identity, expected=None):
        identity = binding(identity)
        if identity["instance_id"] != self.disks.instance_id:
            raise SandboxError("Project browser belongs to another VM instance")
        async with self.manager.allocation_lock():
            observed = await _journal(self.disks.inspection_record, identity["owner"], identity["project_id"],
                                      generation=identity["generation"])
            boot, status = observed["boot"], observed["status"]
            if (boot is None or observed["retired"] or status["fence"] != identity["fence"]
                    or status["deleted"] or not status["attached"]):
                raise SandboxError("Project browser requires its current attached VM epoch")
            runtime = {"backend": "vm", "sandbox_id": boot["binding"].sandbox_id,
                       "runtime_id": boot["workspace"]["domain_uuid"]}
            if expected is not None and runtime != expected:
                raise SandboxError("Browser VM incarnation changed")
            manager = self.runtime.provisioner._manager(boot)
            async with manager.io_lock(runtime["sandbox_id"], timeout=0):
                await self._verify_vm(manager, boot)
                yield manager, boot, runtime

    async def _verify_vm(self, manager, boot):
        provisioner = self.runtime.provisioner
        await _journal(provisioner._verify_files, manager, boot)
        if await provisioner._observe(manager, boot) != "running":
            raise SandboxError("Project browser VM is not running")
        # Legacy QGA-only VMs remain valid project VMs, but cannot be silently
        # adopted by this adapter or have their frozen bootstrap rewritten.
        path = str(manager._directory(boot["binding"].sandbox_id) / SOCKET_NAME)
        for arguments in ((), ("--inactive",)):
            root = ElementTree.fromstring(await manager._virsh("dumpxml", boot["workspace"]["domain_uuid"], *arguments))
            channels = [c for c in root.findall("devices/channel")
                        if c.find("target") is not None and c.find("target").get("name") == CHANNEL_NAME]
            if (len(channels) != 1 or channels[0].get("type") != "unix"
                    or channels[0].find("source") is None
                    or channels[0].find("source").get("path") != path
                    or channels[0].find("source").get("mode") != "bind"):
                raise SandboxError("This VM has no verified private project browser channel")

    @staticmethod
    def _socket(manager, boot):
        path = manager._directory(boot["binding"].sandbox_id) / SOCKET_NAME
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise SandboxError("Unsafe project browser socket")
        return path, (info.st_dev, info.st_ino)

    async def _control(self, manager, boot, epoch, action, session=None):
        request = {"action": action, "epoch": epoch}
        if session is not None:
            request["session_id"] = session
        tasks = min(128, manager.pids_limit)
        if action in {"start", "stop"}:
            request["tasks"] = tasks
        code, output, _ = await manager._guest_exec_raw(boot["binding"].sandbox_id,
            "/usr/bin/python3", ["-I", HELPER_PATH], input_bytes=json.dumps(request).encode(),
            timeout=390 if action == "prepare" else 45, domain_uuid=boot["workspace"]["domain_uuid"])
        if code:
            raise SandboxError("Guest browser " + action + " was not confirmed")
        try:
            if len(output) > 8192:
                raise ValueError()
            result = json.loads(output)
            expected = {k: boot["binding"].guest_config()[k]
                        for k in ("project_id", "generation", "filesystem_uuid", "serial")}
            expected.update(epoch=epoch, verified=True)
            if action == "prepare":
                expected["toolchain_ready"] = True
                fields = set(expected)
            else:
                expected.update(session_id=session, unit=_unit(session), tasks=tasks)
                fields = {*expected, "state", "unit_present", "unit_active", "cgroup_empty", "admission_active"}
            if (not isinstance(result, dict) or set(result) != fields
                    or any(type(result.get(k)) is not type(v) or result[k] != v for k, v in expected.items())):
                raise ValueError()
            if action != "prepare" and (not isinstance(result["state"], str)
                    or result["state"] not in {"starting", "running", "closing", "closed"}
                    or any(type(result[k]) is not bool for k in (
                        "unit_present", "unit_active", "cgroup_empty", "admission_active"))):
                raise ValueError()
        except (ValueError, TypeError):
            raise SandboxError("Guest browser receipt does not match its frozen VM and epoch") from None
        await self._verify_vm(manager, boot)
        return result

    async def _connect(self, manager, boot):
        path, identity = self._socket(manager, boot)
        connecting = asyncio.create_task(asyncio.wait_for(asyncio.open_unix_connection(path), 10))
        try:
            reader, writer = await asyncio.shield(connecting)
        except asyncio.CancelledError:
            while not connecting.done():
                try:
                    await asyncio.shield(connecting)
                except asyncio.CancelledError:
                    continue
            if connecting.exception() is None:
                _, writer = connecting.result()
                writer.close()
                await writer.wait_closed()
            raise
        channel = BrowserPipe(reader, writer)
        try:
            if self._socket(manager, boot) != (path, identity):
                raise SandboxError("Project browser socket changed during connection")
            await self._verify_vm(manager, boot)
        except BaseException:
            await channel.eof()
            raise
        return channel

    async def open(self, owner, project_id, *, generation, fence, launch_id=None):
        identity = binding({"instance_id": self.disks.instance_id, "owner": owner,
                            "project_id": project_id, "generation": generation, "fence": fence})
        launch_id = launch_id or uuid.uuid4().hex
        previous = await _journal(self.journal.current, identity)
        if previous is not None and previous.get("launch_id") == launch_id:
            if previous["binding"] != identity:
                raise SandboxError("Browser retry changed its workspace binding")
            return previous
        async with self._workspace(identity) as (manager, boot, runtime):
            await self._control(manager, boot, fence, "prepare")
        config = {"schema": 1, "binding": identity, "workspace": "/workspace",
                  "executable_path": CHROMIUM_PATH, "allow_private_network": False,
                  "max_profile_bytes": min(256 * 1024**2, boot["binding"].quota_bytes),
                  "cache_bytes": 32 * 1024**2}

        async def launch(record):
            self.sessions[record["session_id"]] = deepcopy(record)
            # Session -> allocation -> VM IO, shared with execute/drain. Never
            # wait for the session lock while holding the allocation lock.
            async with self._workspace(identity, runtime) as (manager, boot, _):
                result = await self._control(manager, boot, fence, "start", record["session_id"])
                if not result["admission_active"] or result["state"] not in {"starting", "running"}:
                    raise SandboxError("Browser start was revoked")
                return await self._connect(manager, boot)

        record = await self.host.open(config, runtime, launch, launch_id=launch_id)
        try:
            await self._verify_session(record)
        except BaseException:
            await _journal(self.journal.uncertain, identity, record["session_id"])
            raise
        return record

    async def _verify_session(self, record):
        identity = record["binding"]
        async with self._workspace(identity, record["runtime"]) as (manager, boot, _):
            observed = await self._control(manager, boot, identity["fence"], "status", record["session_id"])
            if (observed["state"] != "running" or not observed["admission_active"]
                    or not observed["unit_present"] or not observed["unit_active"] or observed["cgroup_empty"]):
                raise SandboxError("Browser does not own an active, admitted guest process")

    async def execute(self, identity, session_id, sequence, method, args, *, persist=None, admission=None):
        async def verify():
            if admission is not None:
                admission()
            record = await _journal(self.journal.current, identity, session_id)
            if record["state"] != "ready":
                raise SandboxError("Browser was stopped or fenced before dispatch")
            await self._verify_session(record)
        return await self.host.execute(identity, session_id, sequence, method, args,
                                       persist=persist, before_dispatch=verify)

    async def receive_pending(self, identity, session_id, *, persist=None, sequence=None):
        return await self.host.receive_pending(identity, session_id, persist=persist, sequence=sequence)

    async def acknowledge(self, identity, session_id, sequence, response_sha256):
        return await self.host.acknowledge(identity, session_id, sequence, response_sha256)

    async def drain(self, identity, session_id, *, cancel_pending=False):
        async def stop(record, channel):
            async with self._workspace(identity, record["runtime"]) as (manager, boot, _):
                observed = await self._control(manager, boot, identity["fence"], "stop", session_id)
                if observed["state"] != "closed" or observed["unit_active"] or not observed["cgroup_empty"]:
                    raise SandboxError("Guest browser termination is unverified")
                if channel is not None:
                    await asyncio.wait_for(channel.eof(), 5)
                return {"binding": identity, "runtime": record["runtime"],
                        "session_id": session_id, "processes_stopped": True}
        result = await self.host.drain(identity, session_id, stop, cancel_pending=cancel_pending)
        self.sessions.pop(session_id, None)
        return result

    async def close(self):
        failures = []
        for sid, record in tuple(self.sessions.items()):
            try:
                await self.drain(record["binding"], sid, cancel_pending=True)
            except Exception:
                failures.append(sid)
                logging.getLogger(__name__).warning("Project VM browser shutdown unconfirmed: %s", sid)
            finally:
                channel = self.host.channels.pop(sid, None)
                if channel is not None:
                    try:
                        await asyncio.wait_for(channel.eof(), 5)
                    except (OSError, TimeoutError):
                        pass
                self.sessions.pop(sid, None)
        return failures
