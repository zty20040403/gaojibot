"""Per-step containers and immutable host-owned artifact snapshots."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import time
import tempfile
from pathlib import Path, PurePosixPath
from typing import Callable

from .control import assert_job_owned
from .workspace_cleanup import MAX_RETENTION_SECONDS, schedule_cleanup


IMPORT_AGENT_ARTIFACT = {"type": "function", "function": {
    "name": "import_agent_artifact", "description": "将已授权上游的产物快照导入自己的沙盒；原始快照只读，构建前复制到自己的工作目录。",
    "parameters": {"type": "object", "additionalProperties": False, "properties": {
        "step_id": {"type": "string"}, "artifact_index": {"type": "integer", "minimum": 0},
        "sandbox_id": {"type": "string"}}, "required": ["step_id", "artifact_index", "sandbox_id"]}}}


class ArtifactCaptureError(ValueError):
    def __init__(self, captured: list[dict], errors: list[str]):
        self.captured = captured
        self.errors = errors
        super().__init__("; ".join(errors))


class StepWorkspaces:
    def __init__(self, root: Path, executor, *, retention_seconds: int = 3600):
        self.root = root / "subagent_artifacts"
        self.retention_seconds = min(max(int(retention_seconds), 0), MAX_RETENTION_SECONDS)
        self.executor = executor
        self.manager = executor.sandbox_manager

    def _path(self, task_id: int, digest: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid artifact digest")
        return self.root / str(int(task_id)) / digest

    def _persist(self, task_id: int, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        path = self._path(task_id, digest)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
                output.write(content)
                name = output.name
            os.chmod(name, 0o400)
            os.replace(name, path)
        return digest

    async def capture(self, task_id: int, artifacts: list) -> list[dict]:
        captured = []
        errors = []
        for item in artifacts:
            try:
                if not isinstance(item, dict):
                    raise ValueError("Invalid artifact entry")
                match = re.fullmatch(r"(s[0-9a-f]{6}):(/workspace/.+)", str(item.get("handle", "")))
                if not match:
                    raise ValueError("Artifact must reference a real sandbox file or directory")
                sandbox_id, path = match.groups()
                content, directory = await self.manager.export_artifact(
                    self.executor.owner, sandbox_id, path.removeprefix("/workspace/"))
                if not content:
                    raise ValueError("Artifact is empty")
                digest = await asyncio.to_thread(self._persist, task_id, content)
                await asyncio.to_thread(self._clear_retention_marker, task_id, digest)
                filename = PurePosixPath(str(item.get("name") or path)).name
                if directory and not filename.lower().endswith(".zip"):
                    filename += ".zip"
                captured.append({**item, "snapshot": digest, "size": len(content),
                                 "kind": "file", "source_kind": "directory" if directory else "file", "name": filename})
            except Exception as exc:
                handle = str(item.get("handle", "")) if isinstance(item, dict) else "invalid entry"
                errors.append(f"{handle}: {exc}")
        if errors:
            raise ArtifactCaptureError(captured, errors)
        return captured

    async def import_artifact(self, task_id: int, upstream: dict, arguments: dict) -> str:
        result = upstream.get(str(arguments.get("step_id")))
        if result is None:
            raise ValueError("This step may only read its declared upstream artifacts")
        index = arguments.get("artifact_index")
        artifacts = result.get("artifacts", [])
        if not isinstance(index, int) or index < 0 or index >= len(artifacts):
            raise ValueError("Unknown upstream artifact")
        item = artifacts[index]
        content = await asyncio.to_thread(self._path(task_id, item.get("snapshot", "")).read_bytes)
        sandbox_id = str(arguments.get("sandbox_id", ""))
        filename = PurePosixPath(item["name"]).name
        if filename in {"", ".", ".."}:
            raise ValueError("Invalid artifact filename")
        target = f"upstream/{item['snapshot']}/{filename}"
        await self.manager.install_readonly_file(self.executor.owner, sandbox_id, target, content)
        return json.dumps({"ok": True, "path": f"/workspace/{target}", "sha256": item["snapshot"], "read_only": True})

    async def validate(self, task_id: int, artifact: dict) -> dict:
        content = await asyncio.to_thread(self._path(task_id, artifact.get("snapshot", "")).read_bytes)
        if not content or hashlib.sha256(content).hexdigest() != artifact["snapshot"]:
            return {"ok": False, "error": "Artifact checksum mismatch or empty file"}
        # Format checks run in a separate container, not in the producing agent's process.
        vm_backend = getattr(self.manager, "backend", "oci") == "vm"
        owner = (
            f"{self.executor.base_owner}:task#{task_id}/verifier"
            if vm_backend else self.executor.owner
        )
        sandbox = await self.manager.create(owner, "python")
        sid = sandbox["sandbox_id"]
        suffix = Path(artifact["name"]).suffix.lower()
        path = "acceptance" + suffix
        try:
            await self.manager.write_file(owner, sid, path, content, allow_large=True)
            commands = {
                ".pdf": "pdfinfo /workspace/acceptance.pdf && pdffonts /workspace/acceptance.pdf && pdftotext /workspace/acceptance.pdf -",
                ".zip": "unzip -t /workspace/acceptance.zip",
                ".docx": "unzip -t /workspace/acceptance.docx", ".xlsx": "unzip -t /workspace/acceptance.xlsx",
                ".pptx": "unzip -t /workspace/acceptance.pptx",
                ".png": f"{'python3' if vm_backend else 'python'} -c 'from PIL import Image; Image.open(\"/workspace/acceptance.png\").verify()'",
            }
            command = commands.get(suffix)
            if not command:
                return {"ok": True, "checks": ["nonempty", "sha256"], "functional": "requires_review"}
            vm_packages = (
                ["poppler-utils"] if suffix == ".pdf"
                else ["python3-pil"] if suffix == ".png"
                else []
            ) if vm_backend else []
            check = await self.manager.exec(
                owner, sid, command, 45,
                packages=vm_packages,
            )
            ok = check.returncode == 0
            if suffix == ".pdf":
                ok = ok and bool(re.search(r"Pages:\s*[1-9][0-9]*", check.stdout))
                font_lines = [line for line in check.stdout.splitlines() if re.search(r"\s+(yes|no)\s+(yes|no)\s+(yes|no)\s+\d+\s+\d+\s*$", line)]
                ok = ok and bool(font_lines) and all(re.search(r"\s+yes\s+(?:yes|no)\s+(?:yes|no)\s+\d+\s+\d+\s*$", line) for line in font_lines)
                # A rendered first page catches broken PDFs that text extraction alone misses.
                rendered = await self.manager.exec(owner, sid,
                    "pdftoppm -f 1 -singlefile -scale-to 1000 -png /workspace/acceptance.pdf /workspace/rendered", 45)
                ok = ok and rendered.returncode == 0
            return {"ok": ok, "checks": ["nonempty", "sha256", "format"],
                    "details": (check.stdout + check.stderr)[-4000:], "functional": "requires_review"}
        finally:
            await self.manager.destroy(owner, sid)

    async def quiesce_for_validation(self, task_id: int, completed) -> None:
        if getattr(self.manager, "backend", "oci") != "vm":
            return
        base_owner = str(getattr(self.executor, "base_owner", self.executor.owner))
        for outcome in completed.values():
            owner = f"{base_owner}:task#{task_id}/{outcome.step.key}"
            for sandbox in await self.manager.list(owner):
                if (
                    sandbox.get("purpose", "task") == "task"
                    and str(sandbox.get("status", "")).startswith("Up ")
                ):
                    await self.manager.stop_owned(owner, sandbox["sandbox_id"])

    async def deliver(self, task_id: int, artifact: dict) -> str:
        assert_job_owned()
        content = await self.prepare_delivery(task_id, artifact)
        assert_job_owned()
        return await self.executor.send_file_content(content, artifact["name"])

    async def prepare_delivery(self, task_id: int, artifact: dict) -> bytes:
        content = await asyncio.to_thread(self._path(task_id, artifact["snapshot"]).read_bytes)
        if not content or hashlib.sha256(content).hexdigest() != artifact["snapshot"] or len(content) != artifact["size"]:
            raise ValueError("Artifact bytes changed after acceptance")
        return content

    async def reconcile(self, filename: str, size: int) -> dict:
        result = await self.executor.confirm_group_file(filename, size, attempts=1)
        return {**result, "filename": filename, "size": size}

    async def stop_step(self):
        """Quiesce step containers without deleting their workspaces."""
        for sandbox in await self.manager.list(self.executor.owner):
            if sandbox.get("purpose", "task") == "task":
                await self.manager.stop_owned(
                    self.executor.owner,
                    sandbox["sandbox_id"],
                )

    async def restore_step(self):
        for sandbox in await self.manager.list(self.executor.owner):
            if sandbox.get("purpose", "task") == "task":
                await self.manager.start_owned(self.executor.owner, sandbox["sandbox_id"])

    async def finalize_task(
        self,
        task_id: int,
        runs,
        *,
        artifact_digests: tuple[str, ...] = (),
        cleanup_revision: int | None = None,
        finished_at: int | None = None,
    ):
        """Stop task containers and retain their persistent workspace volumes."""
        base_owner = getattr(self.executor, "base_owner", None)
        if not isinstance(base_owner, str) or not base_owner:
            base_owner = str(self.executor.owner)
        sandboxes = []
        for run in runs:
            owner = f"{base_owner}:task#{task_id}/{run.step_key}"
            for sandbox in await self.manager.list(owner):
                if sandbox.get("purpose", "task") == "task":
                    await self.manager.stop_owned(owner, sandbox["sandbox_id"])
                    sandboxes.append({"owner": owner, "sandbox_id": sandbox["sandbox_id"]})
        acknowledged_at = int(time.time())
        for digest in artifact_digests:
            await asyncio.to_thread(
                self._mark_for_retention,
                task_id,
                digest,
                acknowledged_at,
            )
        if cleanup_revision is not None and finished_at is not None:
            await asyncio.to_thread(schedule_cleanup, self.root, task_id=task_id,
                revision=cleanup_revision, finished_at=finished_at, sandboxes=sandboxes,
                snapshots=artifact_digests, retention_seconds=self.retention_seconds)

    def _retention_marker(self, task_id: int, digest: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid artifact digest")
        return self.root / ".retention" / str(int(task_id)) / f"{digest}.json"

    def _clear_retention_marker(self, task_id: int, digest: str) -> None:
        self._retention_marker(task_id, digest).unlink(missing_ok=True)

    def _mark_for_retention(
        self,
        task_id: int,
        digest: str,
        acknowledged_at: int,
    ) -> None:
        marker = self._retention_marker(task_id, digest)
        if not self._path(task_id, digest).is_file():
            return
        if marker.is_file() and not marker.is_symlink():
            previous = json.loads(marker.read_text(encoding="utf-8"))
            acknowledged_at = min(acknowledged_at, int(previous["acknowledged_at"]))
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(
            {
                "task_id": int(task_id),
                "sha256": digest,
                "acknowledged_at": int(acknowledged_at),
                "delete_after": int(acknowledged_at) + self.retention_seconds,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        with tempfile.NamedTemporaryFile(dir=marker.parent, delete=False) as output:
            output.write(payload)
            name = output.name
        os.chmod(name, 0o400)
        os.replace(name, marker)


def prune_acknowledged_artifacts(root: Path, *, now: int | None = None,
                                 can_prune: Callable[[int, str], bool] | None = None) -> tuple[int, int]:
    """Delete only snapshots whose confirmed-delivery retention period expired."""

    retention_root = root / ".retention"
    if not retention_root.is_dir():
        return (0, 0)
    current = int(time.time() if now is None else now)
    deleted = 0
    invalid = 0
    for task_dir in retention_root.iterdir():
        if task_dir.is_symlink() or not task_dir.is_dir() or not task_dir.name.isdigit():
            invalid += 1
            continue
        for marker in task_dir.iterdir():
            digest = marker.stem
            if (
                marker.is_symlink()
                or not marker.is_file()
                or marker.suffix != ".json"
                or not re.fullmatch(r"[a-f0-9]{64}", digest)
            ):
                invalid += 1
                continue
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
                if (
                    int(payload.get("task_id")) != int(task_dir.name)
                    or payload.get("sha256") != digest
                ):
                    continue
                # Old seven-day tickets obey the new one-hour ceiling as well.
                if min(int(payload["delete_after"]), int(payload["acknowledged_at"]) + MAX_RETENTION_SECONDS) > current:
                    continue
                if can_prune is not None and not can_prune(int(task_dir.name), digest):
                    continue
                if (root / ".workspace-cleanup" / f"{task_dir.name}.json").exists():
                    continue
                if (root / task_dir.name).is_symlink():
                    invalid += 1
                    continue
                artifact = root / task_dir.name / digest
                if artifact.is_symlink():
                    invalid += 1
                    continue
                artifact.unlink(missing_ok=True)
                marker.unlink(missing_ok=True)
                deleted += 1
            except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
                invalid += 1
        try:
            task_dir.rmdir()
        except OSError:
            pass
        artifact_dir = root / task_dir.name
        try:
            artifact_dir.rmdir()
        except OSError:
            pass
    try:
        retention_root.rmdir()
    except OSError:
        pass
    return (deleted, invalid)
