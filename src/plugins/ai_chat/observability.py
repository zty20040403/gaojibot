from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any

from fastapi.responses import Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from src.bot_storage import DatabaseCalls


PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
TURN_BUCKETS = (0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120, 300, 900, 1800)
STAGE_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120, 300)
LOOP_LAG_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30)
_logger = logging.getLogger("ai_chat.observability")


class BotTelemetry:
    """Low-cardinality metrics and optional OTLP traces for the bot runtime."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.turns = Counter(
            "gaoji_ai_turns_total",
            "AI turns handled by the bot.",
            ("platform", "kind", "status"),
            registry=self.registry,
        )
        self.turn_duration = Histogram(
            "gaoji_ai_turn_duration_seconds",
            "End-to-end AI turn duration before final delivery.",
            ("platform", "kind", "status"),
            buckets=TURN_BUCKETS,
            registry=self.registry,
        )
        self.stage_duration = Histogram(
            "gaoji_stage_duration_seconds",
            "Duration of bounded processing stages.",
            ("stage", "status"),
            buckets=STAGE_BUCKETS,
            registry=self.registry,
        )
        self.model_requests = Counter(
            "gaoji_model_requests_total",
            "Requests attempted against model profiles.",
            ("profile", "provider", "status"),
            registry=self.registry,
        )
        self.model_duration = Histogram(
            "gaoji_model_request_duration_seconds",
            "Model request latency, including provider transport.",
            ("profile", "provider", "status"),
            buckets=TURN_BUCKETS,
            registry=self.registry,
        )
        self.model_fallbacks = Counter(
            "gaoji_model_fallbacks_total",
            "Successful model requests routed to a fallback profile.",
            ("requested_profile", "actual_profile"),
            registry=self.registry,
        )
        self.tool_calls = Counter(
            "gaoji_tool_calls_total",
            "Tool calls completed by the Agent loop.",
            ("tool", "status"),
            registry=self.registry,
        )
        self.tool_duration = Histogram(
            "gaoji_tool_call_duration_seconds",
            "Tool execution latency.",
            ("tool", "status"),
            buckets=TURN_BUCKETS,
            registry=self.registry,
        )
        self.deliveries = Counter(
            "gaoji_deliveries_total",
            "Outbound delivery attempts.",
            ("platform", "status"),
            registry=self.registry,
        )
        self.delivery_duration = Histogram(
            "gaoji_delivery_duration_seconds",
            "Outbound platform delivery latency.",
            ("platform", "status"),
            buckets=STAGE_BUCKETS,
            registry=self.registry,
        )
        self.tokens = Counter(
            "gaoji_model_tokens_total",
            "Tokens reported by model providers.",
            ("profile", "direction"),
            registry=self.registry,
        )
        self.cached_tokens = Counter(
            "gaoji_model_cached_tokens_total",
            "Input tokens served from provider prompt caches (subset of input tokens).",
            ("profile",),
            registry=self.registry,
        )
        self.runtime_tasks = Gauge(
            "gaoji_runtime_tasks",
            "Currently active in-process AI tasks.",
            registry=self.registry,
        )
        self.event_loop_lag = Gauge(
            "gaoji_event_loop_lag_seconds",
            "Latest event loop scheduling delay; sampled once per second.",
            registry=self.registry,
        )
        self.event_loop_lag_distribution = Histogram(
            "gaoji_event_loop_lag_observed_seconds",
            "Event loop scheduling delays, for rate-based percentile queries.",
            buckets=LOOP_LAG_BUCKETS,
            registry=self.registry,
        )
        self.outbox = Gauge(
            "gaoji_outbox_deliveries",
            "Current durable outbox deliveries by status.",
            ("status",),
            registry=self.registry,
        )
        self.project_bytes = Gauge("gaoji_project_bytes", "Last measured workspace and retained-version bytes.", ("kind",), registry=self.registry)
        self.projects = Gauge("gaoji_projects", "Retained project counts by state.", ("state",), registry=self.registry)
        self.project_usage_unknown = Gauge("gaoji_project_usage_unknown", "Projects without a current verified usage sample.", registry=self.registry)
        self.project_rebuildable_unknown = Gauge("gaoji_project_rebuildable_unknown", "Projects without a current rebuildable-bytes estimate.", registry=self.registry)
        self.project_quota_rejections = Counter("gaoji_project_quota_rejections_total", "Project capacity admissions rejected.", registry=self.registry)
        self._configured = False
        self._tracer = trace.get_tracer("gaoji")

    def update_project_gauges(self, value):
        for kind in ("workspace", "rebuildable", "versions"):
            self.project_bytes.labels(kind=kind).set(value["bytes"].get(kind, 0))
        for state in ("active", "stopped", "hibernated", "archived", "trash"):
            self.projects.labels(state=state).set(value["states"].get(state, 0))
        self.project_usage_unknown.set(value["unknown"])
        self.project_rebuildable_unknown.set(value["rebuildable_unknown"])

    async def monitor_event_loop(self, interval_seconds: float = 1.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("event loop sampling interval must be positive")
        loop = asyncio.get_running_loop()
        while True:
            expected = loop.time() + interval_seconds
            await asyncio.sleep(interval_seconds)
            lag = max(loop.time() - expected, 0.0)
            self.event_loop_lag.set(lag)
            self.event_loop_lag_distribution.observe(lag)

    def configure(
        self,
        service_name: str,
        *,
        service_version: str = "unknown",
        otlp_endpoint: str = "",
    ) -> None:
        if self._configured:
            return
        resource = Resource.create(
            {
                "service.name": service_name or "gaoji",
                "service.version": service_version,
                "deployment.environment": os.getenv("ENVIRONMENT", "prod"),
            }
        )
        provider = TracerProvider(resource=resource)
        endpoint = otlp_endpoint.strip()
        if endpoint:
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
            )
        try:
            trace.set_tracer_provider(provider)
        except Exception:
            # Another host integration may already own the global provider.
            pass
        self._tracer = trace.get_tracer("gaoji")
        self._configured = True

    @contextmanager
    def span(
        self,
        name: str,
        *,
        attributes: dict[str, str | int | float | bool] | None = None,
    ) -> Iterator[str]:
        with self._tracer.start_as_current_span(
            name,
            attributes=attributes or {},
        ) as span:
            context = span.get_span_context()
            trace_id = (
                f"{context.trace_id:032x}"
                if context.is_valid
                else _new_local_trace_id()
            )
            token = _trace_id.set(trace_id)
            try:
                yield trace_id
            except BaseException as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                _trace_id.reset(token)

    @contextmanager
    def stage(self, stage: str) -> Iterator[None]:
        started = time.monotonic()
        status = "succeeded"
        try:
            with self._tracer.start_as_current_span(f"gaoji.{stage}"):
                yield
        except BaseException:
            status = "failed"
            raise
        finally:
            self.stage_duration.labels(stage=stage, status=status).observe(
                time.monotonic() - started
            )

    @asynccontextmanager
    async def tool(self, name: str) -> AsyncIterator[None]:
        started = time.monotonic()
        status = "succeeded"
        try:
            with self._tracer.start_as_current_span(
                "gaoji.tool",
                attributes={"tool.name": name},
            ):
                yield
        except BaseException:
            status = "failed"
            raise
        finally:
            self.tool_calls.labels(tool=name, status=status).inc()
            self.tool_duration.labels(tool=name, status=status).observe(
                time.monotonic() - started
            )

    def observe_model(
        self,
        *,
        requested_profile: str,
        actual_profile: str,
        provider: str,
        status: str,
        duration: float,
    ) -> None:
        self.model_requests.labels(
            profile=actual_profile,
            provider=provider,
            status=status,
        ).inc()
        self.model_duration.labels(
            profile=actual_profile,
            provider=provider,
            status=status,
        ).observe(max(duration, 0.0))
        if status == "succeeded" and actual_profile != requested_profile:
            self.model_fallbacks.labels(
                requested_profile=requested_profile,
                actual_profile=actual_profile,
            ).inc()

    def observe_tokens(self, profile: str, input_tokens: int, output_tokens: int, *, cached_tokens: int = 0) -> None:
        if input_tokens > 0:
            self.tokens.labels(profile=profile, direction="input").inc(input_tokens)
        if output_tokens > 0:
            self.tokens.labels(profile=profile, direction="output").inc(output_tokens)
        cached = min(max(cached_tokens, 0), max(input_tokens, 0))
        if cached:
            self.cached_tokens.labels(profile=profile).inc(cached)

    @contextmanager
    def delivery(self, platform: str) -> Iterator[None]:
        started = time.monotonic()
        status = "committed"
        try:
            with self._tracer.start_as_current_span(
                "gaoji.delivery",
                attributes={"messaging.system": platform},
            ):
                yield
        except BaseException:
            status = "failed"
            raise
        finally:
            self.deliveries.labels(platform=platform, status=status).inc()
            self.delivery_duration.labels(platform=platform, status=status).observe(
                time.monotonic() - started
            )

    def update_runtime_gauges(self, running_tasks: Any, delivery_store: Any) -> None:
        if running_tasks is not None:
            self.runtime_tasks.set(len(running_tasks.list_all()))
        if delivery_store is not None:
            stats = delivery_store.stats()
            for status in (
                "pending",
                "sending",
                "ambiguous",
                "committed",
                "failed",
                "cancelled",
            ):
                self.outbox.labels(status=status).set(int(stats.get(status, 0)))

    def render(self) -> bytes:
        return generate_latest(self.registry)

    def admin_snapshot(
        self,
        *,
        running_tasks: Any = None,
        delivery_store: Any = None,
    ) -> dict[str, object]:
        """Return a credential-free process snapshot for the admin console."""
        self.update_runtime_gauges(running_tasks, delivery_store)
        samples = [
            sample
            for metric in self.registry.collect()
            for sample in metric.samples
        ]

        models = _counter_breakdown(
            samples,
            "gaoji_model_requests_total",
            ("profile", "provider"),
            status_label="status",
        )
        _attach_histogram_percentile(
            models,
            samples,
            "gaoji_model_request_duration_seconds",
            ("profile", "provider"),
        )
        tools = _counter_breakdown(
            samples,
            "gaoji_tool_calls_total",
            ("tool",),
            status_label="status",
        )
        _attach_histogram_percentile(
            tools,
            samples,
            "gaoji_tool_call_duration_seconds",
            ("tool",),
        )
        deliveries = _counter_breakdown(
            samples,
            "gaoji_deliveries_total",
            ("platform",),
            status_label="status",
        )
        _attach_histogram_percentile(
            deliveries,
            samples,
            "gaoji_delivery_duration_seconds",
            ("platform",),
        )
        stages = _histogram_breakdown(
            samples,
            "gaoji_stage_duration_seconds",
            ("stage", "status"),
        )
        turns = _counter_breakdown(
            samples,
            "gaoji_ai_turns_total",
            ("platform", "kind"),
            status_label="status",
        )
        _attach_histogram_percentile(
            turns,
            samples,
            "gaoji_ai_turn_duration_seconds",
            ("platform", "kind"),
        )
        tokens = _plain_counter_breakdown(
            samples,
            "gaoji_model_tokens_total",
            ("profile", "direction"),
        )
        fallbacks = _plain_counter_breakdown(
            samples,
            "gaoji_model_fallbacks_total",
            ("requested_profile", "actual_profile"),
        )
        outbox = {
            str(sample.labels.get("status", "unknown")): int(sample.value)
            for sample in samples
            if sample.name == "gaoji_outbox_deliveries"
        }

        model_requests = sum(int(item["total"]) for item in models)
        model_failures = sum(int(item.get("failed", 0)) for item in models)
        tool_calls = sum(int(item["total"]) for item in tools)
        tool_failures = sum(int(item.get("failed", 0)) for item in tools)
        delivery_attempts = sum(int(item["total"]) for item in deliveries)
        delivery_failures = sum(int(item.get("failed", 0)) for item in deliveries)
        turn_count = sum(int(item["total"]) for item in turns)
        turn_failures = sum(
            int(item.get("failed", 0)) + int(item.get("crashed", 0))
            for item in turns
        )
        input_tokens = sum(
            int(item["value"])
            for item in tokens
            if item.get("direction") == "input"
        )
        output_tokens = sum(
            int(item["value"])
            for item in tokens
            if item.get("direction") == "output"
        )
        cached_tokens = sum(int(sample.value) for sample in samples
                            if sample.name == "gaoji_model_cached_tokens_total")
        return {
            "generated_at": int(time.time()),
            "window": "process",
            "totals": {
                "turns": turn_count,
                "turn_failures": turn_failures,
                "turn_p95_seconds": _overall_histogram_p95(
                    samples,
                    "gaoji_ai_turn_duration_seconds",
                ),
                "model_requests": model_requests,
                "model_failures": model_failures,
                "fallbacks": sum(int(item["value"]) for item in fallbacks),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_tokens": cached_tokens,
                "cache_hit_ratio": cached_tokens / input_tokens
                if input_tokens
                else 0.0,
                "tool_calls": tool_calls,
                "tool_failures": tool_failures,
                "deliveries": delivery_attempts,
                "delivery_failures": delivery_failures,
                "running_tasks": int(_sample_value(samples, "gaoji_runtime_tasks")),
                "event_loop_lag_seconds": _sample_value(
                    samples, "gaoji_event_loop_lag_seconds"
                ),
                "outbox_pending": int(outbox.get("pending", 0)),
                "outbox_ambiguous": int(outbox.get("ambiguous", 0)),
            },
            "models": models,
            "tools": tools,
            "deliveries": deliveries,
            "stages": stages,
            "turn_breakdown": turns,
            "tokens": tokens,
            "fallback_routes": fallbacks,
            "outbox": outbox,
        }


_trace_id: ContextVar[str] = ContextVar("gaoji_trace_id", default="")
telemetry = BotTelemetry()


def current_trace_id() -> str:
    return _trace_id.get()


def register_metrics_endpoint(
    app: Any,
    *,
    path: str,
    service_name: str,
    service_version: str,
    otlp_endpoint: str = "",
    running_tasks: Any = None,
    delivery_store: Any = None,
    db_call: DatabaseCalls | None = None,
) -> None:
    calls = db_call if db_call is not None else DatabaseCalls()
    normalized_path = "/" + path.strip("/")
    telemetry.configure(
        service_name,
        service_version=service_version,
        otlp_endpoint=otlp_endpoint,
    )

    @app.get(normalized_path, include_in_schema=False)
    async def prometheus_metrics() -> Response:
        telemetry.update_runtime_gauges(running_tasks, None)
        if delivery_store is not None:
            await calls(telemetry.update_runtime_gauges, None, delivery_store)
        return Response(
            content=telemetry.render(),
            media_type=PROMETHEUS_CONTENT_TYPE,
            headers={"Cache-Control": "no-store"},
        )


def observed_ai_turn(
    function: Callable[..., Any],
) -> Callable[..., Any]:
    @functools.wraps(function)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        event = args[1] if len(args) > 1 else kwargs.get("event")
        platform = "onebot-v11"
        kind = "group" if hasattr(event, "group_id") else "private"
        started = time.monotonic()
        status = "crashed"
        with telemetry.span(
            "gaoji.ai_turn",
            attributes={
                "messaging.system": platform,
                "messaging.conversation.type": kind,
            },
        ) as trace_id:
            _logger.info(
                "AI turn started trace_id=%s platform=%s kind=%s",
                trace_id,
                platform,
                kind,
            )
            try:
                result = await function(*args, **kwargs)
                status = str(getattr(result, "status", "aborted"))
                return result
            except BaseException:
                status = "crashed"
                raise
            finally:
                telemetry.turns.labels(
                    platform=platform,
                    kind=kind,
                    status=status,
                ).inc()
                telemetry.turn_duration.labels(
                    platform=platform,
                    kind=kind,
                    status=status,
                ).observe(time.monotonic() - started)
                _logger.info(
                    "AI turn finished trace_id=%s platform=%s kind=%s status=%s duration_seconds=%.3f",
                    trace_id,
                    platform,
                    kind,
                    status,
                    time.monotonic() - started,
                )

    return wrapped


def _new_local_trace_id() -> str:
    return os.urandom(16).hex()


def _sample_value(samples: list[Any], name: str) -> float:
    return sum(float(sample.value) for sample in samples if sample.name == name)


def _plain_counter_breakdown(
    samples: list[Any],
    metric_name: str,
    group_labels: tuple[str, ...],
) -> list[dict[str, object]]:
    grouped: dict[tuple[str, ...], float] = defaultdict(float)
    for sample in samples:
        if sample.name != metric_name:
            continue
        key = tuple(str(sample.labels.get(label, "")) for label in group_labels)
        grouped[key] += float(sample.value)
    return [
        {
            **dict(zip(group_labels, key)),
            "value": int(value),
        }
        for key, value in sorted(grouped.items())
    ]


def _counter_breakdown(
    samples: list[Any],
    metric_name: str,
    group_labels: tuple[str, ...],
    *,
    status_label: str,
) -> list[dict[str, object]]:
    grouped: dict[tuple[str, ...], dict[str, int]] = defaultdict(dict)
    for sample in samples:
        if sample.name != metric_name:
            continue
        key = tuple(str(sample.labels.get(label, "")) for label in group_labels)
        status = str(sample.labels.get(status_label, "unknown"))
        grouped[key][status] = grouped[key].get(status, 0) + int(sample.value)
    rows: list[dict[str, object]] = []
    for key, statuses in sorted(grouped.items()):
        rows.append(
            {
                **dict(zip(group_labels, key)),
                **statuses,
                "total": sum(statuses.values()),
                "p95_seconds": None,
            }
        )
    return rows


def _histogram_breakdown(
    samples: list[Any],
    metric_name: str,
    group_labels: tuple[str, ...],
) -> list[dict[str, object]]:
    keys = {
        tuple(str(sample.labels.get(label, "")) for label in group_labels)
        for sample in samples
        if sample.name == f"{metric_name}_count"
    }
    rows = [{**dict(zip(group_labels, key))} for key in sorted(keys)]
    _attach_histogram_percentile(rows, samples, metric_name, group_labels)
    return rows


def _attach_histogram_percentile(
    rows: list[dict[str, object]],
    samples: list[Any],
    metric_name: str,
    group_labels: tuple[str, ...],
) -> None:
    for row in rows:
        labels = {label: str(row.get(label, "")) for label in group_labels}
        count = sum(
            float(sample.value)
            for sample in samples
            if sample.name == f"{metric_name}_count"
            and all(str(sample.labels.get(key, "")) == value for key, value in labels.items())
        )
        total = sum(
            float(sample.value)
            for sample in samples
            if sample.name == f"{metric_name}_sum"
            and all(str(sample.labels.get(key, "")) == value for key, value in labels.items())
        )
        buckets = sorted(
            (
                float(sample.labels.get("le", "+Inf")),
                float(sample.value),
            )
            for sample in samples
            if sample.name == f"{metric_name}_bucket"
            and all(str(sample.labels.get(key, "")) == value for key, value in labels.items())
        )
        threshold = count * 0.95
        p95: float | None = None
        for boundary, cumulative in buckets:
            if cumulative >= threshold and boundary != float("inf"):
                p95 = boundary
                break
        row["count"] = int(count)
        row["average_seconds"] = round(total / count, 4) if count else None
        row["p95_seconds"] = p95


def _overall_histogram_p95(samples: list[Any], metric_name: str) -> float | None:
    count = sum(
        float(sample.value)
        for sample in samples
        if sample.name == f"{metric_name}_count"
    )
    if count <= 0:
        return None
    buckets: dict[float, float] = defaultdict(float)
    for sample in samples:
        if sample.name == f"{metric_name}_bucket":
            buckets[float(sample.labels.get("le", "+Inf"))] += float(sample.value)
    threshold = count * 0.95
    for boundary, cumulative in sorted(buckets.items()):
        if cumulative >= threshold and boundary != float("inf"):
            return boundary
    return None
