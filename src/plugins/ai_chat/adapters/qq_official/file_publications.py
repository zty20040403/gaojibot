"""PG implementation of PublicationStore; no schema setup or network.

Caller provides a whole-transaction factory and its existing DatabaseCalls.
Publication authority is internal host composition, never a chat/tool callback.
Reads grant no new authority. Artifact immutability belongs to root's manifest.
"""
from __future__ import annotations

import inspect
import math
import threading
import time
from dataclasses import replace

from .file_links import (
    ArtifactRef,
    LinkError,
    Publication,
    _digest,
    _origin,
    _ref,
    _timestamp,
)

MAX_BIGINT = 2**63 - 1
_SELECT = """SELECT p.publication_ref,p.credential_ref,p.request_key,p.token_digest,
    p.wire_commitment,p.origin,p.created_at,p.expires_at,p.state,p.served_complete_count,
    a.artifact_id,a.sha256,a.byte_size,a.mime,a.filename
    FROM qq_official_file_publications p
    JOIN qq_official_file_artifacts a ON a.artifact_id=p.artifact_id"""
_COLUMNS = frozenset({"publication_ref", "credential_ref", "request_key", "token_digest"})


class PgPublicationStore:
    def __init__(self, *, transaction_factory, calls, clock=time.time,
                 publication_authority=None, allow_loopback=False):
        if (not callable(transaction_factory) or not callable(calls) or not callable(clock)
                or (publication_authority is not None and not callable(publication_authority))
                or inspect.iscoroutinefunction(publication_authority)
                or inspect.iscoroutinefunction(getattr(publication_authority, "__call__", None))  # noqa: B004 - inspect async callable instances, not callability.
                or type(allow_loopback) is not bool):
            raise LinkError("invalid_publication_store")
        self._transaction, self._calls, self._clock = transaction_factory, calls, clock
        self._authority, self._allow_loopback = publication_authority, allow_loopback
        self._clock_lock, self._last_now = threading.Lock(), 0

    def __repr__(self):
        return "PgPublicationStore(<protected>)"

    def _now(self):
        with self._clock_lock:
            value = self._clock()
            if type(value) not in {int, float} or not 0 <= value <= MAX_BIGINT or not math.isfinite(value):
                raise LinkError("invalid_publication_clock")
            self._last_now = max(self._last_now, int(value))
            return self._last_now

    async def _call(self, operation, *args):
        try:
            return await self._calls(operation, *args)
        except Exception:  # noqa: BLE001 - no DB driver parameters/authority exceptions escape.
            raise LinkError("publication_storage_unavailable") from None

    def _validate(self, value: Publication) -> Publication:
        # Reconstruct frozen data as defense against malformed restored/internal
        # instances; do not coerce bool/float/string numbers into accepted values.
        if not isinstance(value, Publication) or not isinstance(value.artifact, ArtifactRef):
            raise LinkError("invalid_publication_row")
        checked = replace(value, artifact=replace(value.artifact))
        if (not _ref(checked.artifact.artifact_id)
                or checked.artifact.size > 104857600
                or checked.artifact.mime not in {"application/pdf", "application/zip"}
                or checked.served_complete_count > MAX_BIGINT
                or type(checked.origin) is not str or len(checked.origin.encode()) > 1024
                or _origin(checked.origin, self._allow_loopback) != checked.origin):
            raise LinkError("invalid_publication_row")
        return checked

    def _decode(self, row):
        if row is None:
            return None
        return self._validate(Publication(
            row["publication_ref"], row["credential_ref"], row["request_key"],
            ArtifactRef(row["artifact_id"], row["sha256"], row["byte_size"], row["mime"], row["filename"]),
            row["token_digest"], row["wire_commitment"], row["origin"], row["created_at"],
            row["expires_at"], row["state"], row["served_complete_count"]))

    def _lookup(self, column, key):
        if column not in _COLUMNS:
            raise LinkError("invalid_publication_lookup")
        with self._transaction() as cursor:
            # The interpolated column comes from a closed internal allowlist.
            return self._decode(cursor.execute(_SELECT + f" WHERE p.{column}=?", (key,)).fetchone())

    async def by_request(self, request_key):
        if not _digest(request_key):
            raise LinkError("invalid_publication_lookup")
        return await self._call(self._lookup, "request_key", request_key)

    async def by_credential(self, credential_ref):
        if not _ref(credential_ref):
            raise LinkError("invalid_publication_lookup")
        return await self._call(self._lookup, "credential_ref", credential_ref)

    async def by_digest(self, token_digest):
        if not _digest(token_digest):
            raise LinkError("invalid_publication_lookup")
        return await self._call(self._lookup, "token_digest", token_digest)

    async def get(self, publication_ref):
        if not _ref(publication_ref):
            raise LinkError("invalid_publication_lookup")
        return await self._call(self._lookup, "publication_ref", publication_ref)

    def _create(self, incoming):
        value = self._validate(incoming)
        if value.state != "active" or value.served_complete_count != 0:
            raise LinkError("invalid_publication_initial_state")
        with self._transaction() as cursor:
            # Callback owns account/endpoint/source locks BEFORE artifact lock.
            if self._authority is None or self._authority(cursor, value.artifact, value.expires_at) is not True:
                raise LinkError("publication_authority_unavailable")
            artifact = cursor.execute("""SELECT artifact_id,sha256,byte_size,mime,filename,
                bytes_state,retain_until,pending_deadline_at
                FROM qq_official_file_artifacts WHERE artifact_id=? FOR SHARE""",
                (value.artifact.artifact_id,)).fetchone()
            if artifact is None or ArtifactRef(artifact["artifact_id"], artifact["sha256"],
                    artifact["byte_size"], artifact["mime"], artifact["filename"]) != value.artifact:
                raise LinkError("publication_artifact_changed")
            if (artifact["bytes_state"] != "available"
                    or not _timestamp(artifact["retain_until"])
                    or not _timestamp(artifact["pending_deadline_at"])
                    or value.expires_at > min(artifact["retain_until"], artifact["pending_deadline_at"])):
                raise LinkError("publication_artifact_unavailable")
            # Deadline evaluated after all authority/artifact lock acquisition.
            if not value.created_at <= self._now() < value.expires_at:
                raise LinkError("publication_time_unavailable")
            cursor.execute("""INSERT INTO qq_official_file_publications
                (publication_ref,credential_ref,request_key,artifact_id,token_digest,
                 wire_commitment,origin,created_at,expires_at,state,served_complete_count)
                VALUES (?,?,?,?,?,?,?,?,?,'active',0)
                ON CONFLICT(request_key) DO NOTHING""",
                (value.publication_ref, value.credential_ref, value.request_key, value.artifact.artifact_id,
                 value.token_digest, value.wire_commitment, value.origin, value.created_at, value.expires_at))
            winner = self._decode(cursor.execute(_SELECT + " WHERE p.request_key=? FOR UPDATE OF p",
                                                  (value.request_key,)).fetchone())
            if winner is None or (winner.artifact, winner.origin, winner.expires_at) != (
                    value.artifact, value.origin, value.expires_at):
                raise LinkError("publication_request_conflict")
            return winner

    async def create(self, publication):
        return await self._call(self._create, publication)

    def _deactivate(self, publication_ref, state):
        with self._transaction() as cursor:
            row = cursor.execute("""SELECT state,expires_at FROM qq_official_file_publications
                WHERE publication_ref=? FOR UPDATE""", (publication_ref,)).fetchone()
            if row is None:
                return
            if state == "expired" and row["state"] == "active" and row["expires_at"] > self._now():
                raise LinkError("publication_not_expired")
            if state == "deleted" or row["state"] == "active":
                cursor.execute("UPDATE qq_official_file_publications SET state=? WHERE publication_ref=?",
                               (state, publication_ref))

    async def deactivate(self, publication_ref, state):
        if not _ref(publication_ref) or type(state) is not str or state not in {"revoked", "expired", "deleted"}:
            raise LinkError("invalid_publication_transition")
        return await self._call(self._deactivate, publication_ref, state)

    def _record_served(self, publication_ref, observed_at, sha256, size):
        with self._transaction() as cursor:
            # Lock first so a wait behind revoke/another writer cannot use a
            # stale pre-wait clock to count an expired transmission.
            row = cursor.execute("""SELECT publication_ref FROM qq_official_file_publications
                WHERE publication_ref=? FOR UPDATE""", (publication_ref,)).fetchone()
            if row is None:
                return False
            current = max(observed_at, self._now())
            cursor.execute("""UPDATE qq_official_file_publications p
                SET served_complete_count=served_complete_count+1
                FROM qq_official_file_artifacts a
                WHERE p.publication_ref=? AND p.artifact_id=a.artifact_id
                  AND p.state='active' AND p.expires_at>?
                  AND a.sha256=? AND a.byte_size=?
                  AND p.served_complete_count<?""",
                (publication_ref, current, sha256, size, MAX_BIGINT))
            return cursor.rowcount == 1

    async def record_served(self, publication_ref, *, now, sha256, size):
        if (not _ref(publication_ref) or not _timestamp(now) or not _digest(sha256)
                or type(size) is not int or not 1 <= size <= 200_000_000):
            raise LinkError("invalid_publication_observation")
        return await self._call(self._record_served, publication_ref, now, sha256, size)
