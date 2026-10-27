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

## New-plan sequencing

- The user identified `docs/project-workspace-v1-plan.md` as the newer plan.
  Complete the remaining earlier roadmap work first, then follow its M0-M7
  milestones. Reuse the routine/recovery and preview foundations; do not create
  competing implementations for the overlapping 9.2 and 9.4 requirements.
- Project quotas, version storage, member workbench and watch routines are not
  implemented by the Phase 9.2 foundation. M0 technical validation started on
  2026-10-27 after the old roadmap's local completion commit `9cfab47`. The new
  user-authored plan remains unchanged and outside these implementation commits.

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

- [x] Migrate tests to isolated PostgreSQL fixtures.
- [x] Remove production SQLite branches and ad-hoc schema creation.
- [x] Keep legacy SQLite import; make Alembic the sole schema authority.
- [x] Full PostgreSQL regressions and legacy-import acceptance.

### Phase 9: Product features

- [x] 9.1 Per-group adaptive proactive thresholds with feedback and opt-in flag.
      Local regression/build pass; real QQ feedback acceptance remains pending.
- [x] 9.2 Durable cron routines: tools, command, creator ownership, budgets,
      ordinary Agent runs and durable Outbox delivery.
      Superseded by the new project-workspace plan (由新计划取代): reuse this
      foundation in M1/M4 rather than create a second scheduler.
      Local implementation and final regression complete. Includes shared model
      budgets, child takeover, member checks, quiet completion, original-account
      delivery and atomic result/journal settlement. Keep disabled until live
      acceptance. Watch sources and project integration belong to the newer plan.
- [x] 9.3 Nightly group wiki, search command/admin page, opt-in newcomer brief.
      Local implementation, PostgreSQL regression and responsive UI checks pass.
      Includes evidence validation, versioned settings/audit, original-account
      durable delivery and fresh membership checks. Disabled by default; real
      generation quality and QQ private-message acceptance remain pending.
- [x] 9.4 Sandbox web artifacts to expiring preview links in chat.
      Superseded by the new project-workspace plan (由新计划取代): M6 adds
      version-backed previews and mandatory independent-origin boundaries.
      Scoped publication and receipt recovery, full PostgreSQL regression,
      admin build and responsive browser checks pass locally. HTTP links remain
      network-reachable capabilities; real QQ and recipient access gates pending.
- [x] 9.5 Delegated task collaborators and two-administrator high-risk quorum.
      Group command, fresh member checks, actual-executor isolation, revision
      feedback, UI and independent OTP votes are implemented. Full PG regression,
      admin build and responsive UI checks pass. Real QQ collaboration and
      two-phone review remain pending acceptance; no production deployment.
- [x] 9.6 Official-SDK MCP client through the registry, conservative permissions,
      and read-only MCP server with group-scoped tokens.
      SDK transports, scoped registry, runtime endpoint/reaper and versioned admin
      API and console are connected locally with focused regression coverage.
      Operator guide and local Docker/TLS acceptance are complete. Approval-bound
      continuation storage and original-account Outbox guards are implemented;
      main Agent/runtime recovery wiring and final PG/UI gates now pass locally.
      MCP stays disabled by default; actual QQ approval/delivery and production
      reverse-proxy acceptance remain pending. Not deployed.
- [x] Per-feature focused/full PG tests, UI builds and recorded live gates.

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

### Phase 8 PostgreSQL-only storage migration

- Added a real PostgreSQL test fixture that creates an owned, randomly named
  schema through Alembic, resets data between tests, and drops only that schema
  during cleanup. It rejects remote databases and the configured bot database.
- Converted the first batch of storage and handler tests to explicitly request
  this fixture. Reopened stores within one test share the same PostgreSQL data;
  context evaluation subcases explicitly reset their data.
- Initial fixture, ledger, pins, reminders and quota run: 23 tests passed in
  4.391 seconds; .cache/roadmap/phase8-fixture-focused.log. This is a focused
  result, not a full-suite acceptance result for Phase 8.
- Removed obsolete temporary SQLite paths from the migrated reopen tests.
  Test-source Ruff and diff whitespace checks pass.
- Removed the runtime SQLite branches and ad hoc table creation from the ledger,
  context, journal, delivery, durable jobs, Sub-Agent and security stores. Runtime
  now requires PostgreSQL and an Alembic HEAD match; failed revision checks close
  the newly opened pool. PostgreSQL advisory locks and job-lease fencing remain.
- Tests now run against class-owned or app-owned schemas. The CLI context replay
  and live evaluation paths share an isolated PostgreSQL workspace; existing
  prediction scoring still works without a database. Real model calls stay opt-in.
- Legacy migration uses a frozen SQLite fixture, not the removed SQLite runtime.
  The sandbox runner and separate model-control daemon keep their local receipt
  caches; these are not alternative Bot persistence implementations.
- Initial full run: 1,723 tests, 1 failure, 180 errors, 12 skipped, 203.644 seconds;
  .cache/roadmap/phase8-full-first.log. It exposed four obsolete SQLite-only test
  assertions and the CLI replay's in-memory backend. Those are now migrated.
  The majority of subsequent errors were caused by the 512 MiB temporary database
  filling up; that owned container exited. Its replacement uses 2 GiB temporary
  data storage and bounded WAL/checkpoint settings. No production database failed.
- A focused launch also exposed a missing NoneBot initialization when running
  vision tests independently; fixed that setup dependency. Final focused run:
  59 tests, OK, 19.057 seconds; .cache/roadmap/phase8-targeted-second.log.
  Includes legacy import, replay with mocked models, media, fixture guards and
  startup failure cleanup. Ruff and diff whitespace checks pass.
- Final full PG suite: 1,734 tests, OK (12 skipped), 230.169 seconds;
  .cache/roadmap/phase8-full-second.log. CI-path Ruff, Python compilation and diff
  whitespace checks passed. PostgreSQL confirmed zero leftover owned test schemas
  and pgvector remained in public after all fixture cleanup.
- Formatting was limited to changed ranges and checked for identical Python ASTs;
  unrelated configuration/App dictionary expansion was removed. No Desktop App
  feature or UI source changed. The separate project-workspace design is untouched.
- Phase 8 is complete locally and recorded in this phase's commit. Phases 0-8
  are implemented, with earlier live gates still pending; Phase 9 has not started.
- Work remains local. No production connection, push, deployment, rebuild or real
  model/QQ request was made.

### Phase 9.1 adaptive proactive replies

- Added explicit global and per-group opt-in, defaulting off. Existing group
  permissions, local sampling/check budgets and static behavior outside opted-in
  groups remain. Explicit mentions and stale adaptive triggers do not provoke
  unsolicited replies.
- PostgreSQL migration 0031 stores send trials, per-actor feedback and per-group/
  bot Thompson sampling arms. Only acknowledged sends learn. Duplicated triggers,
  notices and settlement workers cannot multiply observations. Restart recovery
  expires uncertain sends without resending or inventing feedback.
- Only reactions, direct replies/mentions and another member's recall count.
  Nearby unrelated group chatter is ignored. Retraction and event order are
  enforced; a missing response is neutral with low weight, not a negative vote.
  Feedback storage errors cannot stop normal message ingestion.
- Focused checks: 68 tests, OK, 6.243 seconds;
  .cache/roadmap/phase9-1-focused.log. Includes real PostgreSQL concurrency and
  reopen tests, simulated QQ receipts, startup wiring and configuration defaults.
- Configuration, reward weights, operational privacy and limitations are recorded
  in proactive-learning.md. No real QQ or model provider was contacted. Live
  feedback IDs and improved group behavior remain unverified.
- Full PostgreSQL suite: 1,766 tests, OK (12 skipped), 235.245 seconds;
  .cache/roadmap/phase9-1-full.log. CI-path Ruff, Python compilation, diff whitespace
  checks and the admin TypeScript/Vite production build pass. No UI source changed.
- Phase 9.1 is complete locally and recorded in this phase's commit. Phase 9.2
  has not started. Earlier live acceptance gates still apply; there has been no
  push, production connection, rebuild or real QQ/model call.

### Phase 9.2 routine foundation (in progress, not a completed phase)

- Added croniter with locked/exported dependencies, five-field numeric cron,
  named timezones and bounded calendar search. Tests cover local-time schedules,
  DST changes, leap days and invalid/unreachable expressions.
- Migration 0032 stores creator-owned definitions, versioned execution snapshots
  and budget reservations. Optimistic versions and per-owner limits are checked
  transactionally. Different groups, bots and creators remain isolated.
- Scheduling coalesces downtime to one occurrence and prevents overlapping runs.
  Schedule advancement and durable-job insertion share a transaction. Generated
  results survive process restart; result-to-Outbox insertion is atomic and
  idempotent. Cancel/update fence stale workers and cancel unsent deliveries,
  but never rewrite receipts for messages already sent.
- Each model attempt reserves its full per-run token allowance before execution.
  Lost/unknown attempts retain that charge; known usage settles once into the
  existing usage ledger. Accounting days use Asia/Shanghai, like current quotas.
  Daily run counts survive routine edits, and retries also consume token capacity.
  This is reservation infrastructure, not yet an execution-time model token cap.
- Terminal-job reconciliation unblocks failed schedules and can requeue saved
  results without rerunning their model work. Runtime scheduling, normal Agent
  integration, command/tools, permission enforcement, actual model-call limits
  and original-bot delivery routing are still pending. No new routine runs are
  enabled automatically by this work.
- Focused PostgreSQL suite: 44 tests, OK, 9.722 seconds;
  .cache/roadmap/phase9-2-foundation.log. Includes existing job/usage/delivery
  regressions, concurrent scheduling/reservation/settlement, restart recovery,
  stale leases and rollback after partial job, budget or Outbox writes.
- CI-path Ruff, Python compilation and diff whitespace checks pass. Full PG
  regression and admin build remain required before completing/committing 9.2.
  These edits are uncommitted. No production access, push, deployment, rebuild,
  real QQ/model request or change to the unrelated workspace-plan document.

### Phase 9.2 runtime integration (still in progress)

- Added `/例行` add/list/edit/cancel and registry handlers with creator, bot and
  conversation checks. Scheduled runs cannot create further recurring routines.
- Added a default-off scheduler and normal `_ask_ai` invocation, using a synthetic
  non-ledger event for the original creator. Job and model calls recheck the lease;
  disabling the group prevents further model/tool calls. Logged-out or missing
  QQ accounts defer before reserving a model budget.
- The gateway limits output and counts fallback, compatibility retries and stream
  usage in the current run. It disables hidden SDK retries in budgeted calls only.
  Text estimates use UTF-8 bytes plus overhead, not a universal tokenizer promise.
  Missing usage retains the reservation. Anthropic now honors per-call limits.
- The complete tool catalog measured 45,458 UTF-8 bytes. Increased the default
  routine allowance from 20,000 to 100,000 tokens, with 300,000 per day, so the
  conservative request estimate does not reject the tool definitions alone.
- Migration 0033 adds fenced receipts for non-repeatable tool effects. Completed
  effects are reused within the same scheduled occurrence; an interrupted effect
  without a receipt stops takeover and produces an explicit uncertainty notice.
  Prepared reply segments survive receipt reuse, not just the tool's JSON text.
- Persisted final messages re-enter Outbox without another model call. Delivery
  selects the recorded QQ account, never another connected account. Failure and
  quota notices are durable; quota exhaustion is reported once per accounting day.
- Inline Sub-Agent execution shares the parent's model budget instead of starting
  a detached model job. This is not yet complete Sub-Agent restart recovery:
  child task IDs/checkpoints, revisions/external continuations, and independently
  queued vision work need further integration. Do not mark 9.2 complete or enable
  it in production on the strength of the basic worker tests.
- Midpoint full PG suite before runtime integration: 1,807 tests, OK (12 skipped),
  243.183 seconds; .cache/roadmap/phase9-2-midpoint-full.log.
- Worker-focused checks: 56 tests, OK, 6.201 seconds. The first expanded run
  found one startup fixture expecting the old worker set; its expectation now
  includes and verifies the routine callback. Expanded rerun: 125 tests, OK,
  10.821 seconds; .cache/roadmap/phase9-2-worker-integration-second.log.
- Admin TypeScript/Vite production build passed. These changes remain local and
  uncommitted; the unrelated Desktop/workspace document is untouched. No external
  model, QQ, production host, push or rebuild was used.
- First post-wiring full PG run: 1,830 tests, one failure, 12 skipped,
  245.178 seconds; .cache/roadmap/phase9-2-worker-full.log. An existing service
  verification fixture froze the receipt clock but used wall time for intent
  creation. Crossing a second could correctly reject the fixture's own receipt
  as preceding the operation. A fixed-epoch regression reproduced the mismatch.
- Unified the fixture clocks and timestamp each simulated submission, including
  successive hosts. Production receipt validation is unchanged; a regression
  still rejects receipts predating the operation. Focused rerun: 54 tests, OK
  (one PostgreSQL-only skip), 2.360 seconds.
- Full PostgreSQL rerun: 1,832 tests, OK (12 skipped), 243.362 seconds;
  .cache/roadmap/phase9-2-worker-full-second.log. This includes the PostgreSQL
  case skipped by the focused clock run. Ruff and diff whitespace checks pass.
  The known detached-worker and Sub-Agent recovery gaps above still prevent
  completion or activation of Phase 9.2.

### Phase 9.2 bounded vision and recovery ownership (still in progress)

- Budgeted foreground vision now runs inside its parent's execution context,
  using the shared concurrency limit. It does not enqueue a detached vision job
  or join another caller's shielded request. Completed cache entries remain
  reusable. Cancellation reaches the provider before usage settlement; unknown
  usage is conservatively charged. Media handlers propagate budget exhaustion.
- The first focused vision launch lacked TEST_POSTGRES_DSN and failed in fixture
  setup. The correctly configured rerun passed 35 tests in 0.887 seconds;
  .cache/roadmap/phase9-2-vision-budget-second.log. No external model was called.
- Migration 0034 preserves one absolute execution deadline across retries.
  Expiry does not require QQ to be online and does not reserve another budget.
  Exception/cancellation paths close their own journal turns as crashed/aborted.
  Terminal recovery validates the job envelope and lease before queueing an
  idempotent notification. Retrying a failed notification cannot restart a model.
- Sub-Agent task creation now commits the task, initial context checkpoint,
  dispatch controls and initial events together. Notifications and job enqueue
  happen after commit. An enqueue failure leaves a fully reconstructable task.
- An initial expanded run found a missing test import, then two preparation-test
  assumptions: existing JSON encoding stringifies arbitrary objects, and a new
  pooled connection cannot see an uncommitted transaction. Tests now inject a
  genuinely invalid cyclic payload and check both transaction-local visibility
  and external isolation. Recovery-focused rerun: 121 tests, OK, 13.992 seconds;
  .cache/roadmap/phase9-2-recovery-focused-second.log.
- Migration 0035 adds durable occurrence-to-child ownership. Creation and binding
  are atomic; concurrent preparation and takeover reuse the same task and frozen
  context. A child inherits the parent's persisted deadline. Ordinary Sub-Agent
  workers do not enqueue, execute or emit a separate final message for these
  children. Resume/revise/model controls require the actual parent job fence.
- Child ownership and terminal-recovery checks: 112 tests, OK, 16.651 seconds;
  .cache/roadmap/phase9-2-child-ownership.log. Includes rollback, separate-store
  concurrency, reopened-store takeover, wrong owner/bot/scope, stale lease and
  disabled-parent rejection. The initial migration CLI invocation used a package
  without a __main__; Alembic then applied both migrations to local test databases.
- Ownership storage is not yet wired through all routine delegation paths. The
  automatic revise entry, external continuations and parent/child completion
  still need end-to-end integration. Member-departure checks, quiet completion
  and the new plan's explicit confirmation/watch behavior also remain pending.
- Full PostgreSQL regression: 1,861 tests, OK (12 skipped), 248.232 seconds;
  .cache/roadmap/phase9-2-ownership-full.log. The admin TypeScript/Vite build,
  Ruff and diff whitespace checks pass. PostgreSQL confirmed zero leftover
  owned test schemas; pgvector remains installed in public.
- This phase is not complete, not committed and remains disabled by default.
  No production connection, QQ/model request, push, deployment or rebuild was
  performed. The Desktop App and the newer user-authored plan are unchanged.

### Phase 9.2 owned-child execution and quiet completion (still in progress)

- Normal delegation tools and automatic delegate/workflow entry now prepare or
  resume an occurrence-owned child. The ordinary Sub-Agent dispatcher cannot
  separately execute or post the final text for that child. The parent uses the
  persisted result, not another detached task, and retains final Outbox ownership.
- Parent takeover first resumes unfinished children with their original context
  and saved steps, then supplies their receipts as quoted recovery evidence for
  final synthesis. A pending remote receipt suspends the whole parent turn;
  it cannot be swallowed as an ordinary model/tool error or silently retried.
- Child orchestration does not create a conflicting generic "started" effect.
  Child tool effects continue to use the existing session/external receipts;
  parent tools retain routine effect receipts. Every tool/model request still
  checks the parent lease and cancellation, sharing its model budget and deadline.
- Parent cancellation/expiry reconciliation settles unfinished child statuses
  transactionally, preserving completed steps, draft results and remote receipt
  data. It does not claim to cancel already submitted remote operations or delete
  their output. Late interruption cannot reopen a terminal task. Revisions keep
  the original parent deadline rather than resetting it for each revision.
- Group membership is checked before model spending and rechecked before model
  and tool calls at most 30 seconds after the last successful check. A confirmed
  departure disables the creator's schedule; an unavailable/invalid roster defers
  without assuming permission or disabling a valid schedule. Private routines
  do not query a group roster. Live NapCat roster acceptance remains pending.
- Migration 0036 adds a terminal `quiet` state. Intentional silence survives
  takeover without emitting a `[silence]` marker or running the model again.
  Prepared images/voice are not discarded because accompanying text is silent.
- Initial focused baseline: 58 tests passed. Real workflow tests initially found
  the mock specialist reports lacked required envelope fields, causing legitimate
  extra correction rounds. Corrected the test responses, not production report
  validation. Expanded child/worker/Sub-Agent suite: 65 tests, OK, 11.795 seconds;
  .cache/roadmap/phase9-2-child-execution.log.
- Quiet/membership/store/tool checks: 75 tests, OK, 11.941 seconds;
  .cache/roadmap/phase9-2-routine-finish-focused.log. Normal pipeline entry checks:
  33 tests, OK, 5.947 seconds; .cache/roadmap/phase9-2-entrypoints.log. This covers
  all three Sub-Agent tools and both automatic entry modes without detached work.
- Revision deadline regression: one test, OK, 1.408 seconds;
  .cache/roadmap/phase9-2-revision-deadline.log. It exercises a real coordinator
  revision, keeps one child ID, reruns only affected steps and enqueues no ordinary
  background workflow. Model output and artifact validation are mocked; these
  tests do not claim live model quality or real QQ delivery.
- Full PostgreSQL regression before the final revision-deadline adjustment:
  1,875 tests, OK (12 skipped), 252.046 seconds;
  .cache/roadmap/phase9-2-takeover-full.log. Includes the interrupted real child
  recovery test: completed frontend work runs once, interrupted backend resumes,
  and integration completes without a second child or final Outbox message.
  The deadline adjustment was verified by its separate focused test above, not
  retroactively counted as part of this full run.
- Admin TypeScript/Vite build, Ruff and diff whitespace checks pass. Both isolated
  local databases are at migration 0036 and have zero leftover test schemas.
  Phase 9.2 still needs final recovery review (including journal settlement after
  saved-output takeover) and a full regression on its eventual commit candidate.
  This checkpoint does not complete or commit the phase. No production access,
  push or rebuild; the newer plan is still untouched.

### Phase 9.2 final validation

- Routine output and journal completion now commit in one transaction, including
  final text, usage, digest and trace archive. A journal failure rolls both back;
  a lost commit response leaves a durable completion that takeover only delivers.
  Quiet turns retain `silence`; a budget/timeout failure remains `aborted`, never
  upgraded to execution success merely because its failure notice was delivered.
- Added regressions for post-commit interruption, Outbox unavailability, journal
  rollback, original-turn ownership and stale leases. Focused store/worker/journal
  run: 86 tests, OK, 13.913 seconds; .cache/roadmap/phase9-2-atomic-finish.log.
- Final full PostgreSQL regression, including the revision deadline fix:
  1,881 tests, OK (12 skipped), 251.708 seconds;
  .cache/roadmap/phase9-2-final-full.log. Admin TypeScript/Vite build passed;
  .cache/roadmap/phase9-2-final-admin-build.log. Ruff passed over every CI path.
- README documents the opt-in flag, creator-scoped commands, budget and delivery
  boundaries. This completes the earlier roadmap's 9.2 foundation only. New-plan
  confirmation, watch sources, project storage and member workbench are pending.
- This phase is a local-only commit. No real model/QQ calls, server connections,
  push or rebuild. Live roster, provider accounting and delivery acceptance remain
  pending; the supplied newer plan is unchanged and excluded from the commit.

### Phase 9.3 wiki storage (in progress, not committed)

- Applied migration 0037 only to the two disposable local databases. Added
  group-scoped opt-in settings, current knowledge pages and publication receipts.
  Wiki access and newcomer brief preferences are separate, both default off.
- Added a bounded ContextStore chapter reader that verifies scope, visibility,
  original message IDs and canonical IR hashes. Captured P1/P2/P3 sources include
  quoted participant statements with principal IDs. Expertise entries must cite
  the named participant's own quoted messages, not another user's claims.
- Concurrent enqueueing uses one frozen source/job per group/configuration/slot.
  Publication and receipt commit together under the current job lease. A lost
  response can reuse the receipt; old generations cannot overwrite newer pages.
  Reads revalidate sources, so clearing history or changing canonical evidence
  immediately hides derived entries. Disabling the wiki cancels pending work.
- Initial focused run: 26 tests, one failure because the fixture edited the
  rebuildable search cache rather than canonical message IR. Fixed the fixture
  to modify the actual body and added a cache-independence regression; no
  production evidence validation was relaxed. Intermediate rerun: 27 passed.
- Final storage/context/Historian focused run: 29 tests, OK, 8.518 seconds;
  .cache/roadmap/phase9-3-wiki-store-final.log. Ruff over CI paths and whitespace
  checks pass. A full phase regression and UI build are still pending integration.
- The initial source capture is bounded to recent chapters. Incremental retention
  of older verified wiki entries must be implemented before this can be presented
  as a continuing knowledge page. No nightly generator, QQ command, newcomer
  delivery, admin API or page is wired yet. This is not a completed phase and
  remains uncommitted. The newer M0-M7 project plan has not started.
- Phase 9.2 was committed locally as `3e50940`. No push, production connection,
  real QQ/model call or deployment occurred in this step.

### Phase 9.3 incremental generation and commands (in progress)

- Replaced recent-only capture with oldest-first bounded batches. Verified older
  entries survive later batches; unchanged chapters (including empty results)
  are checkpointed, while regenerated/hidden evidence cannot retain derived
  entries. Evidence validation pages through more than 100 source chapters.
  Exact category/title/participant keys may be updated by newer evidence without
  deleting unrelated knowledge. Semantic duplicate/conflict resolution is not
  claimed by this deterministic merge.
- Added the nightly durable worker, global opt-in, configured timezone/hour,
  original-account/group availability checks and generation without tools.
  At most four batches per group/date and three persisted generation reservations
  per job. Each reservation bounds model requests and tokens, including retries;
  interruptions cannot reset that allowance. Checked unchanged days skip further
  source scans; one group's scheduling failure does not starve other groups.
- The uncommitted 0037 migration now also owns the generation budget table and
  checked-day field. The first local downgrade attempt failed because the added
  table was not present in the earlier disposable schema; the transaction rolled
  back. Recreated only the Wiki tables in the two isolated test databases and
  reapplied 0037. No existing production schema or server was accessed.
- Added `/群百科 [关键词]`, `/群百科 开启` and `/群百科 关闭`. Only group or bot
  administrators can change settings; reads use the actual incoming group and
  never request a model. Private lookups and disabled groups are rejected.
  Replies are bounded literal text, not parsed CQ segments. Obvious credential
  patterns are redacted from generation input and rejected in published entries.
  This filter is not a guarantee that every form of private information is found.
- Initial incremental store run: 19 tests, OK, 16.529 seconds;
  .cache/roadmap/phase9-3-wiki-incremental.log. Initial integrated run: 71 tests,
  one error and one failure from stale matcher/worker expectation lists. Updated
  those lists and added the new adapter coverage rather than relaxing behavior.
- Expanded integration run: 73 tests, OK, 21.292 seconds;
  .cache/roadmap/phase9-3-wiki-worker-final.log. Ruff then caught a missing
  MessageSegment import in the command boundary; fixed it and added a direct
  literal-CQ regression. Final worker/adapter run: 24 tests, OK, 5.746 seconds;
  .cache/roadmap/phase9-3-wiki-command-final.log. Ruff over every CI path passes.
- Full isolated PostgreSQL regression for this checkpoint: 1,917 tests, OK
  (12 skipped), 300.350 seconds; .cache/roadmap/phase9-3-worker-full.log. Admin
  TypeScript/Vite build passes; .cache/roadmap/phase9-3-worker-admin-build.log.
  Both disposable databases are at 0037 and have zero leftover test schemas.
  No phase commit until newcomer delivery, admin API/UI, their tests/build and
  the complete phase regression have been finished. This is not live model or
  QQ acceptance, and no Wiki admin page has been implemented yet.
- The newer M0-M7 plan remains unchanged and untracked. No push, rebuild, SSH,
  real model request or real QQ message was performed.

### Phase 9.3 newcomer delivery, admin API and final validation

- Group-increase notices create deduplicated durable jobs only for an opted-in
  group/account. Briefs exclude expertise profiles, contain at most five verified
  entries and expire after one day. Membership is checked both before enqueueing
  delivery and immediately before sending. A confirmed departure or hidden
  evidence cancels delivery; an unavailable roster defers without assuming access.
- Job takeover reuses one Outbox record and never switches to another QQ account.
  A missing native receipt is ambiguous, not delivered or automatically resent.
  Briefs and their outgoing echoes are excluded from the private ledger and
  cross-platform mirroring, including late ambiguous echoes after restart.
- Added administrator-only Wiki routes, strict configuration inputs, mandatory
  resource If-Match plus group-setting version checks, and existing audit records.
  Store calls and event publication run off the event loop. SSE invalidates wiki
  views when evidence is edited/hidden; ordinary new chat alone does not rescan it.
- Added the group wiki page: search, five entries per page, original evidence IDs,
  version/timestamp, independent wiki/newcomer switches and five recent jobs.
  SSE preserves drafts and focused controls. Saving temporarily disables group
  switching so a late response cannot populate another group's draft.
- Initial newcomer tests exposed unescaped SQL LIKE percent literals in psycopg
  statements. Bound those patterns as parameters. Subsequent failures were a
  hardcoded principal fixture and an incorrect audit-status expectation; corrected
  the fixtures to the actual canonical principal and existing `succeeded` status.
- Focused integration: 91 tests, OK, 45.168 seconds;
  .cache/roadmap/phase9-3-integration-final.log. Complete PostgreSQL regression:
  1,936 tests, OK (12 skipped), 307.658 seconds;
  .cache/roadmap/phase9-3-final-full.log. Both disposable databases are at 0037,
  with zero leftover owned test schemas. Ruff across CI paths and diff checks pass.
- After that full run, the only product edit was the frontend save/switch guard.
  The admin TypeScript/Vite build passed again. Playwright component-harness checks
  at 1280x900 and 390x844 verify SSE draft/focus retention, search, pagination,
  delayed-save isolation and no overflow or browser errors;
  .cache/roadmap/phase9-3-wiki-visual-final.log. Screenshots were inspected, the
  temporary harness removed, and its local server stopped. This is mocked UI
  verification, not live QQ or provider acceptance.
- Phase 9.3 is locally complete. Remaining earlier-plan work is 9.4-9.6; the new
  project-workspace M0-M7 plan is not started, remains unmodified and is excluded
  from the commit. No push, rebuild, production access or real messages occurred.

### Phase 9.4 preview publication and delivery

- Reuse the existing sandbox artifact upload, Fleet Worker preview jobs,
  persistent Sub-Agent continuations and Outbox. Do not create a second website
  deployment service. Added build/publication guidance and documented an ordinary
  chat request, prerequisites and the distinction from future project workspaces.
- Serialize scoped idempotency keys and commit jobs/events/preview reservations
  together. Replays keep the original preview identity, TTL and default deadline;
  changed artifacts, TTL, constraints and explicit deadlines remain conflicts.
  Old split-commit orphan routes are repaired only with verified binding and URL;
  expired or invalid historical receipts cannot become active routes.
- Preview completion must name the exact worker/job URL. New jobs are retryable
  and checkpointable; worker takeover republishes or reconciles the actual local
  files rather than trusting a URL from another host. Existing job policies are
  retained. Deployment must upgrade Bot, Control and Worker together.
- Harden ZIP extraction, bounded streamed writes, path/identity/lifetime checks,
  concurrent temporary directories and artifact-hash replay checks. Expired
  routes return 404 before cleanup. Interrupted staging directories have at most
  a one-hour deadline; cleanup waits for the normal worker loop to run.
- Published links and UTC expiry are appended from host-bound durable receipts,
  even when model synthesis omits them. Partial task status stays partial.
  Explicit revision ancestry may expose earlier links labelled with their source
  revision; foreign actor/scope/key receipts are rejected. Routine-owned children
  hand links to their parent's one Outbox result instead of independently sending.
- Anonymous static-asset CORS supports module scripts inside the existing opaque
  CSP sandbox. No cookies, credentials or control APIs are enabled. Preview URLs
  are random, network-reachable capabilities, not member-authenticated pages;
  this is documented rather than claiming private group-only HTTP access.
- The first focused run exposed the old scheduler's hard-coded refusal to retry
  preview jobs, plus a test using TaskStep.step_key rather than its actual key.
  Fixed both. Expanded focused run: 121 tests, OK, 18.496 seconds;
  .cache/roadmap/phase9-4-focused.log. Whole-suite run before the final staging
  cleanup addition: 1,951 tests, OK (12 skipped), 306.775 seconds;
  .cache/roadmap/phase9-4-first-full.log.
- Final worker-focused check: 6 tests, OK, 0.682 seconds. Playwright at 1280x900
  and 390x844 passed after the staging change: local CSS and ES module imports,
  moving/nonblank canvas, button action, no horizontal overflow and blocked cookie
  access; .cache/roadmap/phase9-4-browser.log. Screenshots were inspected; the
  temporary fixture, local server and browser were removed/stopped.
- Ruff and diff checks pass. Admin TypeScript/Vite build passes; real model generation, recipient
  network access and QQ receipt/reconnect acceptance remain authorized live gates.
  Control's source ZIPs are not newly garbage-collected by preview expiration.
  The newer plan remains unmodified and untracked; no production action occurred.
- Staging-cleanup whole-suite run: 1,953 tests, OK (12 skipped), 301.752 seconds;
  .cache/roadmap/phase9-4-staging-full.log. Final review added an upgrade replay
  regression: policy fields derived by the service are not new caller intent,
  so an existing job retains its old execution flags rather than conflicting or
  silently acquiring new retry permissions. A verifying orphan remains pending.
  Preview-focused tests after this correction: 14 tests, OK, 1.814 seconds;
  .cache/roadmap/phase9-4-upgrade-focused.log.
- Final full PostgreSQL regression after the upgrade fix: 1,954 tests, OK
  (12 skipped), 299.403 seconds; .cache/roadmap/phase9-4-final-full.log.
  Both disposable databases remain at 0037 with zero leftover owned test schemas.
  Phase 9.4 is locally complete; 9.5 and 9.6 remain before the newer M0-M7 plan.
  No push, rebuild, production access or real QQ/model request occurred.

### Phase 9.5 collaboration foundation (in progress)

- Added migration 0038 for task-local principal grants and revision-bound
  feedback. The task owner alone changes grants. Private conversations, other
  groups/tasks and routine-owned child jobs cannot share these grants. New rows
  reference canonical principals rather than copying QQ identifiers.
- Every mutation uses the task control version, with controls locked before
  tasks to match revision locking. Incoming-message keys deduplicate feedback;
  key reuse with different text is rejected. Revocation atomically dismisses
  that participant's pending feedback, and replay cannot reactivate it.
- Bound active collaborators to 20 and pending comments to 10 of at most 1,000
  characters. The owner or comment author can dismiss a pending comment. Oversize
  batches are rejected rather than silently truncated or partially consumed.
- An explicit owner revision can merge the inbox in the same transaction as the
  revision/checkpoint. It forces durable supervisor contract refresh before
  workers start, quotes each comment with author/evidence IDs, and retains scope,
  original requester and delivery identity. Concurrent revocation invalidates
  the version; a crash rolls back comment consumption together with the revision.
- First focused run: 79 tests, 12 errors in the new fixture because store.path
  is a display path, not a PostgreSQL database source. Reused the explicit fixture
  database object for ledger/reopened stores. Existing regressions passed.
  Corrected integration run: 79 tests, OK, 14.050 seconds;
  .cache/roadmap/phase9-5-collaboration-focused-final.log.
- Expanded collaboration tests: 16 tests, OK, 3.838 seconds;
  .cache/roadmap/phase9-5-collaboration-concurrency.log. Includes two concurrent
  revision callers, independent connection replay, restart, SQL constraints,
  limits, revoked grants and transaction rollback. Ruff over all CI paths and
  git diff checks pass. Disposable class schemas were removed by their fixtures.
- This is a foundation checkpoint, not a completed phase: no public collaboration
  entry point is enabled and collaborators still cannot execute as the creator.
  Fresh group membership checks, command/tool/UI integration, distinct execution
  identity and high-risk two-admin quorum remain. No phase commit or complete
  phase regression yet; no push, rebuild, SSH, real QQ or model calls occurred.
  The newer M0-M7 plan remains unchanged, untracked and not started.

### Phase 9.5 execution and quorum checkpoint

- Added the explicit `/协作` group command, fresh member-list verification,
  participant feedback merging and the natural revision-tool entry point.
  The actual reviser's event, context and grant generation are frozen separately
  from original task ownership and group delivery. Revocation/regrant cannot
  revive an old execution. Unknown membership pauses; departure or revocation
  stops later tools rather than falling back to the creator's identity.
- Model routing, quota, server authorization and external continuation polling
  use the actual executor. Background approval watchers also verify current
  group membership, outside the task's normal guard. Historical receipts retain
  their original revision actor. Existing submitted remote actions are not
  automatically undone when collaboration is revoked.
- Added the read-only control-panel collaboration view with five recent comments,
  expansion, exact timestamps, creator and actual executor. Console revision is
  creator-only; participants revise from the original group. Collaborator runs
  cannot inherit the creator's locked model policy or personal memory.
- Collaboration integration: 141 tests, OK, 32.645 seconds;
  .cache/roadmap/phase9-5-collaboration-execution.log.
- Added migration 0039 and independent administrator OTP review. Server task
  grants and Fleet host mutations require quorum=2; ordinary settings and isolated
  Worker jobs do not. Private `复核 AP-...` creates an idempotent second-person
  challenge. Both account and QQ identities must differ. Votes bind exact payload,
  generation, bot and account versions; one vote never enters execution.
- Recheck votes before claim, after preflight and when reusing task authorization.
  Expiry, resend, revocation and changed accounts invalidate old votes. Review
  challenges never execute commands or produce misleading completion receipts.
  Removed the generic already-approved-handler bypass around Fleet host checks.
- Focused security/collaboration run: 65 tests, OK, 15.496 seconds;
  .cache/roadmap/phase9-5-quorum-focused.log. Two additional watcher/bypass cases
  are included in the currently running full phase regression.
- Local Chrome fixture passed at 1280x900 and 390x844: SSE retains input, selector
  focus and expanded details; long unbroken feedback wraps; collaborator-only
  controls and 1/2 approval state display correctly. Screenshots inspected, fixture
  sources deleted and temporary Vite server stopped. No production was contacted.
- Both disposable base databases were upgraded to 0039. Full phase regression
  is running; no phase commit yet. The newer plan remains untouched/untracked.
- First complete regression: 2,010 tests, 1 failure and 12 errors (12 skipped),
  321.113 seconds; .cache/roadmap/phase9-5-first-full.log. Old fixtures lacked a
  second admin or the new actor/revisable-task interfaces. Updated those fixtures
  to exercise the actual contracts instead of relaxing production authorization.
  Removing the broad approved-handler bypass also dropped a Worker `submitted`
  projection; restored that receipt metadata on the ordinary, permission-checked
  path so queued work is not described as finished. Added two edge regressions:
  a maximum-length review summary and refusing to relabel a completed command as
  cancelled when someone later retracts a vote.
- Repair-focused regression: 118 tests, OK, 23.520 seconds;
  .cache/roadmap/phase9-5-repair-focused.log. Final complete PostgreSQL regression:
  2,013 tests, OK (12 skipped), 319.989 seconds;
  .cache/roadmap/phase9-5-final-full.log. Includes migration from 0038 with legacy
  grants: cancel reusable old authorization, retain ambiguous in-flight commands
  as needs_attention, and never automatically replay an operation.
- Admin TypeScript/Vite build, Ruff over all CI paths and diff checks pass. Both
  disposable databases are at 0039_approval_quorum with no leftover owned test
  schemas. Phase 9.5 is locally complete. Real QQ member changes, two-admin OTP
  delivery and production upgrade acceptance remain explicit live gates.
  The newer plan is still unmodified/untracked. No push, rebuild, server access
  or real model call occurred.

### Phase 9.6 protocol and storage foundation (in progress)

- Phase 9.5 was committed locally as a65741b after its final full regression.
- Added the official mcp 2.3.0 SDK, using its current Client/MCPServer API rather
  than copying older FastMCP examples. uv.lock and the generated requirements
  export preserve existing dependency versions and add the SDK dependencies.
- Added migration 0040 for versioned server registrations, pinned tool catalogs
  and revocable group read tokens. New servers/tools start disabled. Remote
  annotations cannot grant permissions. Changed definitions, endpoints, scope
  grants or reappearing tools lose previous safe/enable settings. CAS prevents
  a delayed catalog fetch or old UI write from replacing newer configuration.
- Registration accepts only HTTP endpoints without inline credentials, or named
  operator-defined stdio profiles. Secrets are references to dedicated environment
  names, not database/UI values. Remote JSON Schema references and resolution-base
  changes are rejected. Transport-side SSRF/stdio isolation remains unfinished;
  no outbound MCP client or host launcher has been enabled.
- Group tokens contain 256 random bits; only hashes persist. Tokens bind one QQ
  group, bot account and issuing administrator version, expire within 30 days,
  support revocation and share a 60-request/minute limit across processes. Token
  values are returned once, excluded from list projections and not yet exposed
  by an admin route. Issuance must avoid putting that one-time value in audit logs.
- Implemented an unmounted official-SDK stateless HTTP endpoint with only
  context_search/ledger_search. Tokens, never model arguments, choose the group.
  Reads respect ledger visibility and validate chapter evidence. Raw OneBot data,
  attachment URLs and sender QQ IDs are excluded; text is redacted and quoted as
  untrusted evidence. Personal memory is not exposed. Revocation is rechecked
  after reads; host/origin checks, concurrency and body limits apply.
- First focused run found a missing NoneBot test initializer. The next run
  exposed SDK lifecycle ownership and unstructured dict responses; fixed both
  without bypassing the SDK. Verified 20 tests in 3.798 seconds, OK;
  .cache/roadmap/phase9-6-mcp-boundaries-final.log. Includes real SDK protocol calls
  over in-memory ASGI, concurrent group clients, guessed message IDs, forged
  scope, cleared history, malformed inputs, account changes, revoke-during-read,
  secret-safe errors, rate limits and concurrent administrator updates.
- Ruff over all CI paths and diff checks pass. This is not phase completion:
  client transport fencing, registry integration, approved call execution,
  lifecycle mounting, console/SSE/API wiring, help and full PG/UI regression
  remain. No phase commit yet; newer M0-M7 work and all live services untouched.
- Lockfile/requirements export drift check passes. Both disposable base databases
  are now at 0040_mcp_boundaries with zero leftover owned test schemas.

### Phase 9.6 client transport and durable call foundation (in progress)

- Added HTTP transport with exact endpoint matching, validated numeric DNS
  pinning, original TLS SNI/Host, no ambient proxy, no redirects and bounded
  request/response/session bytes. Private addresses require an exact operator
  allowlist entry; link-local/metadata addresses remain prohibited. Credentials
  resolve only through dedicated operator references and are redacted from
  projected results, including JSON-escaped echoes. Compressed responses are
  rejected before HTTP decoding to avoid expansion outside the byte limits.
- SDK discovery is bounded by pages, tool count and total definition bytes.
  Every call rechecks the frozen tool fingerprint. The public one-round SDK
  session API avoids automatic schema-change retries or sampling/input rounds;
  remote output-schema regexes are not executed on the bot event loop. Text and
  structured results are quoted as untrusted evidence. Binary/resource results
  are not automatically fetched or imported.
- Added operator-only stdio profiles using immutable, preloaded OCI images.
  The launcher fixes an unprivileged user, no network, no host mounts, read-only
  root, bounded tmpfs/CPU/memory/PIDs and no Docker log accumulation. Image-declared
  volumes are rejected. A bounded framing adapter uses official SDK parsing and
  Client dispatch; it avoids the SDK stdio reader's unbounded unterminated-line
  buffer. Cleanup checks installation ownership and expiry; no other containers
  are eligible. Real Docker isolation/expiry acceptance is still pending: tests
  use real SDK subprocesses but replace the Docker launcher.
- Added migration 0041 and an actor/group/bot-bound invocation ledger. Concurrent
  claimers have one winner; identical invocation IDs reuse saved results and
  cannot change arguments. Recovery can reclaim a pre-dispatch connection, but
  a dispatched call without a result becomes needs_attention and is never
  automatically replayed. Results distinguish dispatch started from confirmed
  success. Expired calls cannot keep accumulating active execution slots.
- Added a host service that checks current membership before execution and
  disclosure. Unreviewed tools require the existing two-independent-admin OTP
  flow, bound to the exact invocation/configuration/arguments hash. Remote calls
  recheck configuration and approval immediately before the persisted dispatch
  boundary. Safe tools still receive receipts and duplicate prevention.
- Approval execution now also rejects an expired worker lease, including existing
  server-task approvals. Initial tests exposed AnyIO exception wrapping, cached
  HTTP response streams bypassing byte accounting, escaped credential echoes and
  memory streams left open during cancellation; each was fixed and covered.
- Focused MCP and existing account/quorum/server-task/collaboration regression:
  133 tests, OK, 27.161 seconds, with ResourceWarning promoted to errors and no
  leftover stream warnings. Log: .cache/roadmap/phase9-6-client-security-final.log.
  Includes real SDK HTTP and stdio calls, in-memory ASGI, local PG concurrency,
  host denial, definition changes, partial authorization, cancellation, lease
  expiry, crash recovery and no replay after an ambiguous remote result.
- Ruff across all CI paths, diff whitespace checks and requirements export drift
  pass. Existing dependency versions are unchanged. Both disposable base databases
  were upgraded to 0041_mcp_calls. No test contacts a real model or QQ account.
- This is still not Phase 9.6 completion: dynamic registry policy, durable Agent
  continuation after approvals, runtime/config/Nix integration, periodic reaping,
  admin API/SSE/UI/help and the final full PG/UI gates remain. No phase commit,
  push, SSH, rebuild or live deployment. The newer M0-M7 plan is unchanged and
  has not started.

### Phase 9.6 registry, lifecycle and administrator API (in progress)

- Registered external tools through a per-turn ToolRegistry snapshot, never a
  global mutable catalog. A single bounded SQL join selects enabled tools for
  the current group. Private chats, routine runs and specialist workers do not
  silently inherit them. The host rechecks current group/bot/user membership,
  the advertised endpoint/schema/trust version and the exact approval at dispatch.
- Replaying the same operation in the same incoming message uses its durable
  receipt even when a provider changes tool-call IDs. A new incoming message has
  a new identity. Unreviewed calls still require the dedicated two-admin flow;
  user text and remote annotations cannot approve them. Trace distinguishes
  waiting from confirmed success and unknown remote outcomes.
- Remote input-schema regexes are not executed by the event-loop validator;
  shape/type checks still run. Unrepresentable numeric hints fail softly rather
  than crashing validation. Remote servers remain responsible for their full
  input-schema semantics.
- Added opt-in AI_MCP_ENABLED / AI_MCP_CONFIG_FILE runtime assembly. The bounded
  operator JSON file lists exact private endpoint exceptions, preloaded stdio
  profiles, explicit Docker binary/socket and a stable installation owner.
  Only GAOJI_MCP_SECRET_* credentials enter the HTTP client. No new provider,
  model, QQ connection or process is contacted merely by loading defaults.
- Mounted the configured read-only /mcp endpoint independently of the admin
  pages. One supervised task owns both entry and exit of the official SDK's
  AnyIO session manager. Startup awaits readiness; stopped/unready endpoints
  return 503. Another supervised task recovers calls and reaps expired owned
  containers. Fresh OneBot membership checks fail closed.
- Added session/CSRF/admin-protected server registration, catalog discovery,
  per-tool trust/enable settings, group-token creation/revocation and paginated
  invocation receipts. Mutations require If-Match plus entity versions and write
  audit records; MCP tables invalidate the SSE resource. Call lists default to
  five rows. One-time tokens are excluded from MutationResult, audit and listing
  responses; the issue response has Cache-Control: no-store.
- Registry/tool regressions: 72 tests, OK, 8.031 seconds
  (.cache/roadmap/phase9-6-registry.log). Runtime/config tests: 45 tests, OK,
  3.081 seconds (.cache/roadmap/phase9-6-runtime.log). The initial runtime run
  omitted AI_POSTGRES_SCHEMA=qq_bot_test; it failed against a nonexistent test
  schema. Correcting the invocation, without changing the safeguard, passed.
- Admin/account/wiki regressions: 29 tests, OK, 21.481 seconds
  (.cache/roadmap/phase9-6-admin.log). Combined SDK/transports/registry/runtime/
  admin/existing-tool regression: 164 tests, OK, 21.919 seconds
  (.cache/roadmap/phase9-6-integration-focused.log). These use isolated local
  PostgreSQL and simulated QQ/provider boundaries, not production acceptance.
- Still unfinished: console UI/help, approval-result continuation into a durable
  Agent task and original-conversation Outbox, deployment/Nix documentation,
  final full PG/admin build and live Docker isolation/TLS/QQ acceptance gates.
  No Phase 9.6 commit yet. No push, SSH, rebuild or live deployment. The newer
  M0-M7 plan remains unchanged, untracked and not started.

### Phase 9.6 console, operator guide and local isolation acceptance (in progress)

- Added the MCP console view with external server registration, catalog refresh,
  per-tool enable/approval policy, group-only token issue/revoke and invocation
  receipts. It uses the existing admin API, resource versions, audit and SSE
  resource invalidation. Tokens stay in component memory only and are hidden on
  navigation/revoke; results are rendered as text, never HTML.
- Editors are keyed by server/tool identity, not live revisions. Incoming updates
  preserve drafts and focused selects. Stale entity versions block saving until
  explicitly reloaded. Calls, tokens and tools use five-row pages; calls show
  precise Shanghai timestamps and distinguish unknown outcomes from success.
- Added reproducible tools/verify_mcp_ui.py against the actual production bundle
  and intercepted fake APIs. At 1280x900 and 390x844 it verifies create/edit,
  catalog discovery, policy writes, CAS, SSE draft/focus retention, pagination,
  one-time token handling/revocation, inert result text, disabled state and no
  horizontal overflow. Screenshots were inspected after scrolling to the top.
  Log: .cache/roadmap/phase9-6-ui.log. The final UI build is recorded below.
- Added .env.example defaults and docs/mcp-bridge.md with operator JSON, existing
  Nix module environment wiring, credentials, exact private endpoint exceptions,
  immutable isolated stdio profiles, reader proxy configuration and console steps.
  No Nix module defaults, shared nix-config, runtime secrets or servers changed.
  The guide explicitly marks pending continuation/delivery work, not production
  readiness; README links to it as development documentation.
- tools/verify_mcp_isolation.py ran against the already-installed local Python
  image, without builds/pulls or host mounts. Actual Docker inspect and in-container
  checks verify UID 65534, read-only root, no external route, tmpfs noexec, no host
  secret inheritance, CPU/memory/PID/FD settings and no persistent logs. Actual
  expiry cleanup removes only expired matching-owner containers and preserves
  live/foreign fixtures; every created fixture was removed in cleanup.
  Log: .cache/roadmap/phase9-6-isolation.log. The first smoke assertion incorrectly
  searched escaped projected JSON and assumed Linux exposes only lo in sysfs;
  the check now decodes the data and verifies the actual route table plus Docker
  network configuration. No security controls were relaxed.
- Added loopback-only real TLS tests with temporary certificates and the official
  MCP server/client: numeric pinning retains verified hostname/SNI and original
  Host, ignores ambient proxy variables, and rejects untrusted certificates or
  wrong hostnames before HTTP/tool execution. No real upstream was contacted.
  The initial test fixture needed an explicit dict[str, str] SDK output type.
- The registry edge-case follow-up passed 34 tests in 2.415 seconds
  (.cache/roadmap/phase9-6-registry-final.log). Final targeted test/build results
  for this chunk: 121 tests, OK, 21.031 seconds, ResourceWarning promoted to
  errors (.cache/roadmap/phase9-6-console-focused.log); admin TypeScript/Vite
  build passed (.cache/roadmap/phase9-6-console-build.log). Ruff across all CI
  paths and git diff whitespace checks pass. The full Phase 9.6 gate has not run.
- Still required before the Phase 9.6 local commit: durable Agent continuation
  after approval, original-conversation Outbox delivery, integrated recovery
  tests and full isolated PostgreSQL/admin build. Actual QQ quorum/group delivery
  and production reverse proxy acceptance remain separate live checks. No push,
  SSH, rebuild, real model or QQ request. New plan M0-M7 has not started.

### Phase 9.6 durable continuation storage and Outbox guards (in progress)

- Added migration 0042 with immutable approval-bound conversation checkpoints,
  continuation jobs and effect receipts. The checkpoint preserves previous tool
  results, binds the exact next MCP operation and explicitly marks remaining
  unexecuted calls in the same batch. Orphan/duplicate tool results, new system
  messages, changed arguments, excessive sizes and nonfinite values are rejected.
- Terminal remote receipts atomically create one continuation job and replace
  only the pending result slot. The original group, actor, bot, question, profile
  and message identity cannot be overwritten by retries. Changed checkpoint
  hashes fail closed. Remote unknown outcomes are not interpreted as success.
- Continuation transcript writes are append-only and revision-checked. Model
  reservations survive restarts; tool effects have durable input identities and
  receipts. A completed effect is reused after a crash before transcript save;
  an uncertain non-idempotent effect cannot be automatically executed again.
  Pure/idempotent work can be retried by the next valid lease holder.
- A focused test exposed that durable-job attempts can be decremented during
  recovery/deferral. Added a monotonic lease_generation, independent of retry
  counts, and carried it through worker heartbeat, settlement and deferral.
  Even a restarted claim with the same worker name cannot revive an old owner.
  Legacy direct store calls retain their existing optional API; the worker
  always supplies the generation and rejects expired leases.
- Result persistence and Outbox enqueue share one transaction and a stable key.
  OneBot delivery selects the original bot, rechecks original group membership,
  waits if that bot/runtime is unavailable, and parks a missing native QQ receipt
  as ambiguous rather than claiming delivery or blindly sending twice.
- Validation: the new checkpoint/storage suite passed 22 tests in 4.650 seconds.
  The first related suite passed 128 tests in 27.978 seconds. After extending
  generation fencing to the shared worker, the final focused MCP/job/Outbox/
  routine regression passed 171 tests in 36.917 seconds with ResourceWarning
  treated as errors (.cache/roadmap/phase9-6-continuations-final.log). These are
  real isolated local PostgreSQL transactions with fake QQ/model boundaries.
- Still unfinished: capturing checkpoints from the live main Agent before
  proposing approval; registering and running the durable continuation handler;
  reconnecting saved transcripts/reply state and model budgets; failure notice
  reconciliation; integrated runtime recovery tests and the full Phase 9.6 gate.
  The new storage is not yet wired into McpRuntime, so this is not an end-to-end
  approval continuation release. No phase commit, push or deployment. The newer
  M0-M7 plan remains unmodified, untracked and not started.

### Phase 9.6 main Agent continuation integration (in progress)

- Main group turns now persist an immutable checkpoint before proposing an MCP
  approval and return a waiting notice. The ordinary Agent loop accepts saved
  transcripts, reuses pending tool calls before asking the model again, and saves
  each completed result/final answer through an awaited checkpoint hook.
- McpRuntime registers a durable continuation dispatcher and maintenance for
  terminal call receipts and failed/cancelled jobs. Reply state, original event
  identity and host context survive takeover. A second approval pauses the same
  job; it cannot reset the one-hour deadline, eight physical model requests,
  262144 conservative token reservations or 32 host-effect limit.
- Shared model budgets gained an optional asynchronous physical-request
  reservation hook. It runs before provider dispatch, including compatibility
  retries and fallback attempts. Durable reservations are never refunded based
  on missing usage after a crash.
- Host effects store their exact pending tool-call identity plus prepared reply
  state. Unknown mutations stop before another model/tool request; receipt-write
  failures also stop. Nested Sub-Agent execution retains its own receipt path,
  while the parent delegation remains conservatively non-repeatable without a
  saved receipt. Unverifiable membership yields rather than becoming an ordinary
  model error. Model-profile changes stop automatic continuation.
- Targeted verification: 81 existing/extended tests passed in 10.999 seconds
  (.cache/roadmap/phase9-6-continuation-wiring-first.log). The new runtime/normal
  pipeline/real Agent-loop suite passed 18 tests in 3.739 seconds after correcting
  its profile-identity fixture (.cache/roadmap/phase9-6-continuation-runtime-second.log).
  Includes actual isolated PostgreSQL approval quorum, simulated remote execution,
  restart, original Outbox identity, missing receipts, final checkpoint recovery,
  deferred batch calls and preserved model budget. No live QQ or model calls.
- Full isolated regression and final admin build are pending for this phase.
  No commit/push/deployment yet. The newer M0-M7 plan is still untouched.

### Phase 9.6 final local acceptance

- The first full run found two tool-catalog regressions: MCP handlers accessed
  runtime dependencies even when the caller's capabilities excluded MCP. Added
  an early scope/capability return, preserving the existing browser/Sub-Agent
  fixtures and assertions. First run: 2164 tests, two errors, 12 skipped,
  357.862 seconds (.cache/roadmap/phase9-6-first-full.log).
- Review also separated previous-turn transcript history from this turn's tool
  call fingerprints. Old conversation calls no longer consume the resumed
  turn's repeat budget; pending deferred calls remain distinguishable from
  already completed effects. The history boundary is immutable across a second
  approval and is validated before the checkpoint is saved.
- Focused runtime/MCP/browser/Sub-Agent/loop regression: 94 tests, OK,
  14.272 seconds (.cache/roadmap/phase9-6-post-review-focused.log).
- Final full isolated PostgreSQL suite: 2167 tests, OK (12 skipped),
  350.802 seconds, ResourceWarning promoted to errors
  (.cache/roadmap/phase9-6-final-full.log). Both disposable database schemas are
  at Alembic 0042. No production database or provider was used.
- Final TypeScript/Vite admin build passed
  (.cache/roadmap/phase9-6-final-admin-build.log). Ruff across all CI paths,
  compileall and git diff whitespace checks pass. Earlier real local OCI/TLS
  and responsive UI checks remain recorded above.
- Phase 9.6 is ready for its local commit. This completes the earlier roadmap's
  local implementation, not the still-pending real QQ/provider/live-network
  acceptance gates. Nothing pushed, rebuilt or deployed; no SSH connection.
  The newer plan document is unchanged and excluded from this phase's commit.

## Project workspace plan: M0-M7

The earlier roadmap's local implementation is committed as `9cfab47`. Starting
2026-10-27, the newer project-workspace plan owns the overlapping 9.2/9.4 work.
Its original file remains unmodified and unstaged. The following checklist is
implementation evidence, not a claim that the production App has these features.

- [x] M0: technical validation and ADR 006 (local experiments complete).
- [x] M1: scoped stores, migration and content-addressed versions (local acceptance complete).
- [ ] M2: project quota helper, lifecycle, snapshots and metrics.
- [ ] M3: project tools/commands, Sub-Agent integration and admin page.
- [ ] M4: confirmed routines and bounded, private-network-safe watch sources.
- [ ] M5: independently authenticated member workbench.
- [ ] M6: version-backed static previews on a separate origin.
- [ ] M7: operator/App configuration, documentation and acceptance records.

### M0 first local experiments

- Added `tools/verify_project_storage.py`, an opt-in local Docker experiment
  using an explicit Unix socket and existing image. No pulls/builds, host data,
  network, production access or product changes. Only a temporary loop helper
  runs privileged; independent writers are unprivileged and resource-limited.
- Docker Desktop 29.6.1 / LinuxKit 6.12.76 / aarch64: helper-owned sparse ext4
  with Docker local device mounts works. At a 96 MiB image limit, project A
  returned ENOSPC after 86,536,192 bytes; project B then wrote 16 MiB and retained
  its original sentinel hash. Trim reduced A's allocated bytes from 91,078,656
  to 4,571,136. Candidate B's direct-image `o=loop` mount failed with EINVAL.
- Three short fsynced 32 MiB sequential writes measured medians of 973.6 MiB/s
  for an ordinary volume and 608.2 MiB/s for the quota volume. These are local
  microbenchmark samples, not a general performance promise. Evidence:
  `.cache/roadmap/m0-storage-desktop-device.json`.
- Failed probes are preserved: the first inherited the image's non-root user;
  the second attempted an unsupported proc-path bind mount. The final probe
  selects root only for the helper and uses Docker's supported device mount
  mechanism. All attempts verified removal of their newly created resources.
- Host QEMU 10.2.2: a 128 MiB QCOW2 allocated 67,436,544 bytes after a 64 MiB
  write, then 327,680 bytes after discard; image check reported zero errors.
  This initial host-only test is NOT guest-fstrim or production VM acceptance.
- Follow-up real guest experiment: `tools/verify_project_guest_discard.py` boots
  an offline ARM64 LinuxKit guest from installed vendor assets, with a private
  read-only root clone, one TCG CPU and 768 MiB RAM. Only its new 128 MiB test
  disk is formatted, after capacity/serial checks. Fixed PCI slots and explicit
  shell readiness resolved the early probe's boot/console assumptions; a
  private clone avoids interfering with Docker's existing root-image lock.
- With a read-only QCOW2 base plus overlay, 48 MiB of random guest file data
  allocated 51,838,976 host bytes. Delete+sync did not reduce it; guest fstrim
  with virtio discard=unmap reduced it to 1,638,400. A retained sentinel survived
  unmount/remount, the base SHA-256 was unchanged and image check passed. Every
  guest process and temporary root/base/overlay was removed. Evidence:
  `.cache/roadmap/m0-guest-discard-backing.json`. This does not prove production
  libvirt wiring or integrity after a guest restart.
- Added a PostgreSQL acceptance test through the existing routine dispatcher,
  ordinary tool executor, real Agent loop and Outbox. QQ/model boundaries are
  simulated; a real tool prepares an expression and final reply. Original
  canonical messages remain unchanged and takeover returns the same delivery.
  Passed one test in 0.981 seconds (.cache/roadmap/m0-synthetic-accepted.log).
  Early fixture assertions assumed nullable reply IDs, raw JSON tool messages
  and native OneBot names instead of the actual Message IR; corrected the test,
  not product behavior. An early full run was cancelled immediately after a
  fixture failure was noticed; it is not counted as a passing regression.
- ADR 006 records evidence and provisional choices. Native Ubuntu/NixOS host
  checks, Windows Desktop and production backend behavior are NOT inferred
  from this LinuxKit guest or mocks. M0 remains incomplete; no project features
  or production quota claims enabled.
- Current checkpoint full PostgreSQL regression: 2168 tests, OK (12 skipped),
  358.407 seconds, ResourceWarning promoted to errors
  (.cache/roadmap/m0-final-full.log). Ruff and compileall across CI paths and
  whitespace checks pass. No product/frontend/helper/Nix module changed, so
  M0 does not introduce a new UI build or Nix gate.
- Nothing pushed, deployed or changed in nix-config. No SSH or real QQ/model
  request. Native-host gates remain open; real local guest and Docker results
  are recorded separately from unit tests. This is a local M0 checkpoint, not
  the completion of M0-M7.

### M0 native-host completion

- Added a test-only native-host probe, local QEMU runner, disposable NixOS
  fixture and Ubuntu cloud-init seed. Ubuntu and NixOS run their own guest
  kernel/systemd/Docker; no distribution-container substitution. Both use
  two vCPUs and 2 GiB RAM, a read-only fixture ISO, no host mounts, credentials,
  SSH or forwarded ports. NixOS is offline; Ubuntu uses public apt repositories.
- Ubuntu 24.04.5 / kernel 6.8.0-147 / Docker 29.1.3: candidate A's host loop mount
  plus ordinary Docker bind mount returned ENOSPC after 88,076,288 bytes on a
  96 MiB image. Another project's 16 MiB write and sentinel survived. Physical
  allocation fell from 94,380,032 to 4,288,512 bytes after delete+trim. Candidate
  B's direct-image Docker local mount failed with EINVAL.
- NixOS 26.05 / kernel 6.18.43 / Docker 29.6.2: the same A check returned ENOSPC
  after 85,983,232 bytes, with the second project unaffected. Allocation fell
  from 93,081,600 to 4,599,808 bytes. Candidate B failed with EINVAL here too.
  The fixture used the repo's existing pinned nixpkgs, with no lock-file change.
- Short, noisy ordinary/quota write medians were 973.5/578.4 MiB/s on Ubuntu,
  1841.7/1020.0 MiB/s on NixOS. The ADR preserves the outlier and limitations;
  these do not establish production throughput or a universal overhead ratio.
- Both guests passed clean shutdown/reboot, active Docker, retained root-file
  sentinel, no returning owned containers/volumes and QCOW2 integrity checks.
  Project mounts were removed before reboot: this is NOT durable project-loop
  reconciliation or libvirt acceptance. Those are explicit M2 product gates.
- The first NixOS harness missed its ANSI-coloured shell prompt. The test VM
  was stopped, its log retained, and the prompt parser corrected. A separate
  builder's emulated syscall-filter error was confined to that disposable
  builder; no host or product security settings changed.
- Evidence: `.cache/roadmap/m0-native-ubuntu.json`,
  `.cache/roadmap/m0-native-nixos.json`, corresponding console logs and the
  updated ADR 006. All test VMs, loop devices, containers and volumes removed.
  Downloaded Ubuntu image, exported worker tar, fixture ISOs and built NixOS
  images removed; the native fixture directory is now 52 KiB. No global prune.
- Final full PostgreSQL regression: 2168 tests, OK (12 skipped), 368.366 seconds,
  ResourceWarning promoted to errors (.cache/roadmap/m0-native-final-full.log).
  Ruff across CI paths, compileall, Nix expression parsing and whitespace checks
  pass. The actual NixOS fixture build and boot passed. No product Nix module,
  helper or frontend changed; no new UI build/production flake check required.
- M0's requested experiments and regression are complete. M1-M7 are not
  implemented yet; nothing pushed or deployed. Next is M1's data layer, extending
  the existing RoutineStore rather than creating a competing scheduler. Its new
  migration must follow current head 0042, not the plan's historical 0030 note.

### M1 data-layer implementation and review

- Added migration `0043_project_workspace`: scoped projects/limits/grants, current
  focus, content versions/references/events, preview metadata, task baselines,
  pending schedule/watch definitions and independent workbench credentials.
  Existing RoutineStore/queue/Outbox continue to own scheduling; no second engine.
- ProjectStore validates platform/principal identity and scope, enforces editor
  grants and optimistic revisions, serializes per-scope quota/count admission,
  keeps restore history and charges deduplicated objects once per project.
- Private CAS objects use bounded streaming, SHA-256, atomic hard-link publication,
  file/directory fsync and integrity checks. Collection shares the publication
  lock, preserves references and gives unreferenced/orphan/staging data a 24-hour
  grace period. No user workspace can write into this object root.
- Workbench credentials are separate from administrator sessions: hashed one-use
  ten-minute codes, scoped seven-day sessions and hashed CSRF secrets. Principal
  revocation is serialized with redemption. Live membership and HTTP security
  remain explicit M5 entry-point obligations, not claimed as implemented routes.
- Focused project/routine regression: 54 tests, OK, 9.740 seconds
  (.cache/roadmap/m1-reviewed-focused.log). Includes real isolated PostgreSQL
  concurrency, cancelled drafts, quota races, upgrade/backfill, reversible empty
  downgrade and refusal to downgrade retained data.
- Review caught a remaining legacy-setter permission bypass for confirmed
  project/structured routines. It now rejects those definitions; a regression
  assertion covers it. An in-progress full run was stopped deliberately and is
  not counted as passing (.cache/roadmap/m1-final-full.log). Its one leftover
  schema, owned by the stopped test PID, was removed from the disposable DB.
- Final full isolated PostgreSQL regression, including that fix: 2195 tests,
  OK (12 skipped), 380.655 seconds, ResourceWarning promoted to errors
  (.cache/roadmap/m1-accepted-full.log). Both disposable base schemas are at
  0043. Provider/QQ boundaries remain simulated, not real-account acceptance.
- After range-formatting only the edited routine methods, the focused suite
  again passed 54 tests in 9.420 seconds (.cache/roadmap/m1-final-focused.log).
- CI-path Ruff and compileall pass. M1 changes no frontend, product helper or Nix
  module, so there is no additional UI/Nix build gate for this milestone.
- `docs/project-workspace-data.md` records the data contract and remaining M2-M6
  integration responsibilities. The approved plan is unchanged and unstaged.
  No push, SSH, production migration, model request, QQ call or deployment.
- M1 is complete locally. Next is M2: helper-owned quota volumes and durable
  project lifecycle. The current runner admits only task/shell purposes; adding
  project volumes must preserve its fixed-operation RPC boundary and the
  Desktop runner's unprivileged isolation, rather than exposing arbitrary mounts.
  M2-M7 remain incomplete; no project feature is enabled in production.

### M2 quota-helper checkpoint (lifecycle still in progress)

- Added a default-disabled project-volume primitive to the existing authenticated
  Unix runner. Its fixed operations never accept caller-controlled host paths,
  devices, mount flags or shell commands. Existing task/shell behavior and the
  Desktop runner's unprivileged installation are unchanged.
- Private atomic journals bind owner/instance/project, filesystem UUID, image
  device/inode and operation fence. Each mutation rechecks loop backing identity,
  mount geometry and ext4 capacity. Stale or changed requests cannot overtake
  unfinished work. Missing images and uncertain formats are never overwritten.
- Added resumable create/mount/unmount/grow/shrink/fsck/trim/delete operations,
  stopped-container reference protection and cancellation that waits for a
  bounded filesystem utility instead of claiming rollback. Shrink verifies the
  smaller filesystem before truncating; deletion keeps an ownership tombstone.
- Focused journal/Unix RPC regression: 53 tests, OK in 0.589 seconds
  (.cache/roadmap/m2-volumes-reviewed-final.log). Host mutations are explicitly
  simulated in these tests. Initial failures were fixture setup permissions and
  macOS canonical paths; neither required relaxing product ownership checks.
- Real utility probe: the product helper created a 64 MiB ext4 image, grew it to
  96 MiB, shrank it back, ran fsck, preserved a sentinel and resumed interrupted
  growth to 80 MiB before deletion. This was a capability-free, networkless local
  container with no host mounts or Docker socket. It proves regular-file ext4
  utility behavior, NOT native mounted quotas, reboot or production wiring.
  Evidence: .cache/roadmap/m2-image-utilities.log. The initial Dockerfile used an
  image-ID form BuildKit interpreted as a registry name; the verified existing
  local tag was used instead. No production image was pulled or changed.
- The temporary container, test files and fixture image were removed. Exactly
  nine verified fixture build-cache IDs were pruned (35.64 MB); unrelated images,
  containers, runtime caches and data were preserved.
- Full isolated PostgreSQL regression: 2220 tests, OK (12 skipped), 377.363
  seconds, ResourceWarning promoted to errors (.cache/roadmap/m2-helper-full.log).
  CI-path Ruff, compileall and whitespace checks pass. Local `nix flake check
  --no-update-lock-file` passed package/import checks for aarch64-darwin; its
  incompatible-system warning is not Linux acceptance. No frontend changed.
- docs/project-volume-helper.md records the contract and remaining gates:
  native mount namespace/reboot verification, atomic container binding, durable
  lifecycle/fence allocation, archive/restore workspace incarnations, Desktop
  soft quotas, VM disk integration and accounting. M2 is NOT complete.
- User now authorizes incremental GitHub pushes after each verified checkpoint.
  Push the current work branch without merging main or deploying. The approved
  plan stays unchanged and unstaged. SSH, production changes, real model/QQ
  requests and nix-config edits remain outside this work.

### M2 incarnation and lifecycle-journal checkpoint

- User explicitly approved using the existing GitHub SSH key after HTTPS push
  lacked workflow scope. All earlier checkpoints through `50eac2b` were pushed
  to `wip/desktop-app`. This exception permits GitHub only, not production SSH;
  there was no main merge, server deployment or nix-config change.
- Volume RPC now carries a positive workspace generation. Archive restoration
  requires the next generation, a new UUID and a higher project-wide fence.
  Atomic publication preserves the tombstone high-water mark. Stale reads,
  bindings and mutations cannot operate on the replacement, even with a higher
  supplied fence. Interrupted new-generation format resumes without overwriting.
- Migration 0044 adds lifecycle runs and per-step journals to PostgreSQL, reusing
  `DurableJobStore`. Request, frozen plan, incarnation and project lock commit
  together. Each step persists arguments, a physical fence, a transport receipt,
  lease generation and evidence. Takeover keeps the physical intent but rotates
  the uncertain receipt. Old leases cannot snapshot, acknowledge or finish.
- Scoped project writers respect lifecycle locks. Only the current pending
  snapshot step may publish a version, with lease validity checked again before
  commit. Snapshot evidence references a real retained version. Completion,
  queue settlement, unlock and audit commit atomically. Failed operations retain
  their lock/journal; invalid transitions and pinned archival are refused.
- Linked unfinished tasks, required-but-missing deliveries, unresolved receipts
  and artifacts without current-revision acknowledgement conservatively block
  transitions. Physical command quiescence and atomic admission coordination
  still require the backend integration; these SQL guards alone do not prove
  that an external writer has stopped.
- Focused PostgreSQL/Unix RPC regression: 101 tests, OK in 11.131 seconds
  (.cache/roadmap/m2-lifecycle-reviewed.log). An initial added RPC test had four
  lines from its preceding test misplaced; restored those lines to their
  original test, without changing product generation checks.
- Full isolated regression: 2241 tests, OK (12 skipped), 381.857 seconds with
  ResourceWarning promoted to errors (.cache/roadmap/m2-lifecycle-full.log).
  CI-path Ruff, compileall and whitespace checks passed. `nix flake check
  --no-update-lock-file` passed aarch64-darwin package/import checks
  (.cache/roadmap/m2-lifecycle-nix.log); incompatible Linux systems were omitted,
  so this is not native Linux acceptance. No frontend changes required a build.
- The tests cleaned their temporary workspaces/schemas; only small ignored
  diagnostic logs remain. No real QQ/model call, production connection or change
  to the Desktop App/approved plan was made.
- The worker is deliberately not registered and helper remains default-disabled.
  Fixture step acknowledgements are not physical execution evidence. Still to
  do: Docker/VM adapters, native mount/reboot tests, soft quotas, rebuildable-data
  policy, idle scheduler, retention/accounting, compensation and user controls.
  M2 and subsequent milestones remain incomplete. The approved plan is unchanged.

### M2 Docker project-workspace checkpoint

- Added a default-disabled Docker project adapter and fixed Unix operations for
  start/stop/remove/status. The RPC requires an explicit workspace generation
  and mutation fence; it cannot accept host paths, images or arbitrary mounts.
- The helper reserves the container ID, creation token, immutable image and
  root identity before Docker admission. Pending creation protects the volume
  even before a container exists. Lost acknowledgements and runner-process
  interruption reconcile the same container rather than allocating another.
- Project containers use the private quota bind, not temporary task volumes.
  Startup and live exec probes compare root device/inode; inspect verifies
  ownership, generation, token, mounts, readonly cache, resource limits and
  confinement. Wrong namespaces, missing containers and altered bindings fail
  closed. Public status omits private paths/tokens; creation errors are redacted.
- Existing Docker IDs, not mutable names, are used for stop/start/remove.
  Removal refuses running containers, never forces deletion and preserves
  project files. Generic task/shell lifecycle and reclaim cannot remove projects.
  File transfers share execution locks, and project admission uses existing
  owner/global capacity limits. The default installed runner is unchanged.
- Focused final regression: 141 tests, OK in 1.465 seconds
  (.cache/roadmap/m2-workspaces-final-focused.log). Tests use real private
  journals and Unix sockets with explicitly simulated Docker/kernel state.
  Initial fixture failures were an unawaited async mock and trying to simulate
  remount preservation without a real kernel; corrected the fixtures rather
  than weakening product checks. No native quota claim is based on those tests.
- Full isolated PostgreSQL regression: 2257 tests, OK (12 skipped), 385.290
  seconds (.cache/roadmap/m2-workspaces-full.log), ResourceWarning as errors.
  That run began before the final creation-error redaction and its additional
  regression were added; the 141-test focused run above covers the final code.
  No real model/QQ requests or production services were used.
- CI-path Ruff, compileall and whitespace checks passed. Local `nix flake check
  --no-update-lock-file` passed aarch64-darwin imports/package checks for the
  final product code (.cache/roadmap/m2-workspaces-final-nix.log). Incompatible
  Linux systems were omitted, not validated. No frontend changed.
- The approved plan is still unchanged and unstaged. Disposable test files are
  cleaned automatically; only small ignored diagnostic logs remain. This
  checkpoint is for the existing work branch, not main or a server deployment.
- M2 is still incomplete. Runner-process recovery with intact mounts is covered;
  host reboot can lose/change loop mounts and currently quarantines the old
  binding. Native remount/rebind recovery, Ubuntu/NixOS acceptance, lifecycle
  worker dispatch, VM integration, Desktop soft quotas, idle scheduling,
  rebuildable-data policy, retention/accounting and compensation remain.

### M2 native host-reboot recovery checkpoint

- Project bindings now record the Linux boot UUID. Lost mounts report
  `needs_recovery`; file and command access remain blocked. Reconciliation
  requires a different verified boot, the original backing identity/UUID and
  unchanged capacity/generation. Same-boot mount loss and legacy bindings without
  boot proof fail closed rather than silently creating an empty workspace.
- A pending start keeps its original fence. Only the verified stopped container
  is removed; independent consumer checks precede fsck/remount. The helper
  publishes the replacement binding atomically and confirms the actual Docker
  mount before acknowledging. No reformat, quota change, forced removal or
  automatic application restart is introduced.
- Focused regression: 53 tests, OK in 1.035 seconds
  (.cache/roadmap/m2-reboot-focused.log). Fault cases include interruption after
  fsck, attachment and mount; an unacknowledged pre-reboot start; active old
  containers; unexpected underlying files; invalid/unreadable boot identity.
- Native product RPC acceptance passed on disposable aarch64 Ubuntu 24.04 and
  NixOS guests under QEMU HVF. Each wrote a 64 MiB project to ENOSPC while the
  second project remained writable, retained a pending start, powered off,
  booted the same guest disk and recovered both files under new container IDs.
  Original filesystem UUIDs and generation 1 were preserved. Both guests
  removed owned containers, mounts, loops and cache volume, powered off cleanly,
  and passed qemu-img checks. The temporary overlay disks were removed.
- Evidence: .cache/roadmap/m2-native-ubuntu.json and m2-native-nixos.json. Fixture
  archive SHA256: c3f9886b74d453dbbb11d93f5ef58c644b9856ace26c7355f8201bb8f07f8ede.
  The injected write reached 57,307,136 bytes on Ubuntu and 55,574,528 on NixOS
  before ENOSPC; these exclude filesystem overhead, not a promise of 64 MiB
  usable data. The seeded Python fixture tests storage, not Nix installation.
- Early harness attempts exposed ISO filename truncation, incomplete fixture
  imports and an incorrectly public test-state directory. Fixed fixture
  packaging and setup, without weakening product ownership/isolation checks.
  Ubuntu used public package-download NAT only; NixOS had no NIC. Neither guest
  had SSH, host directory mounts, production credentials or real QQ/model calls.
- Native-builder container removed. This checkpoint's approximately 2.4 GiB of
  downloaded/generated guest disks, ISOs and exported worker tar were removed;
  only small diagnostics and fixture source staging remain. Unrelated Docker
  images, services and user files were not cleaned or changed.
- Full isolated PostgreSQL regression: 2265 tests, OK (12 skipped), 384.167
  seconds with ResourceWarning promoted to errors
  (.cache/roadmap/m2-reboot-full.log). CI-path Ruff, compileall and whitespace
  checks passed. `nix flake check --no-update-lock-file` passed aarch64-darwin
  imports/package checks (.cache/roadmap/m2-reboot-nix.log); omitted platforms
  are not covered by that command. The native Linux evidence above separately
  validates the helper, not a complete Linux product installation. No UI changed.
- The default runner remains unchanged/disabled. M2 still needs lifecycle worker
  dispatch, VM-backed project disks, Desktop soft quotas, automatic dependency
  reconstruction, idle scheduling, retention/accounting and compensation. This
  checkpoint does not complete M2, merge main or deploy to a server. The approved
  plan remains unchanged and unstaged.

### M2 reserved lifecycle execution checkpoint

- Added a private OCI lifecycle executor over the existing durable queue and
  authenticated Unix helper. Fresh wake now drives create, reserve, mount,
  container start, release and final observation. Queue and project completion
  commit atomically. Registration remains explicit and disabled in startup.
- Protocol-2 plans contain a private reservation; public request/journal results
  redact it and the helper persists only its hash. Allocation and I/O locks
  protect reservation publication. Unrelated commands, file transfers, starts
  and volume mutations cannot interleave subsequent maintenance steps, even
  after runner restart. Existing active transfers prevent reservation entry.
- Interrupted effects reuse their physical fence, while lease takeover rotates
  transport receipts. Cancellation preserves the journal and reservation, not
  a falsely completed task. Deletion holds its reservation in the tombstone
  until owned release; a new generation cannot replace it early. Wrong tokens,
  generations, stale fences and malformed requests are rejected.
- Host-resolved access is required before every step and checked again after
  effects. Missing/revoked membership or mismatched scope cannot continue or
  acknowledge. The generic queue recognizes same-generation domain settlement
  without a false lost-lease warning or heartbeat cancellation.
- Corrected two transition-plan edges: stopped workspaces wake through their
  existing binding rather than remounting a still-referenced volume; archive
  from hibernation mounts for snapshot export rather than stopping a nonexistent
  container. These fixes do not enable the missing retained-file adapter.
- Whole-plan preflight refuses missing snapshot/restore/prune/rebuild adapters
  before touching a container. Only core OCI wake dispatch is currently complete;
  stop/hibernate/archive and version restore are NOT enabled end to end. VM,
  malformed and old protocol-1 plans fail closed, without automatic plan rewrite
  or unlocking. Recovery UI/compensation remain future integration work.
- Focused regression: 100 tests, OK in 15.779 seconds
  (.cache/roadmap/m2-executor-focused.log). Actual PostgreSQL schemas, private
  journals and Unix RPC use explicit fake Docker/kernel observations. Coverage
  includes lost start acknowledgement without duplicate containers, cancellation
  and takeover, access revocation, reservation write/I/O races, tombstone release,
  failed final verification and same-generation queue settlement. This is not
  new native Linux acceptance; earlier reboot evidence predates this executor.
- Full isolated PostgreSQL regression: 2283 tests, OK (12 skipped), 383.501
  seconds with ResourceWarning promoted to errors
  (.cache/roadmap/m2-executor-full.log). No product changes followed that run.
- CI-path Ruff, compileall and whitespace checks passed. `nix flake check
  --no-update-lock-file` passed aarch64-darwin package/import checks
  (.cache/roadmap/m2-executor-nix.log); incompatible platforms were omitted.
  No frontend build was needed. Temporary test workspaces and schemas are
  automatically removed; small ignored diagnostics remain.
- M2 still needs retained-file execution, rebuild/lockfile policy, VM integration,
  Desktop soft quotas, idle scheduling, accounting/retention, complete admission
  and compensation, followed by native end-to-end acceptance. M2-M7 remain
  incomplete. No production SSH, QQ/model request, server configuration, App
  change, main merge or deployment occurred. The approved plan is unchanged.

### M2 offline retained-file checkpoint

- Added an explicit `DockerProjectFiles` adapter for reserved offline snapshots
  and retained-version restore. Stop can now publish a real version before
  completing; a first wake with a baseline restores and checks it before Docker
  starts. The adapter remains opt-in and absent from ordinary startup.
- The authenticated Unix protocol carries bounded binary chunks (at most
  1 MiB), canonical manifest pages and generation/fence/reservation identity.
  The helper never accepts a host path or starts an application to export files.
  Descriptor-relative traversal rejects links, special files, nested mounts,
  unsafe modes and changed identities; limits remain 256 MiB/file and 20,000
  files within quota. Only empty root filesystem recovery directories are ignored.
- Migration 0045 persists job-owned staged references, charged to scope quota
  and protected from GC. Streaming does not hold the durable job row, so queue
  renewal is independent of a slow file. Lease/access are checked before and
  after each transfer; takeover verifies and reuses completed staged objects.
  Source-tree recheck precedes atomic manifest publication and stage removal.
  Missing/private-mutable objects cannot be published as valid references.
- Restore writes only into an empty, never-started reserved volume. Identical
  chunk retries are idempotent, different overlaps or out-of-order writes fail.
  Full file hashes, executable modes, empty files and the complete tree are
  verified. A separate uncached `restore_check` prevents a cached completion
  receipt from disguising changed bytes. Failed restores preserve the partial
  volume and reservation, without claiming success or deleting unrelated files.
- Shared canonical path/manifest validation lives outside the NoneBot plugin.
  The Desktop controller recipe includes that module; an isolated stdlib-only
  import test checks the actual recipe's copied Python sources, not the checkout.
- Focused file/lifecycle/store regression: 75 tests, OK in 19.526 seconds
  (.cache/roadmap/m2-files-focused.log), including real local PostgreSQL and Unix
  RPC with simulated Docker/kernel observations. The additional controller
  recipe/import suite passed 4 tests in 0.429 seconds. There are no real model,
  QQ, native mounts or production credentials in these fixtures.
- The first full run exposed two stale fixed test schemas (0044 instead of the
  required 0045), while per-test temporary schemas were already migrated. Only
  the two local PostgreSQL test databases on port 25432 were upgraded. The 14
  affected integration/runtime tests then passed in 1.288 seconds. Initial
  diagnostics remain in .cache/roadmap/m2-files-full-initial.log; no revision
  guard was bypassed and no production database was contacted.
- Final full isolated PostgreSQL regression: 2305 tests, OK (12 skipped),
  389.474 seconds with ResourceWarning promoted to errors
  (.cache/roadmap/m2-files-full.log). No product changes followed that run.
- CI-path Ruff, compileall and whitespace checks passed. `nix flake check
  --no-update-lock-file` passed aarch64-darwin package/import checks
  (.cache/roadmap/m2-files-nix.log); other platforms were omitted. No frontend
  changed. Temporary file fixtures are automatically removed; only small local
  ignored diagnostics remain. No server SSH, deployment, QQ/model call, App
  release, nix-config change or main merge occurred.
- Remaining M2 gates: lockfile/rebuild classification (including dependency
  symlinks), prune/rebuild execution, automatic idle/retention/usage policies,
  VM project disks, Desktop soft quotas, complete task admission/compensation
  and native end-to-end acceptance. The current snapshot deliberately retains
  all supported regular files and fails closed on unsupported entries. Hibernate
  and archive plans needing missing adapters are rejected before mutation.
  M2-M7 remain incomplete; the approved plan is unchanged and unstaged.

### M2 locked dependency cleanup checkpoint

- Added bounded root npm/uv/cargo recipe classification. Validated lock inputs,
  command and optional exact `.gaoji/rebuildable` paths are bound into canonical
  format-2 manifests; old format-1 versions remain readable. Missing/unsupported
  recipes retain files and record warnings. No command executes on the host.
- Snapshot omits only observed classified directories, without deleting them.
  Git metadata, notes, browser login state and locked inputs are protected.
  Version rollback preserves the recipe; wake/archive cannot silently substitute
  changed mutable project configuration for a retained version's recipe.
- Hibernate cleanup requires the acknowledged current snapshot and freezes its
  digest/fence. A private journal persists exact root identities before deletion.
  Descriptor-relative cleanup never follows links or crosses filesystems; changed
  retained inputs/replaced roots stop processing. Interrupted cleanup can resume
  at the same fence after lease takeover. Uncached completion verification checks
  both retained bytes and absent rebuildable roots before continuing hibernation.
- Focused helper/file/lifecycle/store regression: 94 tests, OK in 26.229 seconds
  (.cache/roadmap/m2-prune-focused.log). Includes real local PostgreSQL and Unix
  RPC with explicit simulated Docker/kernel observations: no-lock retention,
  publication-before-cleanup, changed lock/root, symlink target preservation,
  mid-deletion restart, lost acknowledgement takeover, fresh completion checks,
  rollback recipe retention and rebuild preflight after metadata changes. Review
  also restricted default dependency-directory matches to the project root:
  a root lockfile cannot justify cleaning an unrelated nested project's files.
  Generic caches and explicit declarations remain separately classified.
- Full isolated PostgreSQL regression: 2323 tests, OK (12 skipped), 393.766
  seconds with ResourceWarning promoted to errors
  (.cache/roadmap/m2-prune-full.log). This run loaded the code before the final
  nested-directory restriction; the final classifier was separately verified by
  the 94-test focused suite above, including the additional nested-project case.
- Final CI-path Ruff, compileall and whitespace checks passed. `nix flake check
  --no-update-lock-file` passed current aarch64-darwin package/import checks
  (.cache/roadmap/m2-prune-nix.log); incompatible systems were omitted, not tested.
  Test workspaces and temporary schemas are removed by their fixtures. Only small
  ignored diagnostics remain; no frontend/App build or live acceptance was run.
- M2 remains incomplete: isolated dependency reconstruction, VM project disks,
  Desktop soft quotas, idle/retention/usage policies, complete task admission and
  compensation, and native full-lifecycle acceptance are still required. A wake
  requiring the missing rebuild adapter fails preflight; ordinary startup does
  not register the private project worker. No production services were changed.
- Current authorization permits verified checkpoint pushes to GitHub
  `wip/desktop-app` using the existing SSH key. It does not permit a main merge,
  production SSH, server deployment, QQ/model calls or nix-config changes. The
  user's new plan remains unchanged, untracked and excluded from commits.

### M2 isolated dependency reconstruction checkpoint

- Added private reconstruction after retained-file restoration and before the
  ordinary workspace starts. Only a retained, hash-bound npm/uv/cargo recipe can
  authorize it; missing-lock versions preserve dependencies without inventing an
  installer. Unsupported old frozen execution order fails preflight.
- A named isolated dependency container has a pinned image, no automatic restart
  or auto-remove, a fixed 900-second deadline and a private operation journal.
  Lost create/start/remove acknowledgements reconcile its immutable identity and
  Docker state. A missing already-dispatched container is never silently rerun.
  Worker lease takeover preserves the original effect fence and deadline.
- Zero exit alone is insufficient: OOM, changed input hashes or changed retained
  files fail reconstruction. Completion removes only the observed stopped helper
  container, commits the volume outcome and obtains a fresh uncached check. A
  crash between completion journals cannot be reported as success. Failed builds
  preserve files, retained versions and the maintenance reservation for recovery.
- Progress is persisted in the current pending lifecycle step under its live
  lease. It is not a completion receipt and cannot be written by a stale worker.
  The runtime remains explicitly opt-in; normal startup registers no project job.
- The full simulated lifecycle exposed stale private journals blocking a new
  generation after archive. Deletion now validates and removes only recognized,
  same-generation internal journals; foreign or linked journals stop deletion,
  and unknown files remain protected rather than being swept.
- Focused regression: 177 tests, OK in 33.951 seconds
  (.cache/roadmap/m2-rebuild-focused.log), using actual local PostgreSQL and private
  Unix RPC with explicit Docker/kernel fixtures. Coverage includes two wake paths,
  full stop/hibernate/wake/archive/restore, failure progress, cancellation/lease
  takeover, lost acknowledgements, fresh verification and retired-journal safety.
- Full isolated PostgreSQL regression: 2348 tests, OK (12 skipped), 400.284
  seconds with ResourceWarning promoted to errors
  (.cache/roadmap/m2-rebuild-full.log). No product changes followed this run.
  CI-path Ruff, compileall and whitespace checks passed. `nix flake check
  --no-update-lock-file` passed aarch64-darwin package/import checks
  (.cache/roadmap/m2-rebuild-nix.log); incompatible systems were omitted, not
  tested. No frontend changed. Temporary workspaces and test schemas are removed
  by their fixtures; ignored diagnostics are small and contain no live credentials.
- Remaining M2 gates: native full-lifecycle/host-reboot acceptance, VM project
  disks, Desktop soft quotas, automatic idle/retention/usage policies, complete
  task admission and compensation/retry UX. No production switch, QQ/model call,
  App release, nix-config change or main merge is part of this checkpoint.
  The approved plan remains unchanged, untracked and excluded from commits.

### M2 automatic idle and retention checkpoint

- Added an explicitly enabled host scheduler, with bounded round-robin project
  pages, isolated failures and live owner/membership resolution. It does not
  manufacture an administrator identity, start itself on import, purge projects
  or directly operate containers. Unsupported/unverified backends are skipped.
- Idle policy uses actual activity: 30 minutes to stop, seven days to hibernate,
  60 days to archive. Pinned projects may stop/hibernate but cannot archive.
  Eligibility, optimistic version, task locks and unresolved deliveries are
  checked again under the project lock before queue admission. Backend preflight
  runs before a durable job is created. Concurrent schedulers cannot create two
  operations for one project; executor receipts still determine completion.
- Fixed lifecycle snapshots renewing the activity/focus timestamp. Both new and
  unchanged maintenance snapshots now preserve it, and a successful manual wake
  renews it. Opening a project races safely with admission: an earlier open
  cancels eligibility; an admitted transition blocks opening until it finishes.
- The private helper reports active commands/file transfers, and automatic
  maintenance requires an idle, correctly identified workspace. Old helpers
  without this observation are not assumed idle. Reservations remain the final
  serialized gate against a command starting after the observation.
- Retention reads the full history rather than the UI's 200-version page. It
  keeps labelled/pinned versions, ten recent automatic versions, daily versions
  for 30 days and weekly versions thereafter, using UTC calendar buckets. Archive
  retention keeps the latest/permanent versions. Clock-future versions remain
  protected. Durable task and preview references override retention, including
  expired previews until their reference is explicitly removed.
- Version deletion and its audit are atomic. Object collection is bounded to
  100 blob deletions per hourly pass and retains the existing 24-hour grace and staged
  snapshot/reference guards. Neither retention nor inventory polling renews
  activity. No original project or unrelated cache directory is swept.
- Focused regression: 134 tests passed in 46.145 seconds before hourly collection
  and its two additional tests (.cache/roadmap/m2-policy-focused.log). Final full
  isolated PostgreSQL regression: 2378 tests passed (12 skipped), 435.110 seconds
  with ResourceWarning promoted to errors (.cache/roadmap/m2-policy-full.log).
  This includes actual PostgreSQL and private Unix RPC with simulated Docker and
  kernel observations, not native Linux acceptance. No product code changed after
  this run. CI-path Ruff, compileall and whitespace checks passed. Nix package and
  import checks passed on aarch64-darwin (.cache/roadmap/m2-policy-nix.log);
  incompatible systems were omitted, not tested. Temporary fixture schemas and
  directories are cleaned automatically; the remaining diagnostics are small.
- The user authorized merging the verified work into main and deleting the two
  merged branches. Both remote branches are deleted; the older detached checkout
  and its ignored user data were preserved. Checkpoints now target the bot's main
  branch. No server SSH, rebuild, App publication or nix-config update occurred.
- M2 remains incomplete: native lifecycle/host-reboot acceptance, VM project
  disks, Desktop soft quotas, measured usage and quota alerts/metrics, complete
  task admission, compensation/retry/trash workflows and production composition.
  M3-M7 remain pending. The supplied plan is still untouched and excluded.

### M2 measured usage and quota checkpoint

- Added private, read-only project accounting bound to the volume owner,
  generation and observed mount identity. Mounted ext4 usage comes from available
  filesystem blocks, not the sparse image's apparent size. Offline usage requires
  a clean ext4 header and matching geometry; sampling never mounts a volume.
  Reserved blocks are unavailable in both measurements. A verified deletion can
  report zero; a missing volume or failed probe cannot.
- Rebuildable-byte estimates use the existing pinned-lockfile recipes. Traversal
  is descriptor-relative, bounded and does not follow symlinks or cross devices.
  Shared hard links are counted once and retained aliases prevent reclamation
  claims. Unknown recipes retain everything. Scan failure or an unmounted volume
  yields an unknown estimate, not a fabricated zero or permission to delete.
- Migration 0046 adds a separate usage-observation cache. Failure preserves the
  last known values but invalidates write admission. Samples must match project
  identity, quota and generation, pass the optimistic version check and be fresh.
  Metadata observation does not renew activity, focus or configuration version.
- Added scoped usage/status and capacity gates. Unknown/stale usage, including
  another project in the same conversation, blocks admission. Session totals
  include retained versions. At 95 percent the private dependency executor also
  checks live volume usage before creating an installer. Full-volume rejection
  names cleanup, old-version pruning and administrator expansion as options.
  This is not yet atomic reservation of concurrent future writes, nor a parser
  for arbitrary shell commands that happen to install packages.
- At 80 percent a warning is queued in the same PostgreSQL transaction as usage,
  with one idempotency key per conversation and UTC day. Concurrent samplers,
  restarts and multiple full projects cannot enqueue duplicate daily warnings.
  No live QQ message was sent by this checkpoint.
- The opt-in sampler uses bounded inventory pages, verifies the current owner
  before and after the helper probe, isolates failures and preserves cancellation.
  It never registers itself in production startup. Telemetry exposes workspace,
  version and known rebuildable bytes, state counts, quota rejections and separate
  unknown total/rebuildable counts. Workspace metrics are last-known values, not
  a claim that stale measurements are current. Lifecycle transition counters and
  the complete capacity-snapshot composition remain pending.
- Focused local PostgreSQL/helper regression: 111 tests passed in 27.398 seconds
  (.cache/roadmap/m2-usage-focused.log), before a rejection-message clarification.
  Docker and kernel observations in these tests are fixtures; they are not native
  Linux enforcement acceptance. The approved plan was not modified.
- The first full run exposed an outdated local acceptance database (0045) and
  a logging assertion affected by Alembic's process-wide logging configuration.
  Upgraded only the loopback test schema to 0046 and asserted the sanitized log
  call without relying on global logger configuration. The 22 targeted checks
  then passed (.cache/roadmap/m2-usage-environment-check.log).
- Final full isolated PostgreSQL regression: 2411 tests, OK (12 skipped),
  426.260 seconds with ResourceWarning promoted to errors
  (.cache/roadmap/m2-usage-full.log). No product code changed after this run.
  CI-path Ruff, compileall and whitespace checks passed. Nix package/import
  checks passed on aarch64-darwin (.cache/roadmap/m2-usage-nix.log); incompatible
  platforms were omitted, not tested. Fixture-owned temporary directories and
  schemas are cleaned automatically. Only small ignored diagnostics remain.
- This checkpoint is committed/pushed to the bot main branch only. It does not
  update nix-config, enable the worker in production, deploy to a server or make
  live QQ/model calls. No user plan or unrelated local changes are included.
- Still required in M2: native lifecycle and host-reboot acceptance, VM project
  disks and discard, Desktop soft quotas with running-writer cancellation,
  complete atomic admission, compensation/retry/trash workflows and runtime
  composition. Commands, control-panel usage UI and later M3-M7 work remain open.

### M2 independent VM disk foundation

- Extracted the existing private directory, owner/instance checks, operation
  fences and atomic intent journal into one shared implementation. Native ext4
  volumes retain their existing format and API. New qcow2 journals explicitly
  identify their storage backend; neither backend can adopt the other's data.
- Added private host-owned project data disks outside temporary VM system
  overlays. Virtual size is exactly the project quota, with sparse allocation
  and no backing chain, external data file, encryption or internal snapshots.
  Guest filesystem usage is deliberately not inferred from qcow2 allocation.
- Creation, offline disk growth and deletion are journalled. Reopening after a
  lost acknowledgement inspects the existing image rather than formatting again.
  Missing, replaced, corrupt or partially initialized disks fail closed. Deletion
  leaves a generation/fence tombstone; recreation requires the next generation.
  Growth here changes only the block device: guest filesystem growth and all
  shrinking remain separate, unimplemented lifecycle operations.
- A durable attachment claim protects a disk even before a VM is defined.
  Release requires the domain to be undefined, not merely stopped. Inventory
  checks live and inactive libvirt definitions and backing-store references;
  failed inventory is not interpreted as an empty list. A matching domain name
  alone does not authorize adopting an unjournalled attachment. No generic task
  cleanup can delete a claimed project disk through this API.
- Existing VM creation previously normalized unsupported purposes to `task`.
  It now rejects `project` rather than silently assigning temporary retention.
  The project disk API is not exposed through a model tool, helper RPC or runtime
  startup yet. The next VM work must integrate the reserved lifecycle executor,
  guest filesystem identity/mounting, independent system overlays, discard and
  trim, retained-file operations and recovery before enabling this backend.
- Focused regression: 99 tests passed in 1.766 seconds, followed by a stricter
  encrypted-header rejection and explicit growth fsync covered by the final
  regression below. The local QEMU test created a real 64 MiB sparse qcow2,
  wrote a known byte pattern, reopened it, grew it to 96 MiB, verified the pattern
  and deleted it. Fixture-owned directories and disk files are automatically
  removed. Libvirt inventory tests use fixtures, not a running production daemon.
- Final full isolated PostgreSQL regression: 2434 tests, OK (12 skipped),
  433.520 seconds with ResourceWarning promoted to errors
  (.cache/roadmap/m2-vm-disks-full.log). CI-path Ruff, compileall and whitespace
  checks passed. Nix package/import checks passed on aarch64-darwin
  (.cache/roadmap/m2-vm-disks-nix.log); incompatible systems were omitted, not
  tested. No product code changed after the final regression.
- This is not guest ENOSPC/trim/reboot acceptance and does not complete M2.
  The supplied plan remains unchanged and excluded from commits. No production
  SSH, nix-config edits, rebuild, live QQ/model call or App release occurred.

### M2 VM guest mount and identity contract

- Added a standalone root-only guest helper with fixed `/dev/vdb` and
  `/workspace` targets. It verifies the virtio serial, exact device capacity,
  ext4 UUID/geometry, mount device number/root/options and uid/gid 1000 ownership.
  A plain directory on the system disk cannot pass workspace verification.
  Filesystem accounting uses available guest blocks, not qcow2 physical size.
- Formatting needs explicit first-boot permission and a bounded read proving
  the entire device is zero, not just an empty superblock. Foreign filesystems,
  partition tables, partial nonzero disks, extra mounts and unexpected capacity
  are rejected. Successful mount consumes the guest's initialization permission
  through an fsynced atomic config replacement. Root-owned configuration and a
  bounded guest lock serialize management commands. Host-side initialization
  acknowledgement across replacement system overlays still needs lifecycle wiring.
- The mountpoint is empty and root-only before mounting and after unmounting;
  pre-existing files or symlinks are not hidden or removed. Mount retry reconciles
  a previously successful format/mount and finishes ownership initialization.
  Unmount never uses force/lazy flags. Trim verifies the workspace before and after
  `sync -f`/`fstrim`; its receipt confirms the guest discard command, not an invented
  number of physical bytes reclaimed on the host.
- The existing VM domain/cloud-init builders now accept a private claimed-disk
  binding. Project data is a separate virtio disk with `discard=unmap`, a stable
  serial and project identity metadata. The root-owned helper/config and a bounded
  oneshot mount service replace generic mkdir/chown setup for project VMs. Normal
  task/shell templates remain unchanged. Public `create` still rejects project
  purpose: the private builders are not yet a production provisioning path.
- Project-mode command/read/write/export admission verifies the guest mount and
  matches its structured receipt against host metadata before user work. Generic
  start/stop/destroy cannot operate project VMs; the durable project executor must
  own these transitions. Live membership, host disk-claim revalidation, task/IO
  reservations, snapshot/restore, dependency rebuilding and boot recovery remain
  requirements of that adapter, not capabilities asserted by this checkpoint.
- Focused regression: 78 tests passed in 1.084 seconds
  (.cache/roadmap/m2-vm-mount-focused.log). This covers blank-device scanning,
  interrupted initialization, identity mismatches, system-disk fallback rejection,
  busy unmounts, atomic config replacement, XML/cloud-init assembly and tool guards.
  Guest mount/block observations are fixtures. No new native Linux/libvirt guest
  was booted here; real quota/trim/reboot acceptance remains a separate gate.
- Final full isolated PostgreSQL regression: 2458 tests, OK (12 skipped),
  438.906 seconds with ResourceWarning promoted to errors
  (.cache/roadmap/m2-vm-mount-full.log). CI-path Ruff, compileall and whitespace
  checks passed. Nix package/import checks passed on aarch64-darwin
  (.cache/roadmap/m2-vm-mount-nix.log); incompatible systems were omitted, not
  tested. No product code changed after this run. Temporary test files were
  cleaned automatically; only small ignored diagnostics remain.
- M2 remains incomplete and disabled in production. No server SSH, rebuild,
  nix-config change, live QQ/model call or App publication occurred. The supplied
  plan remains unchanged and excluded from the checkpoint.

### M2 VM initialization acknowledgement and IO admission

- Shared the existing durable maintenance reservation journal between the OCI
  and VM disk backends without changing the OCI protocol. VM claim/release,
  offline growth and deletion now require the owning reservation when one exists.
  A restarted process cannot infer that a reserved workspace is available.
- Added a first-format grant bound to both the sandbox ID and immutable libvirt
  domain UUID. The grant is persisted before cloud-init configuration is returned.
  Repeating the original preparation reconciles its journal. Replacing a system
  disk never receives a second grant, even if the first guest reply was lost:
  it may mount the existing expected filesystem, but a blank or damaged disk
  requires explicit recovery, not automatic formatting. Completed initialization
  is persisted separately from qcow2 image creation and survives claim release.
- Guest mount replies now prove that initialization permission was atomically
  consumed. The private host mount bridge compares both live and persistent
  libvirt definitions, immutable identity, data/system disk separation, discard,
  resource limits, isolated networking and the owned guest-agent channel. Failed
  inventory is never treated as an absent VM. Host acknowledgement checks the
  current disk consumer and the fresh, sealed mount receipt; interruption before
  or after either journal write can be reconciled using the same fence.
- Project admission now needs an explicitly composed host adapter, a settled
  disk claim, completed initialization and no maintenance reservation. It checks
  the guest mount and then revalidates the host claim. Command execution, imports,
  reads and exports share the same in-process VM IO lock; exporting a file does
  not recursively enter that lock. Generic temporary task/shell workflows remain
  available. No production registration or project factory was added here.
- Focused regression: 129 checks passed in 1.910 seconds
  (.cache/roadmap/m2-vm-bootstrap-focused.log), including lost preparation/mount/
  acknowledgement replies, replacement domains, invalid receipts, stale fences,
  reservation races, busy transfers and non-deadlocking exports. Libvirt and guest
  observations in these tests are fixtures, not native Linux VM acceptance.
- Remaining VM integration is substantive: journalled system-overlay provisioning,
  cross-process operation/IO leases, guest process-group quiescence (including
  detached writers), snapshot/restore/prune/dependency adapters, a VM-specific
  lifecycle plan and native boot/hibernate/reboot acceptance. In particular,
  a stopped VM cannot service guest trim/file operations; the OCI step ordering
  must not be reused blindly. Native provisioning must also verify that the
  cloud image's default user does not conflict with the required sandbox
  UID/GID 1000; the current shared template still declares both `default` and
  `sandbox`. M2 is not complete or enabled by this checkpoint.
- The user-supplied plan is unchanged. No production SSH, nix-config update,
  rebuild, live model/QQ request or App publication is part of this work.
- Final full isolated PostgreSQL regression: 2476 tests, OK (12 skipped),
  428.931 seconds with ResourceWarning promoted to errors
  (.cache/roadmap/m2-vm-bootstrap-full.log). CI-path Ruff, compileall and whitespace
  checks passed. Nix package/import checks passed on aarch64-darwin
  (.cache/roadmap/m2-vm-bootstrap-nix.log); incompatible platforms were omitted,
  not tested. No product code changed after this run. Fixture-owned temporary
  files were cleaned automatically; only small ignored diagnostics remain.

### M2 resumable VM provisioning

- Added a private provisioning driver with one durable reservation/effect fence
  from allocation through guest verification. It freezes the domain UUID, sandbox
  ID, base-image checksum and resource contract, and journals each phase before
  creating, defining or starting anything. Live and persistent domain inspection
  is required; command return values alone cannot complete a boot.
- Pre-boot retries can regenerate only known system/seed files after proving no
  VM exists. Once prepared, file identities are immutable except guest writes to
  the system overlay. Missing or replaced files fail closed. The project data
  disk is never regenerated. Lost define/start acknowledgements resume the same
  VM; a host restart can restart that stopped VM with fresh capacity admission.
- Ordinary VM allocation and project provisioning share a host file lock.
  Cancellation waits for synchronous writes before releasing it, and host
  subprocesses are reaped even when cancellation races PID acquisition. These
  checks do not yet provide cross-process guest IO leases or detached-writer
  quiescence, which remain prerequisites for lifecycle composition.
- Project cloud-init no longer also creates a distribution-default user.
  Mount/usage verification checks both the sandbox name and exclusive UID/GID
  1000 mapping. Existing workspaces retain their frozen memory/CPU/PID limits
  when host defaults change. Temporary task/shell cloud-init is unchanged.
- Focused checks: 149 passed in 2.925 seconds with ResourceWarning as errors
  (.cache/roadmap/m2-vm-provision-focused.log). This includes real local sparse
  QEMU system/data images, backing/quota checks and writes that preserve the
  independent project disk. Libvirt, guest execution and cloud-localds seed
  output are fixtures; no native VM boot is claimed by this checkpoint.
- Full isolated PostgreSQL regression: 2496 tests, OK (12 skipped), 431.948
  seconds with ResourceWarning as errors
  (.cache/roadmap/m2-vm-provision-full.log). Ruff, compileall and whitespace
  checks passed. Nix package/import checks passed on aarch64-darwin
  (.cache/roadmap/m2-vm-provision-nix.log); incompatible platforms were omitted,
  not tested. No product code changed after the regression. Temporary test
  directories and images were cleaned automatically; only ignored logs remain.
- M2-M7 remain incomplete. No production registration, server SSH, nix-config
  update, rebuild, live QQ/model request or App release is included. The approved
  input plan is unchanged and excluded from commits.

### M2 cross-process VM IO and guest execution epochs

- Commands, file transfers, provisioning and maintenance now share a per-VM host
  file lock, in addition to the existing async lock. Separate manager processes
  cannot reserve a workspace during another process's IO. Lock files are private,
  outside removable domain directories, and reject links or special files.
  Cancellation drains lock acquisition and journal writes before unlocking.
- Added a standalone root-owned guest gate with a durable execution epoch.
  Admission starts closed. Maintenance persists `closing` before draining guest
  commands; reactivation advances the epoch, so delayed requests cannot write
  into a later lifecycle operation. Epoch state and project/disk identity are
  checked again inside the guest, independent of host process survival.
- Project commands and fixed root import/export/package helpers use transient
  systemd services with whole-cgroup termination, runtime and task limits.
  Sandbox commands retain their unprivileged user identity. Quiescence requires
  an exclusive guest IO lock, an empty cgroup and no sandbox-UID processes; a
  successful stop command alone cannot produce an acknowledgement. Failed or
  uncertain quiescence leaves admission closed. Boot completes only after gate
  quiescence, and release activates the guest before settling the host journal.
- Focused regression: 140 checks passed in 2.965 seconds with ResourceWarning
  promoted to errors (.cache/roadmap/m2-vm-gate-focused.log). Real filesystem and
  independent-process lock tests cover stale epochs, interrupted acquisition,
  lost control/release replies, corrupt journals and retained reservations.
  Guest mounts, systemd and cgroup observations are explicit fixtures, not a
  claim of native Linux VM acceptance. Ruff, compileall and whitespace checks
  passed.
- Full isolated PostgreSQL regression: 2519 tests, OK (12 skipped), 431.206
  seconds with ResourceWarning as errors (.cache/roadmap/m2-vm-gate-full.log).
  Nix package/import checks passed on aarch64-darwin
  (.cache/roadmap/m2-vm-gate-nix.log); incompatible platforms were omitted,
  not tested. No product code changed after this run. All fixture directories,
  child processes and transient test images were cleaned up; ignored logs remain.
- M2 is still incomplete and production-disabled. Remaining work includes
  native command-tree/disconnect/reboot acceptance, safe guest retained-file
  snapshot/restore/prune and dependency reconstruction, the VM-specific durable
  lifecycle and desktop soft-quota integration. The input plan is unchanged.
  No server connection, nix-config edit, rebuild or App release is included.

### M2 guest retained-file snapshot, restore and cleanup

- Added a private VM file transport with host intent journalling, current
  reservation/domain checks and guest-side maintenance exclusion. A lost QGA
  reply preserves pending state; an incomplete restore cannot release the host
  reservation. Completed replies are checked against the original project,
  generation, effect fence and manifest before acknowledging the operation.
- Reused the existing retained-file engine and CAS executor instead of creating
  a second snapshot format. Scans, manifest pagination, binary chunk checks,
  partial-prefix retries, whole-tree restore verification and locked-recipe
  cleanup execute inside the guest. No untrusted VM filesystem is host-mounted.
  The file executor now has a backend-neutral name and evidence description.
- Project cloud-init installs a deterministic stdlib-only helper archive with
  an explicit source allowlist, root-only permissions and isolated Python
  invocation. No Bot configuration, secrets, Docker or host volume driver is
  included. Binary RPC chunks are bounded to 48 KiB to fit nested QGA/base64
  command arguments. Temporary task/shell VMs remain unchanged.
- Focused checks: 123 passed in 14.298 seconds with ResourceWarning promoted to
  errors (.cache/roadmap/m2-vm-files-focused.log). These exercise real private
  files and journals, lost acknowledgements, incomplete restore, wrong identity,
  changed prefixes, symbolic/hard links, FIFOs, retained source preservation and
  isolated archive imports. Guest mount, systemd/cgroup and libvirt actions are
  explicit fixtures, not native Linux VM acceptance.
- Full isolated PostgreSQL regression: 2533 tests, OK (12 skipped), 431.144
  seconds with ResourceWarning as errors (.cache/roadmap/m2-vm-files-full.log).
  Whole-CI-path Ruff, compileall and whitespace checks passed. Nix package/import
  checks passed on aarch64-darwin (.cache/roadmap/m2-vm-files-nix.log);
  incompatible platforms were omitted, not tested. Byte comparisons verified
  that the packaged VM file client, guest engine and bundle builder match the
  checked sources. No product code changed after this run. Temporary fixture
  files were cleaned automatically; only small ignored diagnostic logs remain.
- M2 is not complete or enabled:
  VM lifecycle composition, dependency reconstruction, native reboot/hibernate
  and maximum-size transport deadline acceptance, plus desktop soft quotas,
  remain substantive work. M3-M7 are also pending. The supplied plan is unchanged;
  no production SSH, nix-config change, rebuild, live QQ/model call or App release
  is included in this checkpoint.

### M2 ordered VM shutdown and retirement

- VM lifecycle plans now use protocol 3, with guest snapshot/prune/trim/unmount
  before shutdown. Stopped and hibernated guests are booted with admission closed
  when guest operations are needed. OCI protocol-2 ordering is unchanged. Wake
  restores the retained baseline before dependency reconstruction and release.
- Added a private, fenced maintenance journal for quiesce, trim, unmount, stop
  and VM retirement. Graceful shutdown is reconciled against the original
  domain, not its command exit code. A crash after journalling but before sending
  shutdown can retry that request while the same domain remains running and its
  guest gate is closed. Already-stopped guests are not shut down again; timeouts
  never force-destroy or remove disks.
- Retirement requires acknowledged unmount, verified domain absence, and exact
  file/directory/socket identities. Cleanup only unlinks frozen boot artifacts
  with no other VM consumers, fsyncing progress. Unknown/replaced files fail
  closed. Partial deletion and lost completion replies resume from the journal.
  The independent project data disk and initialization seal survive retirement;
  a subsequent boot receives a new VM identity without reformatting that disk.
- Guest disk operations share the closed execution gate and exclusive IO/disk
  locks, and verify empty command cgroups/no sandbox processes. Unmount retries
  work after the mount is already absent. Boot clears previous unmount evidence.
  Nonblocking host maintenance also rejects an already-held in-process IO lock
  instead of waiting indefinitely. Boot-file checks reject FIFOs without blocking.
- Focused VM regression: 142 tests passed in 4.049 seconds, ResourceWarning as
  errors (.cache/roadmap/m2-vm-maintenance-focused.log). Real private journals,
  filesystem/socket identities, preserved data, interrupted shutdown/undefine/
  deletion and guest receipt parsing are exercised. OS mounts, systemd/cgroups
  and libvirt remain explicit fixtures, not native VM acceptance.
- Initial focused runs exposed two test-harness errors: the new test imported
  plugin code before initializing NoneBot, and macOS's long temporary path
  exceeded the Unix socket address limit. The fixture now initializes explicitly
  and uses a short private temporary directory; no assertions were relaxed.
- Ruff, compileall and whitespace checks passed. Nix package/import checks
  passed on aarch64-darwin (.cache/roadmap/m2-vm-maintenance-nix.log); incompatible
  systems were omitted, not tested. Byte comparisons confirm the packaged
  maintenance adapter, guest gate and VM manager match the checked source.
- Full isolated PostgreSQL regression: 2559 tests, OK (12 skipped), 432.914
  seconds with ResourceWarning promoted to errors
  (.cache/roadmap/m2-vm-maintenance-full.log). No product code changed after this
  run. Temporary fixture files/sockets were cleaned automatically; only ignored
  diagnostic logs remain.
- M2 remains incomplete and production-disabled: dependency reconstruction,
  assembled VM lifecycle execution, native reboot/hibernate/power-loss and
  maximum-size file-transfer acceptance, plus desktop soft quotas, remain.
  M3-M7 are pending. No production connection, nix-config edit, rebuild or App
  publication is included; the user-authored plan is unchanged and excluded.

### M2 VM dependency reconstruction

- Added a private dependency adapter using the reserved VM file transport.
  Fresh guests import canonical version metadata without overwriting their
  retained project data. The PostgreSQL executor verifies the frozen version
  hash and recipe; each transfer rechecks its lease and live membership. The
  guest checks locked inputs and the complete retained tree before execution.
- Installation runs in a durable, uniquely identified systemd unit rather than
  the QGA request process. Boot identity, source, resource limit, deadline and
  execution phase survive client disconnection. Status can inspect the task
  without waiting on its IO lock. Lost start and completion replies reconcile
  the existing operation, never replay uncertain project scripts.
- Guest toolchain setup is fixed and administrator-owned; the allowlisted
  locked recipe runs as sandbox, inside the existing process/cgroup controls.
  Quota admission, a total 900-second deadline, child-process drain and retained
  source revalidation are required. Reboot, failed scripts or unverified
  termination keep the project reserved instead of falsely marking it active.
- Shared retained-tree verification keeps OCI and VM semantics consistent.
  The stdlib-only guest bundle includes this adapter, not Bot credentials or
  host drivers. No public model tool or unauthenticated control endpoint added.
- Focused validation: 55 tests passed in 13.390 seconds, with ResourceWarning
  promoted to errors (.cache/roadmap/m2-vm-dependencies-focused.log). Initial
  test runs exposed a wrong fixture project ID, a mismatched expected exception
  type and nested-fixture cleanup; these test-harness defects were corrected
  without weakening the assertions.
- Full isolated PostgreSQL regression: 2583 tests, OK (12 skipped), 431.540
  seconds with ResourceWarning as errors
  (.cache/roadmap/m2-vm-dependencies-full.log). Whole-CI Ruff, compileall and
  whitespace checks passed. No product code changed after this run.
- Nix package/import checks passed on aarch64-darwin
  (.cache/roadmap/m2-vm-dependencies-nix.log); incompatible systems were omitted,
  not tested. Byte comparisons confirm that the packaged guest dependency
  helper, VM client and PostgreSQL file executor match the checked source.
  Filesystem/journal tests use explicit systemd/libvirt fixtures, not native VM
  acceptance. Temporary fixture files were cleaned; only ignored logs remain.
- M2 is still incomplete and production-disabled. Assembled VM lifecycle
  execution, native reboot/hibernate/power-loss and maximum-size file-transfer
  acceptance, desktop soft quotas and remaining product integration are pending.
  M3-M7 are pending. No production SSH, nix-config change, deployment or App
  publication is included; the user-authored plan remains unchanged/untracked.

### M2 assembled VM lifecycle checkpoint

- Added private `VmProjectRuntime` and protocol-3 `VmProjectBackend`, composed
  with the existing PostgreSQL lifecycle/file executors. Ordered whole-plan
  validation happens before effects; this does not register a production worker
  or expose a model-facing host command API.
- Real PostgreSQL leases, CAS versions, retained files and guest journals now
  exercise complete stop, hibernate, wake, archive and restore transitions.
  Hibernation preserves the data disk while removing dependencies/system VM;
  archive recovery restores a new disk generation and the pinned dependencies.
- Lost boot, snapshot, restore-chunk, activation, retirement and dependency
  completion acknowledgements resume their original effect identities. Lease
  takeover does not duplicate VMs/installers. Revoked membership, failed recipes
  and unpublished snapshots cannot silently release admission or retire data.
- Completion freshly verifies immutable domains, bootstrap files, disk claims,
  retired paths, exact host/guest admission epochs and guest writer state.
  Stopped-domain validation now also runs before reservation release. Reappearing
  domains/directories and superseded journals reject completion.
- Inspection exposed a pre-existing mismatch: project VM XML created an
  unbounded host `console.log` absent from retirement's exact file allowlist.
  New project VMs use PTY serial output; ordinary VMs keep their existing file
  console. Guest command output continues to use QGA.
- Final relevant regression: 426 project tests, OK in 84.020 seconds
  (`.cache/roadmap/m2-vm-lifecycle-regression.log`), plus 10 VM artifact tests,
  OK in 0.036 seconds (`.cache/roadmap/m2-vm-lifecycle-artifacts.log`), all with
  ResourceWarning as errors. Initial fixture counters included inherited setup
  operations and an assertion expected the wrong exception base; these harness
  defects were corrected without relaxing the tested product invariants.
- Whole-CI Ruff, compileall and whitespace checks passed. Nix package/import
  checks passed on aarch64-darwin (`.cache/roadmap/m2-vm-lifecycle-nix.log`);
  other systems were omitted, not tested. Byte comparisons verified packaged
  VM runtime, lifecycle backend and VM manager match the tested source. The
  unrelated full repository suite was not rerun for this checkpoint.
- OS mounts, systemd and libvirt remain explicit fixtures, not native KVM
  acceptance. M2 remains incomplete: native full lifecycle/power-loss and large
  transfer validation, VM usage/scheduler integration, desktop soft quotas and
  remaining product task integration are still required. M3-M7 are pending.
  No production connection, shared nix-config edit, rebuild or release occurred.
  The user-authored plan is unchanged and excluded; fixture cleanup is automatic.

### M2 VM usage and maintenance scheduler integration

- Added private VM usage sampling and maintenance readiness to the existing
  PostgreSQL sampler/scheduler. Running guests read filesystem usage and perform
  bounded locked-recipe rebuildable-data scans under guest admission/IO locks.
  Active writers and failed scans leave rebuildable bytes unknown, not zero.
- Stopped/hibernated disks are measured without booting or mounting them. A
  bounded QEMU read obtains the ext4 superblock; clean state, checksum, UUID,
  block geometry, image identity, domain state and journal fence must agree.
  No force-share or repair is used. qcow2 host allocation is not confused with
  filesystem usage. Deleted archived workspaces require verified retirement;
  their retained version blobs remain separately charged.
- Sampling failures keep last-known bytes but reject fresh quota admission.
  Idle scheduling rechecks ownership and actual guest writers, then uses the
  existing transactional task/delivery guards and durable lifecycle jobs.
  Sampling and automatic transitions do not reset real user activity.
- Added real PostgreSQL integration across active/stopped/hibernated/archived
  states, including restart-safe scheduling, failed measurement, changing
  disks/domains, revoked identity, stale epochs, busy writers and unconfirmed
  file delivery. Native VM/libvirt/mount effects remain explicit fixtures.
- Initial focused runs exposed fixture-only API/exception mistakes; corrected
  the DB source/transaction and expected exception types, without weakening
  tested invariants. The corrected focused run passed 14 tests in 7.973 seconds.
- Actual utility validation in a disposable local Linux container used 64 MiB
  ext4 images with 1 KiB/4 KiB blocks and 0%/5% reserved space. Empty and populated
  usage matched dumpe2fs; image hashes and retained content stayed unchanged;
  dirty images were rejected. An initial QEMU 7.2 skip/count read returned zero
  bytes, so the product now reads a fixed 2 KiB prefix instead. Evidence:
  `.cache/roadmap/m2-vm-usage-image.log`. No mounts, capabilities, Docker socket
  or active network were available during verification. Temporary images and
  the fixture container, including installed utility packages, were removed.
- Final relevant regression: 440 project tests, OK in 93.095 seconds, with
  ResourceWarning promoted to errors
  (`.cache/roadmap/m2-vm-usage-regression.log`). Whole-CI Ruff, compileall and
  whitespace checks passed; no unrelated full repository test run was needed.
- Nix package/import checks passed on aarch64-darwin
  (`.cache/roadmap/m2-vm-usage-nix.log`); other architectures were omitted, not
  tested. Byte comparisons verify the packaged host/guest accounting modules
  and VM backend match the tested sources.
- M2 remains incomplete and production-disabled: native full lifecycle,
  interruption/power-loss and large-transfer acceptance, desktop soft quotas
  and remaining task/product integration are still required. M3-M7 are pending.
  No production SSH, shared nix-config edit, rebuild or App release occurred.
  The user-authored plan remains unchanged and excluded from this checkpoint.
