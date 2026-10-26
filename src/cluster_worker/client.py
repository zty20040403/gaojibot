from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx


class WorkerControlClient:
    def __init__(self, base_url: str, token_file: Path) -> None:
        self.base_url = base_url
        self.token_file = token_file
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0), follow_redirects=False, trust_env=False
        )

    def _token(self) -> str:
        raw = self.token_file.read_bytes()
        token = raw.rstrip(b"\r\n")
        if len(raw) >= 515 or not 32 <= len(token) <= 512:
            raise ValueError("worker credential is invalid")
        return token.decode("ascii")

    async def close(self) -> None:
        await self.client.aclose()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.post(
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self._token()}"},
            json=payload,
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("control service returned invalid JSON")
        return value

    async def heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/v1/worker/heartbeat", payload)

    async def claim(self) -> dict[str, Any] | None:
        return (await self._post("/v1/worker/claim", {})).get("job")

    async def renew(self, job_id: str, fence: int) -> bool:
        result = await self._post(f"/v1/worker/jobs/{job_id}/renew", {"fence": fence})
        return bool(result.get("cancel_requested", False))

    async def complete(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post(f"/v1/worker/jobs/{job_id}/complete", payload)

    async def checkpoint(
        self, job_id: str, *, fence: int, phase: str,
        executor_version: str, state: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._post(
            f"/v1/worker/jobs/{job_id}/checkpoints",
            {
                "fence": fence,
                "phase": phase,
                "format_version": 1,
                "executor_version": executor_version,
                "state": state,
            },
        )

    async def artifact(
        self, artifact_id: str, *, job_id: str, fence: int
    ) -> tuple[bytes, str, str]:
        async with self.client.stream(
            "GET",
            f"{self.base_url}/v1/worker/artifacts/{artifact_id}",
            params={"job_id": job_id, "fence": fence},
            headers={"Authorization": f"Bearer {self._token()}"},
        ) as response:
            response.raise_for_status()
            expected = response.headers.get("X-Artifact-SHA256", "")
            media_type = response.headers.get("Content-Type", "application/octet-stream")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 25 * 1024 * 1024:
                    raise ValueError("artifact exceeds worker transfer limit")
                chunks.append(chunk)
        return b"".join(chunks), expected, media_type
