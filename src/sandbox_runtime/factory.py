"""Build the VM sandbox manager from settings."""
from __future__ import annotations

from src.plugins.ai_chat.vm_sandbox import VmSandboxManager


def manager_from_settings(settings) -> VmSandboxManager:
    if settings.sandbox_enabled and (not settings.sandbox_vm_image or not settings.sandbox_vm_root):
        raise RuntimeError("VM sandbox requires AI_SANDBOX_VM_IMAGE and AI_SANDBOX_VM_ROOT")
    return VmSandboxManager(
        image=settings.sandbox_vm_image,
        root=settings.sandbox_vm_root,
        max_per_owner=settings.sandbox_max_per_user,
        max_total=settings.sandbox_max_total,
        resident_limit=settings.program_vm_limit,
        default_timeout_seconds=settings.sandbox_timeout_seconds,
        max_file_bytes=settings.sandbox_max_file_bytes,
        memory_mib=settings.sandbox_memory_mib,
        vcpus=settings.sandbox_cpus,
        pids_limit=settings.sandbox_pids_limit,
        operations_owners=settings.sandbox_operations_owners,
        operations_socket=settings.sandbox_operations_socket,
        operations_domain=settings.sandbox_operations_domain,
    )
