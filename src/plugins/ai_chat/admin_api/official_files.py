"""Retired console artifact route (files are now uploaded and sent directly)."""
from __future__ import annotations

from fastapi import HTTPException

from .context import AdminRouteContext, authorize

_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def create_router(context: AdminRouteContext):
    router = context.new_router()

    @router.get("/api/qq-official/artifacts/{artifact_id}/download", name="official_artifact_download")
    @router.head("/api/qq-official/artifacts/{artifact_id}/download", name="official_artifact_head")
    async def download(artifact_id: str):
        authorize()
        raise HTTPException(404, "Artifact unavailable", headers=_HEADERS)

    return router
