"""Retrieval indexing worker (narrative RAG phase 2).

Runs one retrieval_jobs row end to end: mark running -> resolve the user's
embedding engine (user_preferences key "embedding") -> read the source ->
chunk -> embed -> idempotent upsert. Two source kinds share this pipeline:

- chapter (job_type "index_chapter"): the chapter's active version.
  Idempotency key is (source_type, source_id, source_version): re-running
  the same version writes nothing; a new version supersedes the old
  chunks. authority_level="canon".
- recap_rollup (job_type "index_recap"): a settled memory-pyramid recap
  (phase-2 item 9). Idempotency key is the recap content hash; the
  document is indexed with authority_level="derived" so fusion ranks it
  below原文. A STALE or empty recap is never (re)indexed — the
  invalidation path already superseded its chunks, and late jobs settle
  as done without writes.

Three engines share this pipeline (see normalize_embedding_preference):
- "api": vendor HTTP embedding via EmbeddingClient (a missing credential
  fails the job without raising — never retried, never breaks the chain);
- "local": built-in ONNX model via LocalEmbedder (download failures raise
  EmbeddingError and ride the normal pending->failed retry chain);
- "off": chunks are stored with embedding=NULL and embedding_model="none",
  paving the way for keyword-only retrieval.
Other errors re-arm the job as pending until MAX_ATTEMPTS, re-raising so
the queue applies its backoff retry.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from proseforge.domain.retrieval.chunker import chunk_text
from proseforge.domain.revision.proposal import content_hash
from proseforge.infrastructure.embeddings.client import EmbeddingClient, EmbeddingError
from proseforge.infrastructure.embeddings.llama_server import (
    LLAMA_MODELS,
    LlamaServerEmbedder,
)
from proseforge.infrastructure.embeddings.local import (
    DEFAULT_LOCAL_MODEL,
    LocalEmbedder,
    local_model_chunk_chars,
)

logger = logging.getLogger(__name__)

EMBEDDING_PREF_KEY = "embedding"
EMBEDDING_VERSION = "v1"
MAX_ATTEMPTS = 3
# Terminal error for engine=api without a usable credential; the settings
# route re-arms jobs carrying this exact error once the config is fixed.
EMBEDDING_NOT_CONFIGURED_ERROR = "embedding 未配置"

# Window sizes per engine: local models take their window from the registry's
# chunk_chars (bge-m3 era: 1200 for the 8k-context GGUF models, 450 for the
# 512-token fastembed ones); API models keep the 700-char narrative window.
DEFAULT_MAX_CHUNK_CHARS = 700
OFF_IDENTITY = "none"

# Recap-rollup indexing (phase-2 item 9): source_type marker + per-level
# document titles. Recap documents are indexed authority_level="derived".
RECAP_SOURCE_TYPE = "recap_rollup"
_RECAP_TITLES = {
    "volume": "卷梗概（第{start}-{end}章）",
    "book": "全书梗概（第{start}-{end}章）",
    "era": "部梗概（第{start}-{end}章）",
}

# Sweeper job_type -> queue task routing: every retrieval_jobs row carries
# its own worker lane; unknown types fall back to the indexing handler
# (pre-routing behavior) so a future job type degrades instead of vanishing.
SWEEP_TASK_BY_JOB_TYPE = {
    "index_chapter": "proseforge.retrieval.index_document",
    "index_recap": "proseforge.retrieval.index_document",
    "rollup_recap": "proseforge.work.rollup_recap",
    "summarize_chapter": "proseforge.work.summarize_chapter",
}

# user_preferences key for the rebuild marker: while a force rebuild is in
# flight the index is empty by design, so index_health drift alarms must stay
# silent until the rebuild's jobs settle (bge-m3 convergence, 告警抑制).
REBUILD_PREF_KEY = "retrieval_index_rebuild"


@dataclass(frozen=True)
class EmbeddingEngine:
    kind: str  # "local" | "api" | "off"
    identity: str  # written to chunk.embedding_model, basis of 409 checks
    embedder: EmbeddingClient | LocalEmbedder | LlamaServerEmbedder | None
    max_chars: int


@dataclass(frozen=True)
class _IndexSource:
    """Normalized source payload for one indexing job (chapter or recap)."""

    source_version: str  # version id for chapters, content hash for recaps
    content: str
    title: str
    authority_level: str
    chapter_from: int | None
    chapter_to: int | None
    chunk_metadata: dict[str, Any]


async def _load_index_source(uow, job_row) -> tuple[_IndexSource | None, str | None]:
    """Resolve a job's source text. Returns (source, None) to proceed,
    (None, "failed") for a missing source, (None, "skipped") when there is
    deliberately nothing to index (stale/empty recap — invalidated recaps
    never re-enter the index; their chunks were superseded at stale-marking).
    """
    if job_row.source_type == RECAP_SOURCE_TYPE:
        from proseforge.infrastructure.database.models.recap import RecapRollupModel

        recap = await uow.session.get(RecapRollupModel, job_row.source_id)
        if recap is None:
            return None, "failed"
        if recap.stale or not str(recap.content).strip():
            return None, "skipped"
        template = _RECAP_TITLES.get(str(recap.level), _RECAP_TITLES["volume"])
        return _IndexSource(
            source_version=content_hash(str(recap.content)),
            content=str(recap.content),
            title=template.format(start=recap.span_start, end=recap.span_end),
            authority_level="derived",
            chapter_from=int(recap.span_start),
            chapter_to=int(recap.span_end),
            chunk_metadata={
                "recap_level": str(recap.level),
                "span_start": int(recap.span_start),
                "span_end": int(recap.span_end),
            },
        ), None
    chapter_source = await uow.retrieval.get_chapter_with_active_version(job_row.source_id)
    if chapter_source is None:
        return None, "failed"
    chapter, version = chapter_source
    return _IndexSource(
        source_version=str(version.id),
        content=str(version.content),
        title=str(chapter.title),
        authority_level="canon",
        chapter_from=int(chapter.chapter_no),
        chapter_to=int(chapter.chapter_no),
        chunk_metadata={"chapter_no": int(chapter.chapter_no)},
    ), None


def _estimate_tokens(text: str) -> int:
    # Same char/2 proxy the providers use for CJK-heavy text.
    return max(1, len(text) // 2)


def normalize_embedding_preference(config: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve the stored preference JSON to a full engine config.

    No record (or empty) -> the local default. A legacy record without an
    "engine" field but with provider/model is an API config (written before
    the three-engine switch). Unknown engines fall back the same way.
    """
    if not config:
        return {
            "engine": "local",
            "provider": None,
            "model": None,
            "local_model": DEFAULT_LOCAL_MODEL,
            "credential_provider": None,
            "base_url": None,
        }
    engine = config.get("engine")
    if engine not in {"local", "api", "off"}:
        engine = "api" if config.get("provider") and config.get("model") else "local"
    return {
        "engine": engine,
        "provider": config.get("provider"),
        "model": config.get("model"),
        "local_model": config.get("local_model") or DEFAULT_LOCAL_MODEL,
        # Dedicated embedding credential (engine="api" only): the synthetic
        # provider_credentials row name and its endpoint, kept in the clear so
        # the identity can embed the host without decrypting anything.
        "credential_provider": config.get("credential_provider"),
        "base_url": config.get("base_url"),
    }


def embedding_identity(config: dict[str, Any]) -> str:
    """Identity string for a normalized config (chunk.embedding_model value)."""
    engine = config["engine"]
    if engine == "api":
        identity = f"{config['provider']}/{config['model']}"
        # A dedicated base_url changes the embedding space (another gateway
        # may serve a different dimension), so the host joins the identity:
        # switching bases then trips the same 409 reindex guard as switching
        # models, instead of silently mixing vectors.
        base_url = config.get("base_url")
        if base_url:
            host = urlparse(str(base_url)).netloc
            if host:
                identity = f"{identity}@{host}"
        return identity
    if engine == "local":
        return f"local/{config['local_model']}"
    return OFF_IDENTITY


async def mark_index_rebuild_started(uow, user_id: str) -> None:
    """Arm the rebuild marker before a force clear+reindex: while it is set,
    index_health drift alarms stay silent (the empty index is expected)."""
    await uow.user_preferences.set(
        user_id,
        REBUILD_PREF_KEY,
        json.dumps({"state": "rebuilding", "started_at": datetime.now(UTC).isoformat()}),
    )


async def index_health_with_rebuild_suppression(uow, owner_id: str) -> dict[str, int | bool]:
    """index_health_for_owner with rebuild-alarm suppression.

    While the marker says "rebuilding" AND the owner still has unfinished
    (pending/running) index jobs, a drifting index is the expected mid-rebuild
    state: report drift=False plus rebuilding=True instead of alarming all
    night. Once the jobs settle the marker is cleared — drift resolved means
    the rebuild finished; drift remaining with only terminal jobs means the
    rebuild failed, and that drift is real and must surface again.
    """
    health = await uow.retrieval.index_health_for_owner(owner_id=owner_id)
    marker = await uow.user_preferences.get(owner_id, REBUILD_PREF_KEY)
    rebuilding = False
    if marker is not None:
        try:
            rebuilding = json.loads(marker.value_json).get("state") == "rebuilding"
        except ValueError:
            rebuilding = False
    if not rebuilding:
        return health
    unfinished = await uow.retrieval.count_unfinished_jobs_for_owner(owner_id=owner_id)
    if health["drift"] and unfinished > 0:
        return {**health, "drift": False, "rebuilding": True}
    await uow.user_preferences.set(owner_id, REBUILD_PREF_KEY, json.dumps({"state": "done"}))
    return health


async def _resolve_api_client(
    uow,
    user_id: str,
    master_key: str,
    *,
    provider: str,
    model: str,
    credential_provider: str | None = None,
    base_url: str | None = None,
) -> EmbeddingClient | None:
    from proseforge.infrastructure.security.credential_cipher import (
        CredentialCipher,
        derive_key,
    )

    if not provider or not model:
        return None
    # A dedicated embedding credential (synthetic "embedding:<name>" row)
    # wins over the fallback: the chat credential sharing the provider name.
    credential = None
    if credential_provider:
        credential = await uow.credentials.get_for_user(user_id, credential_provider)
    if credential is None:
        credential = await uow.credentials.get_for_user(user_id, provider)
    if credential is None:
        return None
    associated = f"{credential.user_id}:{credential.provider}:{credential.id}".encode()
    secret = json.loads(
        CredentialCipher(derive_key(master_key)).decrypt(
            base64.b64decode(credential.encrypted_payload), associated_data=associated
        )
    )
    effective_base_url = str(secret.get("base_url") or base_url or "")
    if not effective_base_url:
        return None
    return EmbeddingClient(provider, model, secret["api_key"], effective_base_url)


async def _resolve_embedding_engine(uow, user_id: str, master_key: str) -> EmbeddingEngine | None:
    """Build the engine for the user's stored preference (local by default).

    Returns None only when engine=api is configured but unusable (no
    credential / no base_url) — the caller fails the job without retry.
    """
    preference = await uow.user_preferences.get(user_id, EMBEDDING_PREF_KEY)
    config = normalize_embedding_preference(json.loads(preference.value_json) if preference else None)
    kind = config["engine"]
    if kind == "off":
        return EmbeddingEngine(kind="off", identity=OFF_IDENTITY, embedder=None, max_chars=DEFAULT_MAX_CHUNK_CHARS)
    if kind == "local":
        from proseforge.settings import get_settings

        settings = get_settings()
        local_model = str(config["local_model"])
        embedder: LocalEmbedder | LlamaServerEmbedder
        if local_model in LLAMA_MODELS:
            # GGUF models (BGE-M3, Qwen3) run in a spawned llama-server;
            # -t defaults to the host CPU count inside LlamaServerEmbedder.
            embedder = LlamaServerEmbedder(
                local_model,
                cache_dir=settings.embedding_cache_dir,
                hf_endpoint=settings.hf_endpoint,
            )
        else:
            embedder = LocalEmbedder(
                local_model,
                cache_dir=settings.embedding_cache_dir,
                threads=settings.local_embedding_threads,
                hf_endpoint=settings.hf_endpoint,
            )
        return EmbeddingEngine(
            kind="local", identity=embedder.identity, embedder=embedder,
            max_chars=local_model_chunk_chars(local_model),
        )
    client = await _resolve_api_client(
        uow,
        user_id,
        master_key,
        provider=str(config.get("provider") or ""),
        model=str(config.get("model") or ""),
        credential_provider=config.get("credential_provider"),
        base_url=config.get("base_url"),
    )
    if client is None:
        return None
    return EmbeddingEngine(
        kind="api", identity=embedding_identity(config), embedder=client, max_chars=DEFAULT_MAX_CHUNK_CHARS
    )


async def run_index_job(payload: dict[str, object]) -> str:
    """Production queue entry: builds its own engine from settings."""
    from proseforge.infrastructure.database.session import (
        create_engine_and_sessionmaker,
    )
    from proseforge.settings import get_settings

    settings = get_settings()
    engine, session_factory = create_engine_and_sessionmaker(settings)
    try:
        return await execute_index_job(
            payload, session_factory, master_key=settings.master_key.get_secret_value()
        )
    finally:
        await engine.dispose()


async def execute_index_job(payload: dict[str, object], session_factory, *, master_key: str) -> str:
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

    job_id = str(payload["job_id"])
    user_id = str(payload["user_id"])

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        # Atomic claim (single conditional UPDATE): a duplicate dispatch —
        # sweeper replay or lease-expiry redelivery — loses the race and
        # skips, so one job can never run twice in parallel.
        claimed = await uow.retrieval.claim_job(job_id)
        await uow.commit()
    if not claimed:
        return "skipped"

    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            engine = await _resolve_embedding_engine(uow, user_id, master_key)
            if engine is None:
                await _finish_job(session_factory, job_id, status="failed", error=EMBEDDING_NOT_CONFIGURED_ERROR)
                return "failed"
            job_row = await uow.retrieval.get_job(job_id)
            if job_row is None:
                return "skipped"
            index_source, terminal = await _load_index_source(uow, job_row)
            if terminal == "failed":
                await _finish_job(session_factory, job_id, status="failed", error="source not found")
                return "failed"
            if terminal == "skipped" or index_source is None:
                # Stale/empty recap: nothing to index, settle as done.
                await _finish_job(session_factory, job_id, status="done", error=None)
                return "skipped"
            document = await uow.retrieval.get_document(
                project_id=job_row.project_id, source_type=job_row.source_type, source_id=job_row.source_id
            )
            if document is not None and document.source_version == index_source.source_version:
                # Same version already indexed: zero writes.
                await _finish_job(session_factory, job_id, status="done", error=None)
                return "skipped"
            # Detach plain values: the ORM instances expire with this session.
            project_id, source_type, source_id = job_row.project_id, job_row.source_type, job_row.source_id
            version_id, version_content = index_source.source_version, index_source.content
            document_title = index_source.title
            authority_level = index_source.authority_level
            chapter_from, chapter_to = index_source.chapter_from, index_source.chapter_to
            chunk_metadata = index_source.chunk_metadata
            embedding_kind, embedding_identity_str = engine.kind, engine.identity
            embedder, max_chars = engine.embedder, engine.max_chars

        chunks = chunk_text(version_content, max_chars=max_chars)
        if not chunks:
            # Empty chunk output for a chapter that HAS an active version is
            # always a bug (chunker regression or whitespace-only content) —
            # recording it as done would leave the chapter silently
            # unindexed with no failure signal anywhere.
            await _finish_job(session_factory, job_id, status="failed", error="chunker produced 0 chunks for a non-empty version")
            return "failed"
        if embedder is not None:
            if embedding_kind == "local":
                # First indexing run downloads the ONNX model; a failed
                # download raises EmbeddingError into the retry chain below.
                await embedder.ensure_ready()
            result = await embedder.embed(chunks)
            vectors: list[list[float] | None] = list(result.vectors)
            # Registration drift guard: a vector whose width disagrees with
            # the model registry means the registry (or the server) is wrong
            # — fail loudly instead of persisting off-dimension rows that
            # would poison every later similarity query.
            expected_dim = getattr(embedder, "dimension", None)
            if expected_dim is None:
                # API engine has no registry dimension: at least enforce
                # within-job width consistency (query-side identity filters
                # already keep cross-model rows out of retrieval).
                non_empty = [vector for vector in vectors if vector is not None]
                expected_dim = len(non_empty[0]) if non_empty else None
            if expected_dim is not None:
                for vector in vectors:
                    if vector is not None and len(vector) != int(expected_dim):
                        raise EmbeddingError(
                            f"embedding dimension mismatch: got {len(vector)}, expected {expected_dim}"
                        )
        else:
            # Engine "off": keyword-only rows, no vectors.
            vectors = [None] * len(chunks)

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            job_row = await uow.retrieval.get_job(job_id)
            if job_row is None:
                return "skipped"
            document = await uow.retrieval.upsert_document(
                project_id=project_id,
                source_type=source_type,
                source_id=source_id,
                source_version=version_id,
                title=document_title,
                authority_level=authority_level,
                chapter_from=chapter_from,
                chapter_to=chapter_to,
            )
            await uow.retrieval.supersede_active_chunks(document.id)
            for index, chunk_content in enumerate(chunks):
                await uow.retrieval.add_chunk(
                    project_id=project_id,
                    document_id=document.id,
                    chunk_index=index,
                    content=chunk_content,
                    embedding=vectors[index],
                    embedding_model=embedding_identity_str,
                    embedding_version=EMBEDDING_VERSION,
                    token_count=_estimate_tokens(chunk_content),
                    content_hash=content_hash(chunk_content),
                    metadata_json=json.dumps(chunk_metadata),
                )
            job_row.status = "done"
            job_row.completed_at = datetime.now(UTC)
            job_row.error = None
            await uow.commit()
        return "done"
    except Exception as error:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            job_row = await uow.retrieval.get_job(job_id)
            if job_row is not None:
                if job_row.attempt >= MAX_ATTEMPTS:
                    job_row.status = "failed"
                    job_row.completed_at = datetime.now(UTC)
                else:
                    job_row.status = "pending"
                job_row.error = f"{type(error).__name__}: {error}"[:500]
            await uow.commit()
        if job_row is not None and job_row.status == "failed":
            logger.exception("retrieval job %s failed permanently", job_id)
            return "failed"
        raise


async def _finish_job(session_factory, job_id: str, *, status: str, error: str | None) -> None:
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        job_row = await uow.retrieval.get_job(job_id)
        if job_row is not None:
            job_row.status = status
            job_row.error = error
            if status in {"done", "failed"}:
                job_row.completed_at = datetime.now(UTC)
        await uow.commit()


async def sweep_pending_jobs(uow, queue, *, threshold_seconds: float = 300, limit: int = 100, running_threshold_seconds: float = 600) -> int:
    """Re-enqueue retrieval_jobs stuck in pending past the threshold.

    The chapter/revision pipelines commit the job row first and enqueue
    after; a crash in between strands the row in pending forever. This
    sweeper re-dispatches those rows through the queue task matching their
    job_type (SWEEP_TASK_BY_JOB_TYPE): a rollup_recap row goes to the
    recap worker, a summarize_chapter row to the summarizer, index_* rows
    to the indexing worker — before this routing existed every row was
    sent to the indexing handler and non-index jobs died as
    "source not found". Replay is idempotent: execute_index_job skips a
    job whose source version is already indexed, so a duplicate dispatch
    writes nothing. Successfully re-dispatched rows get requested_at
    re-stamped (same transaction), so they are not enqueued again at the
    next sweep while still in flight.

    Jobs stuck in *running* past running_threshold_seconds are first
    re-armed to pending (rearm_stale_running_jobs): a worker killed
    mid-job can never reset its own row, so without the re-arm a killed
    indexing job strands its chapter outside the index permanently.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=threshold_seconds)
    stale = await uow.retrieval.list_stale_pending_jobs(cutoff, limit=limit)
    running_cutoff = datetime.now(UTC) - timedelta(seconds=running_threshold_seconds)
    rearmed = await uow.retrieval.rearm_stale_running_jobs(running_cutoff, limit=limit)
    if rearmed:
        await uow.commit()  # persist running -> pending before enqueuing
        stale = list(stale) + rearmed
    redispatched = 0
    for job_id, owner_id, job_type in stale:
        try:
            await queue.enqueue(
                SWEEP_TASK_BY_JOB_TYPE.get(job_type, SWEEP_TASK_BY_JOB_TYPE["index_chapter"]),
                {"job_id": job_id, "user_id": owner_id},
            )
        except Exception:
            break
        await uow.retrieval.bump_job_requested_at(job_id)
        redispatched += 1
    if redispatched:
        await uow.commit()
        logger.info("retrieval sweep redispatched %d stale pending job(s)", redispatched)
    return redispatched
