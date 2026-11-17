#!/usr/bin/env bash
# Copy each Debian VM's /workspace into the NixOS container of the same
# sandbox id (/var/lib/gaoji-ct/<id>/work). Run as root on the host, with the
# gaoji service and every VM stopped. Old disks are only read, never changed:
# switching AI_SANDBOX_BACKEND back to vm restores the old state.
#
# Usage: migrate_vm_workspaces.sh <vm-root> [--apply]
# Without --apply it only lists what it would copy.
set -euo pipefail
root=${1:?vm root, e.g. /data/services/gaoji/vms}
apply=${2:-}
state=/var/lib/gaoji-ct
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
modprobe nbd max_part=16
dev=/dev/nbd15
mnt=$(mktemp -d)
cleanup() { umount "$mnt" 2>/dev/null || true; qemu-nbd -d "$dev" >/dev/null 2>&1 || true; rmdir "$mnt"; }
trap cleanup EXIT

for dir in "$root"/s*/; do
  id=$(basename "$dir")
  [[ $id =~ ^s[0-9a-f]{6}$ ]] || continue
  disk=$dir/disk.qcow2
  [ -f "$disk" ] || { echo "$id: no disk, skipped"; continue; }
  target=$state/$id/work
  if [ -e "$target" ] && [ -n "$(ls -A "$target" 2>/dev/null)" ]; then
    echo "$id: $target is not empty, skipped"; continue
  fi
  qemu-nbd --read-only --connect="$dev" "$disk"
  part=""
  for _ in $(seq 1 25); do
    udevadm settle
    part=$(lsblk -lnpo NAME,FSTYPE "$dev" 2>/dev/null | awk '$2=="ext4"{print $1}' | tail -1)
    [ -n "$part" ] && break
    sleep 0.2
  done
  [ -n "$part" ] || { echo "$id: no ext4 partition found on $disk" >&2; exit 1; }
  mount -t ext4 -o ro,noload "$part" "$mnt"
  size=$(du -sh "$mnt/workspace" 2>/dev/null | cut -f1)
  echo "$id: /workspace ${size:-missing} -> $target"
  if [ "$apply" = --apply ] && [ -d "$mnt/workspace" ]; then
    install -d -m 0700 "$state" "$state/$id"
    install -d -m 0700 -o 1000 -g 1000 "$target"
    rsync -aHAX --numeric-ids "$mnt/workspace/" "$target/"
    chown -R 1000:1000 "$target"
  fi
  umount "$mnt"
  qemu-nbd -d "$dev" >/dev/null
done
[ "$apply" = --apply ] || echo "dry run; pass --apply to copy"
