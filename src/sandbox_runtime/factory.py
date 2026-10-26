"""Choose one transport explicitly; never fall back from RPC to host Docker."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .client import SandboxClient
from .docker import DockerSandboxManager

if TYPE_CHECKING:
    from src.plugins.ai_chat.vm_sandbox import VmSandboxManager


def manager_from_settings(settings) -> DockerSandboxManager | SandboxClient | VmSandboxManager:
    backend = getattr(settings, "sandbox_backend", "oci")
    if backend not in {"oci", "vm"}:
        raise RuntimeError(f"Unsupported sandbox backend: {backend}")
    socket_path = settings.sandbox_runner_socket
    token = settings.sandbox_runner_token
    if socket_path or token:
        if backend != "oci":
            raise RuntimeError("Sandbox runner cannot be combined with a VM backend")
        return SandboxClient(socket_path, token, max_file_bytes=settings.sandbox_max_file_bytes,
                             image=settings.sandbox_image or "runner")
    if backend == "vm":
        if not settings.sandbox_vm_image or not settings.sandbox_vm_root:
            raise RuntimeError("VM sandbox requires AI_SANDBOX_VM_IMAGE and AI_SANDBOX_VM_ROOT")
        from src.plugins.ai_chat.vm_sandbox import VmSandboxManager

        return VmSandboxManager(
            image=settings.sandbox_vm_image,
            root=settings.sandbox_vm_root,
            max_per_owner=settings.sandbox_max_per_user,
            max_total=settings.sandbox_max_total,
            default_timeout_seconds=settings.sandbox_timeout_seconds,
            max_file_bytes=settings.sandbox_max_file_bytes,
            memory_mib=settings.sandbox_memory_mib,
            vcpus=settings.sandbox_cpus,
            pids_limit=settings.sandbox_pids_limit,
        )
    return DockerSandboxManager(
        image=settings.sandbox_image,
        max_per_owner=settings.sandbox_max_per_user,
        max_total=settings.sandbox_max_total,
        default_timeout_seconds=settings.sandbox_timeout_seconds,
        max_file_bytes=settings.sandbox_max_file_bytes,
        nix_cache_volume=settings.sandbox_nix_cache_volume,
        memory_mib=settings.sandbox_memory_mib,
        cpus=settings.sandbox_cpus,
        pids_limit=settings.sandbox_pids_limit,
        network=settings.sandbox_network,
    )
