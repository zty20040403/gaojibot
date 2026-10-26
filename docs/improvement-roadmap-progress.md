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

- [x] Empty sandbox allowlist denies access; explicit allow-all option.
- [x] CPU, memory and PID limits apply in every sandbox mode.
- [x] Dedicated OCI sandbox network; deny LAN, Tailscale, metadata and host access
      while retaining public package downloads. Modes: isolated, none, bridge.
- [x] Provision and verify network policy in NixOS and App server installation.
- [x] Restrict shared `/shell reset` and `/shell destroy` to administrators.
- [x] Remove personal identifiers from public docs; keep ops logs untracked.
- [x] Replace personal host defaults with configurable, generic defaults.
- [x] Focused tests, full PG suite, Nix checks and Linux module evaluation.
- [ ] Live isolated-network acceptance and rebuilt Desktop helper image test.
      VM networking is not covered by the OCI bridge policy; see sandbox-security.

### Phase 2: CI and dependencies

- [x] Fix baseline test failures rather than weakening assertions.
- [x] Add Ruff E9/F to CI and the Nix dev shell.
- [x] Require Python 3.12; export requirements from uv.lock and check drift in CI.
- [x] Update checkout/setup-python Actions; verify Python and Nix gates locally.
      Hosted GitHub Actions remain unverified until an authorized push.

### Phase 3: Prompt caching and untrusted input

- [x] Stable system prefix and block-sliding timeline; dynamic facts near query.
- [x] Explicit Anthropic cache breakpoints.
- [x] Untrusted chat/web/OCR/transcript boundaries outside system messages.
- [x] Cache-hit chart and Prometheus measurements.
- [x] Stable-prefix and forged-boundary regressions; PG/UI gates.
- [ ] Record real cache-hit comparison after an authorized deployment.

### Phase 4: Reaction feedback and replay

- [x] Scoped NapCat reaction ingestion, filtering and configurable emoji mapping.
- [x] PG vote migration, per-principal deduplication, retraction and asker flag.
- [x] Context-debug vote totals and negative-feedback filter.
- [x] Preserve administrator judgement as the authoritative final label.
- [x] Private case export and bounded candidate-vs-original replay/judge reports.
- [x] Vote/notice/replay tests, PostgreSQL concurrency/export and UI gates.
- [ ] Real QQ emoji-ID confirmation and live reaction acceptance.
- [ ] Real-provider A/B quality comparison; tool/multimodal and historical-memory
      replay remain unsupported and are not counted as evaluated improvements.

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

### Phase 1 validation

- Permissions/resource regressions: 125 tests passed; network, installer and
  update regressions: 163 tests passed. Both used mocked host mutations.
- Full PG suite: 1,591 tests, OK (12 skipped), 86.852 seconds. Log:
  `.cache/roadmap/phase1-final.log`. A fixture relying on an implicit personal
  hostname now declares that hostname explicitly; validation was not relaxed.
- `nix flake check`: local aarch64-darwin package and import checks passed.
  Other architectures were not built. The x86_64-linux NixOS module was evaluated
  separately, confirming limits, deny-by-default access and network dependencies.
- Existing containers on an unrestricted bridge are rejected, not silently
  recreated or deleted. Public downloads, private-network denial and process
  exhaustion still require an isolated live environment before rollout.
- Private ops documents remain on disk under ignored `ops-notes/`; Git history
  was not rewritten. No production host, QQ account or user database was touched.

### Phase 2 validation

- CI now creates a separate `gaoji_acceptance` database, enables vector and
  migrates both databases. Process-loss tests are enabled explicitly. The
  production/test DSN guards were preserved, not bypassed.
- Ruff E9/F: 64 initial findings resolved. Compatibility exports and imports
  needed to initialize test fixtures are retained explicitly. Fixed a real
  undefined-global bug in command memory lookup, plus stale type names.
- Memory lookup regression covers visible group/user entries and rejects other
  users/groups. Full PG suite: 1,592 tests, OK (12 skipped), 85.580 seconds;
  `.cache/roadmap/phase2-full.log`.
- Requirements export drift check passed; pinned hashes now come from uv.lock.
  Only Ruff adds a new package version; old Python-specific alternatives were
  pruned without upgrading existing Python 3.12 runtime versions.
- Local `nix flake check` package/import checks passed on aarch64-darwin.
  Linux builds and hosted CI are not claimed as tested locally.

### Phase 3 validation

- Stable system/tool policy precedes quoted chronological history; minute clock,
  memory, current identity, OCR and current-turn context follow that prefix.
  The live suffix moves in blocks of eight without introducing timeline holes.
- Unchanged history blocks use a process-keyed HMAC boundary. Dynamic material
  uses fresh random boundaries. This intentionally avoids randomizing the whole
  history on every request, which would invalidate the desired cache prefix.
  Markup is escaped; authorization and scope checks remain separate enforcement.
- Tool receipts stay raw in the audit/replay store and are quoted only on the
  model wire. Report-correction paging budgets include escaping and delimiters,
  so a page cannot be credited as completely read after wire truncation.
  Validation errors that may echo model-controlled text are quoted outside
  system messages as well.
- Anthropic cache-read and cache-create inputs are included in total input;
  only reads count as hits. OpenAI-compatible/DeepSeek cache usage is normalized,
  merged into child traces, persisted through existing usage records and exposed
  through gaoji_model_cached_tokens_total. No real cost reduction is claimed.
- Full PG-enabled suite: 1,602 tests, OK (12 skipped), 82.998 seconds;
  .cache/roadmap/phase3-full.log. Early failures exposed changed prompt locations
  and wire-budget compatibility; those were fixed without relaxing size limits.
- Ruff passed for the same paths checked by CI. Admin TypeScript/Vite build
  passed. Isolated headless Chrome checks at 1280x720 and 390x844 passed for both
  chart modes, exact tooltip values and tooltip viewport bounds; screenshots
  were inspected. Fixed a tooltip clipped by the mobile chart scroll container.
- Temporary chart fixtures were removed and their Vite process stopped. No
  real provider, QQ account or production server was contacted. Live one-day
  cache-hit comparison remains pending an authorized deployment.

### Phase 4 validation

- Added Alembic 0030 and upgraded only the two disposable local databases.
  Six notice/store regressions cover filtering, repeat events, replacement,
  retraction, different reply chunks and administrator-label independence.
- PostgreSQL concurrency test sent 16 identical writes through two journal
  instances: exactly one change persisted. A scoped read-only repeatable-read
  export excludes other groups, future messages and hidden questions.
- Export files use exclusive creation with mode 0600; feedback-exports remains
  ignored. Reports contain private raw chat and require review before sharing.
  Replay requires explicit provider-data consent, exact profile restrictions,
  bounded cases/timeouts and no tool execution. The candidate sees no old answer
  or feedback label. Eight local tests exercise scoring, privacy boundaries,
  cancellation and the real context-projection code with mocked model calls.
- A/B reports separate negative wins/ties/losses, positive regressions, errors
  and skipped coverage. They never set release_ready=true. Multimodal/tool turns,
  missing evidence and possibly truncated original answers are skipped. Current
  memories are not used as fabricated point-in-time memories.
- Fixed an SSE detail-refresh gap: runtime data changes do not increment admin
  configuration versions. Vote detail/filter now refresh on actual data updates;
  unfinished notes survive updates, and saving cannot erase newer edits.
  Direct turn lookup also makes old negative turns accessible beyond 500 rows.
- Full PG suite: 1,617 tests, OK (12 skipped), 87.310 seconds;
  .cache/roadmap/phase4-full.log. CI-path Ruff and admin TypeScript/Vite build pass.
  Isolated Chrome at 1280x800 and 390x844 verified filters, count changes with an
  unchanged configuration version, draft preservation and no viewport overflow.
  Both screenshots inspected; temporary frontend fixtures/server removed.
- No real QQ, model provider or production host was contacted. Emoji mappings
  intentionally remain empty until a real test-group event confirms the IDs.
