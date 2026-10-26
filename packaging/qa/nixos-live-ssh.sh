#!/usr/bin/env bash
# Run only from the serial console of the fixed disposable QEMU guest.
set -euo pipefail
test "$(id -u)" = 0
test "$(cat /sys/class/block/vda/serial)" = gaoji-qa-disk
test ! -e /var/lib/gaoji-app
seed=/run/gaoji-qa-seed
mkdir -p "$seed"
if ! mountpoint -q "$seed"; then
    mount -o ro /dev/disk/by-label/GAOJIQA "$seed"
fi
id=$(cat "$seed/vm-id")
[[ "$id" =~ ^gaoji-local-qa-[a-f0-9]{32}$ ]]
install -m 644 "$seed/vm-id" /etc/gaoji-local-qa
install -d -m 700 /root/.ssh
install -m 600 "$seed/client-key.pub" /root/.ssh/authorized_keys
install -m 600 "$seed/host-key" /etc/ssh/ssh_host_ed25519_key
install -m 644 "$seed/host-key.pub" /etc/ssh/ssh_host_ed25519_key.pub
systemctl restart sshd
printf 'GAOJI_QA_LIVE_SSH_READY\n'
