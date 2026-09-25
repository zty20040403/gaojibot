from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from src.bot_storage import DatabaseError, PostgresDatabase
from src.bot_storage.schema import HEAD_REVISION

from .alert_history import AlertEventStore
from .alert_notifier import AlertNotificationPreferences
from .bridges import (
    BlueBubblesClient,
    BridgeManager,
    MatrixClient,
    MirrorRouter,
    MirrorStateStore,
)
from .browser_tools import BrowserManager, RichMessageRenderer
from .cold_archive import ColdArchiveService
from .config import Settings
from .content_sources import ContentSourceStore
from .context_store import CaptureCandidate, ContextStore
from .context_pipeline import TopicGraphStore
from .delivery import DeliveryStore
from .fleet_client import FleetControlClient
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
from .ocr import RecentImageStore
from .pins import PinStore
from .quota import UsageStore
from .reminders import ReminderStore
from .sandbox import DockerSandboxManager
from .vm_sandbox import VmSandboxManager
from .self_source import SelfSource
from .semantic_recall import (
    EmbeddingClient,
    PgVectorBackend,
    SemanticIndexState,
    SemanticRecallService,
)
from .skills import SkillRegistry
from .stickers import configure_learned_sticker_state
from .tasks import RunningTaskRegistry
from .storage.jobs import DurableJobStore
from .subagents import (
    SubAgentCoordinator,
    SubAgentStore,
    parse_profile_overrides,
)
from .turn_journal import TurnJournal
from .voice import RecentVoiceStore
from .video import RecentVideoStore
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
    alert_preferences: AlertNotificationPreferences
    model_catalog: ModelCatalog
    llm_gateway: LLMGateway = field(repr=False)
    self_source: SelfSource
    skill_registry: SkillRegistry
    recent_images: RecentImageStore
    recent_voices: RecentVoiceStore
    recent_videos: RecentVideoStore
    sandbox_manager: DockerSandboxManager | VmSandboxManager
    bridge_router: MirrorRouter
    local_model: LocalModelRuntime | None = field(default=None, repr=False)
    message_ledger: MessageLedger | None = None
    context_store: ContextStore | None = None
    topic_graph_store: TopicGraphStore | None = None
    pin_store: PinStore | None = None
    reminder_store: ReminderStore | None = None
    delivery_store: DeliveryStore | None = None
    job_store: DurableJobStore | None = None
    job_worker: DurableJobWorker | None = None
    subagent_store: SubAgentStore | None = None
    subagent_coordinator: SubAgentCoordinator | None = None
    mirror_state: MirrorStateStore | None = None
    bridge_manager: BridgeManager | None = None
    usage_store: UsageStore | None = None
    semantic_recall: SemanticRecallService | None = None
    semantic_index_state: SemanticIndexState | None = None
    maintenance_state: MaintenanceState | None = None
    historian_service: HistorianService | None = None
    dream_service: DreamService | None = None
    turn_journal: TurnJournal | None = None
    browser_manager: BrowserManager | None = None
    rich_renderer: RichMessageRenderer | None = None
    media_library: MediaLibrary | None = None
    source_store: ContentSourceStore | None = None
    media_cleanup: LegacyMediaCleanup | None = None
    vision_worker: VisionWorker | None = None
    cold_archive: ColdArchiveService | None = None
    alert_store: AlertEventStore | None = None
    fleet_client: FleetControlClient | None = None
    mobile_authorization: MobileAuthorization | None = None
    _closed: bool = field(default=False, init=False, repr=False)

    async def shutdown(self) -> None:
        if self._closed:
            return

        stopped = await self.background_tasks.stop_all()
        if stopped:
            self.logger.info(f"Stopped {stopped} background task(s).")

        cancelled = self.running_tasks.cancel_all()
        if cancelled:
            self.logger.info(
                f"Cancelled {cancelled} running AI task(s) during shutdown."
            )

        for name, resource in (
            ("LLM gateway", self.llm_gateway),
            ("local model health", self.local_model),
            ("bridge manager", self.bridge_manager),
            ("browser manager", self.browser_manager),
            ("rich renderer", self.rich_renderer),
            ("media library", self.media_library),
            ("vision worker", self.vision_worker),
            ("cold archive", self.cold_archive),
            ("fleet control client", self.fleet_client),
        ):
            if resource is None:
                continue
            try:
                await resource.close()
            except Exception as exc:
                self.logger.error(f"Could not close {name}: {exc}")

        for name, resource in (
            ("mirror state", self.mirror_state),
            ("message ledger", self.message_ledger),
            ("context store", self.context_store),
            ("topic graph store", self.topic_graph_store),
            ("pin store", self.pin_store),
            ("reminder store", self.reminder_store),
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
) -> AppContext:
    state_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = cache_dir or state_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    database: PostgresDatabase | None = None
    if settings.postgres_dsn:
        database = PostgresDatabase(
            settings.postgres_dsn,
            schema=settings.postgres_schema,
            min_size=settings.postgres_pool_min_size,
            max_size=max(
                settings.postgres_pool_max_size,
                settings.postgres_pool_min_size,
            ),
            timeout_seconds=settings.postgres_pool_timeout_seconds,
            health_check_interval_seconds=(
                settings.postgres_health_check_interval_seconds
            ),
            node_names=settings.postgres_node_names,
        )
        database.require_revision(HEAD_REVISION)
        logger.info(
            f"PostgreSQL storage ready in schema {settings.postgres_schema}."
        )
    elif not settings.legacy_sqlite_allowed:
        raise RuntimeError(
            "AI_POSTGRES_DSN is required; SQLite fallback is disabled. "
            "Run the migration or explicitly set AI_ALLOW_LEGACY_SQLITE=true "
            "for a temporary rollback."
        )

    def store_source(filename: str):
        return database if database is not None else state_dir / filename

    configure_learned_sticker_state(store_source("learned_stickers.json"))

    memory = ConversationMemory(
        settings.max_context_turns,
        store_source("conversation_history.json"),
    )
    group_context = GroupContextMemory(
        settings.group_context_messages,
        settings.group_context_chars,
        store_source("group_context.json"),
    )
    long_term_memory = LongTermMemoryStore(
        store_source("long_term_memory.json"),
        max_entries_per_scope=settings.memory_max_entries,
        max_content_chars=settings.memory_max_chars,
    )

    running_tasks = RunningTaskRegistry()
    user_profiles = GroupUserProfileStore(store_source("user_profiles.json"))
    model_preferences = ModelPreferenceStore(
        store_source("model_preferences.json")
    )
    reasoning_preferences = ModelPreferenceStore(
        store_source("reasoning_preferences.json"),
        namespace="reasoning_preferences",
    )
    alert_preferences = AlertNotificationPreferences(
        store_source("alert_notification_preferences.json")
    )
    model_catalog = ModelCatalog.from_settings(settings)
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
    local_model = LocalModelRuntime(
        local_profile,
        interval_seconds=settings.local_model_probe_interval_seconds,
        timeout_seconds=settings.local_model_probe_timeout_seconds,
        control_url=settings.qwen_control_url,
        control_token=settings.qwen_control_token,
    ) if local_profile is not None else None
    llm_gateway = LLMGateway(
        local_model=local_model,
        catalog=model_catalog,
        fallback_enabled=settings.model_fallback_enabled,
        failure_threshold=settings.model_circuit_failure_threshold,
        cooldown_seconds=settings.model_circuit_cooldown_seconds,
        long_cooldown_seconds=(
            settings.model_circuit_long_cooldown_seconds
        ),
    )

    message_ledger: MessageLedger | None = None
    if settings.ledger_enabled:
        try:
            message_ledger = MessageLedger(store_source("bot_state.sqlite3"))
        except (OSError, RuntimeError, sqlite3.Error, DatabaseError) as exc:
            logger.error(f"Canonical message ledger could not be opened: {exc}")

    source_store = ContentSourceStore(database) if database is not None else None
    alert_store = AlertEventStore(database) if database is not None else None
    mobile_authorization = build_mobile_authorization(settings, database)
    fleet_client: FleetControlClient | None = None
    if settings.cluster_enabled:
        if not settings.cluster_control_token_file:
            raise RuntimeError(
                "AI_CLUSTER_CONTROL_TOKEN_FILE is required when cluster access is enabled"
            )
        try:
            fleet_client = FleetControlClient(
                settings.cluster_control_url,
                settings.cluster_control_token_file,
                timeout_seconds=settings.cluster_control_timeout_seconds,
                mobile_authorization=mobile_authorization,
            )
        except ValueError as exc:
            raise RuntimeError(f"Fleet control client could not start: {exc}") from exc

    context_store: ContextStore | None = None
    if settings.context_lifecycle_enabled and message_ledger is not None:
        try:
            context_store = ContextStore(
                store_source("context_store.sqlite3"),
                input_budget_tokens=settings.context_input_budget_tokens,
                high_watermark_tokens=settings.context_high_watermark_tokens,
                low_watermark_tokens=settings.context_low_watermark_tokens,
                compartment_target_tokens=(
                    settings.context_compartment_target_tokens
                ),
                raw_tail_min_messages=settings.context_raw_tail_min_messages,
                max_compartments=settings.context_max_compartments,
                historian_managed=settings.historian_enabled,
            )
        except (OSError, RuntimeError, sqlite3.Error, DatabaseError) as exc:
            logger.error(f"Context store could not be opened: {exc}")

    topic_graph_store: TopicGraphStore | None = None
    if message_ledger is not None:
        try:
            topic_graph_store = TopicGraphStore(
                store_source("topic_graph.sqlite3")
            )
        except (OSError, RuntimeError, sqlite3.Error, DatabaseError) as exc:
            logger.error(f"Topic graph store could not be opened: {exc}")

    pin_store: PinStore | None = None
    if message_ledger is not None:
        try:
            pin_store = PinStore(store_source("pins.sqlite3"))
        except (OSError, RuntimeError, sqlite3.Error, DatabaseError) as exc:
            logger.error(f"Pinned message store could not be opened: {exc}")

    reminder_store: ReminderStore | None = None
    if settings.reminders_enabled:
        try:
            reminder_store = ReminderStore(
                store_source("reminders.sqlite3"),
                max_per_scope=settings.reminder_max_per_scope,
            )
        except (OSError, RuntimeError, sqlite3.Error, DatabaseError) as exc:
            logger.error(f"Reminder store could not be opened: {exc}")

    delivery_store: DeliveryStore | None = None
    if settings.outbox_enabled:
        try:
            delivery_store = DeliveryStore(
                store_source("delivery_outbox.sqlite3"),
                max_attempts=settings.outbox_max_attempts,
                lease_seconds=settings.outbox_lease_seconds,
            )
            if delivery_store.recovered_ambiguous:
                logger.warning(
                    f"Parked {delivery_store.recovered_ambiguous} interrupted "
                    "delivery attempt(s) as ambiguous pending echo review."
                )
        except (OSError, RuntimeError, sqlite3.Error, DatabaseError) as exc:
            logger.error(f"Durable delivery outbox could not be opened: {exc}")

    try:
        bridge_router = MirrorRouter.from_json(settings.mirror_routes_json)
    except ValueError as exc:
        bridge_router = MirrorRouter()
        logger.error(f"Cross-platform mirror configuration is invalid: {exc}")

    mirror_state: MirrorStateStore | None = None
    bridge_manager: BridgeManager | None = None
    if bridge_router.bundles:
        if message_ledger is None or delivery_store is None:
            logger.error(
                "Cross-platform mirrors require both the canonical ledger and outbox."
            )
        else:
            try:
                mirror_state = MirrorStateStore(
                    store_source("bridge_state.sqlite3")
                )
                matrix_client: MatrixClient | None = None
                imessage_client: BlueBubblesClient | None = None
                if settings.matrix_enabled:
                    if not (
                        settings.matrix_homeserver
                        and settings.matrix_access_token
                        and settings.matrix_user_id
                    ):
                        logger.error(
                            "Matrix is enabled but homeserver, access token, "
                            "or user id is missing."
                        )
                    else:
                        matrix_client = MatrixClient(
                            settings.matrix_homeserver,
                            settings.matrix_access_token,
                            user_id=settings.matrix_user_id,
                            sync_timeout_ms=settings.matrix_sync_timeout_ms,
                        )
                if settings.imessage_enabled:
                    if not (
                        settings.imessage_base_url
                        and settings.imessage_password
                        and settings.imessage_chat_guid
                    ):
                        logger.error(
                            "iMessage is enabled but BlueBubbles URL, password, "
                            "or chat GUID is missing."
                        )
                    else:
                        imessage_client = BlueBubblesClient(
                            settings.imessage_base_url,
                            settings.imessage_password,
                        )
                bridge_manager = BridgeManager(
                    bridge_router,
                    message_ledger,
                    delivery_store,
                    mirror_state,
                    matrix=matrix_client,
                    imessage=imessage_client,
                )
            except (
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                sqlite3.Error,
                DatabaseError,
            ) as exc:
                if mirror_state is not None:
                    mirror_state.close()
                mirror_state = None
                bridge_manager = None
                logger.error(f"Cross-platform bridge could not be configured: {exc}")

    usage_store: UsageStore | None = None
    if settings.quota_enabled:
        try:
            usage_store = UsageStore(
                store_source("usage.sqlite3"),
                daily_call_limit=settings.quota_daily_calls,
                daily_input_token_limit=settings.quota_daily_input_tokens,
                daily_output_token_limit=settings.quota_daily_output_tokens,
            )
        except (OSError, RuntimeError, sqlite3.Error, DatabaseError) as exc:
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
                semantic_index_state = SemanticIndexState(
                    store_source("semantic_index_state.sqlite3")
                )
            except (
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                sqlite3.Error,
                DatabaseError,
            ) as exc:
                semantic_recall = None
                semantic_index_state = None
                logger.error(f"Semantic recall could not be configured: {exc}")

    maintenance_state: MaintenanceState | None = None
    if settings.historian_enabled or settings.dream_enabled:
        try:
            maintenance_state = MaintenanceState(
                store_source("maintenance_state.sqlite3")
            )
        except (OSError, RuntimeError, sqlite3.Error, DatabaseError) as exc:
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
            protected_provider=(
                lambda scope: (
                    pin_store.protected_message_ids(scope)
                    if pin_store is not None
                    else ()
                )
            ),
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
            turn_journal = TurnJournal(
                store_source("turn_journal.sqlite3"),
                archive_ttl_days=settings.turn_archive_ttl_days,
                archive_max_per_scope=settings.turn_archive_max_per_scope,
                archive_max_bytes=settings.turn_archive_max_bytes,
                event_max_chars=settings.turn_event_max_chars,
            )
        except (OSError, RuntimeError, sqlite3.Error, DatabaseError) as exc:
            logger.error(f"Turn journal could not be opened: {exc}")

    browser_manager: BrowserManager | None = None
    if settings.browser_enabled:
        browser_manager = BrowserManager(
            state_dir / "browser_profiles",
            timeout_seconds=settings.browser_timeout_seconds,
            max_sessions=settings.browser_max_sessions,
            idle_seconds=settings.browser_idle_seconds,
            executable_path=settings.browser_executable_path,
            allow_private_network=settings.browser_allow_private_network,
        )

    rich_renderer: RichMessageRenderer | None = None
    if settings.rich_render_enabled:
        rich_renderer = RichMessageRenderer(
            executable_path=settings.browser_executable_path,
            timeout_seconds=settings.browser_timeout_seconds,
            codesnap_enabled=settings.codesnap_enabled,
            codesnap_executable_path=settings.codesnap_executable_path,
            codesnap_config_path=settings.codesnap_config_path,
            codesnap_font_family=settings.codesnap_font_family,
            codesnap_theme=settings.codesnap_theme,
            codesnap_timeout_seconds=settings.codesnap_timeout_seconds,
            codesnap_cache_root=cache_dir / "codesnap",
            codesnap_cache_entries=settings.codesnap_cache_entries,
        )

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
            media_library = MediaLibrary(
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
            )
            vision_worker = VisionWorker(
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
            )
            media_cleanup = LegacyMediaCleanup(
                database,
                media_root=media_root,
                archive_root=archive_root,
            )
        except (OSError, RuntimeError, TypeError, ValueError, DatabaseError) as exc:
            raise RuntimeError(f"Durable media library could not start: {exc}") from exc

    if settings.archive_enabled:
        if database is None:
            raise RuntimeError("The automatic cold archive requires PostgreSQL.")
        if media_library is None:
            raise RuntimeError("The automatic cold archive requires the media library.")
        if archive_root is None:
            raise RuntimeError("AI_ARCHIVE_ROOT is required when archiving is enabled.")
        cold_archive = ColdArchiveService(
            database,
            media_root=media_library.root,
            archive_root=archive_root,
            media_retention_days=settings.archive_media_retention_days,
            delivery_retention_days=settings.archive_delivery_retention_days,
            delivery_min_bytes=settings.archive_delivery_min_bytes,
            interval_seconds=settings.archive_interval_seconds,
            batch_size=settings.archive_batch_size,
            warning=logger.warning,
        )

    if settings.sandbox_backend == "vm":
        if not settings.sandbox_vm_image or not settings.sandbox_vm_root:
            raise RuntimeError("VM sandbox requires AI_SANDBOX_VM_IMAGE and AI_SANDBOX_VM_ROOT")
        sandbox_manager = VmSandboxManager(
            image=settings.sandbox_vm_image,
            root=settings.sandbox_vm_root,
            max_per_owner=settings.sandbox_max_per_user,
            max_total=settings.sandbox_max_total,
            default_timeout_seconds=settings.sandbox_timeout_seconds,
            max_file_bytes=settings.sandbox_max_file_bytes,
        )
    elif settings.sandbox_backend == "oci":
        sandbox_manager = DockerSandboxManager(
            image=settings.sandbox_image,
            max_per_owner=settings.sandbox_max_per_user,
            max_total=settings.sandbox_max_total,
            default_timeout_seconds=settings.sandbox_timeout_seconds,
            max_file_bytes=settings.sandbox_max_file_bytes,
            nix_cache_volume=settings.sandbox_nix_cache_volume,
        )
    else:
        raise RuntimeError(f"Unsupported sandbox backend: {settings.sandbox_backend}")

    job_store: DurableJobStore | None = None
    job_worker: DurableJobWorker | None = None
    if settings.durable_jobs_enabled:
        try:
            job_store = DurableJobStore(
                store_source("durable_jobs.sqlite3"),
                lease_seconds=settings.durable_job_lease_seconds,
                default_max_attempts=settings.durable_job_max_attempts,
            )
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

            async def execute_sandbox_job(job):
                payload = job.payload
                result = await sandbox_manager.exec(
                    str(payload.get("owner") or ""),
                    str(payload.get("sandbox_id") or ""),
                    str(payload.get("command") or ""),
                    int(payload.get("timeout_seconds") or 300),
                    packages=[
                        str(item)
                        for item in (payload.get("packages") or [])
                        if isinstance(item, str)
                    ],
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        f"sandbox command exited with {result.returncode}: "
                        f"{result.stderr[-1000:]}"
                    )
                return {
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "observed_manifest": (
                        asdict(result.manifest)
                        if result.manifest is not None
                        else None
                    ),
                    "sandbox_id": str(payload.get("sandbox_id") or ""),
                }

            async def compensate_sandbox_job(job, _reason):
                payload = job.payload
                await sandbox_manager.destroy(
                    str(payload.get("owner") or ""),
                    str(payload.get("sandbox_id") or ""),
                )

            job_worker.register(
                "agent.sandbox_exec",
                execute_sandbox_job,
                timeout_seconds=330,
                compensator=compensate_sandbox_job,
            )
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
        except (OSError, RuntimeError, sqlite3.Error, DatabaseError) as exc:
            if job_store is not None:
                job_store.close()
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
            subagent_store = SubAgentStore(store_source("subagents.sqlite3"))
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
        except (OSError, RuntimeError, ValueError, sqlite3.Error, DatabaseError) as exc:
            if subagent_store is not None:
                subagent_store.close()
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
        alert_preferences=alert_preferences,
        model_catalog=model_catalog,
        llm_gateway=llm_gateway,
        local_model=local_model,
        self_source=SelfSource(project_root),
        skill_registry=SkillRegistry(project_root / "skills"),
        recent_images=RecentImageStore(settings.ocr_recent_image_seconds),
        recent_voices=RecentVoiceStore(settings.voice_recent_seconds),
        recent_videos=RecentVideoStore(settings.video_recent_seconds),
        sandbox_manager=sandbox_manager,
        bridge_router=bridge_router,
        message_ledger=message_ledger,
        context_store=context_store,
        topic_graph_store=topic_graph_store,
        pin_store=pin_store,
        reminder_store=reminder_store,
        delivery_store=delivery_store,
        job_store=job_store,
        job_worker=job_worker,
        subagent_store=subagent_store,
        subagent_coordinator=subagent_coordinator,
        mirror_state=mirror_state,
        bridge_manager=bridge_manager,
        usage_store=usage_store,
        semantic_recall=semantic_recall,
        semantic_index_state=semantic_index_state,
        maintenance_state=maintenance_state,
        historian_service=historian_service,
        dream_service=dream_service,
        turn_journal=turn_journal,
        browser_manager=browser_manager,
        rich_renderer=rich_renderer,
        media_library=media_library,
        source_store=source_store,
        media_cleanup=media_cleanup,
        vision_worker=vision_worker,
        cold_archive=cold_archive,
        alert_store=alert_store,
        fleet_client=fleet_client,
        mobile_authorization=mobile_authorization,
    )
