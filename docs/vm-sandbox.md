# KVM sandbox backend

The bot can use isolated Debian virtual machines for code and file tasks. The
default sandbox backend remains OCI. Switching to VMs is explicit:

```nix
services.gaoji.sandbox = {
  enable = true;
  backend = "vm";
  vmImage = pinnedDebianGenericcloudImage;
  vmRoot = "/data/services/gaoji/vms";
};
```

`vmImage` must be an immutable, checksum-verified QCOW2 image. Keep `vmRoot`
on persistent storage outside the Nix store. The service user needs access to
`/dev/kvm`; the NixOS module enables libvirt and supplies QEMU, virsh and
cloud-localds. On tank, override the default `vmRoot` to the 14 TB data volume;
the system partition is not suitable for growing per-task disks. Grant the
bot service write access to that path. Do not enable this backend on a host
without KVM.

Each task gets a QCOW2 overlay, cloud-init seed, domain definition and owner
metadata under `vmRoot`. The guest has a `sandbox` user and `/workspace`. The
manager communicates through QEMU Guest Agent: it does not mount the host
workspace or pass bot credentials into the guest. Debian packages requested by
the model are installed inside that guest. The `nix_search` tool is hidden in
VM mode; OCI mode keeps its existing Nix package workflow.

Stopping a VM runs `sync`, freezes guest filesystems through the guest agent,
then stops QEMU. This gives a recoverable on-disk state, not an application-level
graceful shutdown. If sync or freezing fails, the VM is left running and an
error is returned. On restart, a missing libvirt domain is redefined from the
saved XML and its disk is reused. A failed or undelivered task must not have its
VM disk reclaimed.

The disposable test on tank has verified creation, command execution, file
write/read, read-only handoff, ZIP export, filesystem-frozen stop, removal of
the libvirt domain, redefinition, restart and persisted file readback. The test
VM and temporary source files were removed. This does **not** verify the bot's
systemd service, QQ delivery, or production database migration.

The guest uses QEMU user-mode networking and can make outbound connections.
It is not a network-isolated security boundary. The Nix module does not enable
VM mode for tank by default; keep the established Podman backend during the
initial bot/database migration, then enable VM mode in a separate change after
systemd-level and QQ file-delivery acceptance.
