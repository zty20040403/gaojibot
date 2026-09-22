# Native QQ / NapCat deployment

`services.gaoji.napcat.backend = "native"` runs a pinned, patched QQ package
under the unprivileged `gaoji-napcat` account and bubblewrap. Set `nativePackage`
to a Nix derivation providing `bin/qq` and `napcat/`. Docker remains the default
for existing deployments. This changes the transport, not bot data or models.

The native service shares the host network, connects to the bot through loopback,
and exposes its WebUI on loopback only. It mounts only its own `QQ`, `config`
and read-only `outbox` directories, plus read-only system dependencies.
It conflicts with the former dedicated Docker unit so both cannot be started by
systemd at once. Do not manually launch a second client with the same account.

## Migration

1. Fetch the shared configuration, check running generations, and build first.
2. Stop this instance's health/login timers and old QQ service. Confirm its
   container is stopped before copying any data. Do not stop other QQ accounts.
3. Keep one private offline backup of the entire QQ and NapCat configuration
   directories, with their original ownership. Preserve the password and login
   recovery state; never copy secrets into the repository or Nix store.
4. After the `gaoji-napcat` system user is created, transfer ownership of this
   instance's persistent directory to it before starting native QQ. Existing
   child files are not recursively changed by tmpfiles.
5. Switch the configuration. The native pre-start updates the existing
   reverse-WebSocket client and WebUI binding, retaining the WebUI token,
   authentication settings and unrelated configuration. Ambiguous clients stop
   startup instead of silently deleting another connection.
6. Verify the actual QQ account, version, WebSocket connection, loopback port,
   login notification and failed-unit status. A running service alone does not
   establish that QQ is logged in. Complete any Tencent verification manually.

Health recovery and the bounded password-login timer automatically use the
selected service. A previous CAPTCHA/security latch is intentionally retained.
The password-login unit skips a tick while the local WebUI port is not yet
listening, rather than reporting an expected startup delay as a service failure.
Changing software versions does not bypass account verification or guarantee
continuous login.

## Rollback

Stop both QQ units and their health/login timers first. Restore the private
offline QQ/config backup with its original ownership, then switch to the prior
configuration (or set `backend = "docker"` with the previous image pin).
Remove a temporary mask on the old unit only when deliberately rolling back.
Never reuse a downgraded client's modified database as the only rollback copy.
After successful login and a stability check, remove the migration backup when
it is no longer needed; do not accumulate one for every rebuild.
