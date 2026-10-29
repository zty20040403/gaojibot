"""Directional, read-only public history. Caller owns one PG transaction.

No native lookup, identity mapping, personal-memory lookup, writes to original
history, model calls, or dispatch capabilities are exposed. Root owns approval,
logical route and actual endpoint checks through the mandatory guard.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Callable
import uuid

from src.bot_storage import StoreCursor
from src.plugins.ai_chat.context_store import CompartmentRecord, ContextStore
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import CanonicalMessage, MessageLedger
from src.plugins.ai_chat.long_term_memory import MemoryEntry
from src.plugins.ai_chat.memory_scopes import group_memory_scope
from src.plugins.ai_chat.platform_types import EndpointKey


class HistoryUnavailable(ValueError):
    def __init__(self, reason: str = "history_unavailable") -> None:
        super().__init__(reason)


@dataclass(frozen=True)
class HistoryCheck:
    """Exact facts to verify against root's journal; NOT an authority token."""

    migration_id: str
    route_id: str
    route_epoch: int
    source_scope_key: str = field(repr=False)
    target_endpoint_key: str = field(repr=False)
    target_scope_key: str = field(repr=False)
    target_endpoint_generation: int
    target_endpoint_route_epoch: int
    purpose: str
    manifest_digest: str = field(repr=False)


Guard = Callable[[StoreCursor, HistoryCheck], None]


@dataclass(frozen=True)
class HistoryPreview:
    grant_id: str
    manifest_digest: str = field(repr=False)
    source_cutoff: int
    visible_timeline_count: int
    chapter_count: int
    note_count: int


@dataclass(frozen=True)
class PublicHistoryView:
    grant_id: str
    source_scope_key: str = field(repr=False)
    target_scope_key: str = field(repr=False)
    source_conversation_id: int
    # None is honest for a bound target which has no physical ledger row yet.
    target_conversation_id: int | None
    messages: tuple[CanonicalMessage, ...] = field(repr=False)
    chapters: tuple[CompartmentRecord, ...] = field(repr=False)
    public_notes: tuple[MemoryEntry, ...] = field(repr=False)
    cutoff: int
    effective_floor: int


def _json(value: object) -> str:
    result = json.dumps(value, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), allow_nan=False)
    if len(result.encode("utf-8")) > 65536:
        raise HistoryUnavailable("history_snapshot_too_large")
    return result


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _positive(value: int) -> int:
    if type(value) is not int or value <= 0:
        raise HistoryUnavailable("invalid_history_request")
    return value


def _ids(values: tuple[int, ...], maximum: int) -> tuple[int, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise HistoryUnavailable("invalid_history_request")
    if any(type(item) is not int or item <= 0 for item in values):
        raise HistoryUnavailable("invalid_history_request")
    if len(set(values)) != len(values):
        raise HistoryUnavailable("invalid_history_request")
    return tuple(sorted(values))


def _floors(cursor, source_key, target_key):
    # Lock current visibility in global ID order. Clear cannot race this read.
    rows = cursor.execute("""SELECT c.scope_key,c.conversation_id,v.min_canonical_message_id,v.cleared_at
        FROM conversations c JOIN conversation_visibility v USING(conversation_id)
        WHERE c.scope_key IN (?,?) ORDER BY c.conversation_id FOR SHARE OF v""",
                          (source_key, target_key)).fetchall()
    mapped = {str(row["scope_key"]): row for row in rows}
    if source_key not in mapped:
        raise HistoryUnavailable()
    source, target = mapped[source_key], mapped.get(target_key)
    return (int(source["conversation_id"]), int(source["min_canonical_message_id"]),
            int(target["conversation_id"]) if target is not None else None,
            int(target["min_canonical_message_id"]) if target is not None else 1,
            source["cleared_at"], target["cleared_at"] if target is not None else None)


def _messages(cursor, source, *, floor, cutoff, ids=None, limit=100):
    params = [source, floor, cutoff]
    predicate = ""
    if ids is not None:
        if not ids:
            return ()
        predicate = " AND m.canonical_message_id IN (" + ",".join("?" for _ in ids) + ")"
        params.extend(ids)
    params.append(limit)
    # Raw platform envelopes never enter this read view. Keep only the existing
    # canonical-message fields, with bounded bodies and no private-state joins.
    rows = cursor.execute("""SELECT m.canonical_message_id,c.scope_key,m.native_message_id,
        m.sender_native_user_id,m.sender_principal_id,m.sender_display,m.direction,m.message_kind,
        m.body_json,m.occurred_at,m.reply_to_native_message_id,m.reply_to_canonical_message_id,
        m.sender_actor_key,m.source_endpoint_key FROM messages m
        JOIN conversations c USING(conversation_id)
        WHERE c.scope_key=? AND m.canonical_message_id>=? AND m.canonical_message_id<=?
        AND m.message_kind!='command'
        AND octet_length(m.body_json)<=262144 AND octet_length(m.rendered_text)<=262144""" + predicate +
        " ORDER BY m.canonical_message_id DESC LIMIT ?", tuple(params)).fetchall()
    return tuple(MessageLedger._row_to_message(row) for row in reversed(rows))


def _chapter(cursor, scope, source_id):
    row = cursor.execute("""SELECT * FROM context_compartments
        WHERE scope_key=? AND compartment_id=? AND active=1
        AND octet_length(source_message_ids_json)<=65536
        AND octet_length(evidence_ids_json)<=65536
        AND octet_length(participants_json)<=8192
        AND octet_length(summary_p1)<=16384 AND octet_length(summary_p2)<=8192
        AND octet_length(summary_p3)<=4096 AND octet_length(summary_p4)<=4096
        AND octet_length(topic)<=4096 FOR SHARE""",
                         (scope, source_id)).fetchone()
    if row is None:
        return None
    return ContextStore._row_to_compartment(row)


def _note(cursor, scope, source_id):
    # The DB state is authoritative; never use a cached in-memory store. The SQL
    # predicate excludes every user scope before returning content to this port.
    rows = cursor.execute("""SELECT entry::text AS entry_json FROM state_blobs,
        LATERAL jsonb_array_elements(payload_json::jsonb->'entries') AS entry
        WHERE namespace='long_term_memory' AND entry->>'scope_key'=?
        AND entry->>'scope_type'='group' AND entry->>'id'=?
        AND octet_length(entry::text)<=65536 LIMIT 2""",
                          (group_memory_scope(ConversationScope.from_key(scope)), str(source_id))).fetchall()
    if len(rows) != 1:
        return None
    try:
        data = json.loads(rows[0]["entry_json"])
        allowed = MemoryEntry.__dataclass_fields__
        entry = MemoryEntry(**{key: value for key, value in data.items() if key in allowed})
        if (entry.id != source_id or type(entry.id) is not int
                or type(entry.version) is not int or entry.version <= 0
                or not isinstance(entry.content, str) or not entry.content
                or len(entry.content) > 4096):
            return None
        if entry.source_message_id is not None:
            _positive(entry.source_message_id)
        return entry
    except (TypeError, ValueError, KeyError):
        return None


def _chapter_valid(cursor, chapter, floor, cutoff):
    ids = chapter.source_message_ids
    if (not ids or len(ids) > 5000 or any(type(item) is not int or item <= 0 for item in ids)
            or tuple(sorted(set(ids))) != ids):
        return False
    if (chapter.start_message_id != ids[0] or chapter.end_message_id != ids[-1]
            or chapter.message_count != len(ids)
            or not set(chapter.evidence_ids).issubset(ids)):
        return False
    rows = _messages(cursor, chapter.scope_key, floor=floor, cutoff=cutoff, ids=ids, limit=5000)
    return (tuple(item.canonical_message_id for item in rows) == ids
            and ContextStore._source_hash(list(rows)) == chapter.source_hash)


def _note_valid(cursor, note, scope, floor, cutoff, *, floors_unchanged):
    if note.source_message_id is None:
        # A reviewed manual note has no defensible message position. Any clear
        # since approval hides it; never fabricate an anchor to keep it visible.
        return floors_unchanged
    return bool(_messages(cursor, scope, floor=floor, cutoff=cutoff,
                          ids=(note.source_message_id,), limit=1))


def _check(row, purpose):
    return HistoryCheck(
        str(row["migration_id"]), str(row["route_id"]), int(row["route_epoch"]),
        str(row["source_scope_key"]), str(row["target_endpoint_key"]),
        str(row["target_scope_key"]), int(row["target_endpoint_generation"]),
        int(row["target_endpoint_route_epoch"]), purpose, str(row["manifest_digest"]),
    )


def _guard(cursor, guard, row, purpose):
    # Explicit raising-check contract. In particular a bool-style callback
    # returning False must not accidentally authorize a read.
    if not callable(guard) or guard(cursor, _check(row, purpose)) is not None:
        raise HistoryUnavailable("history_guard_contract")


def prepare_public_history(cursor: StoreCursor, *, guard: Guard, migration_id: str,
                           route_id: str, route_epoch: int, source_scope: ConversationScope,
                           target_endpoint: EndpointKey, endpoint_generation: int,
                           endpoint_route_epoch: int, chapter_ids: tuple[int, ...] = (),
                           public_note_ids: tuple[int, ...] = (), now: int) -> HistoryPreview:
    """Host preview operation; selected mutable records are explicit and bounded.

    Does not activate a grant. Root must authenticate preview authority, freeze
    the returned digest in its approved journal and then call activate.
    """
    if (not callable(guard) or source_scope.kind != "group" or target_endpoint.kind != "group"
            or target_endpoint.account.platform != "qq-official"
            or source_scope.key == target_endpoint.scope.key):
        raise HistoryUnavailable("invalid_history_request")
    chapter_ids, public_note_ids = _ids(chapter_ids, 20), _ids(public_note_ids, 30)
    row = dict(migration_id=migration_id, route_id=route_id, route_epoch=_positive(route_epoch),
               source_scope_key=source_scope.key, target_scope_key=target_endpoint.scope.key,
               target_endpoint_key=target_endpoint.key,
               target_endpoint_generation=_positive(endpoint_generation),
               target_endpoint_route_epoch=_positive(endpoint_route_epoch), manifest_digest="")
    _guard(cursor, guard, row, "prepare")  # Root route/endpoint lock always first.
    source_id, source_floor, _, target_floor, source_clear, target_clear = _floors(
        cursor, source_scope.key, target_endpoint.scope.key)
    cutoff = int(cursor.execute("SELECT COALESCE(MAX(canonical_message_id),0) FROM messages "
                               "WHERE conversation_id=?", (source_id,)).fetchone()[0])
    floor = max(source_floor, target_floor)
    snapshots = []
    if public_note_ids:
        cursor.execute("SELECT namespace FROM state_blobs WHERE namespace='long_term_memory' FOR SHARE")
    for kind, identifiers, loader in (("chapter", chapter_ids, _chapter),
                                      ("public_note", public_note_ids, _note)):
        for identifier in identifiers:
            record = loader(cursor, source_scope.key, identifier)
            valid = record is not None and (
                _chapter_valid(cursor, record, floor, cutoff) if kind == "chapter" else
                _note_valid(cursor, record, source_scope.key, floor, cutoff, floors_unchanged=True)
            )
            if not valid:
                raise HistoryUnavailable("history_preview_source_changed")
            payload = asdict(record)
            snapshots.append((kind, identifier, record.version if kind == "public_note" else None,
                              _json(payload), _digest(payload)))
    count = int(cursor.execute("""SELECT COUNT(*) FROM messages WHERE conversation_id=?
        AND canonical_message_id BETWEEN ? AND ? AND message_kind!='command'""",
                               (source_id, floor, cutoff)).fetchone()[0])
    manifest = {**row, "source_conversation_id": source_id, "source_floor": source_floor,
                "target_floor": target_floor, "source_cleared_at": source_clear,
                "target_cleared_at": target_clear, "source_cutoff": cutoff, "visible_timeline_count": count,
                "snapshots": [[kind, ident, version, digest] for kind, ident, version, _, digest in snapshots]}
    manifest.pop("manifest_digest")
    encoded, digest, grant_id = _json(manifest), _digest(manifest), uuid.uuid4().hex
    cursor.execute("""INSERT INTO qq_migration_public_history_grants
        (grant_id,migration_id,route_id,route_epoch,source_scope_key,source_conversation_id,
         target_endpoint_key,target_scope_key,target_endpoint_generation,target_endpoint_route_epoch,
         source_floor,target_floor,source_cleared_at,target_cleared_at,source_cutoff,
         manifest_json,manifest_digest,state,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'prepared',?)""",
        (grant_id, migration_id, route_id, route_epoch, source_scope.key, source_id,
         target_endpoint.key, target_endpoint.scope.key, endpoint_generation, endpoint_route_epoch,
         source_floor, target_floor, source_clear, target_clear, cutoff, encoded, digest, now))
    for kind, identifier, version, encoded_snapshot, snapshot_digest in snapshots:
        cursor.execute("""INSERT INTO qq_migration_public_history_snapshots
            (grant_id,kind,source_id,source_version,snapshot_json,snapshot_digest) VALUES (?,?,?,?,?,?)""",
                       (grant_id, kind, identifier, version, encoded_snapshot, snapshot_digest))
    return HistoryPreview(grant_id, digest, cutoff, count, len(chapter_ids), len(public_note_ids))


def _load(cursor, grant_id):
    row = cursor.execute("SELECT * FROM qq_migration_public_history_grants WHERE grant_id=?",
                         (grant_id,)).fetchone()
    if row is None:
        raise HistoryUnavailable()
    try:
        manifest = json.loads(row["manifest_json"])
        if _digest(manifest) != row["manifest_digest"]:
            raise HistoryUnavailable("history_snapshot_changed")
        for key, value in manifest.items():
            if key not in {"snapshots", "visible_timeline_count"} and row[key] != value:
                raise HistoryUnavailable("history_snapshot_changed")
    except (KeyError, TypeError, ValueError):
        raise HistoryUnavailable("history_snapshot_changed") from None
    return row, manifest


def _snapshots(cursor, row, manifest, *, floor, source_floor, target_floor):
    snapshots = cursor.execute("SELECT * FROM qq_migration_public_history_snapshots WHERE grant_id=? "
                               "ORDER BY kind,source_id LIMIT 51", (row["grant_id"],)).fetchall()
    evidence = [[item["kind"], item["source_id"], item["source_version"], item["snapshot_digest"]]
                for item in snapshots]
    if evidence != manifest["snapshots"]:
        raise HistoryUnavailable("history_snapshot_changed")
    chapters, notes = [], []
    if any(item["kind"] == "public_note" for item in snapshots):
        cursor.execute("SELECT namespace FROM state_blobs WHERE namespace='long_term_memory' FOR SHARE")
    for item in snapshots:
        data = json.loads(item["snapshot_json"])
        if _digest(data) != item["snapshot_digest"]:
            raise HistoryUnavailable("history_snapshot_changed")
        kind, identifier = str(item["kind"]), int(item["source_id"])
        current = (_chapter if kind == "chapter" else _note)(cursor, row["source_scope_key"], identifier)
        # Mutation, tombstone/deactivation or deletion suppresses the frozen item;
        # a newer record can only enter after a distinct preview and approval.
        if current is None or _digest(asdict(current)) != item["snapshot_digest"]:
            continue
        if kind == "chapter":
            if _chapter_valid(cursor, current, floor, row["source_cutoff"]):
                chapters.append(current)
        elif _note_valid(cursor, current, row["source_scope_key"], floor, row["source_cutoff"],
                         floors_unchanged=(source_floor == row["source_floor"]
                                           and target_floor == row["target_floor"])):
            notes.append(current)
    return tuple(chapters), tuple(notes), len(snapshots)


def activate_public_history(cursor: StoreCursor, *, guard: Guard, grant_id: str,
                            approved_manifest_digest: str) -> None:
    row, manifest = _load(cursor, grant_id)
    if row["state"] not in {"prepared", "active"} or approved_manifest_digest != row["manifest_digest"]:
        raise HistoryUnavailable("history_approval_changed")
    # Guard checks root's real authenticated approval of THIS digest, not merely
    # the caller-supplied digest. It must lock journal/route before grant state.
    _guard(cursor, guard, row, "activate")
    locked = cursor.execute("SELECT state FROM qq_migration_public_history_grants "
                            "WHERE grant_id=? FOR UPDATE", (grant_id,)).fetchone()
    if locked["state"] not in {"prepared", "active"}:
        raise HistoryUnavailable()
    source_id, source_floor, _, target_floor, source_clear, target_clear = _floors(
        cursor, row["source_scope_key"], row["target_scope_key"])
    if (source_id != row["source_conversation_id"] or source_floor != row["source_floor"]
            or target_floor != row["target_floor"] or source_clear != row["source_cleared_at"]
            or target_clear != row["target_cleared_at"]):
        raise HistoryUnavailable("history_preview_source_changed")
    chapters, notes, count = _snapshots(cursor, row, manifest, floor=max(source_floor, target_floor),
                                       source_floor=source_floor, target_floor=target_floor)
    if len(chapters) + len(notes) != count:
        raise HistoryUnavailable("history_preview_source_changed")
    cursor.execute("UPDATE qq_migration_public_history_grants SET state='active' WHERE grant_id=?",
                   (grant_id,))


def read_public_history(cursor: StoreCursor, *, guard: Guard, grant_id: str,
                        target_endpoint: EndpointKey, route_epoch: int,
                        limit: int = 100) -> PublicHistoryView:
    """Read only source public data; caller reads actual target with its old port.

    This deliberately does not resolve native reply IDs, return references to
    private memory, traverse a grant graph, or silently union target identities.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise HistoryUnavailable("invalid_history_request")
    _positive(route_epoch)
    row, manifest = _load(cursor, grant_id)
    if (row["state"] != "active" or row["target_endpoint_key"] != target_endpoint.key
            or row["target_scope_key"] != target_endpoint.scope.key or row["route_epoch"] != route_epoch):
        raise HistoryUnavailable()
    _guard(cursor, guard, row, "read")
    state = cursor.execute("SELECT state FROM qq_migration_public_history_grants "
                           "WHERE grant_id=? FOR SHARE", (grant_id,)).fetchone()
    if state["state"] != "active":
        raise HistoryUnavailable()
    source_id, source_floor, target_id, target_floor, source_clear, target_clear = _floors(
        cursor, row["source_scope_key"], row["target_scope_key"])
    if source_id != row["source_conversation_id"]:
        raise HistoryUnavailable()
    if target_clear != row["target_cleared_at"]:
        # A physical target can have no rows beyond an earlier clear. In that
        # case its global-ID floor need not advance, but shared history is gone.
        return PublicHistoryView(grant_id, row["source_scope_key"], row["target_scope_key"],
                                 source_id, target_id, (), (), (), row["source_cutoff"],
                                 max(source_floor, target_floor, row["source_cutoff"] + 1))
    floor = max(row["source_floor"], row["target_floor"], source_floor, target_floor)
    messages = _messages(cursor, row["source_scope_key"], floor=floor, cutoff=row["source_cutoff"], limit=limit)
    chapters, notes, _ = _snapshots(cursor, row, manifest, floor=floor,
                                   source_floor=source_floor, target_floor=target_floor)
    if source_clear != row["source_cleared_at"]:
        notes = tuple(item for item in notes if item.source_message_id is not None)
    _guard(cursor, guard, row, "read")
    return PublicHistoryView(grant_id, row["source_scope_key"], row["target_scope_key"], source_id,
                             target_id, messages, chapters, notes, row["source_cutoff"], floor)
