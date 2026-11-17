"""Build the sandbox manager from settings: KVM guests or NixOS containers."""
from __future__ import annotations

from src.plugins.ai_chat.vm_sandbox import VmSandboxManager


def manager_from_settings(settings) -> VmSandboxManager:
    backend = getattr(settings, "sandbox_backend", "vm")
    options = dict(
        max_per_owner=settings.sandbox_max_per_user,
        max_total=settings.sandbox_max_total,
        default_timeout_seconds=settings.sandbox_timeout_seconds,
        max_file_bytes=settings.sandbox_max_file_bytes,
        memory_mib=settings.sandbox_memory_mib,
        vcpus=settings.sandbox_cpus,
        pids_limit=settings.sandbox_pids_limit,
        operations_owners=settings.sandbox_operations_owners,
        operations_socket=settings.sandbox_operations_socket,
        operations_domain=settings.sandbox_operations_domain,
    )
    if backend == "container":
        from src.plugins.ai_chat.container_sandbox import ContainerSandboxManager

        if settings.sandbox_enabled and (not settings.sandbox_container_broker or not settings.sandbox_vm_root):
            raise RuntimeError("Container sandbox requires AI_SANDBOX_CONTAINER_BROKER and AI_SANDBOX_VM_ROOT")
        return ContainerSandboxManager(root=settings.sandbox_vm_root, broker=settings.sandbox_container_broker, **options)
    if backend != "vm":
        raise RuntimeError(f"Unsupported sandbox backend: {backend}")
    if settings.sandbox_enabled and (not settings.sandbox_vm_image or not settings.sandbox_vm_root):
        raise RuntimeError("VM sandbox requires AI_SANDBOX_VM_IMAGE and AI_SANDBOX_VM_ROOT")
    return VmSandboxManager(image=settings.sandbox_vm_image, root=settings.sandbox_vm_root, **options)
