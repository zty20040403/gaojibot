"""Mount the fleet API using the same protected router as other pages."""
from fastapi import APIRouter

from ..admin_fleet import register_fleet_admin_routes
from .context import AdminRouteContext, authorize, authorize_management


def create_router(context: AdminRouteContext) -> APIRouter:
    router = context.new_router()
    register_fleet_admin_routes(
        router, context.services, authorize, context.versioned, authorize_management
    )
    return router
