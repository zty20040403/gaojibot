"""Build the NixOS container sandbox manager from settings."""
from __future__ import annotations

from src.plugins.ai_chat.container_sandbox import ContainerSandboxManager


def manager_from_settings(settings) -> ContainerSandboxManager:
    backend = getattr(settings, "sandbox_backend", "container")
    if backend != "container":
        raise RuntimeError(f"Unsupported sandbox backend: {backend}")
    if settings.sandbox_enabled and (not settings.sandbox_container_broker or not settings.sandbox_vm_root):
        raise RuntimeError("Container sandbox requires AI_SANDBOX_CONTAINER_BROKER and AI_SANDBOX_VM_ROOT")
    return ContainerSandboxManager(
        root=settings.sandbox_vm_root,
        broker=settings.sandbox_container_broker,
        max_per_owner=settings.sandbox_max_per_user,
        max_total=settings.sandbox_max_total,
        default_timeout_seconds=settings.sandbox_timeout_seconds,
        max_file_bytes=settings.sandbox_max_file_bytes,
        memory_mib=settings.sandbox_memory_mib,
        vcpus=settings.sandbox_cpus,
        pids_limit=settings.sandbox_pids_limit,
        operations_owners=settings.sandbox_operations_owners,
        operations_domain=settings.sandbox_operations_domain,
    )
