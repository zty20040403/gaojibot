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
network setting is OCI-specific; KVM network policy must be provisioned
separately in libvirt. Do not treat the OCI firewall tests as VM isolation proof.

The rules use Docker's documented [DOCKER-USER filtering hook](https://docs.docker.com/engine/network/firewall-iptables/)
plus an INPUT rule for host-local traffic. A dedicated bridge-only deny guard
stays installed if provisioning fails. Rules for other interfaces are not
flushed. [Bridge ICC](https://docs.docker.com/engine/network/drivers/bridge/) is
disabled as an additional peer-isolation layer, not a substitute for egress rules.

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
