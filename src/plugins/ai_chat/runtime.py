from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .adapters.qq_official.runtime import OfficialRuntime

from src.bot_storage import DatabaseCalls, DatabaseError, PostgresDatabase
from src.bot_storage.schema import HEAD_REVISION

from .application_construction import ConstructionResources
from .cold_archive import ColdArchiveService
from .config import Settings
from .content_sources import ContentSourceStore
from .context_store import CaptureCandidate, ContextStore
from .context_pipeline import TopicGraphStore
from .delivery import DeliveryStore
from .historian import (
    DreamOperation,
    DreamService,
    HistorianResult,
    HistorianService,
    MaintenanceState,
)
from .identity import GroupUserProfileStore
from .ledger import MessageLedger
from .lifecycle import BackgroundTaskSupervisor
from .long_term_memory import LongTermMemoryStore, MemoryEntry
from .llm_gateway import LLMGateway
from .local_model import LocalModelRuntime
from .memory import ConversationMemory, GroupContextMemory
from .media_library import MediaLibrary
from src.bot_storage.media_cleanup import LegacyMediaCleanup
from .model_catalog import ModelCatalog
from .model_preferences import ModelPreferenceStore
from .mobile_authorization import build_mobile_authorization
from src.bot_security.service import MobileAuthorization
from .proactive_learning import ProactiveLearningStore
from .quota import UsageStore
from .sandbox import DockerSandboxManager
from src.sandbox_runtime.factory import manager_from_settings
from .vm_sandbox import VmSandboxManager
from .self_source import SelfSource
from .semantic_recall import (
    EmbeddingClient,
    PgVectorBackend,
    SemanticIndexState,
    SemanticRecallService,
)
from .skills import SkillRegistry
from .tasks import RunningTaskRegistry
from .storage.jobs import DurableJobStore
from .subagents import (
    SubAgentCoordinator,
    SubAgentStore,
    parse_profile_overrides,
)
from .turn_journal import TurnJournal
from .vision_worker import VisionWorker
from .workers.durable_jobs import DurableJobWorker


class RuntimeLogger(Protocol):
    def debug(self, message: object, *args: object, **kwargs: object) -> object: ...

    def error(self, message: object, *args: object, **kwargs: object) -> object: ...

    def exception(self, message: object, *args: object, **kwargs: object) -> object: ...

    def warning(self, message: object, *args: object, **kwargs: object) -> object: ...

    def info(self, message: object, *args: object, **kwargs: object) -> object: ...


HistorianGenerator = Callable[[CaptureCandidate], Awaitable[HistorianResult]]
DreamGenerator = Callable[
    [str, Sequence[MemoryEntry], str],
    Awaitable[list[DreamOperation]],
]
EvidenceProvider = Callable[[MemoryEntry], str]


@dataclass
class AppContext:
    settings: Settings
    state_dir: Path
    project_root: Path
    started_at: int
    logger: RuntimeLogger = field(repr=False)
    background_tasks: BackgroundTaskSupervisor = field(repr=False)
    database: PostgresDatabase | None = field(repr=False)
    memory: ConversationMemory
    group_context: GroupContextMemory
    long_term_memory: LongTermMemoryStore
    running_tasks: RunningTaskRegistry
    user_profiles: GroupUserProfileStore
    model_preferences: ModelPreferenceStore
    reasoning_preferences: ModelPreferenceStore
    model_catalog: ModelCatalog
    llm_gateway: LLMGateway = field(repr=False)
    self_source: SelfSource
    skill_registry: SkillRegistry
    sandbox_manager: DockerSandboxManager | VmSandboxManager
    local_model: LocalModelRuntime | None = field(default=None, repr=False)
    message_ledger: MessageLedger | None = None
    context_store: ContextStore | None = None
    topic_graph_store: TopicGraphStore | None = None
    proactive_learning: ProactiveLearningStore | None = None
    delivery_store: DeliveryStore | None = None
    job_store: DurableJobStore | None = None
    job_worker: DurableJobWorker | None = None
    subagent_store: SubAgentStore | None = None
    subagent_coordinator: SubAgentCoordinator | None = None
    usage_store: UsageStore | None = None
    semantic_recall: SemanticRecallService | None = None
    semantic_index_state: SemanticIndexState | None = None
    maintenance_state: MaintenanceState | None = None
    historian_service: HistorianService | None = None
    dream_service: DreamService | None = None
    turn_journal: TurnJournal | None = None
    media_library: MediaLibrary | None = None
    source_store: ContentSourceStore | None = None
    media_cleanup: LegacyMediaCleanup | None = None
    vision_worker: VisionWorker | None = None
    cold_archive: ColdArchiveService | None = None
    mobile_authorization: MobileAuthorization | None = None
    official_runtime: OfficialRuntime | None = field(default=None, repr=False)
    db_call: DatabaseCalls = field(default_factory=DatabaseCalls, repr=False)
    migration_service: Any = field(default=None, repr=False)
    official_host: Any = field(default=None, repr=False)
    official_observability: Any = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _closing: bool = field(default=False, init=False, repr=False)

    async def start_official_ingress(self, env: Mapping[str, str] | None = None) -> bool:
        """Application startup hook independent of any OneBot connection."""
        if self._closed or self._closing:
            raise RuntimeError("Application context is closed")
        if self.official_runtime is not None:
            return False
        import os
        from .adapters.qq_official.config import OfficialConfig

        config = OfficialConfig.from_env(os.environ if env is None else env)
        if not config.enabled:
            return False
        if self.database is None:
            raise RuntimeError("Official ingress requires durable storage")
        from .adapters.qq_official.runtime import OfficialRuntime

        factories = {}
        host = getattr(self, 'official_host', None)
        migration = getattr(self, 'migration_service', None)
        if host is not None:
            factories.update(migration_factory=host.migration_factory,
                file_services_factory=host.files_factory, approval_factory=host.approval_factory,
                media_factory=host.media_factory,
                background_factory=getattr(host,"background_factory",None))
        elif migration is not None:
            factories['migration_factory'] = migration.official_factory
        runtime = OfficialRuntime(config, self.database, logger=self.logger, ledger=self.message_ledger,
                                  journal=getattr(self, "turn_journal", None), application=self, **factories)
        self.official_runtime = runtime
        try:
            return await runtime.start()
        except BaseException:
            await runtime.stop()
            self.official_runtime = None
            raise

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closing = True
        # Diagnostic output and the public download listener must not prevent
        # the account runtime, leases or shared storage from closing.
        for name in ('official_observability', 'official_host'):
            resource=getattr(self,name,None)
            if resource is not None:
                try:
                    await resource.close()
                except Exception:
                    # Fixed local stage only; paths and raw exception data may
                    # contain private deployment information.
                    try:
                        self.logger.warning(f"Application shutdown cleanup failed: {name}")
                    except Exception:
                        pass
                finally:
                    setattr(self,name,None)

        if self.official_runtime is not None:
            # Stop ingress and drain its transactions before closing shared ledger
            # or database resources, regardless of the OneBot connection state.
            await self.official_runtime.stop()
            self.official_runtime = None

        stopped = await self.background_tasks.stop_all()
        if stopped:
            self.logger.info(f"Stopped {stopped} background task(s).")

        cancelled = await self.running_tasks.cancel_all_and_wait()
        if cancelled:
            self.logger.info(
                f"Cancelled {cancelled} running AI task(s) during shutdown."
            )

        if self.migration_service is not None:
            # All effects/tasks have stopped. Keep baseline locks alive until
            # then; closing the shared DB first would lose historical receipts.
            await self.migration_service.close()
            self.migration_service = None
        self.official_host = None

        await self.db_call.close()

        for name, resource in (
            ("LLM gateway", self.llm_gateway),
            ("local model health", self.local_model),
            ("media library", self.media_library),
            ("vision worker", self.vision_worker),
            ("cold archive", self.cold_archive),
        ):
            if resource is None:
                continue
            try:
                await resource.close()
            except Exception as exc:
                self.logger.error(f"Could not close {name}: {exc}")

        for name, resource in (
            ("message ledger", self.message_ledger),
            ("context store", self.context_store),
            ("topic graph store", self.topic_graph_store),
            ("proactive learning", self.proactive_learning),
            ("delivery store", self.delivery_store),
            ("durable job store", self.job_store),
            ("Sub-Agent task store", self.subagent_store),
            ("usage store", self.usage_store),
            ("semantic index state", self.semantic_index_state),
            ("maintenance state", self.maintenance_state),
            ("turn journal", self.turn_journal),
            ("account authorization", self.mobile_authorization.store if self.mobile_authorization else None),
        ):
            if resource is None:
                continue
            try:
                resource.close()
            except Exception as exc:
                self.logger.error(f"Could not close {name}: {exc}")
        if self.database is not None:
            try:
                self.database.close()
            except Exception as exc:
                self.logger.error(f"Could not close PostgreSQL pool: {exc}")
        self._closed = True


def build_app_context(
    settings: Settings,
    *,
    state_dir: Path,
    cache_dir: Path | None = None,
    project_root: Path,
    logger: RuntimeLogger,
    historian_generator: HistorianGenerator,
    dream_generator: DreamGenerator,
    evidence_provider: EvidenceProvider,
    started_at: int | None = None,
    environment: Mapping[str, str] | None = None,
    _construction: ConstructionResources | None = None,
) -> AppContext:
    """Build shared services; production hosts own asynchronous failure cleanup.

    Direct synchronous callers retain the existing success API. On failure,
    synchronous resources close immediately. They must use the owned async
    wrapper when construction may allocate asynchronous clients, since this
    function cannot await those clients on its caller's running event loop.
    """
    resources = _construction or ConstructionResources(logger)
    try:
        context = _build_app_context(
            settings, state_dir=state_dir, cache_dir=cache_dir,
            project_root=project_root, logger=logger,
            historian_generator=historian_generator,
            dream_generator=dream_generator, evidence_provider=evidence_provider,
            started_at=started_at,
            environment=environment, _construction=resources,
        )
    except BaseException:
        if _construction is None:
            resources.close_sync()
        raise
    if _construction is None:
        resources.transfer()
    return context


def _build_app_context(
    settings: Settings,
    *,
    state_dir: Path,
    cache_dir: Path | None = None,
    project_root: Path,
    logger: RuntimeLogger,
    historian_generator: HistorianGenerator,
    dream_generator: DreamGenerator,
    evidence_provider: EvidenceProvider,
    started_at: int | None = None,
    environment: Mapping[str, str] | None = None,
    _construction: ConstructionResources,
) -> AppContext:
    state_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = cache_dir or state_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    if not settings.postgres_dsn:
        raise RuntimeError(
            "AI_POSTGRES_DSN is required; import legacy data and apply the "
            "Alembic migrations before starting the bot."
        )
    database = _construction.own(PostgresDatabase(
        settings.postgres_dsn,
        schema=settings.postgres_schema,
        min_size=settings.postgres_pool_min_size,
        max_size=max(settings.postgres_pool_max_size, settings.postgres_pool_min_size),
        timeout_seconds=settings.postgres_pool_timeout_seconds,
        health_check_interval_seconds=settings.postgres_health_check_interval_seconds,
        node_names=settings.postgres_node_names,
    ))
    database.require_revision(HEAD_REVISION)
    logger.info(f"PostgreSQL storage ready in schema {settings.postgres_schema}.")

    memory = _construction.own(ConversationMemory(
        settings.max_context_turns,
        database,
    ))
    group_context = _construction.own(GroupContextMemory(
        settings.group_context_messages,
        settings.group_context_chars,
        database,
    ))
    long_term_memory = _construction.own(LongTermMemoryStore(
        database,
        max_entries_per_scope=settings.memory_max_entries,
        max_content_chars=settings.memory_max_chars,
    ))

    running_tasks = RunningTaskRegistry()
    user_profiles = _construction.own(GroupUserProfileStore(database))
    model_preferences = _construction.own(ModelPreferenceStore(database))
    reasoning_preferences = _construction.own(ModelPreferenceStore(
        database,
        namespace="reasoning_preferences",
    ))
    model_catalog = ModelCatalog.from_settings(settings, environ=environment)
    if settings.model_simple_chat_profile:
        try:
            model_catalog.resolve(settings.model_simple_chat_profile)
        except ValueError as exc:
            raise RuntimeError(
                "AI_SIMPLE_CHAT_PROFILE maps to unknown model profile "
                f"{settings.model_simple_chat_profile!r}"
            ) from exc
    for group_id, profile_name in settings.group_model_profiles.items():
        try:
            model_catalog.resolve(profile_name)
        except ValueError as exc:
            raise RuntimeError(
                "AI_GROUP_MODEL_PROFILES_JSON maps QQ group "
                f"{group_id} to unknown model profile {profile_name!r}"
            ) from exc
    if settings.historian_enabled and settings.historian_profile:
        model_catalog.resolve(settings.historian_profile)
    if settings.historian_enabled and not settings.durable_jobs_enabled:
        raise RuntimeError(
            "AI_HISTORIAN_ENABLED requires AI_DURABLE_JOBS_ENABLED=true"
        )
    if settings.dream_enabled and settings.dream_profile:
        model_catalog.resolve(settings.dream_profile)
    local_profile = next((item for item in model_catalog.profiles if item.name == settings.local_model_profile), None)
    local_model = _construction.own(LocalModelRuntime(
        local_profile,
        interval_seconds=settings.local_model_probe_interval_seconds,
        timeout_seconds=settings.local_model_probe_timeout_seconds,
        control_url=settings.qwen_control_url,
        control_token=settings.qwen_control_token,
    )) if local_profile is not None else None
    llm_gateway = _construction.own(LLMGateway(
        local_model=local_model,
        catalog=model_catalog,
        fallback_enabled=settings.model_fallback_enabled,
        failure_threshold=settings.model_circuit_failure_threshold,
        cooldown_seconds=settings.model_circuit_cooldown_seconds,
        long_cooldown_seconds=(
            settings.model_circuit_long_cooldown_seconds
        ),
    ))

    message_ledger: MessageLedger | None = None
    if settings.ledger_enabled:
        try:
            message_ledger = _construction.own(MessageLedger(database))
        except (OSError, RuntimeError, DatabaseError) as exc:
            logger.error(f"Canonical message ledger could not be opened: {exc}")

    source_store = _construction.own(ContentSourceStore(database))
    mobile_authorization = build_mobile_authorization(settings, database, onebot_enabled=False)
    if mobile_authorization is not None:
        _construction.own(mobile_authorization.store)
    context_store: ContextStore | None = None
    if settings.context_lifecycle_enabled and message_ledger is not None:
        try:
            context_store = _construction.own(ContextStore(
                database,
                input_budget_tokens=settings.context_input_budget_tokens,
                high_watermark_tokens=settings.context_high_watermark_tokens,
                low_watermark_tokens=settings.context_low_watermark_tokens,
                compartment_target_tokens=(settings.context_compartment_target_tokens),
                raw_tail_min_messages=settings.context_raw_tail_min_messages,
                max_compartments=settings.context_max_compartments,
                historian_managed=settings.historian_enabled,
            ))
        except (OSError, RuntimeError, DatabaseError) as exc:
            logger.error(f"Context store could not be opened: {exc}")

    topic_graph_store: TopicGraphStore | None = None
    if message_ledger is not None:
        try:
            topic_graph_store = _construction.own(TopicGraphStore(database))
        except (OSError, RuntimeError, DatabaseError) as exc:
            logger.error(f"Topic graph store could not be opened: {exc}")

    delivery_store: DeliveryStore | None = None
    if settings.outbox_enabled:
        try:
            delivery_store = _construction.own(DeliveryStore(
                database,
                max_attempts=settings.outbox_max_attempts,
                lease_seconds=settings.outbox_lease_seconds,
            ))
            if delivery_store.recovered_ambiguous:
                logger.warning(
                    f"Parked {delivery_store.recovered_ambiguous} interrupted "
                    "delivery attempt(s) as ambiguous pending echo review."
                )
        except (OSError, RuntimeError, DatabaseError) as exc:
            logger.error(f"Durable delivery outbox could not be opened: {exc}")

    usage_store: UsageStore | None = None
    if settings.quota_enabled:
        try:
            usage_store = _construction.own(UsageStore(
                database,
                daily_call_limit=settings.quota_daily_calls,
                daily_input_token_limit=settings.quota_daily_input_tokens,
                daily_output_token_limit=settings.quota_daily_output_tokens,
            ))
        except (OSError, RuntimeError, DatabaseError) as exc:
            logger.error(f"Usage and quota store could not be opened: {exc}")

    semantic_recall: SemanticRecallService | None = None
    semantic_index_state: SemanticIndexState | None = None
    if settings.semantic_enabled:
        if not (
            settings.postgres_dsn
            and settings.embedding_api_key
            and settings.embedding_model
        ):
            logger.error(
                "Semantic recall is enabled but PostgreSQL or embedding settings "
                "are incomplete."
            )
        else:
            try:
                semantic_recall = SemanticRecallService(
                    EmbeddingClient(
                        base_url=settings.embedding_base_url,
                        api_key=settings.embedding_api_key,
                        model=settings.embedding_model,
                        dimensions=settings.embedding_dimensions,
                        timeout_seconds=settings.embedding_timeout_seconds,
                    ),
                    PgVectorBackend(
                        settings.postgres_dsn,
                        dimensions=settings.embedding_dimensions,
                        schema=settings.postgres_schema,
                    ),
                )
                semantic_index_state = _construction.own(SemanticIndexState(database))
            except (
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                DatabaseError,
            ) as exc:
                semantic_recall = None
                semantic_index_state = None
                logger.error(f"Semantic recall could not be configured: {exc}")

    maintenance_state: MaintenanceState | None = None
    if settings.historian_enabled or settings.dream_enabled:
        try:
            maintenance_state = _construction.own(MaintenanceState(database))
        except (OSError, RuntimeError, DatabaseError) as exc:
            logger.error(f"Background maintenance state could not be opened: {exc}")

    historian_service: HistorianService | None = None
    if (
        settings.historian_enabled
        and message_ledger is not None
        and context_store is not None
    ):
        historian_service = HistorianService(
            message_ledger,
            context_store,
            long_term_memory,
            historian_generator,
        )

    dream_service: DreamService | None = None
    if settings.dream_enabled:
        dream_service = DreamService(
            long_term_memory,
            dream_generator,
            evidence_provider=evidence_provider,
            min_entries=settings.dream_min_entries,
        )

    turn_journal: TurnJournal | None = None
    if settings.turn_journal_enabled and message_ledger is not None:
        try:
            turn_journal = _construction.own(TurnJournal(
                database,
                archive_ttl_days=settings.turn_archive_ttl_days,
                archive_max_per_scope=settings.turn_archive_max_per_scope,
                archive_max_bytes=settings.turn_archive_max_bytes,
                event_max_chars=settings.turn_event_max_chars,
            ))
        except (OSError, RuntimeError, DatabaseError) as exc:
            logger.error(f"Turn journal could not be opened: {exc}")

    media_library: MediaLibrary | None = None
    media_cleanup: LegacyMediaCleanup | None = None
    vision_worker: VisionWorker | None = None
    cold_archive: ColdArchiveService | None = None
    archive_root = (
        Path(settings.archive_root).expanduser()
        if settings.archive_enabled and settings.archive_root
        else None
    )
    if settings.media_enabled:
        if database is None:
            raise RuntimeError("The durable media library requires PostgreSQL.")
        media_root = (
            Path(settings.media_root).expanduser()
            if settings.media_root
            else state_dir / "media"
        )
        try:
            media_library = _construction.own(MediaLibrary(
                database,
                root=media_root,
                model_catalog=model_catalog,
                llm_gateway=llm_gateway,
                vision_profile=settings.vision_profile,
                semantic_recall=semantic_recall,
                max_source_bytes=settings.media_max_source_bytes,
                max_vision_bytes=settings.media_max_vision_bytes,
                prepare_threshold_bytes=settings.media_prepare_threshold_bytes,
                max_edge_pixels=settings.media_max_edge_pixels,
                timeout_seconds=settings.media_timeout_seconds,
                max_attempts=settings.media_max_attempts,
                lease_seconds=settings.media_lease_seconds,
                batch_size=settings.media_batch_size,
                worker_concurrency=settings.media_worker_concurrency,
                archive_root=archive_root,
            ))
            vision_worker = _construction.own(VisionWorker(
                database,
                model_catalog=model_catalog,
                llm_gateway=llm_gateway,
                vision_profile=settings.vision_profile,
                delivery_store=delivery_store,
                max_source_bytes=settings.media_max_source_bytes,
                max_vision_bytes=settings.media_max_vision_bytes,
                prepare_threshold_bytes=settings.media_prepare_threshold_bytes,
                max_edge_pixels=settings.media_max_edge_pixels,
                timeout_seconds=settings.media_timeout_seconds,
                max_attempts=settings.media_max_attempts,
                lease_seconds=settings.media_lease_seconds,
                batch_size=settings.media_batch_size,
                worker_concurrency=settings.media_worker_concurrency,
                cache_seconds=settings.vision_cache_seconds,
                cache_entries=settings.vision_cache_entries,
            ))
            media_cleanup = _construction.own(LegacyMediaCleanup(
                database,
                media_root=media_root,
                archive_root=archive_root,
            ))
        except (OSError, RuntimeError, TypeError, ValueError, DatabaseError) as exc:
            raise RuntimeError(f"Durable media library could not start: {exc}") from exc

    if settings.archive_enabled:
        if database is None:
            raise RuntimeError("The automatic cold archive requires PostgreSQL.")
        if media_library is None:
            raise RuntimeError("The automatic cold archive requires the media library.")
        if archive_root is None:
            raise RuntimeError("AI_ARCHIVE_ROOT is required when archiving is enabled.")
        cold_archive = _construction.own(ColdArchiveService(
            database,
            media_root=media_library.root,
            archive_root=archive_root,
            media_retention_days=settings.archive_media_retention_days,
            delivery_retention_days=settings.archive_delivery_retention_days,
            delivery_min_bytes=settings.archive_delivery_min_bytes,
            interval_seconds=settings.archive_interval_seconds,
            batch_size=settings.archive_batch_size,
            warning=logger.warning,
        ))

    sandbox_manager = _construction.own(manager_from_settings(settings))

    job_store: DurableJobStore | None = None
    job_worker: DurableJobWorker | None = None
    if settings.durable_jobs_enabled:
        try:
            job_store = _construction.own(DurableJobStore(
                database,
                lease_seconds=settings.durable_job_lease_seconds,
                default_max_attempts=settings.durable_job_max_attempts,
            ))
            job_worker = DurableJobWorker(
                job_store,
                logger=logger,
                poll_seconds=settings.durable_job_poll_seconds,
                concurrency=settings.durable_job_concurrency,
            )
            if media_library is not None:
                async def index_stickers(_job):
                    queued = await asyncio.to_thread(
                        media_library.enqueue_sticker_embeddings
                    )
                    return {"queued": queued}

                job_worker.register("media.index_stickers", index_stickers)

            if historian_service is not None:
                job_worker.register(
                    "context.historian_capture",
                    historian_service.handle_job,
                    timeout_seconds=240,
                )
            if job_store.recovered_jobs:
                logger.warning(
                    f"Recovered {job_store.recovered_jobs} expired durable job lease(s)."
                )
        except (OSError, RuntimeError, DatabaseError) as exc:
            if job_store is not None:
                _construction.close_one(job_store)
            job_store = None
            job_worker = None
            logger.error(f"Durable application job queue could not be opened: {exc}")

    subagent_store: SubAgentStore | None = None
    subagent_coordinator: SubAgentCoordinator | None = None
    if settings.subagents_enabled:
        try:
            profile_overrides = parse_profile_overrides(
                settings.subagent_profiles_json
            )
            for profile_name in profile_overrides.values():
                model_catalog.resolve(profile_name)
            subagent_store = _construction.own(SubAgentStore(database))
            subagent_coordinator = SubAgentCoordinator(
                subagent_store,
                model_catalog,
                logger=logger,
                max_steps=settings.subagent_max_steps,
                max_parallelism=settings.subagent_max_parallelism,
                max_tool_rounds=settings.subagent_max_tool_rounds,
                timeout_seconds=settings.subagent_timeout_seconds,
                profile_overrides=profile_overrides,
            )
            if subagent_store.recovered_tasks:
                logger.warning(
                    "Preserved %s interrupted Sub-Agent task(s) for checkpoint resume.",
                    subagent_store.recovered_tasks,
                )
        except (OSError, RuntimeError, ValueError, DatabaseError) as exc:
            if subagent_store is not None:
                _construction.close_one(subagent_store)
            subagent_store = None
            subagent_coordinator = None
            logger.error(f"Sub-Agent task system could not be opened: {exc}")

    return AppContext(
        settings=settings,
        state_dir=state_dir,
        project_root=project_root,
        started_at=int(time.time()) if started_at is None else started_at,
        logger=logger,
        background_tasks=BackgroundTaskSupervisor(logger),
        database=database,
        memory=memory,
        group_context=group_context,
        long_term_memory=long_term_memory,
        running_tasks=running_tasks,
        user_profiles=user_profiles,
        model_preferences=model_preferences,
        reasoning_preferences=reasoning_preferences,
        model_catalog=model_catalog,
        llm_gateway=llm_gateway,
        local_model=local_model,
        self_source=SelfSource(project_root),
        skill_registry=SkillRegistry(project_root / "skills"),
        sandbox_manager=sandbox_manager,
        message_ledger=message_ledger,
        context_store=context_store,
        topic_graph_store=topic_graph_store,
        proactive_learning=(
            _construction.own(ProactiveLearningStore(database, feedback_seconds=settings.proactive_feedback_seconds))
            if settings.proactive_adaptive else None
        ),
        delivery_store=delivery_store,
        job_store=job_store,
        job_worker=job_worker,
        subagent_store=subagent_store,
        subagent_coordinator=subagent_coordinator,
        usage_store=usage_store,
        semantic_recall=semantic_recall,
        semantic_index_state=semantic_index_state,
        maintenance_state=maintenance_state,
        historian_service=historian_service,
        dream_service=dream_service,
        turn_journal=turn_journal,
        media_library=media_library,
        source_store=source_store,
        media_cleanup=media_cleanup,
        vision_worker=vision_worker,
        cold_archive=cold_archive,
        mobile_authorization=mobile_authorization,
    )
