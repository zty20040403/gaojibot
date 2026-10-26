from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response

from ..admin_dashboard import ADMIN_FAVICON_SVG, admin_asset_path, dashboard_html
from .context import AdminRouteContext


def create_router(context: AdminRouteContext) -> APIRouter:
    router = context.new_router()
    services = context.services
    prefix = context.prefix

    @router.get("", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> str:
        return dashboard_html(prefix, services.version)

    @router.get("/assets/{asset_path:path}", include_in_schema=False)
    async def dashboard_asset(asset_path: str) -> FileResponse:
        resolved = admin_asset_path(asset_path)
        if resolved is None:
            raise HTTPException(status_code=404, detail="admin asset not found")
        return FileResponse(
            resolved,
            headers={"Cache-Control": "public, max-age=300"},
        )

    @router.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> Response:
        return Response(
            content=ADMIN_FAVICON_SVG,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    return router
