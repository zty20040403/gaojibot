# Local iMessage Configuration Acceptance

## Scope

Local source and desktop work only. No SSH to h310, h610, tank or any other
user server; no real BlueBubbles account, Apple ID, QQ session, model API key,
production data or Docker resources were used. The prior QA VM remained off.
No commit, push or NixOS rebuild. Existing shared worktree changes are preserved.

## Implementation

- Independent enable flag, Mac URL, own identity, private password and explicit
  QQ/chat GUID mappings. Default disabled. No SMS routes or automatic contact
  enumeration. Existing Matrix bundles are merged by their canonical QQ group.
- Password binding to URL/identity and configuration revision, private backup
  round-trip, public-state exclusion, credential omission when disabled.
- A fixed `probe-imessage` operation runs in the Bot Compose network, with its
  payload on stdin. Only explicitly configured chats are queried. Probe success
  means read API compatibility, not AppleScript permission or successful send.
- Activation repeats the probe before changing the running Bot or QQ setup.
- Opt-in supervised polling, inclusive bounded time windows, persisted page
  offsets, first-enable baseline, per-chat limits and round-robin scheduling.
  Changed page counts replay the window; durable sources/outbox keys deduplicate.
- Inbound source completion now follows all idempotent fan-out writes, repairing
  partial enqueue after interruption. This also benefits the existing bridges.
- AppleScript text sending includes the required temp GUID but does not treat it
  as durable server-side idempotency. Ambiguous responses park the send and await
  a matching self-message echo, rather than automatic retries.
- Error messages omit raw upstream bodies and password-bearing URLs. Responses
  have bounded bytes/time and do not follow redirects.

Protocol checked against the official [REST documentation](https://docs.bluebubbles.app/server/developer-guides/rest-api-and-webhooks),
[chat route](https://github.com/BlueBubblesApp/bluebubbles-server/blob/master/packages/server/src/server/api/http/api/v1/routers/chatRouter.ts),
[database date bounds](https://github.com/BlueBubblesApp/bluebubbles-server/blob/master/packages/server/src/server/databases/imessage/index.ts),
and [send validator](https://github.com/BlueBubblesApp/bluebubbles-server/blob/master/packages/server/src/server/api/http/api/v1/validators/messageValidator.ts).
These public sources were read without private credentials. No source instructions
were used to direct user-data access.

## Verified

100 Python tests passed across `test_desktop_imessage`, `test_desktop_matrix`,
`test_desktop_components`, `test_instance_manager`, `test_local_runtime`,
`test_bluebubbles_poll`, `test_bridges` and `test_onebot_delivery`.

- Real localhost HTTP fixtures check encoded passwords, route auth, fixed probe
  dispatch, chat identity, query window, incompatible metadata and redirects.
- Temporary SQLite ledgers/state/outboxes test 450 equal-timestamp events,
  inclusive boundaries, partial pages, reopening a persisted cursor after a
  simulated process interruption, changed row count, changed routing during a
  request, another unavailable chat, partial fan-out and echoed ambiguous sends.
- These do not prove a real BlueBubbles Mac, Apple network or PostgreSQL-backed
  bridge deployment. No actual external message was sent.
- Ten desktop control protocol tests passed. The first sandboxed run could not
  bind localhost (`EPERM`); the permitted localhost-only rerun passed.
- TypeScript/Vite build passed. Actual Electron/preload UI with local IPC fixtures
  passed legacy manager compatibility, key replacement, combined Matrix/iMessage
  save, adding/removing mappings, polling/draft/focus preservation and model pages.
- Desktop and 390px screenshots of the iMessage panel were visually reviewed;
  fields and buttons fit without page overflow. They contain synthetic data.

## Remaining Evidence

Native iMessage attachment transfer is not implemented; existing text fallback
is preserved. Real BlueBubbles and QQ text delivery, Mac automation permission
checks, Linux execution of the new probe, Windows real
execution, code signing and the complete V1 remain unverified. This is not a
history importer: messages appearing late in an already completed time window
are not guaranteed to be imported. Both machines must maintain correct clocks.

## Final Preview Packages

0.1.17 packages were built locally with the same 275-file runtime archive:
`ba31b0f903ac3546ae016882028e54edb728233a1b9add22ccbb183121320458`.
Runtime application version remains 0.20.0; bundle identity is content-addressed.

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| Gaoji-0.1.17-arm64.dmg | 132316589 | aea5eed649bb0a59b9b7e18bebc069b67d85cf9a9450963b9191107b947386cb |
| Gaoji-0.1.17-arm64-mac.zip | 132231853 | 726a9e298a7446fdf26531ee27e2307548eef6656a363ac3937a352d8b271c3a |
| Gaoji Setup 0.1.17.exe | 114988961 | ddc1e23cde32621447c9fd4a1bc6650a08346020165f0cb6f1ab1838ea7f535b |
| Gaoji-0.1.17-win.zip | 157803011 | 2bf4bb1dc94fd8dd54c621d626b85a484061974f06cde5d0c7a7b7ea67e30526 |

- Both package verifiers matched critical runtime modules, installer and bridge
  worker assembly to current source. Windows PE structure passed verification.
- `hdiutil verify` passed for the DMG; both ZIPs passed complete CRC checking.
- The final packaged Mac executable passed `verify-setup-ui.cjs`, including
  `verify-models-ui.cjs`, through its real preload and renderer with IPC fixtures.
  Settled desktop and narrow iMessage screenshots were reviewed again.
- All test windows, temporary localhost servers and build processes finished.
  No local VM was started, no host Docker objects were changed, and no server
  connection or deployment was made in this turn.

Both platforms remain unsigned previews. These checks do not close the remaining
real-platform and full V1 acceptance requirements above.
