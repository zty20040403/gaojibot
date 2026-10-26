#!/usr/bin/env bash
# Installs only the guest OS. The desktop installer must install every Bot dependency.
set -euo pipefail
test "$(id -u)" = 0
seed=/run/gaoji-qa-seed
test "$(cat /etc/gaoji-local-qa)" = "$(cat "$seed/vm-id")"
test "$(cat /sys/class/block/vda/serial)" = gaoji-qa-disk
test ! -e /var/lib/gaoji-app
disk=/dev/disk/by-id/virtio-gaoji-qa-disk
test "$(readlink -f "$disk")" = /dev/vda
# An interrupted or installed disk requires inspection, never automatic formatting.
test ! -e /dev/vda1
if blkid "$disk"; then
    printf 'QA disk is not empty; refusing to replace it\n' >&2
    exit 1
fi
test ! -e /run/gaoji-qa-baseline-started
touch /run/gaoji-qa-baseline-started
parted --script "$disk" mklabel msdos mkpart primary ext4 1MiB 100% set 1 boot on
udevadm settle
mkfs.ext4 -L gaoji-qa-root /dev/vda1
mount /dev/disk/by-label/gaoji-qa-root /mnt
install -d -m 755 /mnt/etc/nixos /mnt/etc/ssh
install -m 644 "$seed/configuration.nix" /mnt/etc/nixos/configuration.nix
install -m 644 "$seed/identity.json" /mnt/etc/nixos/identity.json
install -m 600 "$seed/host-key" /mnt/etc/ssh/ssh_host_ed25519_key
install -m 644 "$seed/host-key.pub" /mnt/etc/ssh/ssh_host_ed25519_key.pub
nixos-install --root /mnt --no-root-password --max-jobs 1 --cores 2 --option builders ''
sync
printf 'GAOJI_QA_BASELINE_INSTALLED\n'
