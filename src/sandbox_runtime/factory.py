"""Choose one transport explicitly; never fall back from RPC to host Docker."""
from __future__ import annotations

from .client import SandboxClient
from .docker import DockerSandboxManager


def manager_from_settings(settings) -> DockerSandboxManager | SandboxClient:
    socket_path = settings.sandbox_runner_socket
    token = settings.sandbox_runner_token
    if socket_path or token:
        return SandboxClient(socket_path, token, max_file_bytes=settings.sandbox_max_file_bytes,
                             image=settings.sandbox_image or "runner")
    return DockerSandboxManager(
        image=settings.sandbox_image,
        max_per_owner=settings.sandbox_max_per_user,
        max_total=settings.sandbox_max_total,
        default_timeout_seconds=settings.sandbox_timeout_seconds,
        max_file_bytes=settings.sandbox_max_file_bytes,
        nix_cache_volume=settings.sandbox_nix_cache_volume,
    )
