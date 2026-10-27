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

- [x] Replace four forced keyword routes with a lightweight model classifier.
- [x] Classifier unavailability falls back to automatic tool selection.
- [x] Update routing documentation, configuration and Trace accounting.
- [x] Natural-routing regressions and complete PG suite.
- [ ] Authorized real-provider comparison; Phase 4's replay currently excludes
      tool/multimodal turns and cannot prove tool-choice quality on its own.

### Phase 6: Responsibility boundaries

- [x] ToolHandler interface and registry; memory and alert handlers extracted.
- [x] Typed TurnState owns reply media, source evidence and delegation counters.
- [x] Scoped context search/expansion, group roster and pin handlers extracted.
- [x] Web search, installed skills and packaged-source inspection extracted.
- [x] Image OCR/vision, QQ video and voice handlers extracted.
- [x] Global sticker search and visual reply handlers extracted.
- [x] Sub-Agent delegate/run/resume handlers and typed dispatch callbacks extracted.
- [x] Existing scoped reminder create/list/cancel handlers extracted.
- [x] Fleet read/management/job/artifact/guardian handlers extracted.
- [x] Move agent-executor-backed tool dispatch.
- [x] Registry-driven tool availability and complete handler coverage.
- [x] Agent loop/trace and OneBot orchestrator naming with temporary re-exports.
- [x] Admin API routers split by responsibility.
- [x] Startup-time context assembly; migrate consumers and remove old globals.
- [x] Tool/loop/routing regressions, full PG suite and admin UI build.

### Phase 7: Nonblocking database work

- [x] Event-loop lag sampling and Prometheus metric.
- [x] Move hot-path synchronous store calls to a bounded/thread-safe db_call.
- [x] Slow-store concurrency regression and local before/after P99 evidence.
- [ ] Measure production event-loop lag after an authorized deployment.

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

### Phase 5 validation

- Removed image/video/alert/model-status keyword forcing. A bounded classification
  uses only AI_SIMPLE_CHAT_PROFILE, with thinking disabled, at most 128 output
  tokens and a default three-second budget covering history loading and inference.
  It cannot fall back to another profile or perform extra empty-output recovery.
- Missing profile, cached unavailable health, invalid schema, errors and timeout
  leave tool_choice=auto. Cancellation propagates. Explicit commands and resumed
  work bypass classification; existing task routing rules remain documented.
- Only applicable enabled read tools are offered. Per-conversation scope, current
  message exclusion and explicit replies are tested; this bounded classifier view
  does not replace the answer model's full chronological context. Permissions and
  final tool availability are enforced independently of the classifier's answer.
- Returned usage and decision metadata are added to Trace without polluting the
  answer transcript or replacing its requested/actual model identity. Error text
  is not stored, since provider exceptions can contain credentials.
- Focused routing/config/fleet tests: 58 passed. Full PG suite: 1,629 tests,
  OK (12 skipped), 90.990 seconds; .cache/roadmap/phase5-full.log. CI-path Ruff
  and git diff --check pass. This phase does not modify the admin UI.
- No live model, QQ or production-host calls. Mock classification tests prove
  routing contracts, not semantic accuracy or latency on the user's local Qwen.

### Phase 6 first extraction validation

- Added ToolHandler (definition, availability, host policy and async execution),
  a registry, scoped ToolContext and mutable per-turn TurnState. All former
  nonlocal reply/delegation state now belongs to that turn; list/set defaults
  are independent between turns.
- Memory add/list/remove and authoritative alert lookup now live in domain
  modules. Group-edit authorization, provenance, visible-memory scope checks,
  secret rejection and alert ranking semantics are preserved.
- Migrated tools share the same visibility/execution gate. A disabled or
  unavailable registered tool returns an error, never falls through to legacy
  execution. Capacity, approval, telemetry and durable execution wrappers remain
  outside domain handlers. Definition projections are copied, not mutated.
- The temporary legacy catalog projection preserves prompt ordering while other
  domains still use the old dispatcher. The 22-flag available_tools interface is
  NOT removed yet; full registry coverage and subsequent Phase 6 work remain open.
- Focused registry/agent/loop/routing tests: 65 passed. Full PG suite: 1,638 tests,
  OK (12 skipped), 84.575 seconds; .cache/roadmap/phase6-domain-full.log. CI-path
  Ruff, diff whitespace check and admin TypeScript/Vite build passed. No UI source
  or production services were changed. Existing eager-startup ForwardRef warnings
  are still pending the Phase 6 startup-assembly refactor.

### Phase 6 context extraction validation

- Moved context expansion/search, group roster and pin/unpin into tools/context;
  the registry now owns nine handlers. The current-turn bot and capabilities are
  explicit state. Legacy catalog ordering and outer execution guards remain.
- Kept conversation-scope filtering for raw messages, chapters, pins and turn
  expansion. Semantic results are post-filtered to the current group and allowed
  memory scopes, then deduplicated against lexical results. Semantic failure
  leaves lexical results available. Group identities use canonical mentions.
- Five new regressions cover current/foreign-group evidence, other-user memory,
  semantic failure, scoped pin/unpin, expansion and current-group roster output.
- Focused tests: 85 passed. Full PG suite: 1,643 tests, OK (12 skipped), 83.834
  seconds; .cache/roadmap/phase6-context-full.log. CI-path Ruff, diff whitespace
  check and admin TypeScript/Vite build passed. No production host was contacted.
- Phase 6 is still in progress: remaining tool domains, registry-only selection,
  agent naming, admin routers and delayed startup assembly are not finished.

### Phase 6 source extraction validation

- Moved web search, use_skill and inspect_source into tools/sources; twelve
  handlers now share registry visibility and execution checks. The current user
  text is explicit TurnState data for a search without a supplied query.
- Preserved search bounds/freshness, source deduplication across calls, skill
  lookup and the existing source-inspection whitelist. Cancellation propagates;
  disabled capabilities do not invoke network search or source/skill readers.
- Focused tests: 84 passed. An initial new-test failure was a helper argument
  named name colliding with the skill's name parameter; corrected the helper.
- Full PG suite: 1,647 tests, OK (12 skipped), 87.152 seconds;
  .cache/roadmap/phase6-sources-full.log. CI-path Ruff, diff whitespace check and
  admin TypeScript/Vite build passed. No real search/provider/QQ requests made.

### Phase 6 media extraction validation

- Moved five image/video/voice handlers into tools/media; the registry owns 17
  handlers. Both intent discovery and video execution use one video resolver.
  Resolved OCR inputs and voice message IDs are explicit per-turn state.
- Preserved current-message, reply and same-user recent-cache lookup in groups
  and private chats, canonical-handle scope checking, transient image handling
  and the existing 45-second foreground vision wait cap. Voice is prepared once
  per turn and still delivered by the existing reply flow, not the handler.
- Focused tests: 87 passed. Initial import regression exposed a legacy entrypoint
  export of _resolve_video_reference; it now forwards to the extracted resolver
  until delayed startup removes the legacy exports. No duplicate resolver body.
- Full PG suite: 1,654 tests, OK (12 skipped), 85.021 seconds;
  .cache/roadmap/phase6-media-full.log. CI-path Ruff, diff whitespace check and
  admin TypeScript/Vite build passed. Real OCR/vision/voice/QQ were not invoked.

### Phase 6 sticker extraction validation

- Moved global sticker search, sticker preparation and QQ face preparation into
  tools/stickers; 20 handlers now use the registry. Matching/variation behavior
  and the final reply delivery flow remain unchanged.
- Regressions cover current-turn handles, query precedence over stale handles,
  recent-image avoidance for change requests, missing matches, safety/type
  checks, one visual reply per turn and the no-library legacy fallback.
- Focused tests: 93 passed. Full PG suite: 1,660 tests, OK (12 skipped), 84.609
  seconds; .cache/roadmap/phase6-stickers-full.log. CI-path Ruff, diff whitespace
  check and admin TypeScript/Vite build passed. No real group messages sent.

### Phase 6 subagent extraction validation

- Moved delegate_agent, run_subagents and resume_subagent into tools/subagents;
  the registry owns 23 handlers. Coordinator operations are typed per-turn
  callbacks; existing context packets, durable dispatch and progress hooks stay
  in their original orchestration path. No subagents were launched for this work.
- Preserved the three-delegation budget, shared run/resume gate, input validation
  and cancellation behavior. An unbound callback returns an unavailable error
  before changing counters rather than touching a not-yet-defined closure.
- Added actual chat-dispatch tests for all three handlers with a mocked
  coordinator, asserting current scope/user and delegated context packets.
  The first focused command named a nonexistent test module; reran the correct
  agent modules: 145 tests passed.
- Full PG suite: 1,665 tests, OK (12 skipped), 85.844 seconds;
  .cache/roadmap/phase6-subagents-full.log. CI-path Ruff, diff whitespace check and
  admin TypeScript/Vite build passed. Live delegated-task delivery remains untested.

### Phase 6 reminder extraction validation

- Moved existing reminder_set/list/cancel into tools/reminders; the registry owns
  26 handlers. This is not Phase 9's cron/routine feature. Existing ReminderStore
  scheduling, retry and scope policy remain unchanged.
- New regressions check group/private scope, host-derived creator identities,
  absent ledger/store behavior, handle/date validation and disabled execution.
  Model-supplied scope/creator fields cannot replace the actual event identity.
- Focused tests: 87 passed. Full PG suite: 1,669 tests, OK (12 skipped), 85.524
  seconds; .cache/roadmap/phase6-reminders-full.log. CI-path Ruff, diff whitespace
  check and admin TypeScript/Vite build passed. No real reminders were scheduled.
- tool_executor.py is now 1,909 lines (3,126 before Phase 6). Remaining work is
  fleet/agent-backed dispatch, registry-only catalog selection, agent naming,
  admin routers and startup assembly. Phases 7-9 have not begun.

### Phase 6 fleet extraction validation

- Moved 19 fleet handlers into tools/fleet; 45 handlers now share registry
  visibility and execution checks. Host-granted fleet/log/management capabilities
  remain separate, and management execution rechecks administrator registration.
- Preserved actor/origin derivation from the actual event, scoped sandbox artifact
  reads, worker-vs-probe targeting, operation verification/idempotency payloads,
  bounded logs and observe-only guardians. Semantic runbook failures retain
  lexical retrieval; remote failures remain structured and cancellation propagates.
- Seven new regressions plus existing dispatch/provider coverage: 88 focused
  tests passed. Full PG suite: 1,676 tests, OK (12 skipped), 85.725 seconds;
  .cache/roadmap/phase6-fleet-full.log. CI-path Ruff, diff whitespace check and
  admin TypeScript/Vite build passed. No production host or external API contacted.
- tool_executor.py is 1,550 lines. Registry-only selection, executor-backed tools,
  naming, admin routers and delayed startup remain pending in Phase 6.

### Phase 6 registry completion validation

- All 74 built-in tools now have registered handlers. Removed the 22-boolean
  available_tools interface, legacy catalog projection and executor fallthrough.
  The registry validates full catalog coverage and preserves original prompt order.
- Sandbox, browser, conversation and shared-source adapters use the existing
  scoped executor and delivery/error handling. Missing capabilities or executors
  fail closed both at advertisement and execution, including direct tool calls.
  Private sessions no longer advertise source tools without a working executor.
- VM-specific descriptions are projected onto copies; Nix package search is not
  available for VM sandboxes. Projections cannot rename registered tools, and OCI
  definitions remain unchanged across VM calls. No nonlocal state remains in
  tool_executor.py, now 1,491 lines versus 3,126 before Phase 6.
- Eight new regressions exercise every executor mapping, capability/global
  disables, absent dependencies, stable catalog order, VM/OCI separation and
  cancellation. Focused tests: 117 passed. Full PG suite: 1,684 tests, OK (12
  skipped), 85.276 seconds; .cache/roadmap/phase6-catalog-full.log. CI-path Ruff,
  diff whitespace check and admin TypeScript/Vite build passed.
- Phase 6.1 is complete locally. Naming/compatibility, admin routers and delayed
  startup assembly remain in Phase 6; Phases 7-9 have not begun.

### Phase 6 naming and compatibility validation

- Moved the model-independent loop to agent_loop.py and renamed DeepSeekTrace
  to AgentTrace. Moved the OneBot service to onebot_chat.py; the separate
  application/chat_orchestrator.py remains unchanged. Updated internal callers,
  test patch targets, evaluation tools and README paths.
- deepseek.py and chat_orchestrator.py are one-release import-only shims. The
  compatibility trace names reference the same type, and legacy runtime setup
  configures the canonical runtime rather than making a second instance.
- AST comparison against the previous commit verified that both moved modules
  contain only the planned naming/import/docstring changes. Compatibility and
  architecture regressions enforce one implementation and bounded shim size.
- First full regression failed two tests: an architecture assertion still named
  chat_orchestrator.py, and an intent-routing mock patched the legacy module.
  Corrected both references without weakening behavior assertions. The initial
  failure log remains .cache/roadmap/phase6-naming-full.log.
- Focused tests after correction: 113 passed. Final PG suite: 1,688 tests, OK
  (12 skipped), 85.486 seconds; .cache/roadmap/phase6-naming-full-retry.log.
  CI-path Ruff, diff whitespace check and admin TypeScript/Vite build passed.
- Phases 6.1 and 6.2 are complete locally. Next: split admin API routers while
  retaining AccountRoute authentication for every child route, then move runtime
  assembly to startup. No push, production rebuild or live provider request made.

### Phase 6 admin router validation

- Split 51 page endpoints across 12 page routers, plus a protected adapter for
  the existing fleet router. admin.py is now 112 lines instead of 2,918. Shared
  services, request contracts, versioned mutations and realtime monitoring have
  explicit owners; existing security and fleet implementations remain in place.
- Each child router uses the same AccountRoute authentication wrapper. Legacy
  and v1 paths share handlers, configuration versions, audit records and the
  event broker. Existing AdminServices and event-broker imports remain usable.
- AST comparison verified 82 moved endpoint/helper/class definitions unchanged
  except relative imports. No business behavior, task handling or SSE policy was
  intentionally changed. Custom nested admin prefixes are covered as well.
- The first focused run exposed 22 failures: FastAPI's lazy router inclusion
  meant aliases were absent when scanning the aggregate router. Aliases are now
  created on each child before inclusion, without depending on framework-private
  traversal APIs. The corrected focused run: 113 tests, OK (1 skipped).
- Added five API boundary regressions and an architecture regression. Every
  registered API rejects anonymous requests; every mutation retains CSRF/origin
  checks. Version conflicts and audit identities are shared across old/v1 paths.
  Boundary/architecture run: 14 tests passed. Its initial standalone run needed
  explicit NoneBot initialization, matching existing admin test fixtures.
- Full PG suite: 1,694 tests, OK (12 skipped), 89.146 seconds;
  .cache/roadmap/phase6-admin-full.log. CI-path Ruff, diff whitespace checks and
  admin TypeScript/Vite build passed. Pre-existing eager-startup annotation
  warnings remain for Phase 6.4; they are not suppressed by this extraction.
- Phases 6.1-6.3 are complete locally. Phase 6.4 startup assembly is next;
  Phases 7-9 and previously listed live acceptance checks remain pending.
  No push, production connection, deployment or real model/QQ request was made.

### Phase 6 delayed startup validation

- Moved application assembly to the driver startup hook. The 41-line plugin
  entrypoint retains matcher registration and lifecycle hooks, not service/store
  instances. OneBotRuntime owns assembly and shutdown; typed matcher adapters
  forward to its handlers, and background workers start after interrupted-turn
  recovery. All 28 message/notice handlers retain their original registrations.
- Concurrent starts assemble once. Events before readiness are rejected, approval
  messages stay outside chat history, and failed handler/worker initialization
  closes the returned context. Shutdown releases only this owner's model client.
  Cleanup of resources allocated inside a failing build_app_context before it
  returns is not newly guaranteed by this wrapper.
- Migrated eight handler test modules to isolated per-test service fixtures,
  removing their dependence on import-time application globals. A fresh-process
  import regression rejects attempts to open PostgreSQL or SQLite and confirms
  no runtime state/cache directories are created on import.
- The first lifecycle run had one failed assertion: Python exposes the imported
  memory module as a package attribute. Corrected the check to distinguish that
  module from the removed service instance, without permitting database opens.
  Handler regressions: 71 tests passed. Lifecycle/architecture/runtime: 25 passed.
- Added nine startup regressions. Full PG suite: 1,703 tests, OK (12 skipped),
  90.800 seconds; .cache/roadmap/phase6-startup-full.log. CI-path Ruff, diff
  whitespace checks and admin TypeScript/Vite build passed.
- Phases 6.1-6.4 are complete locally. Phases 7-9 and the previously listed live
  acceptance checks remain pending. No push, production connection, deployment
  or real model/QQ request was made.

### Phase 7 nonblocking database validation

- Added an application-owned DatabaseCalls boundary with four concurrent worker
  slots, ContextVar propagation, exception propagation and shutdown draining.
  Cancelling a caller does not release its slot or close stores while its write
  is still running. It does not retry queries or pass live cursors between threads.
- Offloaded OneBot canonical ingest, profile/context reads, normal chat history,
  journal events, reply planning, durable send attempts and receipts, and related
  command/tool store calls. Same-scope ingest is serialized without holding up
  unrelated groups. Mutable memory/profile stores now lock complete read/write
  operations, including persistence, rather than only individual list updates.
- Shutdown waits for tracked turns to finish cancellation cleanup, then drains
  database work before closing stores/pools. A turn cancelled during creation
  still records the newly created turn as aborted. This is not an async rewrite
  of every store, background worker, bridge or third-party tool implementation.
- Added one-second event-loop lag sampling after startup recovery, a latest-value
  Gauge, a Histogram and the current lag in the admin telemetry totals. Metrics
  collection no longer queries delivery stats directly on the event loop.
- Review found that moving media ingest to a worker thread also moved its asyncio
  Event notification. Notifications now use the worker loop's thread-safe queue;
  clearing the event before polling also prevents losing a wakeup during a query.
- Initial full run: 1,717 tests with 84 errors, all caused by eight domain test
  fixtures missing the new db_call dependency. Added real DatabaseCalls instances
  and cleanup to those fixtures; retained every existing behavioral assertion.
  Their 46 focused tests passed. Boundary/startup/metric/benchmark checks: 39
  passed. Media notification and parsing checks: 26 passed.
- An added weak-lock cleanup assertion initially retained a cancelled task's
  exception frame. It now releases that completed task and collects the frame
  before checking lock reclamation; ordering and cross-group progress assertions
  remain unchanged. Earlier sampler timing and startup fixture failures are
  preserved in .cache/roadmap/phase7-boundary-focused.log and
  .cache/roadmap/phase7-hotpath-focused.log.
- Final full PG suite: 1,723 tests, OK (12 skipped), 92.562 seconds;
  .cache/roadmap/phase7-full-final.log. CI-path Ruff and diff whitespace checks
  passed. Only changed formatting ranges were processed; AST equality was checked.
  This phase has no admin UI source changes.
- Local controlled comparison: 40 identical read-only SELECT pg_sleep(0.03)
  queries against the disposable loopback PostgreSQL database, with a 5 ms
  heartbeat. Inline P99 lag was 60.0176 ms; db_call P99 was 6.7996 ms. Total
  elapsed times were 2.3530 s and 2.2894 s. These are synthetic slow-query results,
  not production latency or a claim that PostgreSQL queries became faster.
  Evidence: .cache/roadmap/phase7-loop-benchmark-final.json.
- Phases 0-7 are implemented locally, subject to the live acceptance gates above.
  Phases 8-9 remain pending. No push, production connection, rebuild, real model
  request or QQ interaction was performed.

#### Reproducing the loop measurement

The default benchmark is synthetic and makes no network request:

```sh
.build-venv/bin/python tools/benchmark_event_loop.py
```

For a disposable local PostgreSQL database, set TEST_POSTGRES_DSN and optionally
TEST_POSTGRES_SCHEMA, then add --postgres. The tool rejects non-loopback addresses,
alternate routing parameters and database names not ending in _test or _acceptance.
It does not create or alter tables and does not print the DSN.

After an authorized deployment, compare the per-instance five-minute P99:

```promql
histogram_quantile(0.99,
  sum by (instance, le) (rate(gaoji_event_loop_lag_observed_seconds_bucket[5m]))
)
```

gaoji_event_loop_lag_seconds reports the latest sample, not a percentile. Production
sampling is once per second; the benchmark uses 5 ms to expose short blocking calls.
