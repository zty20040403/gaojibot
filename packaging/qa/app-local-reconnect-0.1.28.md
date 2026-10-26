# App 0.1.28 Local Reconnect

Local preview. Only desktop management code changed; the runtime archive is
identical to 0.1.27 (`d0f58b0e06e4f054b1253784a8b57019928675371402a9c199363e5c7e3e7a65`).

## Behavior

- An installed older local instance can be reconnected without the old installer
  bundle. The client validates its image, volume, network, loopback port and owner.
- A running healthy controller is not restarted. A stopped verified controller is
  started by immutable container ID; no build, init, migration or new database.
- A confirmed failed initialization can restart that controller, but an unreachable
  running controller, active operation or maintenance state is not force-restarted.
- New installation records persist the data-volume creation stage. Missing data for
  an initialized or legacy instance is an error, not permission to create empty data.
- Missing old components still require a matching repair bundle. This does not add
  runtime upgrades or retrofit new backend capabilities into old instances.

## Evidence

- 16 focused Node checks passed: legacy connection without a bundle, stopped
  same-ID start, missing data, wrong mount/network/public port, unreachable/busy
  manager, failed initialization retry and download-policy validation.
- Real local Docker lifecycle check passed with a random owned container/network/
  volume and synthetic authenticated HTTP controller. Initial start and stopped
  resume retained the same container ID and persistent volume marker; connecting
  while running did not change StartedAt. It used the production LocalDeployment
  code, no model, QQ or actual database. All test resources were removed afterward.
- This check first exposed that Docker can omit NetworkID before first start. The
  client now checks the declared owned network and checks any assigned NetworkID.
  Fixture interpreter/network setup was corrected before the final passing run;
  failed attempts are not counted as acceptance.
- macOS/Windows packages and hashes are recorded in `desktop/release/0.1.28/`.
  Archive checks alone do not prove Windows execution.
- The packaged Mac executable passed actual startup, preload isolation, password
  masking, desktop/narrow layout and localhost SSH encrypted backup transfer.
  The Docker reconnect exercise used the source module; package validation checks
  that this exact module is included in the installer.

No user server was contacted. Windows execution and full V1 end-to-end acceptance
remain incomplete; the CI draft-access permission change still needs user approval.
The public update source remains 0.1.25.
