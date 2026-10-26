# Gaojibot improvement roadmap

Implementation of the user-approved `async-frolicking-widget.md` plan, started
2026-10-26. Work stays in the existing local checkout, on `wip/desktop-app`.
Phase commits are local only. Publishing, production deployment and real QQ
acceptance are not implied by this roadmap.

## Completion rules

- Preserve the existing Desktop App, deployment platform and local runtime.
- Commit each phase independently after reviewing its diff and test results.
- Run the full unittest suite with an isolated PostgreSQL/pgvector database for
  each phase, plus focused regression tests. Record failures, not just totals.
- Check Nix for phases 1 and 2; build the admin UI for phases 3, 4, 6 and 9.
- Live QQ, firewall and performance checks remain explicit pending acceptance
  until actually exercised. Unit tests alone do not prove those outcomes.
- Do not publish private chat data, feedback exports, credentials or ops notes.
- Do not push, rebuild or connect to production hosts as part of local work.

## Progress

### Phase 0: Preserve and merge

- [x] Recover checkpoint `3b7aeaf` as `wip/desktop-app`.
- [x] Confirm the worktree snapshot matches that checkpoint before switching.
- [x] Fetch origin and inspect the 37 upstream commits ending at `e5f602e`.
- [x] Reconcile Docker re-exports, App RPC transport and the upstream KVM backend.
- [x] Preserve upstream OCI backend reporting in the moved Docker driver.
- [x] Add regressions for VM selection and rejection of conflicting backends.
- [x] Create an isolated local pgvector/pg17 database and apply migrations 1-29.
- [x] Finish and record the complete PostgreSQL-enabled baseline.
- [x] Commit the verified merge locally (this phase's merge commit).

### Phase 1: Safe defaults

- [ ] Empty sandbox allowlist denies access; explicit allow-all option.
- [ ] CPU, memory and PID limits apply in every sandbox mode.
- [ ] Dedicated sandbox network; deny LAN, Tailscale, metadata and host access
      while retaining public package downloads. Modes: isolated, none, bridge.
- [ ] Provision and verify network policy in NixOS and App server installation.
- [ ] Restrict shared `/shell reset` and `/shell destroy` to administrators.
- [ ] Remove personal identifiers from public docs; keep ops logs untracked.
- [ ] Replace personal host defaults with configurable, generic defaults.
- [ ] Focused tests, full PG suite, Nix checks and isolated network acceptance.

### Phase 2: CI and dependencies

- [ ] Fix baseline test failures rather than weakening assertions.
- [ ] Add Ruff E9/F to CI and the Nix dev shell.
- [ ] Require Python 3.12; export requirements from uv.lock and check drift in CI.
- [ ] Update checkout/setup-python Actions; verify Python and Nix gates.

### Phase 3: Prompt caching and untrusted input

- [ ] Stable system prefix and block-sliding timeline; dynamic facts near query.
- [ ] Explicit Anthropic cache breakpoints.
- [ ] Untrusted chat/web/OCR/transcript boundaries outside system messages.
- [ ] Cache-hit chart and Prometheus measurements.
- [ ] Stable-prefix and forged-boundary regressions; PG/UI gates.
- [ ] Record real cache-hit comparison after an authorized deployment.

### Phase 4: Reaction feedback and replay

- [ ] Scoped NapCat reaction ingestion, filtering and configurable emoji mapping.
- [ ] PG vote migration, per-principal deduplication, retraction and asker flag.
- [ ] Context-debug vote totals and negative-feedback filter.
- [ ] Preserve administrator judgement as the authoritative final label.
- [ ] Private case export and candidate-vs-original replay/judge reports.
- [ ] Vote/notice/replay tests, PG/UI gates and live reaction acceptance.

### Phase 5: Intent routing

- [ ] Replace forced keyword routing with a lightweight model classifier.
- [ ] Classifier unavailability falls back to automatic tool selection.
- [ ] Update routing documentation and compare real feedback replay results.
- [ ] Natural-routing regressions and complete PG suite.

### Phase 6: Responsibility boundaries

- [ ] Tool registry, domain handlers and a typed TurnState instead of nonlocals.
- [ ] Registry-driven tool availability and complete handler coverage.
- [ ] Agent loop/trace and OneBot orchestrator naming with temporary re-exports.
- [ ] Admin API routers split by responsibility.
- [ ] Startup-time context assembly; migrate consumers and remove old globals.
- [ ] Tool/loop/routing regressions, full PG suite and admin UI build.

### Phase 7: Nonblocking database work

- [ ] Event-loop lag sampling and Prometheus metric.
- [ ] Move hot-path synchronous store calls to a bounded/thread-safe db_call.
- [ ] Slow-store concurrency regression and before/after P99 evidence.

### Phase 8: PostgreSQL-only stores

- [ ] Migrate tests to isolated PostgreSQL fixtures.
- [ ] Remove production SQLite branches and ad-hoc schema creation.
- [ ] Keep legacy SQLite import; make Alembic the sole schema authority.
- [ ] Full PostgreSQL regressions and legacy-import acceptance.

### Phase 9: Product features

- [ ] 9.1 Per-group adaptive proactive thresholds with feedback and opt-in flag.
- [ ] 9.2 Durable cron routines: tools, command, creator ownership, budgets,
      ordinary Agent runs and durable Outbox delivery.
- [ ] 9.3 Nightly group wiki, search command/admin page, opt-in newcomer brief.
- [ ] 9.4 Sandbox web artifacts to expiring, scoped preview links in chat.
- [ ] 9.5 Delegated task collaborators and two-administrator high-risk quorum.
- [ ] 9.6 Official-SDK MCP client through the registry, conservative permissions,
      and read-only MCP server with group-scoped tokens.
- [ ] Per-feature focused/full PG tests, UI builds and recorded live gates.

## Local validation record

Baseline environment: Python 3.12 in ignored `.build-venv`, dependencies restored
with `uv sync --frozen --inexact`; `pgvector/pgvector:pg17` uses a temporary data
filesystem and a loopback-only port. No existing database is reused.

- Alembic: migrated through `0029_native_ssh_operations`.
- Sandbox runner and backend-selection regressions: 27 tests passed.
- Full baseline: 1,575 tests, OK (12 skipped), 87.297 seconds. Log is ignored at
  `.cache/roadmap/phase0-final-baseline.log`. Includes the five simulated NapCat
  process-loss boundaries; no real QQ account or model provider was contacted.
- The first two runs exposed test-environment safeguards: crash tests require a
  `gaoji_acceptance` database and its DSN must differ from the Bot DSN. Both checks
  were preserved; the successful run used two distinct disposable databases.
  Phase 2 must align CI with this contract rather than weaken either guard.

Starting Docker Desktop automatically resumed three pre-existing Desktop test
containers. They were stopped again without deleting their data; only the
isolated roadmap test database is used for validation.
