from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from src.cluster_control.job_kinds import WORKER_JOB_KINDS

from .client import WorkerControlClient
from .config import WorkerSettings


CAPABILITIES = tuple(sorted(WORKER_JOB_KINDS))
EXECUTOR_VERSION = "worker-v2"
logger = logging.getLogger("gaoji.cluster_worker")


class ClusterWorker:
    def __init__(self, settings: WorkerSettings) -> None:
        self.settings = settings
        self.client = WorkerControlClient(settings.control_url, settings.token_file)
        self.boot_id = uuid.uuid4().hex
        self.stop_event = asyncio.Event()
        self.running: set[asyncio.Task[None]] = set()
        self.settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.receipts = self.settings.state_dir / "receipts"
        self.previews = self.settings.state_dir / "previews"
        self.workspaces = self.settings.state_dir / "workspaces"
        for path in (self.receipts, self.previews, self.workspaces):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def heartbeat_payload(self) -> dict[str, Any]:
        return {
            "boot_id": self.boot_id,
            "protocol_version": 1,
            "availability": "available",
            "capabilities": list(CAPABILITIES),
            "runtime": {
                "system": platform.system().lower(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            },
            "capacity": {
                "cpu_millis": self.settings.cpu_millis,
                "memory_bytes": self.settings.memory_bytes,
                "gpu_slots": self.settings.gpu_slots,
            },
            "public_base_url": self.settings.public_base_url,
        }

    async def close(self) -> None:
        self.stop_event.set()
        for task in list(self.running):
            task.cancel()
        if self.running:
            await asyncio.gather(*self.running, return_exceptions=True)
        await self.client.close()

    async def run(self) -> None:
        heartbeat_at = 0.0
        while not self.stop_event.is_set():
            try:
                if time.monotonic() >= heartbeat_at:
                    await self.client.heartbeat(self.heartbeat_payload())
                    heartbeat_at = time.monotonic() + 15
                    await self._flush_receipts()
                    await asyncio.to_thread(self._clean_expired_previews)
                self.running = {task for task in self.running if not task.done()}
                if len(self.running) < self.settings.concurrency:
                    job = await self.client.claim()
                    if isinstance(job, dict):
                        task = asyncio.create_task(self._run_job(job))
                        self.running.add(task)
                        continue
                await asyncio.wait_for(self.stop_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            except Exception as exc:
                logger.warning("Worker control loop failed: %s", exc)
                await asyncio.sleep(5)

    async def _renew_loop(
        self, job_id: str, fence: int, done: asyncio.Event,
        cancel_requested: asyncio.Event,
    ) -> None:
        while not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), timeout=20)
            except asyncio.TimeoutError:
                if await self.client.renew(job_id, fence):
                    cancel_requested.set()
                    return

    async def _run_job(self, job: dict[str, Any]) -> None:
        job_id = str(job["job_id"])
        fence = int(job["fence"])
        done = asyncio.Event()
        cancel_requested = asyncio.Event()
        renewer = asyncio.create_task(
            self._renew_loop(job_id, fence, done, cancel_requested)
        )
        try:
            constraints = (
                job.get("constraints")
                if isinstance(job.get("constraints"), dict)
                else {}
            )
            checkpointable = constraints.get("checkpointable") is True
            checkpoint = job.get("resume_checkpoint")
            resumed = isinstance(checkpoint, dict) and checkpoint.get("phase") == "completed"
            if checkpointable and not resumed:
                await self._save_checkpoint(
                    job_id, fence, "started", {"kind": str(job.get("kind") or "")}
                )
            result = await self._execute(job)
            if checkpointable:
                await self._save_checkpoint(
                    job_id, fence, "completed", {"result": result}
                )
            receipt = {
                "fence": fence,
                "ok": not cancel_requested.is_set(),
                "result": result if not cancel_requested.is_set() else {"cancelled": True},
                "error_code": "" if not cancel_requested.is_set() else "cancelled",
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Worker job %s failed: %s", job_id, exc)
            receipt = {
                "fence": fence,
                "ok": False,
                "result": {"message": str(exc)[:500]},
                "error_code": type(exc).__name__[:80],
            }
        finally:
            done.set()
            renewer.cancel()
            await asyncio.gather(renewer, return_exceptions=True)
        if job.get("kind") == "preview.static":
            receipt["_cleanup_preview_id"] = str(
                (job.get("payload") or {}).get("preview_id") or ""
            )
        await asyncio.to_thread(self._save_receipt, job_id, receipt)
        await self._send_receipt(job_id, receipt)

    async def _save_checkpoint(
        self, job_id: str, fence: int, phase: str, state: dict[str, Any]
    ) -> None:
        try:
            await self.client.checkpoint(
                job_id, fence=fence, phase=phase,
                executor_version=EXECUTOR_VERSION, state=state,
            )
        except Exception as exc:
            logger.warning("Worker checkpoint for %s was not persisted: %s", job_id, exc)

    def _save_receipt(self, job_id: str, receipt: dict[str, Any]) -> None:
        target = self.receipts / f"{job_id}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)

    async def _send_receipt(self, job_id: str, receipt: dict[str, Any]) -> None:
        public_receipt = {
            key: value for key, value in receipt.items() if not key.startswith("_")
        }
        response = await self.client.complete(job_id, public_receipt)
        if response.get("status") == "cancelled":
            preview_id = str(receipt.get("_cleanup_preview_id") or "")
            if preview_id:
                await asyncio.to_thread(
                    shutil.rmtree, self.previews / preview_id, True
                )
        (self.receipts / f"{job_id}.json").unlink(missing_ok=True)

    async def _flush_receipts(self) -> None:
        for path in sorted(self.receipts.glob("job_*.json")):
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
                await self._send_receipt(path.stem, receipt)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {404, 409}:
                    rejected = self.receipts / "rejected"
                    rejected.mkdir(exist_ok=True, mode=0o700)
                    os.replace(path, rejected / path.name)
                    continue
                return
            except Exception:
                return

    async def _download(
        self, artifact_id: str, workspace: Path, *, job_id: str, fence: int
    ) -> tuple[Path, str]:
        content, expected, media_type = await self.client.artifact(
            artifact_id, job_id=job_id, fence=fence
        )
        digest = hashlib.sha256(content).hexdigest()
        if digest != expected:
            raise ValueError("artifact checksum mismatch during transfer")
        target = workspace / "artifact"
        target.write_bytes(content)
        os.chmod(target, 0o400)
        return target, media_type

    async def _execute(self, job: dict[str, Any]) -> dict[str, Any]:
        checkpoint = job.get("resume_checkpoint")
        if isinstance(checkpoint, dict) and checkpoint.get("phase") == "completed":
            constraints = job.get("constraints") if isinstance(job.get("constraints"), dict) else {}
            if (
                int(checkpoint.get("format_version") or 0) != 1
                or checkpoint.get("executor_version") != EXECUTOR_VERSION
                or constraints.get("executor_version", EXECUTOR_VERSION) != EXECUTOR_VERSION
                or constraints.get("checkpoint_format", "gaoji-result-v1")
                not in {"gaoji-result-v1", "kennethbot-result-v1"}
            ):
                raise ValueError("saved checkpoint is incompatible with this worker")
            state = checkpoint.get("state")
            if not isinstance(state, dict) or not isinstance(state.get("result"), dict):
                raise ValueError("saved checkpoint does not contain a completed result")
            encoded = json.dumps(
                state, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
            if hashlib.sha256(encoded).hexdigest() != checkpoint.get("state_hash"):
                raise ValueError("saved checkpoint checksum mismatch")
            # A saved URL is not a portable artifact: a new worker must publish its own files.
            if job.get("kind") != "preview.static":
                return dict(state["result"])
        kind = str(job["kind"])
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        workspace = self.workspaces / f"{job['job_id']}-{job['fence']}"
        workspace.mkdir(parents=True, exist_ok=False, mode=0o700)
        try:
            if kind == "probe.http":
                return await self._probe_http(payload)
            artifact, media_type = await self._download(
                str(payload["artifact_id"]), workspace,
                job_id=str(job["job_id"]), fence=int(job["fence"]),
            )
            if kind == "artifact.inspect":
                return {
                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    "size_bytes": artifact.stat().st_size,
                    "media_type": media_type,
                }
            if kind == "document.verify":
                return await asyncio.to_thread(self._verify_pdf, artifact)
            if kind == "media.inspect":
                return await asyncio.to_thread(self._inspect_media, artifact)
            if kind == "preview.static":
                return await asyncio.to_thread(self._publish_preview, artifact, payload)
            raise ValueError("unsupported worker job")
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    async def _probe_http(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = str(payload["url"])
        expected_host = str(payload["expected_host"])
        if str(httpx.URL(url).host) != expected_host:
            raise ValueError("probe target host changed")
        started = time.monotonic()
        async with httpx.AsyncClient(
            timeout=10, follow_redirects=False, trust_env=False
        ) as client:
            response = await client.get(url, headers={"Accept": "application/json,text/plain"})
        return {
            "target_id": payload["target_id"],
            "status_code": response.status_code,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "ok": 200 <= response.status_code < 300,
        }

    @staticmethod
    def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command, capture_output=True, text=True, timeout=timeout,
            check=False, env={"PATH": os.environ.get("PATH", "")},
        )

    def _verify_pdf(self, artifact: Path) -> dict[str, Any]:
        info = self._run(["pdfinfo", str(artifact)], 30)
        text = self._run(["pdftotext", str(artifact), "-"], 45) if info.returncode == 0 else None
        if info.returncode != 0:
            raise ValueError("PDF validation failed")
        return {
            "valid": True,
            "info": info.stdout[-4000:],
            "text_preview": (text.stdout[:4000] if text and text.returncode == 0 else ""),
        }

    def _inspect_media(self, artifact: Path) -> dict[str, Any]:
        process = self._run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(artifact)],
            60,
        )
        if process.returncode != 0:
            raise ValueError("media validation failed")
        value = json.loads(process.stdout)
        return {
            "valid": True,
            "format": value.get("format", {}),
            "streams": value.get("streams", [])[:20],
        }

    def _publish_preview(self, artifact: Path, payload: dict[str, Any]) -> dict[str, Any]:
        preview_id = str(payload["preview_id"])
        if re.fullmatch(r"preview_[a-f0-9]{32}", preview_id) is None:
            raise ValueError("invalid preview identity")
        expires_at = payload.get("expires_at")
        if type(expires_at) is not int or not int(time.time()) < expires_at <= int(time.time()) + 604800:
            raise ValueError("invalid or expired preview lifetime")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        public_url = f"{self.settings.public_base_url.rstrip('/')}/previews/{preview_id}/"
        target = self.previews / preview_id
        def reconcile() -> dict[str, Any]:
            if target.is_symlink():
                raise ValueError("preview root cannot be a symlink")
            metadata = json.loads(self._preview_metadata(target).read_text(encoding="utf-8"))
            if (metadata.get("artifact_sha256") != digest or metadata.get("public_url") != public_url
                    or metadata.get("expires_at") != expires_at):
                raise ValueError("preview replay does not match its original artifact and lifetime")
            return {"public_url": metadata["public_url"], "reconciled": True}
        if target.exists() or target.is_symlink():
            return reconcile()
        staging_deadline = min(expires_at, int(time.time()) + 3600)
        temporary = Path(tempfile.mkdtemp(prefix=f".{preview_id}.{staging_deadline}.", dir=self.previews))
        total_size = 0
        written_size = 0
        seen: set[str] = set()
        try:
            with zipfile.ZipFile(artifact) as archive:
                members = archive.infolist()
                if not members or len(members) > 5000:
                    raise ValueError("static preview archive has an invalid file count")
                for member in members:
                    path = PurePosixPath(member.filename)
                    if (path.is_absolute() or ".." in path.parts or "\\" in member.filename
                            or any(ord(char) < 32 for char in member.filename)):
                        raise ValueError("static preview archive contains an unsafe path")
                    if any(part.startswith(".") for part in member.filename.rstrip("/").split("/")):
                        raise ValueError("static preview archive contains a hidden path")
                    kind = (member.external_attr >> 16) & 0o170000
                    if kind not in {0, 0o040000 if member.is_dir() else 0o100000}:
                        raise ValueError("static preview archive contains a symlink or special file")
                    if path.as_posix() in seen:
                        raise ValueError("static preview archive contains duplicate paths")
                    seen.add(path.as_posix())
                    if member.is_dir():
                        continue
                    total_size += member.file_size
                    if total_size > 100 * 1024 * 1024:
                        raise ValueError("expanded preview exceeds 100 MiB")
                    destination = temporary.joinpath(*path.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, destination.open("wb") as output:
                        while chunk := source.read(1024 * 1024):
                            written_size += len(chunk)
                            if written_size > 100 * 1024 * 1024:
                                raise ValueError("expanded preview exceeds 100 MiB")
                            output.write(chunk)
            publish_root = temporary
            if not (publish_root / "index.html").is_file():
                roots = list(temporary.iterdir())
                top_levels = {path.parts[0] for member in members
                    if (path := PurePosixPath(member.filename)).parts}
                if (len(top_levels) != 1 or len(roots) != 1 or not roots[0].is_dir()
                        or not (roots[0] / "index.html").is_file()):
                    raise ValueError("static preview requires index.html at archive root or in its only top-level directory")
                publish_root = roots[0]
            metadata = {
                "preview_id": preview_id,
                "public_url": public_url,
                "expires_at": expires_at,
                "artifact_sha256": digest,
            }
            (publish_root / ".gaoji-preview.json").write_text(
                json.dumps(metadata), encoding="utf-8"
            )
            if staging_deadline <= int(time.time()):
                raise ValueError("preview expired during extraction")
            try:
                os.replace(publish_root, target)
            except OSError:
                if target.exists() or target.is_symlink():
                    return reconcile()
                raise
            if publish_root != temporary:
                temporary.rmdir()
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        return {"public_url": public_url, "files": len(list(target.rglob("*"))), "size_bytes": total_size}

    def _clean_expired_previews(self) -> None:
        now = int(time.time())
        for path in self.previews.iterdir():
            if not path.is_dir() or path.is_symlink():
                continue
            staging = re.fullmatch(r"\.preview_[a-f0-9]{32}\.([0-9]{1,12})\.[a-z0-9_]+", path.name)
            if staging:
                if int(staging[1]) <= now:
                    shutil.rmtree(path, ignore_errors=True)
                continue
            if re.fullmatch(r"preview_[a-f0-9]{32}", path.name) is None:
                continue
            try:
                metadata = json.loads(self._preview_metadata(path).read_text(encoding="utf-8"))
                if int(metadata["expires_at"]) <= now:
                    shutil.rmtree(path)
            except Exception:
                continue

    @staticmethod
    def _preview_metadata(root: Path) -> Path:
        current = root / ".gaoji-preview.json"
        return current if current.is_file() else root / ".kennethbot-preview.json"

    def preview_file(self, preview_id: str, relative_path: str) -> tuple[Path, str] | None:
        if re.fullmatch(r"preview_[a-f0-9]{32}", preview_id) is None:
            return None
        if (self.previews / preview_id).is_symlink():
            return None
        root = (self.previews / preview_id).resolve()
        metadata = self._preview_metadata(root)
        if not metadata.is_file():
            return None
        try:
            if int(json.loads(metadata.read_text(encoding="utf-8"))["expires_at"]) <= int(time.time()):
                return None
        except Exception:
            return None
        requested = relative_path or "index.html"
        if ("\\" in requested or PurePosixPath(requested).is_absolute()
                or any(part.startswith(".") for part in PurePosixPath(requested).parts)):
            return None
        candidate = (root / requested).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.is_file():
            return None
        return candidate, mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
