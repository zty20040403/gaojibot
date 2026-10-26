# Live File Outbox Acceptance

This is an opt-in acceptance tool, not a production API or a background task.
It exercises the real file-outbox implementation, real SIGKILL of owned child
processes, a separate PostgreSQL test database, and an existing authenticated
NapCat WebUI. It does not restart the bot, open a network port, change NapCat
configuration or approve server operations. Never run it without approval for
the exact test account, group and output directory.

## Five Boundaries

| Boundary | Persisted state after kill | Expected recovery |
| --- | --- | --- |
| Queued | queued | Prepare the stored artifact and upload once |
| Prepared | queued | Verify the stored bytes and upload once |
| Claimed, before upload | sending | No receipt: unknown, never silently retry |
| Upload returned, before settlement | sending | Find the QQ receipt without re-upload |
| Acknowledged | acknowledged | Preserve the receipt, no further upload |

The claimed case intentionally remains unknown: a recovered process cannot infer
that an unacknowledged upload never happened. Passing this test means that the
ambiguity is preserved honestly, not that every interrupted delivery can finish
automatically. Real QQ failures must stay visible; the test must not relabel a
missing receipt as success.

Each test file contains only generated acceptance text, at most 1 KiB. A unique
filename and an on-disk, fsynced reservation cap the entire run at four uploads.
The verifier also counts attempted retransmissions so the guard cannot mask an
outbox regression. Group files are not deleted by the tool.

## Execution

Run from the repository root using its Python environment. With no execute flag,
the tool prints a plan and performs no network, database or file operations:

```sh
python -m tools.live_file_outbox_acceptance \
  --webui-url http://127.0.0.1:6100 \
  --bot-id 123 --group-id 456 --requester-id 789
```

The numeric identifiers above are examples, not a configured destination.
For an approved live run, use the actual approved identifiers, add
`--execute-live --confirm-group <same-group-id>`, `--output-dir <new-private-directory>`
and `--token-file <existing-WebUI-token-file-or-webui.json>`.

Set `TEST_POSTGRES_DSN` in the process environment to a dedicated test database
named `gaoji_acceptance` or `gaoji_acceptance_<suffix>`. Never use the bot database.
Use a disposable PostgreSQL instance, not merely another database on the
production HA cluster. The process-loss suite refuses a standby, an instance
with an active replica, or one configured for synchronous standbys: its repeated
schema migrations can stall commits while the shared standby catches up.
The tool creates a random test schema, applies migrations there and drops only
that schema. It does not create or drop databases. It refuses an existing output
directory so restarting the command cannot silently replay a previous run.

The WebUI URL must be an already available loopback HTTP(S) endpoint, with no
credentials, path, query or fragment in the URL. Requests do not use system
proxies or follow redirects. The client reuses NapCat's authenticated
`/api/Debug/call` interface; it does not open a separate OneBot server. Only login
identity, online status, the approved group's root file list and exact generated
uploads are allowed. Second-factor requirements, account mismatches, offline or
unconfirmed online status stop the run. Cached account identity is not evidence
that QQ is online.

Credentials remain in memory. The private `result.json` contains the current
phase, case states, hashes and receipts but no tokens or DSN. Preflight failures
are recorded too, before any test schema or upload is created. Do not publish
that file. Inspect it
before removing the disposable test directory. A failed cleanup is recorded as
a failure rather than hidden. Application, bot and user data are never cleanup
targets. Only owned test child PIDs can be killed, and all such children are
joined before the tool exits.

## Local Harness Verification

The regular guard tests do not contact QQ:

```sh
python -m unittest tests.test_live_file_acceptance
```

Set `TEST_FILE_ACCEPTANCE_DSN` to a dedicated **local** acceptance database and
run `tests.test_live_file_acceptance_process` to check the complete harness with
real child-process loss and a **simulated** NapCat HTTP server. This suite makes
four simulated uploads across all five boundaries. It does not establish live
QQ acceptance. The live gate in `task-outcome-closure.md` stays open until the
approved real run has produced and verified its receipts.

The Debug transport explicitly supplies `file_count=50`: direct debug action
calls can skip the normal schema-default handling. That does not bypass an
unavailable QQ file service. A file-list timeout at preflight stops the run with
zero uploads; it must not be reported as a successful process-loss test.
