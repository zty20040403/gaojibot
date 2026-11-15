from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import math
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Protocol, Sequence
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from src.bot_storage import DatabaseSource, open_store_connection, StoreCursor

from src.plugins.ai_chat.billing_context import active_billing
from src.plugins.ai_chat.model_usage import TokenUsage


@dataclass(frozen=True)
class SemanticDocument:
    scope_key: str
    source_type: str
    source_handle: str
    content: str
    metadata: dict[str, object]

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SemanticHit:
    scope_key: str
    source_type: str
    source_handle: str
    content: str
    score: float
    metadata: dict[str, object]


class VectorBackend(Protocol):
    dimensions: int

    def upsert(
        self,
        document: SemanticDocument,
        vector: Sequence[float],
        *,
        model: str,
    ) -> None: ...

    def search(
        self,
        scope_keys: Sequence[str],
        query: str,
        vector: Sequence[float],
        *,
        model: str,
        limit: int,
    ) -> list[SemanticHit]: ...

    def stats(self) -> dict[str, object]: ...


class EmbeddingClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int,
        timeout_seconds: int = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if self.base_url:
            try:
                target = urlsplit(self.base_url)
                _ = target.port
            except ValueError:raise ValueError('invalid_embedding_endpoint') from None
            if (target.scheme not in {'http','https'} or not target.hostname or target.username is not None
                or target.password is not None or target.query or target.fragment or re.search(r'\s',self.base_url) or '\\' in self.base_url
                or any(part in {'.','..'} for part in target.path.split('/'))):
                raise ValueError('invalid_embedding_endpoint')
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.dimensions = max(int(dimensions), 1)
        self.timeout_seconds = max(int(timeout_seconds), 5)
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        cleaned = [" ".join(str(text).split())[:12000] for text in texts]
        if not cleaned or any(not text for text in cleaned):
            raise ValueError("embedding input must not be empty")
        if not self.configured:
            raise RuntimeError("embedding provider is not configured")
        profile = SimpleNamespace(provider='openai-embeddings',name='embedding',model=self.model,pricing=None)
        context = active_billing()
        receipt = context.request(profile) if context is not None else None
        async def current():
            if context is not None and context.current is not None:await context.current()
        async def trace(name,_info):
            if name in {'http11.send_request_headers.started','http2.send_request_headers.started'}:await current()
        try:
            if receipt is not None:await receipt.start()
            async with asyncio.timeout(self.timeout_seconds),httpx.AsyncClient(
                timeout=self.timeout_seconds,transport=self._transport,trust_env=False,follow_redirects=False,
            ) as client:
                await current()
                async with client.stream('POST',f'{self.base_url}/embeddings',json={
                    'model':profile.model,'input':cleaned,'dimensions':self.dimensions},
                    headers={**({'Authorization':'Bearer '+self.api_key} if self.api_key else {}),'Content-Type':'application/json'},
                    extensions={'trace':trace}) as response:
                    if not 200<=response.status_code<300:raise RuntimeError('embedding_provider_rejected')
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        await current()
                        if len(content)+len(chunk)>4*1024*1024:raise RuntimeError('embedding_response_too_large')
                        content.extend(chunk)
                try:body = json.loads(content)
                except (ValueError,UnicodeError,RecursionError):raise RuntimeError('invalid_embedding_response') from None
                if not isinstance(body,dict) or body.get('error') is not None:raise RuntimeError('invalid_embedding_response')
                if receipt is not None:
                    usage = TokenUsage.read(body.get('usage'))
                    receipt.observe(TokenUsage(usage.input_tokens,0,usage.cached_tokens,usage.cache_write_tokens))
                raw = body.get('data')
                if (not isinstance(raw,list) or len(raw)!=len(cleaned) or any(not isinstance(item,dict)
                    or type(item.get('index')) is not int for item in raw)
                    or {item['index'] for item in raw}!=set(range(len(cleaned)))):
                    raise RuntimeError('embedding_indices_or_count_invalid')
                vectors = []
                for item in sorted(raw,key=lambda item:item['index']):
                    vector = item.get('embedding')
                    if not isinstance(vector,list) or any(type(value) not in {int,float} for value in vector):
                        raise ValueError('embedding_vector_invalid')
                    _validate_vector(vector,self.dimensions)
                    if not any(vector):raise ValueError('embedding_vector_zero')
                    vectors.append([float(value) for value in vector])
                await current()
                return vectors
        except httpx.HTTPError:
            raise RuntimeError('embedding_transport_ambiguous') from None
        finally:
            if receipt is not None:await receipt.finish()


class PgVectorBackend:
    """Small psycopg-backed vector index, loaded only when configured."""

    def __init__(
        self,
        dsn: str,
        *,
        dimensions: int,
        schema: str = "qq_bot",
    ) -> None:
        self.dsn = dsn.strip()
        self.dimensions = max(int(dimensions), 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise ValueError("invalid PostgreSQL schema name")
        self.schema = schema
        self._lock = threading.RLock()
        self._initialized = False

    def upsert(
        self,
        document: SemanticDocument,
        vector: Sequence[float],
        *,
        model: str,
        transaction_cursor=None,
    ) -> None:
        _validate_vector(vector, self.dimensions)
        if transaction_cursor is not None:
            self._upsert(transaction_cursor,document,vector,model)
            return
        self._ensure_schema()
        with self._connect() as connection:
            with connection.cursor() as cursor:self._upsert(cursor,document,vector,model)
            connection.commit()

    def _upsert(self,cursor,document,vector,model):
        cursor.execute('''INSERT INTO semantic_documents (
            scope_key,source_type,source_handle,content,content_hash,metadata_json,
            embedding_model,embedding,created_at,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s::vector,%s,%s)
            ON CONFLICT(scope_key,source_type,source_handle) DO UPDATE SET
              content=EXCLUDED.content,content_hash=EXCLUDED.content_hash,
              metadata_json=EXCLUDED.metadata_json,embedding_model=EXCLUDED.embedding_model,
              embedding=EXCLUDED.embedding,updated_at=EXCLUDED.updated_at
            WHERE semantic_documents.content_hash IS DISTINCT FROM EXCLUDED.content_hash
              OR semantic_documents.embedding_model IS DISTINCT FROM EXCLUDED.embedding_model
              OR semantic_documents.metadata_json IS DISTINCT FROM EXCLUDED.metadata_json''',
            (document.scope_key,document.source_type,document.source_handle,document.content,
             document.content_hash,json.dumps(document.metadata,ensure_ascii=False),model,
             _vector_literal(vector),int(time.time()),int(time.time())))

    def search(
        self,
        scope_keys: Sequence[str],
        query: str,
        vector: Sequence[float],
        *,
        model: str,
        limit: int,
    ) -> list[SemanticHit]:
        scopes = [str(scope) for scope in scope_keys if str(scope)]
        if not scopes:
            return []
        _validate_vector(vector, self.dimensions)
        self._ensure_schema()
        bounded = min(max(int(limit), 1), 50)
        encoded_vector = _vector_literal(vector)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT scope_key, source_type, source_handle,
                           content, metadata_json,
                           LEAST(
                             1.0,
                             GREATEST(0.0, 1.0 - (embedding <=> %s::vector))
                             + CASE WHEN content ILIKE %s THEN 0.12 ELSE 0 END
                           ) AS score
                    FROM semantic_documents
                    WHERE scope_key = ANY(%s)
                      AND embedding_model = %s
                    ORDER BY score DESC, updated_at DESC
                    LIMIT %s
                    """,
                    (
                        encoded_vector,
                        f"%{query}%",
                        scopes,
                        model,
                        bounded,
                    ),
                )
                rows = cursor.fetchall()
        hits: list[SemanticHit] = []
        for row in rows:
            metadata = row[4]
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    metadata = {}
            hits.append(
                SemanticHit(
                    scope_key=str(row[0]),
                    source_type=str(row[1]),
                    source_handle=str(row[2]),
                    content=str(row[3]),
                    metadata=metadata if isinstance(metadata, dict) else {},
                    score=float(row[5]),
                )
            )
        return hits

    def stats(self) -> dict[str, object]:
        self._ensure_schema()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*), COUNT(DISTINCT scope_key),
                           MAX(updated_at) FROM semantic_documents
                    """
                )
                row = cursor.fetchone()
        return {
            "configured": True,
            "documents": int(row[0] if row else 0),
            "scopes": int(row[1] if row else 0),
            "last_updated_at": int(row[2]) if row and row[2] is not None else None,
            "dimensions": self.dimensions,
        }

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT format_type(attribute.atttypid, attribute.atttypmod)
                        FROM pg_attribute AS attribute
                        JOIN pg_class AS relation
                          ON relation.oid = attribute.attrelid
                        JOIN pg_namespace AS namespace
                          ON namespace.oid = relation.relnamespace
                        WHERE namespace.nspname = %s
                          AND relation.relname = 'semantic_documents'
                          AND attribute.attname = 'embedding'
                        """
                        ,
                        (self.schema,),
                    )
                    row = cursor.fetchone()
                    expected = f"vector({self.dimensions})"
                    if row is None:
                        raise RuntimeError(
                            "semantic_documents is missing; run Alembic upgrade"
                        )
                    if str(row[0]) != expected:
                        raise RuntimeError(
                            "embedding dimensions do not match PostgreSQL schema: "
                            f"expected {expected}, got {row[0]}"
                        )
            self._initialized = True

    def _connect(self) -> Any:
        if not self.dsn:
            raise RuntimeError("AI_POSTGRES_DSN is not configured")
        try:
            psycopg = importlib.import_module("psycopg")
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL semantic recall requires psycopg; "
                "run pip install -r requirements.txt"
            ) from exc
        connection = psycopg.connect(self.dsn, connect_timeout=10)
        with connection.cursor() as cursor:
            cursor.execute(
                f'SET search_path TO "{self.schema}", public'
            )
        connection.commit()
        return connection


class SemanticRecallService:
    def __init__(
        self,
        embedder: EmbeddingClient,
        backend: VectorBackend,
    ) -> None:
        if embedder.dimensions != backend.dimensions:
            raise ValueError("embedding and pgvector dimensions must match")
        self.embedder = embedder
        self.backend = backend

    async def index(self, documents: Sequence[SemanticDocument]) -> int:
        unique: dict[tuple[str, str, str], SemanticDocument] = {}
        for document in documents:
            content = " ".join(document.content.split()).strip()
            if not content:
                continue
            unique[
                (
                    document.scope_key,
                    document.source_type,
                    document.source_handle,
                )
            ] = SemanticDocument(
                scope_key=document.scope_key,
                source_type=document.source_type,
                source_handle=document.source_handle,
                content=content[:12000],
                metadata=dict(document.metadata),
            )
        items = list(unique.values())
        if not items:
            return 0
        vectors = await self.embedder.embed([item.content for item in items])
        for item, vector in zip(items, vectors):
            await asyncio.to_thread(
                self.backend.upsert,
                item,
                vector,
                model=self.embedder.model,
            )
        return len(items)

    async def search(
        self,
        scope_keys: Sequence[str],
        query: str,
        *,
        limit: int = 10,
    ) -> list[SemanticHit]:
        cleaned = " ".join(query.split()).strip()
        if not cleaned:
            return []
        vector = (await self.embedder.embed([cleaned]))[0]
        return await asyncio.to_thread(
            self.backend.search,
            list(scope_keys),
            cleaned,
            vector,
            model=self.embedder.model,
            limit=limit,
        )

    async def stats(self) -> dict[str, object]:
        return await asyncio.to_thread(self.backend.stats)


class SemanticIndexState:
    """Local checkpoint for derived vectors; source data remains authoritative."""

    def __init__(self, path: DatabaseSource) -> None:
        self.path, self._connection = open_store_connection(path)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def changed(
        self,
        documents: Sequence[SemanticDocument],
    ) -> list[SemanticDocument]:
        if not documents:
            return []
        keys = [_document_key(document) for document in documents]
        placeholders = ",".join("?" for _ in keys)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT source_key, content_hash FROM semantic_index_state
                WHERE source_key IN ({placeholders})
                """,
                keys,
            ).fetchall()
        known = {str(row["source_key"]): str(row["content_hash"]) for row in rows}
        return [
            document
            for document in documents
            if known.get(_document_key(document)) != document.content_hash
        ]

    def mark(self, documents: Sequence[SemanticDocument]) -> None:
        now = int(time.time())
        with self._transaction() as cursor:
            for document in documents:
                cursor.execute(
                    """
                    INSERT INTO semantic_index_state (
                        source_key, content_hash, indexed_at
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(source_key) DO UPDATE SET
                        content_hash = excluded.content_hash,
                        indexed_at = excluded.indexed_at
                    """,
                    (_document_key(document), document.content_hash, now),
                )

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


def _validate_vector(vector: Sequence[float], dimensions: int) -> None:
    if len(vector) != int(dimensions):
        raise ValueError(
            f"embedding dimension mismatch: expected {dimensions}, got {len(vector)}"
        )
    try:finite = all(math.isfinite(float(value)) for value in vector)
    except (TypeError,ValueError,OverflowError):finite = False
    if not finite:raise ValueError("embedding contains an invalid or non-finite value")


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(format(float(value), ".10g") for value in vector) + "]"


def _document_key(document: SemanticDocument) -> str:
    return json.dumps(
        [document.scope_key, document.source_type, document.source_handle],
        ensure_ascii=False,
        separators=(",", ":"),
    )
