"""Hybrid retrieval legs + RRF fusion for narrative RAG.

Vector leg: pgvector cosine distance (`<=>`), filtered by project,
status="active" and the CURRENT embedding-engine identity — chunks written
by another engine (or with NULL embeddings, the "off" tier) never leak in.
Both legs and neighbor expansion additionally join the parent document and
require document.status="active" AND document.deleted_at IS NULL, so
chunks left under an inactivated document (stale-收口 race window) never
leak back as retrieval evidence.
Keyword leg: pg_trgm similarity prefilter on PG (CJK-friendly, no full-text
segmentation per the plan) with Python-side substring scoring boosted by
exact character name/alias hits; sqlite skips the SQL prefilter and scores
the project's active chunks directly (small data, tests). Fusion: RRF
k=60, top 8, then neighbor expansion (chunk_index ±1 in the same
document) up to 12 evidence blocks.

Authority layering (phase-2 item 9): recap-rollup documents are indexed
with authority_level="derived" while chapter原文 stays "canon".
rrf_fuse_with_authority reorders the fused list so every canon chunk
ranks above every derived chunk — a derived recap may add long-range
memory but NEVER outranks the original text it was distilled from.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import sqlalchemy as sa

from proseforge.infrastructure.database.models.retrieval import (
    RetrievalChunkModel,
    RetrievalDocumentModel,
)

VECTOR_LEG_LIMIT = 20
KEYWORD_LEG_LIMIT = 20
KEYWORD_PREFILTER_LIMIT = 100
RRF_K = 60
FUSED_TOP = 8
EVIDENCE_MAX = 12
ENTITY_BOOST = 3.0

# Authority tiers for fusion ordering: lower tier value ranks first.
# Unknown/missing authority is treated as canon (fail towards原文).
AUTHORITY_TIERS = {"canon": 0, "derived": 1}

_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")
_LATIN_WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class EvidenceBlock:
    chunk_id: str
    document_id: str
    document_title: str
    chapter_no: int | None
    content: str
    score: float
    expanded: bool = False  # True when pulled in by neighbor expansion


@dataclass(frozen=True)
class _Hit:
    chunk_id: str
    score: float


def query_terms(query: str) -> list[str]:
    """CJK bigrams + latin words; single CJK chars kept when the query has
    no bigram (very short queries)."""
    terms = [match.group(0) for match in _LATIN_WORD_RE.finditer(query.lower())]
    cjk_runs = re.findall(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]+", query)
    for run in cjk_runs:
        if len(run) == 1:
            terms.append(run)
        else:
            terms.extend(run[i : i + 2] for i in range(len(run) - 1))
    # Preserve order, drop dups.
    return list(dict.fromkeys(terms))


def keyword_score(content: str, terms: list[str], entities: list[str], *, similarity: float = 0.0) -> float:
    """pg_trgm similarity (0 when unavailable) + substring hits, with an
    exact entity (character name/alias) boost."""
    content_lower = content.lower()
    score = similarity * 2.0
    for term in terms:
        if term and term in content_lower:
            score += 1.0
    for entity in entities:
        if entity and entity.lower() in content_lower:
            score += ENTITY_BOOST
    return score


async def vector_leg(session, *, project_id: str, query_vector: list[float], identity: str, limit: int = VECTOR_LEG_LIMIT) -> list[_Hit]:
    """Cosine-distance top hits on PG; empty on other dialects."""
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return []
    literal = "[" + ",".join(str(value) for value in query_vector) + "]"
    rows = await session.execute(
        sa.text(
            "SELECT c.id, c.embedding <=> CAST(:qv AS vector) AS distance FROM retrieval_chunks c "
            "JOIN retrieval_documents d ON d.id = c.document_id "
            "AND d.status = 'active' AND d.deleted_at IS NULL "
            "WHERE c.project_id = :pid AND c.status = 'active' AND c.embedding_model = :ident "
            "AND c.embedding IS NOT NULL ORDER BY distance LIMIT :lim"
        ),
        {"qv": literal, "pid": project_id, "ident": identity, "lim": limit},
    )
    return [_Hit(chunk_id=row.id, score=1.0 - float(row.distance)) for row in rows]


def _escape_like_pattern(value: str) -> str:
    """Escape ILIKE wildcards (\\, %, _) so the query matches literally;
    paired with ESCAPE '\\' in the PG prefilter."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def keyword_leg(
    session, *, project_id: str, query: str, entities: list[str],
    identity: str | None = None, limit: int = KEYWORD_LEG_LIMIT,
) -> list[_Hit]:
    """Keyword/trigram leg. ``identity`` (the active engine's embedding_model
    string) scopes candidates to chunks written by that engine: without it a
    mid-switch mix (or engine-"off" residue) would fuse chunks from different
    embedding spaces into the same RRF ranking."""
    terms = query_terms(query)
    if not terms and not entities:
        return []
    candidates: list[tuple[str, str, float]] = []  # (chunk_id, content, trgm similarity)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        identity_clause = "AND c.embedding_model = :identity " if identity is not None else ""
        rows = await session.execute(
            sa.text(
                "SELECT c.id, c.content, similarity(c.content, :q) AS sim FROM retrieval_chunks c "
                "JOIN retrieval_documents d ON d.id = c.document_id "
                "AND d.status = 'active' AND d.deleted_at IS NULL "
                "WHERE c.project_id = :pid AND c.status = 'active' "
                f"{identity_clause}"
                "AND (c.content ILIKE '%' || :qlike || '%' ESCAPE '\\' OR similarity(c.content, :q) > 0.03) "
                "ORDER BY sim DESC LIMIT :lim"
            ),
            {"q": query[:200], "qlike": _escape_like_pattern(query[:200]), "pid": project_id, "lim": KEYWORD_PREFILTER_LIMIT, "identity": identity},
        )
        candidates = [(row.id, row.content, float(row.sim)) for row in rows]
    else:
        conditions = [
            RetrievalChunkModel.project_id == project_id,
            RetrievalChunkModel.status == "active",
            RetrievalDocumentModel.status == "active",
            RetrievalDocumentModel.deleted_at.is_(None),
        ]
        if identity is not None:
            conditions.append(RetrievalChunkModel.embedding_model == identity)
        rows = await session.execute(
            sa.select(RetrievalChunkModel.id, RetrievalChunkModel.content)
            .join(RetrievalDocumentModel, RetrievalDocumentModel.id == RetrievalChunkModel.document_id)
            .where(*conditions)
        )
        candidates = [(row.id, row.content, 0.0) for row in rows.all()]
    scored = [
        _Hit(chunk_id=chunk_id, score=keyword_score(content, terms, entities, similarity=sim))
        for chunk_id, content, sim in candidates
    ]
    scored = [hit for hit in scored if hit.score > 0]
    scored.sort(key=lambda hit: hit.score, reverse=True)
    return scored[:limit]


def rrf_fuse(legs: list[list[_Hit]], *, k: int = RRF_K, top: int = FUSED_TOP) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion over any number of legs; returns (chunk_id,
    fused score) ordered by score."""
    scores: dict[str, float] = {}
    for leg in legs:
        for rank, hit in enumerate(leg, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (k + rank)
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return ordered[:top]


async def load_authority_by_chunk(session, chunk_ids: list[str]) -> dict[str, str]:
    """chunk_id -> document authority_level ("canon" | "derived")."""
    if not chunk_ids:
        return {}
    rows = await session.execute(
        sa.select(RetrievalChunkModel.id, RetrievalDocumentModel.authority_level)
        .join(RetrievalDocumentModel, RetrievalDocumentModel.id == RetrievalChunkModel.document_id)
        .where(RetrievalChunkModel.id.in_(chunk_ids))
    )
    return {row.id: row.authority_level for row in rows}


async def rrf_fuse_with_authority(session, legs: list[list[_Hit]], *, k: int = RRF_K, top: int = FUSED_TOP) -> list[tuple[str, float]]:
    """RRF fusion + authority layering (phase-2 item 9).

    Canon chunks ALWAYS rank above derived (recap) chunks, regardless of
    raw fused score — a derived recap never盖过 the原文 it compresses.
    Within a tier the plain RRF score decides. The candidate pool is
    widened before tiering so a derived-only tail does not crowd out
    lower-ranked canon hits.
    """
    candidates = rrf_fuse(legs, k=k, top=max(top * 3, top))
    authority = await load_authority_by_chunk(session, [chunk_id for chunk_id, _ in candidates])
    tiered = sorted(
        candidates,
        key=lambda item: (AUTHORITY_TIERS.get(authority.get(item[0]) or "canon", 0), -item[1]),
    )
    return tiered[:top]


async def _load_chunks(session, chunk_ids: list[str]) -> dict[str, RetrievalChunkModel]:
    if not chunk_ids:
        return {}
    rows = await session.scalars(
        sa.select(RetrievalChunkModel).where(RetrievalChunkModel.id.in_(chunk_ids))
    )
    return {row.id: row for row in rows}


async def _load_documents(session, document_ids: list[str]) -> dict[str, RetrievalDocumentModel]:
    if not document_ids:
        return {}
    rows = await session.scalars(
        sa.select(RetrievalDocumentModel).where(RetrievalDocumentModel.id.in_(document_ids))
    )
    return {row.id: row for row in rows}


def _chapter_no(chunk: RetrievalChunkModel, document: RetrievalDocumentModel | None) -> int | None:
    try:
        metadata = json.loads(chunk.metadata_json or "{}")
        if metadata.get("chapter_no") is not None:
            return int(metadata["chapter_no"])
    except (ValueError, TypeError):
        pass
    if document is None or document.source_type == "recap_rollup":
        # Recap evidence blocks are labelled by document title (卷梗概…),
        # never masquerading as a single chapter's原文.
        return None
    return document.chapter_from


async def expand_neighbors(session, fused: list[tuple[str, float]], *, max_blocks: int = EVIDENCE_MAX) -> list[EvidenceBlock]:
    """Materialize fused hits into evidence blocks, then pull chunk_index±1
    neighbors of each hit (same document, active) until max_blocks."""
    chunks = await _load_chunks(session, [chunk_id for chunk_id, _ in fused])
    documents = await _load_documents(session, list({chunk.document_id for chunk in chunks.values()}))
    blocks: list[EvidenceBlock] = []
    seen: set[str] = set()
    for chunk_id, score in fused:
        chunk = chunks.get(chunk_id)
        if chunk is None:
            continue
        document = documents.get(chunk.document_id)
        blocks.append(EvidenceBlock(
            chunk_id=chunk.id, document_id=chunk.document_id,
            document_title="" if document is None else document.title,
            chapter_no=_chapter_no(chunk, document),
            content=chunk.content, score=score,
        ))
        seen.add(chunk.id)
    if len(blocks) < max_blocks and chunks:
        neighbor_keys = {
            (chunk.document_id, chunk.chunk_index + delta)
            for chunk in chunks.values()
            for delta in (-1, 1)
        }
        neighbors = []
        if neighbor_keys:
            rows = await session.scalars(
                sa.select(RetrievalChunkModel)
                .join(RetrievalDocumentModel, RetrievalDocumentModel.id == RetrievalChunkModel.document_id)
                .where(
                    RetrievalChunkModel.status == "active",
                    RetrievalDocumentModel.status == "active",
                    RetrievalDocumentModel.deleted_at.is_(None),
                    sa.tuple_(RetrievalChunkModel.document_id, RetrievalChunkModel.chunk_index).in_(neighbor_keys),
                )
            )
            neighbors = sorted(rows, key=lambda row: row.chunk_index)
        for neighbor in neighbors:
            if len(blocks) >= max_blocks:
                break
            if neighbor.id in seen:
                continue
            document = documents.get(neighbor.document_id)
            blocks.append(EvidenceBlock(
                chunk_id=neighbor.id, document_id=neighbor.document_id,
                document_title="" if document is None else document.title,
                chapter_no=_chapter_no(neighbor, document),
                content=neighbor.content, score=0.0, expanded=True,
            ))
            seen.add(neighbor.id)
    return blocks
