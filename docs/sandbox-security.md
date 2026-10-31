# Sandbox security defaults

## Access and resource limits

Enabling sandboxes does not grant access to everyone. Set
`AI_SANDBOX_ALLOWED_USERS` to the intended accounts, or deliberately opt into
`AI_SANDBOX_ALLOW_ALL=true`. Desktop deployments grant access only to their
configured administrator. Shared `/shell reset` and `/shell destroy` require
an administrator even when allow-all is enabled.

The OCI defaults are two CPUs, 256 processes and 8,192 MiB per container,
including temporary package helpers. Configure `AI_SANDBOX_CPUS`,
`AI_SANDBOX_PIDS_LIMIT` and `AI_SANDBOX_MEMORY_MIB`; zero and invalid values are
rejected. App runners retain their smaller fixed per-instance limits.
VMs use the same settings, with a minimum of 512 MiB and at most four vCPUs.
Untrusted guest commands receive a hard per-user process limit.
These are per-sandbox caps, not a promise that the host has enough capacity for
every allowed concurrent sandbox; size the concurrency limit for the host.

## OCI networking

`AI_SANDBOX_NETWORK` accepts:

- `isolated` (default): dedicated `gaoji-sandbox` bridge, no peer communication,
  no LAN, Tailscale/CGNAT, metadata or host access. Public IPv4 egress remains
  available for dependencies. IPv6 is blocked to avoid an alternate route.
- `none`: no network access, including dependency downloads.
- `bridge`: explicitly unrestricted ordinary bridge; trusted deployments only.

Both workloads and package-download helpers use the selected policy. Existing
workspaces using another network cannot execute until an administrator recreates
them. Existing files remain available for export; upgrading never silently
deletes a workspace.

The driver refuses to start on a missing or mismatched network. It does not have
permission to guess at host firewall configuration. The NixOS module and Linux
App installer install the same root-owned policy and reapply it on Docker
restart; the NixOS firewall reload hook also reapplies it. After independent
firewall changes on an App server, restart its `gaoji-app-network-<instance>`
unit before using the sandbox. Never flush Docker firewall chains manually.

Docker Desktop uses a short-lived, pinned controller helper with `NET_ADMIN`
inside Docker's Linux network namespace. Only this fixed installer has that
capability, never user code. No Mac/Windows filesystem or Bot state is mounted.
The helper is rerun whenever the sandbox runner starts, including Docker restart.
Failure prevents the runner from becoming ready; there is no bridge fallback.

Rootless Podman does not support this host iptables policy. Select `none`, or
explicitly choose `bridge` only after reviewing the trust implications. The
network setting is OCI-specific; KVM networking is described below. Do not
treat the OCI firewall tests as VM isolation proof.

The rules use Docker's documented [DOCKER-USER filtering hook](https://docs.docker.com/engine/network/firewall-iptables/)
plus an INPUT rule for host-local traffic. A dedicated bridge-only deny guard
stays installed if provisioning fails. Rules for other interfaces are not
flushed. [Bridge ICC](https://docs.docker.com/engine/network/drivers/bridge/) is
disabled as an additional peer-isolation layer, not a substitute for egress rules.

## KVM networking

VMs use QEMU user networking (slirp). The qemu process opens ordinary host
sockets for the guest: `10.0.2.2` is the host's `127.0.0.1`, and every other
address uses the host's routes, including the tailnet. Without a filter a guest
reaches host loopback services (sshd, PostgreSQL) and tailnet services. A probe
on 2026-11-01 showed exactly that on a production group VM.

The NixOS module runs the libvirt session daemon (`virtqemud`) in its own
`gaoji-vm.service` as the bot user, with the bot's `HOME` and
`XDG_RUNTIME_DIR`, so `virsh -c qemu:///session` reaches it and every qemu
inherits its cgroup. The nftables table `inet gaoji_vm` matches sockets of
that cgroup (`socket cgroupv2`) and, for those sockets only:

- accepts TCP to `127.0.0.1` on `services.gaoji.sandbox.vmHostPorts`
  (default 18640 and 18641, the background-agent model relay and app-server
  tunnel; the guest uses `10.0.2.2`);
- accepts port 53 to the nameservers in the host's `/etc/resolv.conf`, which
  is where slirp forwards guest DNS (`10.0.2.3`);
- rejects every other host-local address, the private, CGNAT, link-local,
  benchmark, multicast and reserved IPv4 ranges, and their IPv6 equivalents.

Public internet egress is unchanged. The bot and other host processes are not
matched and keep their own network.

nftables resolves the cgroup when the table is loaded, and a restarted unit gets
a new cgroup, so the table is installed by the unit's root `ExecStartPre`,
inside its fresh cgroup and before `virtqemud` starts; a failed install blocks
the start, and the bot `Requires=` the unit. With `networking.nftables.enable`
the same installer runs at the end of every `nftables.service` start and
reload, after the ruleset is replaced; if that fails it stops `gaoji-vm.service`.
The iptables-based firewall does not touch the table. Do not run
`nft flush ruleset` by hand; if it happens, restart `gaoji-vm.service`.

`vm_sandbox` runs host commands with `LIBVIRT_AUTOSTART=0`, so a missing daemon
is an error instead of an autospawned, unfiltered one in the caller's cgroup.
Any other tool that drives the session as the service user (for example
conversation-image preparation) must also set `LIBVIRT_AUTOSTART=0` and run
while `gaoji-vm.service` is active. Restarting the bot no longer stops VMs;
restarting `gaoji-vm.service` stops all of them.

Alternatives considered: systemd `IPAddressDeny=` is robust but has no port
granularity, so it cannot allow the relay on loopback while blocking sshd and
PostgreSQL there. A dedicated qemu uid for `meta skuid` needs `qemu:///system`
and shared disk ownership. tap or passt with explicit rules (as in maxx) needs
a bridge, DHCP, NAT and a new relay address. The cgroup filter keeps slirp and
the relay address as they are.

### Verifying a host

Run as root on the host after deployment:

1. `systemctl status gaoji-vm.service` is active, `nft list table inet gaoji_vm`
   shows the rules, and `cat /proc/$(pgrep -f 'qemu-system.*gaoji-vm-' | head -n1)/cgroup`
   is `0::/system.slice/gaoji-vm.service`.
2. Open two throwaway listeners as the service user: one on an allowed relay port
   if the relay is not running, and a control on a port that is not allowed, for
   example `python3 -m http.server --bind 127.0.0.1 18639`.
3. From inside a group VM, probe through the guest agent (replace `<vm>`,
   `<tailnet-ip>` and `<port>`; run as the service user with its
   `XDG_RUNTIME_DIR`):

   ```bash
   probe='for t in 10.0.2.2:22 10.0.2.2:5432 10.0.2.2:18639 10.0.2.2:18640 10.0.2.2:18641 <tailnet-ip>:<port> 1.1.1.1:443; do timeout 3 bash -c "</dev/tcp/${t%:*}/${t#*:}" 2>/dev/null && echo "$t open" || echo "$t blocked"; done; getent hosts deb.debian.org'
   pid=$(virsh -c qemu:///session qemu-agent-command <vm> "$(jq -nc --arg c "$probe" '{execute:"guest-exec",arguments:{path:"/bin/bash",arg:["-c",$c],"capture-output":true}}')" | jq .return.pid)
   sleep 15
   virsh -c qemu:///session qemu-agent-command <vm> "{\"execute\":\"guest-exec-status\",\"arguments\":{\"pid\":$pid}}" | jq -r '.return["out-data"]' | base64 -d
   ```

   Expected: `10.0.2.2:22`, `10.0.2.2:5432`, `10.0.2.2:18639` and the tailnet
   address blocked; `10.0.2.2:18640`, `10.0.2.2:18641` and `1.1.1.1:443` open;
   the name resolves.
4. Repeat step 3 after `systemctl reload nftables.service` and after
   `systemctl restart gaoji-vm.service` (the bot restarts a VM on next use).

## Acceptance before rollout

No production host is contacted by the roadmap's local tests. In an explicitly
authorized disposable test environment verify:

- A package download from the public internet succeeds.
- A test listener on LAN, CGNAT, metadata and host addresses is unreachable.
- A second sandbox cannot reach the first one's listener.
- `none` prevents public downloads; IPv6 cannot bypass isolation.
- Docker restart and firewall reload preserve the policy.
- A bounded process-spawn test stays within the container's PID limit. Never
  run an uncontrolled fork bomb or test against a production database.

Private deployment evidence belongs in ignored `ops-notes/`. These acceptance
items remain pending until measured; mocked firewall tests cannot prove them.
