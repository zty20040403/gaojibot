from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from .service import ClusterWorker


def create_app(worker: ClusterWorker) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(worker.run())
        try:
            yield
        finally:
            await worker.close()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(
        title="gaoji Worker Preview",
        version="1",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "worker_id": worker.settings.worker_id}

    @app.get("/previews/{preview_id}/")
    @app.get("/previews/{preview_id}/{relative_path:path}")
    async def preview(preview_id: str, relative_path: str = "") -> FileResponse:
        item = await asyncio.to_thread(worker.preview_file, preview_id, relative_path)
        if item is None:
            raise HTTPException(status_code=404, detail="Preview not found or expired")
        path, media_type = item
        return FileResponse(
            path,
            media_type=media_type,
            headers={
                "Cache-Control": "no-store",
                "X-Robots-Tag": "noindex, nofollow",
                # Static assets use anonymous CORS because sandboxed documents have opaque origins.
                "Access-Control-Allow-Origin": "*",
                "Content-Security-Policy": (
                    "sandbox allow-scripts; default-src 'self' data: blob:; "
                    "script-src 'self' 'unsafe-inline' blob:; "
                    "style-src 'self' 'unsafe-inline'; connect-src 'none'; "
                    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return app
