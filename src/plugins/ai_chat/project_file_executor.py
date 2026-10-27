"""Host-only retained files and fenced dependency runs; never host shell commands."""

import asyncio
import hashlib
import inspect
import time
import uuid

from src.project_content import CHUNK_BYTES, MAX_VERSION_FILES, manifest_bytes
from .project_executor import DockerProjectBackend, _store
from .projects import ProjectAccess, ProjectConflict


class _RemoteFile:
    """Binary reader consumed by a store thread, bounded to one RPC chunk."""

    def __init__(self, loop, reader, size):
        self.loop, self.reader, self.size = loop, reader, size
        self.offset, self.deadline = 0, time.monotonic() + 300

    def read(self, maximum):
        if self.offset == self.size:
            return b""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Project file transfer exceeded its deadline")
        future = asyncio.run_coroutine_threadsafe(
            self.reader(self.offset, maximum), self.loop
        )
        try:
            data = future.result(timeout=min(90, remaining))
        except BaseException:
            future.cancel()
            raise
        if not data or self.offset + len(data) > self.size:
            raise ProjectConflict("Project transfer returned incomplete bytes")
        self.offset += len(data)
        return data


class DockerProjectFiles:
    operations = frozenset(
        {
            "snapshot_workspace",
            "restore_version",
            "prune_rebuildable",
            "rebuild_dependencies",
        }
    )

    def __init__(self, lifecycle, client, *, resolve_access):
        self.lifecycle, self.client = lifecycle, client
        self.projects, self.resolve_access = lifecycle.projects, resolve_access

    async def _context(self, job):
        project, run = await _store(self.lifecycle.context, job)
        access = self.resolve_access(project, run["actor_principal_id"])
        if inspect.isawaitable(access):
            access = await access
        if not isinstance(access, ProjectAccess):
            raise PermissionError("Current project membership is required")
        project, _ = await _store(self.lifecycle.context, job, access=access)
        return access, project

    async def _call(self, step, action, *, key="", **parameters):
        args = step["arguments"]
        identifier = (
            step["request_id"]
            if not key
            else uuid.uuid5(uuid.UUID(step["request_id"]), key).hex
        )
        result, body = await self.client.project_files(
            action,
            args["owner"],
            args["project_id"],
            generation=args["generation"],
            fence=args["fence"],
            reservation=args["reservation"],
            request_id=identifier,
            **parameters,
        )
        if not isinstance(result, dict) or any(
            type(result.get(k)) is not int or result[k] != args[k]
            for k in ("project_id", "generation", "fence")
        ):
            raise ProjectConflict("File receipt belongs to another project operation")
        expected = parameters.get("manifest_sha256")
        if expected is not None and result.get("manifest_sha256") != expected:
            raise ProjectConflict("File receipt belongs to another manifest")
        return result, body

    async def execute(self, job, step, access):
        if step["operation"] == "snapshot_workspace":
            return await self._snapshot(job, step)
        if step["operation"] == "restore_version":
            return await self._restore(job, step)
        if step["operation"] == "prune_rebuildable":
            return await self._prune(job, step)
        if step["operation"] == "rebuild_dependencies":
            return await self._rebuild(job, step)
        raise ValueError("Unsupported project file operation")

    async def _rebuild(self, job, step):
        args = step["arguments"]
        baseline = args["baseline"]
        spec = args["rebuild_spec"]
        if not baseline or not spec or spec["command"] != args["rebuild_command"]:
            raise ProjectConflict("Dependency rebuild has no frozen recipe")

        async def observe(operation):
            await self._context(job)
            result = await self.client.project_rebuild(
                operation,
                args["owner"],
                args["project_id"],
                generation=args["generation"],
                fence=args["fence"],
                reservation=args["reservation"],
                manifest_sha256=baseline["manifest_sha256"],
            )
            if (
                not isinstance(result, dict)
                or any(
                    type(result.get(k)) is not int or result[k] != args[k]
                    for k in ("project_id", "generation", "fence")
                )
                or result.get("manifest_sha256") != baseline["manifest_sha256"]
                or result.get("command") != spec["command"]
                or result.get("state")
                not in {
                    "prepared",
                    "created",
                    "starting",
                    "running",
                    "failed",
                    "completed",
                }
            ):
                raise ProjectConflict(
                    "Dependency receipt does not match the frozen operation"
                )
            await self._context(job)
            return result

        while True:
            observed = await observe("run")
            await _store(self.lifecycle.record_progress, job, step, observed)
            if observed["state"] == "failed":
                raise ProjectConflict(
                    observed.get("error") or "Dependency reconstruction failed"
                )
            if observed["state"] == "completed":
                checked = await observe("status")
                if (
                    checked.get("state") != "completed"
                    or checked.get("pending") is not False
                    or type(checked.get("exit_code")) is not int
                    or checked["exit_code"] != 0
                ):
                    raise ProjectConflict("Dependency completion was not verified")
                return DockerProjectBackend._evidence(
                    step,
                    checked,
                    rebuilt_version=baseline,
                    command=spec["command"],
                    exit_code=0,
                    finished_at=checked.get("finished_at"),
                )
            await asyncio.sleep(2)

    async def _snapshot(self, job, step):
        access, project = await self._context(job)
        observed, _ = await self._call(
            step, "snapshot", rebuild_command=step["arguments"]["rebuild_command"]
        )
        digest, count = observed.get("manifest_sha256"), observed.get("file_count")
        if (
            observed.get("pending") is not False
            or type(count) is not int
            or not 0 <= count <= MAX_VERSION_FILES
            or not isinstance(digest, str)
        ):
            raise ProjectConflict("Incomplete project snapshot manifest")
        entries = []
        while len(entries) < count:
            page, _ = await self._call(
                step,
                "page",
                key=f"page:{len(entries)}",
                manifest_sha256=digest,
                index=len(entries),
            )
            items = page.get("files")
            if (
                not isinstance(items, list)
                or not 1 <= len(items) <= 128
                or page.get("file_count") != count
                or page.get("next_index") != len(entries) + len(items)
                or len(entries) + len(items) > count
                or page.get("pending") is not False
            ):
                raise ProjectConflict("Snapshot pagination is inconsistent")
            entries.extend(items)
        document = {"format": observed.get("format"), "files": entries}
        if document["format"] == 2:
            document["rebuild"] = observed.get("rebuild")
        encoded = manifest_bytes(document, quota_bytes=project["quota_bytes"])
        if hashlib.sha256(encoded).hexdigest() != digest:
            raise ProjectConflict("Snapshot manifest checksum failed")
        staged = []
        for index, item in enumerate(entries):
            access, _ = await self._context(job)

            async def read(offset, maximum, *, index=index, item=item):
                length = min(
                    maximum,
                    CHUNK_BYTES,
                    self.client.max_file_bytes,
                    item["size"] - offset,
                )
                receipt, body = await self._call(
                    step,
                    "read",
                    key=f"read:{index}:{offset}",
                    manifest_sha256=digest,
                    index=index,
                    offset=offset,
                    max_bytes=length,
                )
                if (
                    receipt.get("pending") is not False
                    or receipt.get("index") != index
                    or receipt.get("offset") != offset
                    or len(body) != length
                ):
                    raise ProjectConflict("Snapshot chunk does not match its receipt")
                return body

            stream = _RemoteFile(asyncio.get_running_loop(), read, item["size"])
            staged.append(
                await _store(
                    self.projects.stage_snapshot_file,
                    access,
                    project["project_id"],
                    job,
                    item,
                    stream,
                    manifest_sha256=digest,
                )
            )
        checked, _ = await self._call(
            step, "check", key="snapshot-check", manifest_sha256=digest
        )
        if checked.get("pending") is not False:
            raise ProjectConflict("Snapshot consistency was not confirmed")
        access, project = await self._context(job)
        version = await _store(
            self.projects.snapshot,
            access,
            project["project_id"],
            staged,
            expected_version=project["version"],
            lifecycle_job=job,
            staged_manifest_sha256=digest,
            rebuild=document.get("rebuild"),
        )
        return DockerProjectBackend._evidence(
            step,
            checked,
            snapshot={key: version[key] for key in ("version_no", "manifest_sha256")},
            warnings=observed.get("warnings", []),
        )

    async def _prune(self, job, step):
        await self._context(job)
        snapshot = step["arguments"]["snapshot"]
        options = {
            "manifest_sha256": snapshot["manifest_sha256"],
            "snapshot_fence": step["arguments"]["snapshot_fence"],
        }
        await self._call(step, "prune", **options)
        observed, _ = await self._call(
            step, "prune_check", key="prune-check", **options
        )
        if (
            observed.get("pending") is not False
            or type(observed.get("pruned_roots")) is not int
        ):
            raise ProjectConflict("Rebuildable cleanup was not verified")
        return DockerProjectBackend._evidence(
            step, observed, snapshot=snapshot, pruned_roots=observed["pruned_roots"]
        )

    async def _restore(self, job, step):
        access, project = await self._context(job)
        baseline = step["arguments"]["baseline"]
        manifest = await _store(
            self.projects.manifest,
            access,
            project["project_id"],
            baseline["version_no"],
        )
        encoded = manifest_bytes(manifest, quota_bytes=project["quota_bytes"])
        digest = hashlib.sha256(encoded).hexdigest()
        if digest != baseline["manifest_sha256"]:
            raise ProjectConflict("Retained restore baseline changed")
        begun, _ = await self._call(
            step, "restore_begin", manifest_sha256=digest, manifest_size=len(encoded)
        )
        chunk_size = min(CHUNK_BYTES, self.client.max_file_bytes)
        if begun.get("complete") is not True:
            if begun.get("pending") is not True:
                raise ProjectConflict("Restore was not reserved")
            for offset in range(0, len(encoded), chunk_size):
                await self._context(job)
                await self._call(
                    step,
                    "restore_manifest",
                    key=f"manifest:{offset}",
                    manifest_sha256=digest,
                    offset=offset,
                    payload=encoded[offset : offset + chunk_size],
                )
            for index, item in enumerate(manifest["files"]):
                await self._context(job)
                data = await _store(self.projects.blobs.read, item["sha256"])
                if len(data) != item["size"]:
                    raise ProjectConflict("Retained file size changed")
                for offset in range(0, max(len(data), 1), chunk_size):
                    await self._context(job)
                    await self._call(
                        step,
                        "restore_file",
                        key=f"file:{index}:{offset}",
                        manifest_sha256=digest,
                        index=index,
                        offset=offset,
                        payload=data[offset : offset + chunk_size],
                    )
        await self._context(job)
        await self._call(
            step, "restore_finish", key="restore-finish", manifest_sha256=digest
        )
        # Mutation receipts can be replayed. Completion needs a fresh tree scan.
        observed, _ = await self._call(
            step, "restore_check", key="restore-check", manifest_sha256=digest
        )
        if observed.get("pending") is not False:
            raise ProjectConflict("Restored tree was not verified")
        return DockerProjectBackend._evidence(step, observed, restored_version=baseline)
