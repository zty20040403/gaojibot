"""Scoped wiki projections with evidence checks and fenced publication."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import re
import threading
import time
from typing import Iterator

from src.bot_storage import DatabaseSource, StoreCursor, open_store_connection
from .context_store import CompartmentRecord, ContextStore
from .conversation_scope import ConversationScope
from .delivery import Delivery, DeliveryStore
from .ledger import MessageLedger
from .message_ir import MessageBody, TextNode
from .storage.jobs import DurableJob, DurableJobStore

WIKI_JOB_KIND = "context.group_wiki"
NEWCOMER_JOB_KIND = "context.group_wiki_newcomer"
NEWCOMER_DELIVERY_PREFIX = "wiki-newcomer:"
CATEGORIES = frozenset({"decision", "faq", "jargon", "expertise"})
GENERATION_TOKEN_LIMIT = 65_536
GENERATION_ATTEMPT_LIMIT = 3
_CREDENTIAL = re.compile(
    r"\bsk-[A-Za-z0-9_-]{10,}|Bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"(?:api[_ -]?key|access[_ -]?token|password|secret|密码|验证码)\s*[:=：]\s*\S+",
    re.IGNORECASE,
)


class WikiLeaseLost(RuntimeError):
    pass


class WikiSuperseded(ValueError):
    pass


class WikiBudgetExhausted(RuntimeError):
    pass


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _chapter(chapter: CompartmentRecord) -> dict:
    return {
        "compartment_id": chapter.compartment_id, "handle": chapter.expand_handle,
        "source_hash": chapter.source_hash, "message_ids": list(chapter.source_message_ids),
        "p1": chapter.summary_p1, "p2": chapter.summary_p2, "p3": chapter.summary_p3,
        "topic": chapter.topic, "confidence": chapter.confidence,
        "participants": list(chapter.participants),
    }


class GroupWikiStore:
    def __init__(self, source: DatabaseSource, *, ledger: MessageLedger,
                 context: ContextStore, jobs: DurableJobStore):
        _, self._connection = open_store_connection(source)
        self._lock = threading.RLock()
        self.ledger, self.context, self.jobs = ledger, context, jobs

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[StoreCursor]:
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN")
                yield cursor
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            finally:
                cursor.close()

    @staticmethod
    def _group(scope: ConversationScope) -> None:
        if scope.platform != "onebot-v11" or scope.kind != "group":
            raise ValueError("Group wiki requires a QQ group scope")

    def settings(self, scope: ConversationScope) -> dict:
        self._group(scope)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM group_wiki_settings WHERE scope_key=?", (scope.key,)
            ).fetchone()
        return dict(row) if row is not None else dict(scope_key=scope.key, bot_id="", enabled=False,
                                                      newcomer_enabled=False, version=0, checked_slot="", updated_at=0)

    def configure(self, scope: ConversationScope, *, bot_id: str, enabled: bool,
                  newcomer_enabled: bool, expected_version: int, now: int | None = None) -> dict:
        self._group(scope)
        if not str(bot_id).isdigit() or int(bot_id) <= 0:
            raise ValueError("A concrete sending QQ account is required")
        if type(enabled) is not bool or type(newcomer_enabled) is not bool:
            raise ValueError("Wiki switches must be booleans")
        timestamp = int(time.time()) if now is None else now
        with self._transaction() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(?))", ("group-wiki:" + scope.key,))
            row = cursor.execute("SELECT * FROM group_wiki_settings WHERE scope_key=? FOR UPDATE",
                                 (scope.key,)).fetchone()
            version = int(row["version"]) if row else 0
            if type(expected_version) is not int or expected_version != version:
                raise ValueError("Group wiki settings version changed; refresh before editing")
            cursor.execute("""INSERT INTO group_wiki_settings
                (scope_key,bot_id,enabled,newcomer_enabled,version,updated_at) VALUES (?,?,?,?,?,?)
                ON CONFLICT(scope_key) DO UPDATE SET bot_id=excluded.bot_id,enabled=excluded.enabled,
                    newcomer_enabled=excluded.newcomer_enabled,version=excluded.version,updated_at=excluded.updated_at,
                    checked_slot=''""",
                (scope.key, str(bot_id), enabled, newcomer_enabled, version + 1, timestamp))
            cursor.execute("""UPDATE durable_jobs SET status='cancelled',lease_owner='',lease_until=NULL,
                finished_at=?,updated_at=? WHERE kind IN (?,?) AND scope_key=? AND status IN ('pending','running')""",
                (timestamp, timestamp, WIKI_JOB_KIND, NEWCOMER_JOB_KIND, scope.key))
            cursor.execute("""UPDATE deliveries SET status='cancelled',lease_until=NULL,updated_at=?
                WHERE source_scope_key=? AND idempotency_key LIKE ?
                AND status IN ('pending','failed')""", (timestamp, scope.key, NEWCOMER_DELIVERY_PREFIX + "%"))
            return dict(cursor.execute("SELECT * FROM group_wiki_settings WHERE scope_key=?", (scope.key,)).fetchone())

    def enabled_groups(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._connection.execute(
                "SELECT * FROM group_wiki_settings WHERE enabled ORDER BY scope_key"
            ).fetchall()]

    def admin_groups(self) -> list[dict]:
        with self._lock:
            rows = self._connection.execute("""SELECT s.*,COALESCE(p.revision,0) AS revision,
                COALESCE(p.generated_at,0) AS generated_at FROM group_wiki_settings s
                LEFT JOIN group_wiki_pages p USING(scope_key) ORDER BY s.updated_at DESC, s.scope_key LIMIT 200""").fetchall()
        return [dict(row) for row in rows]

    def admin_jobs(self, scope: ConversationScope, *, limit: int = 5) -> dict:
        self._group(scope)
        with self._lock:
            statuses = self._connection.execute("""SELECT status,COUNT(*) AS count FROM durable_jobs
                WHERE scope_key=? AND kind IN (?,?) GROUP BY status""",
                (scope.key, WIKI_JOB_KIND, NEWCOMER_JOB_KIND)).fetchall()
            rows = self._connection.execute("""SELECT job_id,kind,status,attempts,max_attempts,created_at,updated_at
                FROM durable_jobs WHERE scope_key=? AND kind IN (?,?) ORDER BY updated_at DESC,job_id DESC LIMIT ?""",
                (scope.key, WIKI_JOB_KIND, NEWCOMER_JOB_KIND, min(max(limit, 1), 100))).fetchall()
        return {"items": [dict(row) for row in rows], "counts": {row["status"]: row["count"] for row in statuses}}

    def _snapshot(self, scope: ConversationScope) -> dict:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM group_wiki_pages WHERE scope_key=?", (scope.key,)
            ).fetchone()
        return dict(row) if row else {"revision": 0, "sources_json": "[]", "items_json": "[]"}

    def capture(self, scope: ConversationScope, *, known_sources: list[dict] | None = None) -> list[dict]:
        self._group(scope)
        if known_sources is None:
            known_sources = json.loads(self._snapshot(scope)["sources_json"])
        known = {item["compartment_id"]: item for item in known_sources}
        result: list[dict] = []
        after_id = 0
        # Oldest-first bounded batches preserve backlog, including chapters with no entries.
        while ids := self.context.compartment_ids_after(self.ledger, scope, after_id=after_id):
            after_id = ids[-1]
            for chapter in self.context.visible_compartments(self.ledger, scope, limit=100, compartment_ids=ids):
                source = self._source(scope, chapter)
                if known.get(chapter.compartment_id) == source:
                    continue
                if len(_json([*result, source]).encode()) > 48_000:
                    if not result:
                        raise ValueError("A wiki chapter exceeds the generation input budget")
                    return result
                result.append(source)
                if len(result) >= 32:
                    return result
        return result

    def _source(self, scope: ConversationScope, chapter: CompartmentRecord) -> dict:
        statements = [
            {"message_id": message.canonical_message_id, "principal_id": message.sender_principal_id,
             "speaker": _CREDENTIAL.sub("[REDACTED]", message.sender_display),
             "text": _CREDENTIAL.sub("[REDACTED]", message.prompt_text)[:600]}
            for message in self.ledger.visible_messages_by_ids(scope, chapter.source_message_ids)
            if message.direction == "inbound" and message.message_kind == "chat"
        ][:12]
        source = _chapter(chapter)
        for field in ("p1", "p2", "p3", "topic"):
            source[field] = _CREDENTIAL.sub("[REDACTED]", source[field])
        source["participants"] = [_CREDENTIAL.sub("[REDACTED]", name) for name in source["participants"]]
        return {**source, "statements": statements}

    def enqueue(self, scope: ConversationScope, *, slot: str, now: int | None = None,
                scheduled: bool = False) -> DurableJob | None:
        self._group(scope)
        if not slot or len(slot) > 40:
            raise ValueError("Wiki schedule slot is invalid")
        initial_config = self.settings(scope)
        if not initial_config["enabled"] or (scheduled and initial_config["checked_slot"] == slot):
            return None
        if scheduled:
            with self._lock:
                active = self._connection.execute("""SELECT 1 FROM durable_jobs WHERE kind=? AND scope_key=?
                    AND status IN ('pending','running') LIMIT 1""", (WIKI_JOB_KIND, scope.key)).fetchone()
            if active:
                return None
        snapshot = self._snapshot(scope)
        sources = self.capture(scope, known_sources=json.loads(snapshot["sources_json"]))
        timestamp = int(time.time()) if now is None else now
        with self._transaction() as cursor:
            config = cursor.execute("SELECT * FROM group_wiki_settings WHERE scope_key=? FOR UPDATE",
                                    (scope.key,)).fetchone()
            if config is None or not config["enabled"] or config["version"] != initial_config["version"]:
                return None
            if scheduled:
                counts = cursor.execute("""SELECT
                    COUNT(*) FILTER (WHERE status IN ('pending','running')) AS active,
                    COUNT(*) FILTER (WHERE payload_json::jsonb->>'slot'=?) AS batches
                    FROM durable_jobs WHERE kind=? AND scope_key=?""",
                    (slot, WIKI_JOB_KIND, scope.key)).fetchone()
                if counts["batches"] >= 4:
                    cursor.execute("UPDATE group_wiki_settings SET checked_slot=? WHERE scope_key=?", (slot, scope.key))
                    return None
                if counts["active"] or config["checked_slot"] == slot:
                    return None
            page = cursor.execute("SELECT revision FROM group_wiki_pages WHERE scope_key=?",
                                  (scope.key,)).fetchone()
            revision = int(page["revision"]) if page else 0
            if revision != snapshot["revision"]:
                return None
            if not sources:
                if scheduled:
                    cursor.execute("UPDATE group_wiki_settings SET checked_slot=? WHERE scope_key=?", (slot, scope.key))
                return None
            digest = _fingerprint(sources)
            job, _ = self.jobs.enqueue(
                kind=WIKI_JOB_KIND, scope_key=scope.key,
                idempotency_key=f"wiki:{scope.key}:v{config['version']}:{slot}:r{revision}",
                payload={"scope_key": scope.key, "config_version": config["version"],
                         "bot_id": config["bot_id"], "sources": sources, "source_hash": digest,
                         "base_revision": revision, "slot": slot},
                priority=35, max_attempts=3, resume_on_lease_loss=True,
                now=timestamp, transaction_cursor=cursor,
            )
            return job

    def _fenced(self, cursor: StoreCursor, job: DurableJob, timestamp: int, *, kind: str = WIKI_JOB_KIND) -> dict:
        config = cursor.execute("SELECT * FROM group_wiki_settings WHERE scope_key=? FOR UPDATE",
                                (job.scope_key,)).fetchone()
        row = cursor.execute("SELECT * FROM durable_jobs WHERE job_id=? FOR UPDATE", (job.job_id,)).fetchone()
        if (kind not in {WIKI_JOB_KIND, NEWCOMER_JOB_KIND}
                or row is None or config is None or not config["enabled"] or job.kind != kind
                or row["kind"] != kind or row["scope_key"] != job.scope_key
                or row["status"] != "running" or row["lease_owner"] != job.lease_owner
                or row["attempts"] != job.attempts or (row["lease_until"] or 0) <= timestamp):
            raise WikiLeaseLost("Group wiki execution lease is no longer current")
        payload = json.loads(row["payload_json"])
        if (payload != job.payload or payload["scope_key"] != job.scope_key
                or payload["config_version"] != config["version"] or payload["bot_id"] != config["bot_id"]
                or (kind == WIKI_JOB_KIND and _fingerprint(payload["sources"]) != payload["source_hash"])):
            raise WikiLeaseLost("Group wiki source or configuration changed")
        if kind == NEWCOMER_JOB_KIND and not config["newcomer_enabled"]:
            raise WikiLeaseLost("Newcomer briefs are disabled")
        return payload

    def enqueue_newcomer(self, scope: ConversationScope, *, bot_id: str, user_id: str,
                         noticed_at: int, now: int | None = None) -> DurableJob | None:
        self._group(scope)
        timestamp = int(time.time()) if now is None else now
        config = self.settings(scope)
        if (not config["enabled"] or not config["newcomer_enabled"] or config["bot_id"] != bot_id
                or not user_id.isdigit() or int(user_id) <= 0 or user_id == bot_id
                or type(noticed_at) is not int or not timestamp - 86400 < noticed_at <= timestamp + 60):
            return None
        items = []
        for item in self.page(scope)["items"]:
            if item["category"] == "expertise":
                continue
            if len(self.newcomer_text(scope.key, [*items, item])) > 2400:
                continue
            items.append(item)
            if len(items) == 5:
                break
        if not items:
            return None
        with self._transaction() as cursor:
            current = cursor.execute("SELECT * FROM group_wiki_settings WHERE scope_key=? FOR UPDATE",
                                     (scope.key,)).fetchone()
            if current is None or current["version"] != config["version"]:
                return None
            job, _ = self.jobs.enqueue(kind=NEWCOMER_JOB_KIND, scope_key=scope.key,
                idempotency_key=f"wiki-join:{scope.key}:{bot_id}:{user_id}:{noticed_at}",
                payload={"scope_key": scope.key, "bot_id": bot_id, "user_id": user_id,
                         "config_version": config["version"], "expires_at": noticed_at + 86400, "items": items},
                priority=30, max_attempts=3, resume_on_lease_loss=True, now=timestamp, transaction_cursor=cursor)
        return job

    @staticmethod
    def newcomer_text(scope_key: str, items: list[dict]) -> str:
        lines = [f"群 {scope_key.split(':', 2)[2]} · 新人简报"]
        lines.extend(f"{item['title']}：{item['content']}" for item in items)
        lines.append("这份简报来自本群已整理的百科；完整内容可在群里用 /群百科 查询。")
        return "\n\n".join(lines)

    def newcomer_execution(self, job: DurableJob, *, now: int | None = None) -> dict:
        timestamp = int(time.time()) if now is None else now
        with self._transaction() as cursor:
            payload = self._fenced(cursor, job, timestamp, kind=NEWCOMER_JOB_KIND)
        self._validate_newcomer(payload, timestamp)
        return payload

    def _validate_newcomer(self, payload: dict, timestamp: int) -> None:
        scope = ConversationScope(*payload["scope_key"].split(":", 2))
        config = self.settings(scope)
        if (not config["enabled"] or not config["newcomer_enabled"]
                or config["version"] != payload["config_version"] or config["bot_id"] != payload["bot_id"]
                or timestamp >= payload["expires_at"]):
            raise WikiSuperseded("Newcomer brief expired or its configuration changed")
        items = self.page(scope)["items"]
        if not payload["items"] or any(item not in items for item in payload["items"]):
            raise WikiSuperseded("Newcomer brief evidence is no longer visible or current")

    def queue_newcomer_delivery(self, job: DurableJob, deliveries: DeliveryStore,
                               *, now: int | None = None) -> Delivery:
        timestamp = int(time.time()) if now is None else now
        payload = self.newcomer_execution(job, now=timestamp)
        with self._transaction() as cursor:
            self._fenced(cursor, job, timestamp, kind=NEWCOMER_JOB_KIND)
            delivery, _ = deliveries.enqueue(idempotency_key=f"{NEWCOMER_DELIVERY_PREFIX}{job.job_id}",
                source_scope_key=job.scope_key,
                target_scope=ConversationScope("onebot-v11", "private", payload["user_id"]),
                body=MessageBody((TextNode(0, self.newcomer_text(job.scope_key, payload["items"])),)),
                now=timestamp, transaction_cursor=cursor)
        return delivery

    def newcomer_delivery(self, delivery: Delivery, *, now: int | None = None) -> dict:
        suffix = delivery.idempotency_key.removeprefix(NEWCOMER_DELIVERY_PREFIX)
        job = self.jobs.get(int(suffix)) if suffix.isdigit() else None
        if job is None or job.kind != NEWCOMER_JOB_KIND or job.status not in {"pending", "running", "succeeded"}:
            raise WikiSuperseded("Newcomer brief has no active source job")
        payload = job.payload
        if (delivery.source_scope_key != job.scope_key or payload["scope_key"] != job.scope_key
                or delivery.target_scope.key != f"onebot-v11:private:{payload['user_id']}"
                or delivery.reply_to_native_message_id or delivery.turn_id is not None
                or delivery.body != MessageBody((TextNode(0, self.newcomer_text(job.scope_key, payload["items"])),))):
            raise WikiSuperseded("Newcomer brief target or content does not match its source job")
        self._validate_newcomer(payload, int(time.time()) if now is None else now)
        return payload

    def execution(self, job: DurableJob, *, now: int | None = None) -> dict:
        timestamp = int(time.time()) if now is None else now
        with self._transaction() as cursor:
            payload = self._fenced(cursor, job, timestamp)
            receipt = cursor.execute("SELECT * FROM group_wiki_publications WHERE job_id=?", (job.job_id,)).fetchone()
            page = cursor.execute("SELECT revision FROM group_wiki_pages WHERE scope_key=?", (job.scope_key,)).fetchone()
            if receipt is None and (int(page["revision"]) if page else 0) != payload["base_revision"]:
                raise WikiSuperseded("A newer wiki page was published; stale generation cannot replace it")
        return {**payload, "receipt": dict(receipt) if receipt else None}

    def prepare_generation(self, job: DurableJob, *, now: int | None = None) -> dict:
        execution = self.execution(job, now=now)
        if execution["receipt"] is None:
            scope = ConversationScope(*job.scope_key.split(":", 2))
            if len(self._valid_sources(scope, execution["sources"])) != len(execution["sources"]):
                raise WikiSuperseded("Wiki source evidence changed; schedule a fresh snapshot")
        return execution

    def reserve_generation(self, job: DurableJob, *, now: int | None = None) -> int:
        timestamp = int(time.time()) if now is None else now
        with self._transaction() as cursor:
            payload = self._fenced(cursor, job, timestamp)
            if cursor.execute("SELECT 1 FROM group_wiki_publications WHERE job_id=?", (job.job_id,)).fetchone():
                raise WikiSuperseded("Wiki publication already has a durable receipt")
            page = cursor.execute("SELECT revision FROM group_wiki_pages WHERE scope_key=?", (job.scope_key,)).fetchone()
            if (int(page["revision"]) if page else 0) != payload["base_revision"]:
                raise WikiSuperseded("A newer wiki page was published; generation is no longer needed")
            count = cursor.execute("SELECT COUNT(*) AS n FROM group_wiki_generations WHERE job_id=?", (job.job_id,)).fetchone()
            attempt = int(count["n"]) + 1
            if attempt > GENERATION_ATTEMPT_LIMIT:
                raise WikiBudgetExhausted("Wiki generation budget exhausted; no further automatic model calls")
            cursor.execute("""INSERT INTO group_wiki_generations
                (job_id,attempt,reserved_tokens,started_at) VALUES (?,?,?,?)""",
                (job.job_id, attempt, GENERATION_TOKEN_LIMIT, timestamp))
        return attempt

    def record_generation_usage(self, job: DurableJob, attempt: int, usage: dict) -> None:
        with self._transaction() as cursor:
            cursor.execute("""UPDATE group_wiki_generations SET usage_json=? WHERE job_id=? AND attempt=?
                AND usage_json='{}'""", (_json(usage), job.job_id, attempt))

    def _valid_sources(self, scope: ConversationScope, sources: list[dict]) -> dict[int, dict]:
        expected = {item["compartment_id"]: item for item in sources}
        valid = {}
        ids = tuple(expected)
        for offset in range(0, len(ids), 100):
            actual = self.context.visible_compartments(
                self.ledger, scope, limit=100, compartment_ids=ids[offset:offset + 100],
            )
            valid.update({item.compartment_id: expected[item.compartment_id] for item in actual
                          if self._source(scope, item) == expected[item.compartment_id]})
        return valid

    def _normalize_items(self, scope: ConversationScope, sources: list[dict], items: object) -> list[dict]:
        if not isinstance(items, list) or len(items) > 80:
            raise ValueError("Wiki output must contain at most 80 entries")
        source_by_message = {message: source["compartment_id"] for source in sources for message in source["message_ids"]}
        result = []
        for item in items:
            if not isinstance(item, dict) or set(item) - {"category", "title", "content", "evidence_ids", "subject_principal_id"}:
                raise ValueError("Unexpected wiki entry fields")
            if item.get("category") not in CATEGORIES:
                raise ValueError("Unknown wiki category")
            for field, limit in (("title", 120), ("content", 1600)):
                value = item.get(field)
                if not isinstance(value, str) or not value.strip() or len(value) > limit:
                    raise ValueError(f"Invalid wiki {field}")
                if _CREDENTIAL.search(value):
                    raise ValueError("Wiki entries cannot publish credential-like content")
            evidence = item.get("evidence_ids")
            if (not isinstance(evidence, list) or not 1 <= len(evidence) <= 20
                    or any(type(value) is not int or value not in source_by_message for value in evidence)):
                raise ValueError("Every wiki entry must cite messages in its captured group evidence")
            subject = item.get("subject_principal_id")
            if item["category"] == "expertise":
                messages = self.ledger.visible_messages_by_ids(scope, tuple(evidence))
                quoted = {statement["message_id"] for source in sources for statement in source["statements"]}
                if (type(subject) is not int or subject <= 0 or not messages
                        or any(value not in quoted for value in evidence)
                        or any(message.sender_principal_id != subject for message in messages)):
                    raise ValueError("Expertise must cite the named participant's own group messages")
            elif subject is not None:
                raise ValueError("Only expertise entries may identify a participant")
            result.append({"category": item["category"], "title": item["title"].strip(),
                           "content": item["content"].strip(), "evidence_ids": sorted(set(evidence)),
                           "compartment_ids": sorted({source_by_message[value] for value in evidence}),
                           "subject_principal_id": subject})
        return result

    def publish(self, job: DurableJob, items: object, *, now: int | None = None) -> dict:
        timestamp = int(time.time()) if now is None else now
        execution = self.execution(job, now=timestamp)
        if execution["receipt"] is not None:
            return execution["receipt"]
        scope = ConversationScope(*job.scope_key.split(":", 2))
        self._group(scope)
        sources = execution["sources"]
        if len(self._valid_sources(scope, sources)) != len(sources):
            raise ValueError("Wiki source evidence is no longer visible or has changed")
        normalized = self._normalize_items(scope, sources, items)
        snapshot = self._snapshot(scope)
        valid_previous = self._valid_sources(scope, json.loads(snapshot["sources_json"]))
        changed = {source["compartment_id"] for source in sources}
        retained = [item for item in json.loads(snapshot["items_json"])
                    if all(key in valid_previous and key not in changed for key in item["compartment_ids"])]
        # Exact topic keys can be revised; unrelated older knowledge is never evicted by a batch.
        def entry_key(item):
            return item["category"], item["title"].casefold(), item["subject_principal_id"]
        merged = {entry_key(item): item for item in retained}
        for item in normalized:
            key = entry_key(item)
            previous = merged.get(key)
            if previous is None or max(item["evidence_ids"]) >= max(previous["evidence_ids"]):
                merged[key] = item
        combined_sources = [source for key, source in valid_previous.items() if key not in changed] + sources
        with self._transaction() as cursor:
            payload = self._fenced(cursor, job, timestamp)
            receipt = cursor.execute("SELECT * FROM group_wiki_publications WHERE job_id=?", (job.job_id,)).fetchone()
            if receipt is not None:
                return dict(receipt)
            # Serialize publication against history clearing; reads also revalidate evidence.
            visibility = cursor.execute("""SELECT v.min_canonical_message_id FROM conversation_visibility v
                JOIN conversations c ON c.conversation_id=v.conversation_id WHERE c.scope_key=? FOR SHARE OF v""",
                (scope.key,)).fetchone()
            if visibility is None or any(message < visibility["min_canonical_message_id"]
                                         for source in sources for message in source["message_ids"]):
                raise ValueError("Group history was cleared before wiki publication")
            page = cursor.execute("SELECT revision FROM group_wiki_pages WHERE scope_key=? FOR UPDATE",
                                  (scope.key,)).fetchone()
            revision = int(page["revision"]) if page else 0
            if revision != payload["base_revision"] or revision != snapshot["revision"]:
                raise WikiSuperseded("A newer wiki page was published; stale generation cannot replace it")
            combined_sources = [source for source in combined_sources
                                if min(source["message_ids"]) >= visibility["min_canonical_message_id"]]
            visible_ids = {source["compartment_id"] for source in combined_sources}
            merged_items = [item for item in merged.values() if set(item["compartment_ids"]) <= visible_ids]
            revision += 1
            cursor.execute("""INSERT INTO group_wiki_pages
                (scope_key,revision,source_hash,sources_json,items_json,generated_at) VALUES (?,?,?,?,?,?)
                ON CONFLICT(scope_key) DO UPDATE SET revision=excluded.revision,source_hash=excluded.source_hash,
                    sources_json=excluded.sources_json,items_json=excluded.items_json,generated_at=excluded.generated_at""",
                (scope.key, revision, _fingerprint(combined_sources), _json(combined_sources), _json(merged_items), timestamp))
            cursor.execute("""INSERT INTO group_wiki_publications
                (job_id,scope_key,revision,source_hash,created_at) VALUES (?,?,?,?,?)""",
                (job.job_id, scope.key, revision, payload["source_hash"], timestamp))
            return dict(cursor.execute("SELECT * FROM group_wiki_publications WHERE job_id=?", (job.job_id,)).fetchone())

    def page(self, scope: ConversationScope, *, query: str = "") -> dict:
        self._group(scope)
        with self._lock:
            row = self._connection.execute("""SELECT p.* FROM group_wiki_pages p JOIN group_wiki_settings s USING(scope_key)
                WHERE p.scope_key=? AND s.enabled""", (scope.key,)).fetchone()
        if row is None:
            return {"scope_key": scope.key, "revision": 0, "items": [], "stale_entries": 0, "source_chapters": 0}
        sources = json.loads(row["sources_json"])
        valid = self._valid_sources(scope, sources)
        items = json.loads(row["items_json"])
        visible = [item for item in items if all(key in valid for key in item["compartment_ids"])]
        needle = " ".join(query.split()).casefold()[:200]
        matches = [item for item in visible if needle in (item["title"] + " " + item["content"]).casefold()]
        return {"scope_key": scope.key, "revision": row["revision"], "generated_at": row["generated_at"],
                "items": matches, "stale_entries": len(items) - len(visible), "source_chapters": len(valid)}
