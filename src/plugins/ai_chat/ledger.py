from __future__ import annotations

import json
import re
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Iterator, Literal

from src.bot_storage import DatabaseSource, StoreCursor, StoreRow, open_store_connection

from .conversation_scope import ConversationScope
from .platform_types import ActorKey, EndpointKey
from .message_ir import (
    MessageBody,
    body_from_json,
    body_to_json,
    canonicalize_for_storage,
    render_prompt_text,
    resolve_mentions,
)


Direction = Literal["inbound", "outbound", "system"]
MessageKind = Literal["chat", "command", "system"]


@dataclass(frozen=True)
class CanonicalMessage:
    canonical_message_id: int
    scope_key: str
    native_message_id: str
    sender_native_user_id: str
    sender_principal_id: int | None
    sender_display: str
    direction: str
    message_kind: str
    body: MessageBody
    rendered_text: str
    occurred_at: int
    reply_to_native_message_id: str | None
    reply_to_canonical_message_id: int | None
    sender_actor_key: str = ""
    source_endpoint_key: str = ""

    def is_from_actor(self, actor_key: str = "", native_user_id: str | int = "") -> bool:
        """Compare identity domains, allowing the explicit legacy OneBot bridge."""
        if actor_key:
            actor = ActorKey.parse(actor_key)
            if self.sender_actor_key:
                return self.sender_actor_key == actor.key
            return (
                actor.account.platform == "onebot-v11"
                and ConversationScope.from_key(self.scope_key).platform == "onebot-v11"
                and not self.source_endpoint_key
                and self.sender_native_user_id == actor.native_user_id
            )
        if self.sender_actor_key:
            actor = ActorKey.parse(self.sender_actor_key)
            return actor.account.platform == "onebot-v11" and actor.native_user_id == str(native_user_id)
        return (
            ConversationScope.from_key(self.scope_key).platform == "onebot-v11"
            and not self.source_endpoint_key
            and self.sender_native_user_id == str(native_user_id)
        )

    @property
    def prompt_text(self) -> str:
        if self.message_kind == "command":
            return ""
        matched = re.match(
            r"^!(?:feedback|fb|btw)(?:\s+(.*))?$",
            self.rendered_text.strip(),
            re.IGNORECASE | re.DOTALL,
        )
        if matched is not None:
            return (matched.group(1) or "").strip()
        return self.rendered_text


class MessageLedger:
    def __init__(self, path: DatabaseSource, *, public_history_clear=None) -> None:
        self.path, self._connection = open_store_connection(path)
        self._lock = threading.RLock()
        self.public_history_clear = public_history_clear

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def list_scopes(self) -> list[ConversationScope]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT scope_key
                FROM conversations ORDER BY conversation_id ASC
                """
            ).fetchall()
        return [
            ConversationScope.from_key(str(row["scope_key"]))
            for row in rows
        ]

    def all_visible_messages(self, *, limit: int = 5000) -> list[CanonicalMessage]:
        bounded = min(max(int(limit), 1), 20000)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT m.*, c.scope_key
                FROM messages AS m
                JOIN conversations AS c
                  ON c.conversation_id = m.conversation_id
                JOIN conversation_visibility AS v
                  ON v.conversation_id = m.conversation_id
                WHERE m.canonical_message_id >= v.min_canonical_message_id
                  AND m.message_kind != 'command'
                ORDER BY m.canonical_message_id DESC
                LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        return [self._row_to_message(row) for row in reversed(rows)]

    def record_message(
        self,
        scope: ConversationScope,
        *,
        native_message_id: str,
        sender_native_user_id: str,
        sender_display: str,
        body: MessageBody,
        occurred_at: int = 0,
        direction: Direction = "inbound",
        message_kind: MessageKind = "chat",
        reply_to_native_message_id: str | None = None,
        raw_event: dict[str, Any] | None = None,
        identity_platform: str | None = None,
        actor: ActorKey | None = None,
        source_endpoint: EndpointKey | None = None,
        transaction_cursor: StoreCursor | None = None,
    ) -> CanonicalMessage:
        """Record a message, optionally joining the caller's same-database transaction.

        An external cursor remains owned by the caller: this method neither
        commits, closes it, nor opens another transaction in that case.
        """
        if direction not in {"inbound", "outbound", "system"}:
            raise ValueError("unsupported message direction")
        if message_kind not in {"chat", "command", "system"}:
            raise ValueError("unsupported message kind")
        if actor is None and identity_platform is not None:
            self._require_legacy_platform(identity_platform.strip())
        if actor is not None:
            if source_endpoint is None or not actor.matches_endpoint(source_endpoint):
                raise ValueError("message actor requires its matching source endpoint")
            if str(sender_native_user_id) != actor.native_user_id:
                raise ValueError("message sender differs from its actor")
            if identity_platform is not None and identity_platform != actor.account.platform:
                raise ValueError("message actor differs from identity platform")
        official = (
            source_endpoint.account.platform == "qq-official"
            if source_endpoint is not None else scope.platform == "qq-official"
        )
        if official and (actor is None or source_endpoint is None):
            raise ValueError("official messages require actor and source endpoint")
        if official:
            if actor.identity_kind == "bot" and direction != "outbound":
                raise ValueError("official bot authors are only valid for outbound messages")
            if direction == "outbound" and actor.identity_kind != "bot":
                raise ValueError("official outbound messages require the actual bot author")
        source_key = self._source_key(scope, source_endpoint)
        native_message_id = str(native_message_id) if official else str(native_message_id).strip()
        sender_native_user_id = str(sender_native_user_id) if official else str(sender_native_user_id).strip()
        sender_display = sender_display.strip() or (
            "群成员" if sender_native_user_id else "未知用户"
        )
        occurred_at = int(occurred_at or time.time())
        reply_to_native_message_id = (
            (str(reply_to_native_message_id) if official else str(reply_to_native_message_id).strip())
            if reply_to_native_message_id
            else None
        )

        with (nullcontext(transaction_cursor) if transaction_cursor is not None else self._transaction()) as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(?))",
                (f"ledger:{scope.key}",),
            )
            conversation_id = self._ensure_conversation(cursor, scope)
            identity_platform = actor.account.platform if actor is not None else (identity_platform or scope.platform).strip()
            identity_id: int | None = None
            principal_id: int | None = None
            if sender_native_user_id:
                identity_id, principal_id = self._ensure_identity(
                    cursor,
                    identity_platform,
                    sender_native_user_id,
                    sender_display,
                    occurred_at,
                    actor=actor,
                )

            resolved_body = resolve_mentions(
                canonicalize_for_storage(body),
                lambda native_id, display: self._resolve_mention(
                    cursor,
                    identity_platform,
                    native_id,
                    display,
                    occurred_at,
                    actor=actor,
                    source_endpoint=source_endpoint,
                ),
            )
            reply_to_canonical_id = self._canonical_for_native(
                cursor,
                conversation_id,
                reply_to_native_message_id,
                source_key,
            )
            existing = None
            if native_message_id:
                existing = cursor.execute(
                    """
                    SELECT canonical_message_id
                    FROM messages
                    WHERE conversation_id = ? AND native_message_id = ? AND source_endpoint_key = ?
                    """,
                    (conversation_id, native_message_id, source_key),
                ).fetchone()

            if existing is None:
                cursor.execute(
                    """
                    INSERT INTO messages (
                        conversation_id,
                        native_message_id,
                        sender_identity_id,
                        sender_principal_id,
                        sender_native_user_id,
                        sender_display,
                        direction,
                        message_kind,
                        body_json,
                        rendered_text,
                        occurred_at,
                        reply_to_native_message_id,
                        reply_to_canonical_message_id,
                        raw_event_json, sender_actor_key, source_endpoint_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conversation_id,
                        native_message_id or None,
                        identity_id,
                        principal_id,
                        sender_native_user_id,
                        sender_display,
                        direction,
                        message_kind,
                        body_to_json(resolved_body),
                        occurred_at,
                        reply_to_native_message_id,
                        reply_to_canonical_id,
                        _json_dump(raw_event or {}),
                        actor.key if actor is not None else "",
                        source_key,
                    ),
                )
                canonical_message_id = int(cursor.lastrowid)
                rendered_text = render_prompt_text(
                    resolved_body,
                    canonical_message_id,
                )
                cursor.execute(
                    """
                    UPDATE messages
                    SET rendered_text = ?
                    WHERE canonical_message_id = ?
                    """,
                    (rendered_text, canonical_message_id),
                )
            else:
                canonical_message_id = int(existing["canonical_message_id"])
            if native_message_id:
                cursor.execute(
                    """
                    UPDATE messages
                    SET reply_to_canonical_message_id = ?
                    WHERE conversation_id = ?
                      AND reply_to_native_message_id = ?
                      AND source_endpoint_key = ?
                    """,
                    (
                        canonical_message_id,
                        conversation_id,
                        native_message_id,
                        source_key,
                    ),
                )
            cursor.execute(
                """
                UPDATE conversations SET updated_at = ?
                WHERE conversation_id = ?
                """,
                (int(time.time()), conversation_id),
            )
            row = self._message_row(cursor, canonical_message_id)

        if row is None:
            raise RuntimeError("message was not stored")
        return self._row_to_message(row)

    def get_in_scope(
        self,
        scope: ConversationScope,
        canonical_message_id: int,
    ) -> CanonicalMessage | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT m.*, c.scope_key
                FROM messages AS m
                JOIN conversations AS c
                  ON c.conversation_id = m.conversation_id
                JOIN conversation_visibility AS v
                  ON v.conversation_id = m.conversation_id
                WHERE c.scope_key = ?
                  AND m.canonical_message_id = ?
                  AND m.canonical_message_id >= v.min_canonical_message_id
                """,
                (scope.key, int(canonical_message_id)),
            ).fetchone()
        return self._row_to_message(row) if row is not None else None

    def get_any_in_scope(
        self,
        scope: ConversationScope,
        canonical_message_id: int,
    ) -> CanonicalMessage | None:
        """Resolve an immutable message in a scope, including pre-clear rows.

        This is intentionally separate from normal context visibility. Durable
        references such as pins may survive a clear, but callers still cannot
        cross the conversation boundary.
        """
        with self._lock:
            row = self._connection.execute(
                """
                SELECT m.*, c.scope_key
                FROM messages AS m
                JOIN conversations AS c
                  ON c.conversation_id = m.conversation_id
                WHERE c.scope_key = ? AND m.canonical_message_id = ?
                """,
                (scope.key, int(canonical_message_id)),
            ).fetchone()
        return self._row_to_message(row) if row is not None else None

    def canonical_id_for_native(
        self,
        scope: ConversationScope,
        native_message_id: str | int,
        *,
        endpoint: EndpointKey | None = None,
    ) -> int | None:
        source_key = self._source_key(scope, endpoint)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT m.canonical_message_id
                FROM messages AS m
                JOIN conversations AS c
                  ON c.conversation_id = m.conversation_id
                JOIN conversation_visibility AS v
                  ON v.conversation_id = m.conversation_id
                WHERE c.scope_key = ?
                  AND m.native_message_id = ?
                  AND m.source_endpoint_key = ?
                  AND m.canonical_message_id >= v.min_canonical_message_id
                """,
                (scope.key, str(native_message_id), source_key),
            ).fetchone()
        return int(row[0]) if row is not None else None

    def recent_in_scope(
        self,
        scope: ConversationScope,
        limit: int = 40,
    ) -> list[CanonicalMessage]:
        limit = min(max(int(limit), 1), 500)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM (
                    SELECT m.*, c.scope_key
                    FROM messages AS m
                    JOIN conversations AS c
                      ON c.conversation_id = m.conversation_id
                    JOIN conversation_visibility AS v
                      ON v.conversation_id = m.conversation_id
                    WHERE c.scope_key = ?
                      AND m.canonical_message_id >= v.min_canonical_message_id
                    ORDER BY m.occurred_at DESC, m.canonical_message_id DESC
                    LIMIT ?
                ) ORDER BY occurred_at ASC, canonical_message_id ASC
                """,
                (scope.key, limit),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def visible_messages_after(
        self,
        scope: ConversationScope,
        canonical_message_id: int = 0,
        *,
        limit: int = 500,
    ) -> list[CanonicalMessage]:
        """Read visible source messages in immutable ingest order."""
        limit = min(max(int(limit), 1), 5000)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT m.*, c.scope_key
                FROM messages AS m
                JOIN conversations AS c
                  ON c.conversation_id = m.conversation_id
                JOIN conversation_visibility AS v
                  ON v.conversation_id = m.conversation_id
                WHERE c.scope_key = ?
                  AND m.canonical_message_id > ?
                  AND m.canonical_message_id >= v.min_canonical_message_id
                ORDER BY m.canonical_message_id ASC
                LIMIT ?
                """,
                (scope.key, max(int(canonical_message_id), 0), limit),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def visible_message_count_after(
        self,
        scope: ConversationScope,
        canonical_message_id: int = 0,
    ) -> int:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COUNT(*)
                FROM messages AS m
                JOIN conversations AS c
                  ON c.conversation_id = m.conversation_id
                JOIN conversation_visibility AS v
                  ON v.conversation_id = m.conversation_id
                WHERE c.scope_key = ?
                  AND m.canonical_message_id > ?
                  AND m.canonical_message_id >= v.min_canonical_message_id
                """,
                (scope.key, max(int(canonical_message_id), 0)),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def visible_message_floor(self, scope: ConversationScope) -> int:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT v.min_canonical_message_id
                FROM conversations AS c
                JOIN conversation_visibility AS v
                  ON v.conversation_id = c.conversation_id
                WHERE c.scope_key = ?
                """,
                (scope.key,),
            ).fetchone()
        return int(row[0]) if row is not None else 1

    def visible_messages_by_ids(
        self,
        scope: ConversationScope,
        canonical_message_ids: list[int] | tuple[int, ...],
    ) -> list[CanonicalMessage]:
        ids = [int(message_id) for message_id in canonical_message_ids]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT m.*, c.scope_key
                FROM messages AS m
                JOIN conversations AS c
                  ON c.conversation_id = m.conversation_id
                JOIN conversation_visibility AS v
                  ON v.conversation_id = m.conversation_id
                WHERE c.scope_key = ?
                  AND m.canonical_message_id IN ({placeholders})
                  AND m.canonical_message_id >= v.min_canonical_message_id
                ORDER BY m.canonical_message_id ASC
                """,
                [scope.key, *ids],
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def search_in_scope(
        self,
        scope: ConversationScope,
        query: str,
        limit: int = 10,
    ) -> list[CanonicalMessage]:
        query = query.strip()
        if not query:
            return []
        limit = min(max(int(limit), 1), 100)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT m.*, c.scope_key
                FROM messages AS m
                JOIN conversations AS c
                  ON c.conversation_id = m.conversation_id
                JOIN conversation_visibility AS v
                  ON v.conversation_id = m.conversation_id
                WHERE c.scope_key = ?
                  AND m.canonical_message_id >= v.min_canonical_message_id
                ORDER BY m.occurred_at DESC, m.canonical_message_id DESC
                LIMIT 2000
                """,
                (scope.key,),
            ).fetchall()
        folded_query = query.casefold()
        messages = [self._row_to_message(row) for row in rows]
        matches = [
            message
            for message in messages
            if message.message_kind != "command"
            and folded_query in message.prompt_text.casefold()
        ]
        return matches[:limit]

    def render_recent(
        self,
        scope: ConversationScope,
        *,
        max_messages: int = 40,
        max_chars: int = 4000,
        exclude_native_message_id: str | int | None = None,
        exclude_canonical_message_ids: tuple[int, ...] = (),
        exclude_endpoint: EndpointKey | None = None,
    ) -> str:
        messages = self.recent_in_scope(scope, max_messages)
        excluded_canonical_ids = set(
            int(item) for item in exclude_canonical_message_ids
        )
        if excluded_canonical_ids:
            messages = [
                message
                for message in messages
                if message.canonical_message_id not in excluded_canonical_ids
            ]
        if exclude_native_message_id is not None:
            excluded = str(exclude_native_message_id)
            source_key = self._source_key(scope, exclude_endpoint)
            messages = [
                message
                for message in messages
                if message.native_message_id != excluded or message.source_endpoint_key != source_key
            ]
        lines: list[str] = []
        used_chars = 0
        for message in reversed(messages):
            if not message.prompt_text:
                continue
            sender = (
                f"[mention#{message.sender_principal_id}] {message.sender_display}"
                if message.sender_principal_id is not None
                else message.sender_display
            )
            reply = (
                f" reply:msg#{message.reply_to_canonical_message_id}"
                if message.reply_to_canonical_message_id is not None
                else ""
            )
            timestamp = datetime.fromtimestamp(message.occurred_at).strftime(
                "%m-%d %H:%M"
            )
            line = (
                f"[msg#{message.canonical_message_id}{reply} | {timestamp} | "
                f"{sender}] {message.prompt_text}"
            )
            if lines and used_chars + len(line) + 1 > max_chars:
                break
            if not lines and len(line) > max_chars:
                line = line[:max_chars]
            lines.append(line)
            used_chars += len(line) + 1
        return "\n".join(reversed(lines))

    def render_roster(
        self,
        scope: ConversationScope,
        limit: int = 40,
    ) -> str:
        return "\n".join(
            f"[mention#{principal_id}]={display}"
            for display, principal_id in self.principal_roster(scope, limit)
        )

    def principal_roster(
        self,
        scope: ConversationScope,
        limit: int = 40,
    ) -> tuple[tuple[str, int], ...]:
        limit = min(max(int(limit), 1), 100)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT
                    latest.sender_principal_id,
                    latest.sender_display,
                    latest.last_message_id
                FROM (
                    SELECT
                        m.sender_principal_id,
                        m.sender_display,
                        m.canonical_message_id AS last_message_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY m.sender_principal_id
                            ORDER BY m.canonical_message_id DESC
                        ) AS roster_rank
                    FROM messages AS m
                    JOIN conversations AS c
                      ON c.conversation_id = m.conversation_id
                    JOIN conversation_visibility AS v
                      ON v.conversation_id = m.conversation_id
                    WHERE c.scope_key = ?
                      AND m.canonical_message_id >= v.min_canonical_message_id
                      AND m.sender_principal_id IS NOT NULL
                ) AS latest
                WHERE latest.roster_rank = 1
                ORDER BY last_message_id DESC
                LIMIT ?
                """,
                (scope.key, limit),
            ).fetchall()
        return tuple(
            (str(row["sender_display"]), int(row["sender_principal_id"]))
            for row in rows
        )

    def principal_label_for_native(
        self,
        platform: str,
        native_user_id: str | int,
    ) -> str | None:
        self._require_legacy_platform(platform)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT p.principal_id, p.display_name
                FROM principal_identities AS i
                JOIN principals AS p ON p.principal_id = i.principal_id
                WHERE i.platform = ? AND i.native_user_id = ?
                  AND i.account_key = '' AND i.identity_kind = '' AND i.identity_namespace = ''
                """,
                (platform, str(native_user_id)),
            ).fetchone()
        if row is None:
            return None
        return f"[mention#{int(row['principal_id'])}] {row['display_name']}"

    def principal_id_for_native(
        self,
        platform: str,
        native_user_id: str | int,
    ) -> int | None:
        self._require_legacy_platform(platform)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT principal_id FROM principal_identities
                WHERE platform = ? AND native_user_id = ?
                  AND account_key = '' AND identity_kind = '' AND identity_namespace = ''
                """,
                (platform, str(native_user_id)),
            ).fetchone()
        return int(row[0]) if row is not None else None

    def ensure_principal_identity(
        self,
        platform: str,
        native_user_id: str | int,
        display: str,
        *,
        seen_at: int | None = None,
    ) -> int:
        identities = self.ensure_principal_identities(
            platform,
            [(native_user_id, display)],
            seen_at=seen_at,
        )
        native_id = str(native_user_id).strip()
        if native_id not in identities:
            raise ValueError("invalid principal identity")
        return identities[native_id]

    def ensure_principal_identities(
        self,
        platform: str,
        identities: Iterable[tuple[str | int, str]],
        *,
        seen_at: int | None = None,
    ) -> dict[str, int]:
        normalized_platform = platform.strip()
        self._require_legacy_platform(normalized_platform)
        if not normalized_platform:
            raise ValueError("invalid principal identity platform")
        normalized: dict[str, str] = {}
        for raw_native_id, raw_display in identities:
            native_id = str(raw_native_id).strip()
            if not native_id or native_id == "all":
                continue
            normalized[native_id] = str(raw_display).strip()
        if not normalized:
            return {}

        timestamp = int(seen_at or time.time())
        principals: dict[str, int] = {}
        with self._transaction() as cursor:
            for native_id, display in normalized.items():
                _identity_id, principal_id = self._ensure_identity(
                    cursor,
                    normalized_platform,
                    native_id,
                    display,
                    timestamp,
                )
                principals[native_id] = principal_id
        return principals

    def native_identity_for_principal(
        self,
        platform: str,
        principal_id: int,
    ) -> tuple[str, str] | None:
        self._require_legacy_platform(platform)
        if int(principal_id) <= 0:
            return None
        with self._lock:
            row = self._connection.execute(
                """
                SELECT native_user_id, display_name
                FROM principal_identities
                WHERE platform = ? AND principal_id = ?
                  AND account_key = '' AND identity_kind = '' AND identity_namespace = ''
                ORDER BY last_seen_at DESC, identity_id DESC
                LIMIT 1
                """,
                (platform, int(principal_id)),
            ).fetchone()
        if row is None:
            return None
        return str(row["native_user_id"]), str(row["display_name"])

    def ensure_actor_identity(
        self, actor: ActorKey, display: str, *, seen_at: int | None = None,
    ) -> int:
        with self._transaction() as cursor:
            _, principal = self._ensure_identity(
                cursor, actor.account.platform, actor.native_user_id,
                display, int(time.time() if seen_at is None else seen_at), actor=actor,
            )
        return principal

    def principal_id_for_actor(self, actor: ActorKey) -> int | None:
        row = self._principal_for_actor(actor)
        return int(row["principal_id"]) if row is not None else None

    def principal_label_for_actor(self, actor: ActorKey) -> str | None:
        row = self._principal_for_actor(actor)
        if row is None:
            return None
        return f"[mention#{int(row['principal_id'])}] {row['display_name']}"

    def _principal_for_actor(self, actor: ActorKey) -> StoreRow | None:
        account, kind, namespace = self._identity_domain(actor)
        with self._lock:
            return self._connection.execute(
                """SELECT p.principal_id, p.display_name FROM principal_identities AS i
                   JOIN principals AS p ON p.principal_id=i.principal_id
                   WHERE i.platform=? AND i.native_user_id=? AND i.account_key=?
                     AND i.identity_kind=? AND i.identity_namespace=?""",
                (actor.account.platform, actor.native_user_id, account, kind, namespace),
            ).fetchone()

    def actor_identity_for_principal(
        self, endpoint: EndpointKey, principal_id: int,
    ) -> tuple[ActorKey, str] | None:
        if int(principal_id) <= 0:
            return None
        if endpoint.account.platform == "onebot-v11":
            identity = self.native_identity_for_principal(endpoint.account.platform, principal_id)
            if identity is None:
                return None
            actor = ActorKey(endpoint.account, "user", "", identity[0])
            return (actor, identity[1]) if actor.matches_endpoint(endpoint) else None
        kind = "group_member" if endpoint.kind == "group" else "c2c_user"
        namespace = endpoint.native_conversation_id if endpoint.kind == "group" else ""
        with self._lock:
            rows = self._connection.execute(
                """SELECT native_user_id, display_name FROM principal_identities
                   WHERE platform=? AND account_key=? AND identity_kind=?
                     AND identity_namespace=? AND principal_id=?""",
                (endpoint.account.platform, endpoint.account.key, kind, namespace, int(principal_id)),
            ).fetchall()
        if len(rows) != 1:
            return None
        row = rows[0]
        actor = ActorKey(endpoint.account, kind, namespace, str(row["native_user_id"]))
        return (actor, str(row["display_name"])) if actor.matches_endpoint(endpoint) else None

    @staticmethod
    def _require_legacy_platform(platform: str) -> None:
        if platform == "qq-official":
            raise ValueError("official identities require an ActorKey")

    @staticmethod
    def _identity_domain(actor: ActorKey | None) -> tuple[str, str, str]:
        # OneBot's existing global QQ principals deliberately retain their IDs.
        if actor is None or actor.account.platform == "onebot-v11":
            return "", "", ""
        return actor.account.key, actor.identity_kind, actor.identity_namespace

    @staticmethod
    def _source_key(scope: ConversationScope, endpoint: EndpointKey | None) -> str:
        if endpoint is None:
            if scope.platform == "qq-official":
                raise ValueError("official native message lookup requires its source endpoint")
            return ""
        # Only the matching legacy lane used the empty source namespace. A
        # differently routed OneBot source must keep its endpoint just as an
        # official source does; history binding is not transport equivalence.
        if (
            endpoint.account.platform == "onebot-v11"
            and scope.platform == "onebot-v11"
            and scope.kind == endpoint.kind
            and scope.native_conversation_id == endpoint.native_conversation_id
            and scope.account is None
        ):
            return ""
        return endpoint.key

    def activity_since(
        self,
        scope: ConversationScope,
        since_timestamp: int,
        *,
        limit: int = 3,
        exclude_native_message_id: str | int | None = None,
        exclude_endpoint: EndpointKey | None = None,
    ) -> tuple[int, list[CanonicalMessage]]:
        limit = min(max(int(limit), 1), 20)
        parameters: list[Any] = [scope.key, int(since_timestamp)]
        exclusion = ""
        if exclude_native_message_id is not None:
            exclusion = "AND (m.native_message_id != ? OR m.source_endpoint_key != ?)"
            parameters.extend((str(exclude_native_message_id), self._source_key(scope, exclude_endpoint)))
        with self._lock:
            count_row = self._connection.execute(
                f"""
                SELECT COUNT(*)
                FROM messages AS m
                JOIN conversations AS c
                  ON c.conversation_id = m.conversation_id
                JOIN conversation_visibility AS v
                  ON v.conversation_id = m.conversation_id
                WHERE c.scope_key = ?
                  AND m.occurred_at > ?
                  AND m.canonical_message_id >= v.min_canonical_message_id
                  AND m.direction = 'inbound'
                  {exclusion}
                """,
                parameters,
            ).fetchone()
            rows = self._connection.execute(
                f"""
                SELECT * FROM (
                    SELECT m.*, c.scope_key
                    FROM messages AS m
                    JOIN conversations AS c
                      ON c.conversation_id = m.conversation_id
                    JOIN conversation_visibility AS v
                      ON v.conversation_id = m.conversation_id
                    WHERE c.scope_key = ?
                      AND m.occurred_at > ?
                      AND m.canonical_message_id >= v.min_canonical_message_id
                      AND m.direction = 'inbound'
                      {exclusion}
                    ORDER BY m.occurred_at DESC, m.canonical_message_id DESC
                    LIMIT ?
                ) ORDER BY occurred_at ASC, canonical_message_id ASC
                """,
                [*parameters, limit],
            ).fetchall()
        count = int(count_row[0]) if count_row is not None else 0
        return count, [self._row_to_message(row) for row in rows]

    def hide_history(self, scope: ConversationScope) -> int:
        with self._transaction() as cursor:
            # A clear must consume preexisting directional grants even when the
            # target has no physical conversation or this second has cleared
            # already. Default None preserves the legacy zero-extra-I/O path.
            if self.public_history_clear is not None:
                if self.public_history_clear(cursor, scope) is not None:
                    raise PermissionError("Public history clear was not authorized")
            conversation = cursor.execute(
                """
                SELECT conversation_id FROM conversations WHERE scope_key = ?
                """,
                (scope.key,),
            ).fetchone()
            if conversation is None:
                return 0
            conversation_id = int(conversation["conversation_id"])
            visibility = cursor.execute(
                """
                SELECT min_canonical_message_id
                FROM conversation_visibility
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
            minimum = int(visibility[0]) if visibility is not None else 1
            aggregate = cursor.execute(
                """
                SELECT COUNT(*) AS count, MAX(canonical_message_id) AS maximum
                FROM messages
                WHERE conversation_id = ? AND canonical_message_id >= ?
                """,
                (conversation_id, minimum),
            ).fetchone()
            count = int(aggregate["count"] or 0)
            maximum = int(aggregate["maximum"] or (minimum - 1))
            cursor.execute(
                """
                INSERT INTO conversation_visibility (
                    conversation_id, min_canonical_message_id, cleared_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    min_canonical_message_id = excluded.min_canonical_message_id,
                    cleared_at = excluded.cleared_at
                """,
                (conversation_id, maximum + 1, int(time.time())),
            )
            return count

    @contextmanager
    def _transaction(self) -> Iterator[StoreCursor]:
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN")
                yield cursor
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
            finally:
                cursor.close()

    def _ensure_conversation(
        self,
        cursor: StoreCursor,
        scope: ConversationScope,
    ) -> int:
        row = cursor.execute(
            "SELECT conversation_id FROM conversations WHERE scope_key = ?",
            (scope.key,),
        ).fetchone()
        if row is not None:
            return int(row["conversation_id"])
        now = int(time.time())
        cursor.execute(
            """
            INSERT INTO conversations (
                scope_key, platform, kind, native_conversation_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                scope.key,
                scope.platform,
                scope.kind,
                scope.native_conversation_id,
                now,
                now,
            ),
        )
        conversation_id = int(cursor.lastrowid)
        cursor.execute(
            """
            INSERT INTO conversation_visibility (
                conversation_id, min_canonical_message_id
            ) VALUES (?, 1)
            """,
            (conversation_id,),
        )
        return conversation_id

    def _ensure_identity(
        self,
        cursor: StoreCursor,
        platform: str,
        native_user_id: str,
        display: str,
        seen_at: int,
        *,
        actor: ActorKey | None = None,
    ) -> tuple[int, int]:
        if actor is None:
            self._require_legacy_platform(platform)
        account, kind, namespace = self._identity_domain(actor)
        display = display.strip()
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtext(?))",
            (_json_dump({"platform": platform, "account": account, "kind": kind,
                         "namespace": namespace, "native": native_user_id}),),
        )
        row = cursor.execute(
            """
            SELECT identity_id, principal_id
            FROM principal_identities
            WHERE platform = ? AND native_user_id = ?
              AND account_key = ? AND identity_kind = ? AND identity_namespace = ?
            """,
            (platform, native_user_id, account, kind, namespace),
        ).fetchone()
        if row is None:
            stored_display = display or "群成员"
            cursor.execute(
                """
                INSERT INTO principals(display_name, created_at, updated_at)
                VALUES (?, ?, ?)
                """,
                (stored_display, seen_at, seen_at),
            )
            principal_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO principal_identities (
                    principal_id, platform, native_user_id,
                    display_name, last_seen_at, account_key, identity_kind, identity_namespace
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    principal_id,
                    platform,
                    native_user_id,
                    stored_display,
                    seen_at,
                    account, kind, namespace,
                ),
            )
            return int(cursor.lastrowid), principal_id

        identity_id = int(row["identity_id"])
        principal_id = int(row["principal_id"])
        if display:
            cursor.execute(
                """
                UPDATE principal_identities
                SET display_name = ?, last_seen_at = ?
                WHERE identity_id = ?
                """,
                (display, seen_at, identity_id),
            )
            cursor.execute(
                """
                UPDATE principals SET display_name = ?, updated_at = ?
                WHERE principal_id = ?
                """,
                (display, seen_at, principal_id),
            )
        else:
            cursor.execute(
                """
                UPDATE principal_identities SET last_seen_at = ?
                WHERE identity_id = ?
                """,
                (seen_at, identity_id),
            )
        return identity_id, principal_id

    def _resolve_mention(
        self,
        cursor: StoreCursor,
        platform: str,
        native_user_id: str,
        display: str,
        seen_at: int,
        *,
        actor: ActorKey | None = None,
        source_endpoint: EndpointKey | None = None,
    ) -> int | None:
        if not native_user_id or native_user_id == "all":
            return None
        mention_actor = None
        if actor is not None:
            if actor.account.platform == "qq-official" and actor.identity_kind == "bot":
                if source_endpoint is None or source_endpoint.account != actor.account:
                    raise ValueError("bot mention resolution requires its source endpoint")
                if source_endpoint.kind == "private" and native_user_id != source_endpoint.native_conversation_id:
                    return None
                mention_actor = ActorKey(
                    actor.account, "group_member" if source_endpoint.kind == "group" else "c2c_user",
                    source_endpoint.native_conversation_id if source_endpoint.kind == "group" else "",
                    native_user_id,
                )
            else:
                if actor.identity_kind == "c2c_user" and native_user_id != actor.native_user_id:
                    return None
                mention_actor = ActorKey(
                    actor.account, actor.identity_kind, actor.identity_namespace, native_user_id,
                )
        _, principal_id = self._ensure_identity(
            cursor,
            platform,
            native_user_id,
            display,
            seen_at,
            actor=mention_actor,
        )
        return principal_id

    @staticmethod
    def _canonical_for_native(
        cursor: StoreCursor,
        conversation_id: int,
        native_message_id: str | None,
        source_key: str = "",
    ) -> int | None:
        if not native_message_id:
            return None
        row = cursor.execute(
            """
            SELECT canonical_message_id FROM messages
            WHERE conversation_id = ? AND native_message_id = ? AND source_endpoint_key = ?
            """,
            (conversation_id, native_message_id, source_key),
        ).fetchone()
        return int(row[0]) if row is not None else None

    @staticmethod
    def _message_row(
        cursor: StoreCursor,
        canonical_message_id: int,
    ) -> StoreRow | None:
        return cursor.execute(
            """
            SELECT m.*, c.scope_key
            FROM messages AS m
            JOIN conversations AS c
              ON c.conversation_id = m.conversation_id
            WHERE m.canonical_message_id = ?
            """,
            (canonical_message_id,),
        ).fetchone()

    @staticmethod
    def _row_to_message(row: StoreRow) -> CanonicalMessage:
        canonical_message_id = int(row["canonical_message_id"])
        body = body_from_json(str(row["body_json"]))
        return CanonicalMessage(
            canonical_message_id=canonical_message_id,
            scope_key=str(row["scope_key"]),
            native_message_id=str(row["native_message_id"] or ""),
            sender_native_user_id=str(row["sender_native_user_id"] or ""),
            sender_principal_id=(
                int(row["sender_principal_id"])
                if row["sender_principal_id"] is not None
                else None
            ),
            sender_display=str(row["sender_display"] or ""),
            direction=str(row["direction"]),
            message_kind=str(row["message_kind"] or "chat"),
            body=body,
            # rendered_text is a rebuildable search cache. The canonical IR is
            # the runtime authority for every prompt projection.
            rendered_text=render_prompt_text(body, canonical_message_id),
            occurred_at=int(row["occurred_at"]),
            reply_to_native_message_id=(
                str(row["reply_to_native_message_id"])
                if row["reply_to_native_message_id"] is not None
                else None
            ),
            reply_to_canonical_message_id=(
                int(row["reply_to_canonical_message_id"])
                if row["reply_to_canonical_message_id"] is not None
                else None
            ),
            sender_actor_key=str(row["sender_actor_key"]),
            source_endpoint_key=str(row["source_endpoint_key"]),
        )


def _json_dump(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
