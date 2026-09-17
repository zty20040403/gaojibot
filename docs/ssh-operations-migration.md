# Native SSH Operations

## Architecture

The retired MaxOps Hub is not a failover target. Gaoji uses its own SSH identity
to reach fixed fleet hosts. Ordinary conversation sandboxes do not receive this
identity, private keys or sudo access. Existing task authorization, PostgreSQL
journals, fencing, command preflight, service verification and delivery remain.

```text
QQ / console -> authenticated principal -> operations catalog
  -> observation, or exact task authorization
  -> committed intent and dispatch checkpoint
  -> pinned SSH target -> root-owned receipt + named systemd job
  -> reconnect and observe original handle
  -> verify service instance / boot identity / task postconditions
  -> existing durable final-message delivery
```

The transport has a domain interface (`catalog`, `call`, `execute`,
`authorization_binding`). SSH does not emulate an HTTP Hub. The legacy HTTP
adapter remains for installations not yet migrated; configuration is mutually
exclusive. New tasks use `ssh.execute` / `ssh-management-v1`. Old approvals and
job handles cannot be replayed through the new backend.

## Configuration

On each approved NixOS target, import `nixosModules.host-control`:

```nix
services.gaoji-host-control = {
  enable = true;
  hostId = "example";
  ssh.enable = true;
  ssh.authorizedKeys = [ "ssh-ed25519 <dedicated-public-key>" ];
  ssh.mounts = [ "/" ];
};
```

The default account is `gaoji-operator`. Its key is forced to the JSON endpoint,
without forwarding or interactive login. Sudo permits only that endpoint without
arguments. Authorized commands run as root in dedicated jobs. This is powerful
host administration, not a sandbox; never give the key to ordinary bot users.

Controller configuration:

```nix
services.gaoji-cluster-control = {
  ops.enable = false;
  ops.management.enable = false;
  ssh = {
    enable = true;
    targets.example.destination = "gaoji-operator@example.internal";
    targets.example.port = 2224;
    knownHostsFile = ./verified-host-keys;
    identityFile = config.sops.secrets.gaoji-operations-ssh.path;
    managementHosts = [ "example" ];
    administrators = [ "admin:owner" ];
  };
  hostControlHelpers.example = "/run/current-system/sw/bin/gaoji-host-control";
};
```

Verify target host keys before pinning. The SSH client disables agent forwarding,
user SSH configuration, local commands and connection sharing. Keys are injected
only into the controller through `LoadCredential`. Credential, target or host-key
changes invalidate old approvals. Tailscale SSH without a private key requires
an independently provisioned Gaoji network identity and policy; never borrow
Max's identity or assume host Tailscale access supplies this automatically.
When Tailscale SSH intercepts port 22, use a separately configured OpenSSH port
with key-only authentication and a forced Gaoji command, restricted by the
tailnet firewall. Pin the host as `[hostname]:port` in known_hosts; do not disable
host verification or reuse the unrestricted Tailscale SSH login for this key.

## Recovery Invariants

- Commit the exact intent before dispatch. One operation maps to one target job.
- An ambiguous submission is observed, never blindly resubmitted.
- A dead SSH session does not prove remote work stopped. Use the stored handle.
- A missing receipt is `outcome_unknown`, not success or permission to retry.
- Reboots require fresh, changed boot IDs. Service actions require a successful
  synchronous systemctl receipt, instance evidence and two stable observations.
- Command output is bounded, with retained/observed byte counts and truncation
  flags. Log reads also declare when the returned slice is incomplete. Command
  deadlines still apply after stdout/stderr close. Receipts remain as anti-replay
  tombstones; they are not disposable sandbox artifacts.
- Unreachable hosts are unknown, never healthy. Unsupported integrations such as
  an unconfigured alert source are unavailable, not a fabricated empty result.
- Native observations use the existing fleet data contract: numeric failed-unit
  counts, structured unit records and timestamped resource samples. Read-only
  projections filter failed units through each host's readable-unit policy.
  SSH resource checks are labelled as SSH, not as a working exporter. CPU usage
  reports its actual short sampling window, not a five-minute average.

## Cutover Checklist

1. Fetch shared configuration and preserve unrelated App/server changes.
2. Provision Gaoji credentials and verified host keys; install only its endpoints.
3. Validate host facts, unit states and disk metrics through the real controller.
4. On a disposable unit, test authorization, lost acknowledgements, restart
   recovery and cancellation. Do not restart another person's service for a test.
5. Check console evidence and final delivery. Inspect uncertain legacy jobs
   separately; no automatic conversion or replay.

Local implementation checks passed: target/client fault handling, the existing
host preflight invoked in a real subprocess, PostgreSQL migration and job claims,
Nix module evaluation, and TypeScript compilation. The subprocess check confirms
native jobs persist host receipts without the retired executor's job directory.
The native observation-to-conversation check also verifies disk visibility,
failure counts, unit-policy filtering, stale samples and honest CPU windows.

## Production Acceptance: 2026-09-17

With owner approval, h310, tank and h610 were switched in that order using
bot revision `f2f5709` and shared configuration `a66bfd5`. Each switch completed
successfully. The dedicated OpenSSH listener is port 2224, reachable through
the tailnet firewall; port 22 and ordinary administrator access are unchanged.

- All three targets rejected a connection without the dedicated key and returned
  fresh host facts with it. The controller returned fresh SSH disk, CPU and memory
  samples for all three. An ungranted actor received HTTP 403.
- Disposable service operation `op_22c39ea07b724a379c2b0d762006868f` required
  approval, changed the service invocation ID and passed two independent stable
  observations. Repeating the same intent returned the same operation and job;
  a separate systemd read confirmed no second restart.
- Harmless h310 command operation `op_d2c09fa83f74403eb572aa9bb36ce017` passed
  target-side executable checks and retained a durable receipt. It correctly
  reported command-exit success, not unverified business success.
- Gaoji, the controller, workers and the existing database were active; the QQ
  WebSocket reconnected and the console returned HTTP 200. No host was rebooted.
  Target/client/projection checks also passed 41 focused tests under NixOS.

This acceptance did not send a group message/file, interrupt a production task
to test controller takeover, or reboot a host. Fault recovery remains covered by
the focused local tests, not by a claimed live outage exercise. Only these three
hosts have the new transport; other inventory entries do not imply connectivity.
The retired Hub's alert, Git-workspace and deployment APIs are not recreated by
SSH. The separate existing Alertmanager notifier and Worker jobs are unchanged.
