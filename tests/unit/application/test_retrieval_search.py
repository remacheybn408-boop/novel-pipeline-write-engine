"""Retrieval legs + RRF fusion: fusion math, keyword scoring/boost,
query terms, neighbor expansion. sqlite (vector leg is PG-only -> empty)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.application.retrieval.search import (
    _escape_like_pattern,
    _Hit,
    expand_neighbors,
    keyword_leg,
    keyword_score,
    query_terms,
    rrf_fuse,
    rrf_fuse_with_authority,
    vector_leg,
)
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.retrieval import (
    RetrievalChunkModel,
    RetrievalDocumentModel,
)
from tests.conftest import make_fk_engine

_TABLES = [ProjectModel.__table__, RetrievalDocumentModel.__table__, RetrievalChunkModel.__table__]


def test_rrf_fusion_math():
    leg_a = [_Hit("a", 10.0), _Hit("b", 9.0), _Hit("c", 8.0)]
    leg_b = [_Hit("b", 5.0), _Hit("a", 4.0)]
    fused = rrf_fuse([leg_a, leg_b], top=8)
    scores = dict(fused)
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["c"] == pytest.approx(1 / 63)
    # a and b tie -> deterministic order by score desc; both ahead of c.
    assert fused[-1][0] == "c"
    assert len(rrf_fuse([leg_a], top=2)) == 2


def test_rrf_dedupes_across_legs():
    leg = [_Hit("x", 1.0)]
    fused = rrf_fuse([leg, leg])
    assert [chunk_id for chunk_id, _ in fused] == ["x"]
    assert fused[0][1] == pytest.approx(2 / 61)


def test_keyword_score_entity_boost():
    terms = ["迎战"]
    plain = keyword_score("二人迎战于谷口", terms, [])
    boosted = keyword_score("二人迎战于谷口，烛龙在后", terms, ["烛龙"])
    assert boosted > plain
    assert boosted - plain == pytest.approx(3.0)
    # Similarity component weighs in.
    assert keyword_score("x", [], [], similarity=0.5) == pytest.approx(1.0)


def test_query_terms_cjk_bigrams_and_latin():
    terms = query_terms("烛龙 vs Sherlock 决战")
    assert "烛龙" in terms and "决" not in terms
    assert "决战" in terms
    assert "sherlock" in terms and "vs" in terms
    # Single CJK char kept.
    assert query_terms("龙") == ["龙"]


def test_escape_like_pattern_escapes_wildcards():
    # ILIKE 预过滤的通配符必须按字面匹配（配合 ESCAPE '\'）。
    assert _escape_like_pattern("50%_\\") == "50\\%\\_\\\\"
    assert _escape_like_pattern("普通文本") == "普通文本"
    assert _escape_like_pattern("") == ""


@pytest_asyncio.fixture()
async def session_factory():
    engine = make_fk_engine()
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with factory() as session:
        session.add(ProjectModel(id="p1", owner_id="u1", slug="n", title="N"))
        await session.flush()
        session.add(RetrievalDocumentModel(
            id="d1", project_id="p1", source_type="chapter", source_id="ch1", source_version="v1",
            title="第二章", status="active", authority_level="canon", chapter_from=2, chapter_to=2,
            created_at=now, updated_at=now,
        ))
        await session.flush()
        for index, content in enumerate(["李雷走进山谷", "烛龙现身，李雷迎战", "二人定下三日之约", "韩梅梅在远处观望"]):
            session.add(RetrievalChunkModel(
                id=f"ck{index}", project_id="p1", document_id="d1", chunk_index=index,
                content=content, summary="", metadata_json='{"chapter_no": 2}', search_text=content,
                embedding=None, embedding_model="none", embedding_version="v1",
                token_count=10, content_hash=f"h{index}", status="active", created_at=now, updated_at=now,
            ))
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_vector_leg_empty_on_sqlite(session_factory):
    async with session_factory() as session:
        assert await vector_leg(session, project_id="p1", query_vector=[0.1, 0.2], identity="local/x") == []


@pytest.mark.asyncio
async def test_keyword_leg_entity_boost_ranks_first(session_factory):
    async with session_factory() as session:
        hits = await keyword_leg(session, project_id="p1", query="李雷 迎战", entities=["烛龙"])
    assert hits[0].chunk_id == "ck1"  # term hits + 烛龙 entity boost
    assert {hit.chunk_id for hit in hits} <= {"ck0", "ck1", "ck2", "ck3"}
    # Empty query terms and no entities -> no hits.
    async with session_factory() as session:
        assert await keyword_leg(session, project_id="p1", query="", entities=[]) == []


@pytest.mark.asyncio
async def test_keyword_leg_identity_filter_excludes_foreign_chunks(session_factory):
    """Mid-switch mixes (or engine-off residue) must not leak into the RRF:
    with an identity given, only chunks written by that engine are read."""
    async with session_factory() as session:
        # Seeded chunks carry embedding_model="none" (engine-off identity).
        hits = await keyword_leg(session, project_id="p1", query="李雷 迎战", entities=["烛龙"], identity="local/BAAI/bge-m3")
        assert hits == []
        # Matching identity keeps the leg working.
        hits = await keyword_leg(session, project_id="p1", query="李雷 迎战", entities=["烛龙"], identity="none")
        assert hits[0].chunk_id == "ck1"
        # No identity -> legacy unfiltered behavior.
        hits = await keyword_leg(session, project_id="p1", query="李雷 迎战", entities=["烛龙"])
        assert hits[0].chunk_id == "ck1"


@pytest.mark.asyncio
async def test_neighbor_expansion(session_factory):
    async with session_factory() as session:
        blocks = await expand_neighbors(session, [("ck1", 0.5)])
    ids = {block.chunk_id for block in blocks}
    assert "ck1" in ids and "ck0" in ids and "ck2" in ids  # ±1 neighbors
    assert "ck3" not in ids
    hit = next(block for block in blocks if block.chunk_id == "ck1")
    assert hit.chapter_no == 2 and hit.document_title == "第二章" and hit.score == 0.5
    expanded = [block for block in blocks if block.expanded]
    assert {block.chunk_id for block in expanded} == {"ck0", "ck2"}


@pytest.mark.asyncio
async def test_neighbor_expansion_respects_cap(session_factory):
    async with session_factory() as session:
        blocks = await expand_neighbors(session, [("ck1", 0.5)], max_blocks=2)
    assert len(blocks) == 2


# -- authority layering (phase-2 item 9) ----------------------------------------


async def _add_derived_recap_doc(session_factory, *, chunk_status: str = "active") -> None:
    """A volume-recap document (authority_level="derived") with one chunk."""
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(RetrievalDocumentModel(
            id="d-recap", project_id="p1", source_type="recap_rollup", source_id="roll-1",
            source_version="hash-1", title="卷梗概（第1-3章）", status="active",
            authority_level="derived", chapter_from=1, chapter_to=3,
            created_at=now, updated_at=now,
        ))
        await session.flush()
        session.add(RetrievalChunkModel(
            id="ck-recap", project_id="p1", document_id="d-recap", chunk_index=0,
            content="李雷迎战烛龙的卷梗概", summary="", metadata_json="{}",
            search_text="李雷迎战烛龙的卷梗概",
            embedding=None, embedding_model="none", embedding_version="v1",
            token_count=8, content_hash="hR", status=chunk_status, created_at=now, updated_at=now,
        ))
        await session.commit()


@pytest.mark.asyncio
async def test_authority_fusion_never_ranks_derived_above_canon(session_factory):
    """Raw RRF puts the derived recap first (rank 1 in both legs); authority
    layering must still keep every canon chunk above it — a derived recap
    never盖过 the原文 it compresses."""
    await _add_derived_recap_doc(session_factory)
    legs = [
        [_Hit("ck-recap", 10.0), _Hit("ck1", 9.0)],
        [_Hit("ck-recap", 8.0), _Hit("ck0", 7.0)],
    ]
    # Sanity: plain RRF would rank the recap first.
    assert rrf_fuse(legs, top=3)[0][0] == "ck-recap"
    async with session_factory() as session:
        fused = await rrf_fuse_with_authority(session, legs, top=3)
    assert [chunk_id for chunk_id, _ in fused] == ["ck1", "ck0", "ck-recap"]


@pytest.mark.asyncio
async def test_superseded_recap_chunk_is_not_retrieved(session_factory):
    """Stale 梗概的索引块被 supersede 后（失效标记同事务）检索腿绝不返回；
    supersede 前同一块是可检索的（对照）。"""
    await _add_derived_recap_doc(session_factory)
    async with session_factory() as session:
        hits = await keyword_leg(session, project_id="p1", query="卷梗概", entities=[])
    assert [hit.chunk_id for hit in hits] == ["ck-recap"]

    async with session_factory() as session:
        from sqlalchemy import update as sa_update

        await session.execute(
            sa_update(RetrievalChunkModel)
            .where(RetrievalChunkModel.id == "ck-recap")
            .values(status="superseded")
        )
        await session.commit()
    async with session_factory() as session:
        hits = await keyword_leg(session, project_id="p1", query="卷梗概", entities=[])
    assert all(hit.chunk_id != "ck-recap" for hit in hits)


@pytest.mark.asyncio
async def test_recap_evidence_block_labelled_by_title_not_chapter(session_factory):
    """梗概证据块按文档标题标注（卷梗概…），不冒充某一章原文。"""
    await _add_derived_recap_doc(session_factory)
    async with session_factory() as session:
        blocks = await expand_neighbors(session, [("ck-recap", 0.5)])
    assert len(blocks) == 1
    assert blocks[0].chapter_no is None
    assert blocks[0].document_title == "卷梗概（第1-3章）"


# -- document-status defense in depth (stale-收口 race window) --------------------


async def _set_document_state(session_factory, *, status: str, deleted: bool = False) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        from sqlalchemy import update as sa_update

        await session.execute(
            sa_update(RetrievalDocumentModel)
            .where(RetrievalDocumentModel.id == "d1")
            .values(status=status, deleted_at=now if deleted else None)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_keyword_leg_skips_chunks_under_inactive_document(session_factory):
    """stale 收口把 document 置 inactive 后，其下仍是 active 的 chunks
    （竞态窗口内新写入的）绝不回渗为关键词腿证据。"""
    async with session_factory() as session:
        hits = await keyword_leg(session, project_id="p1", query="李雷 迎战", entities=[])
    assert hits  # control: active document is searchable

    await _set_document_state(session_factory, status="inactive")
    async with session_factory() as session:
        assert await keyword_leg(session, project_id="p1", query="李雷 迎战", entities=[]) == []

    await _set_document_state(session_factory, status="active", deleted=True)
    async with session_factory() as session:
        assert await keyword_leg(session, project_id="p1", query="李雷 迎战", entities=[]) == []


@pytest.mark.asyncio
async def test_neighbor_expansion_skips_inactive_document(session_factory):
    """邻居扩展同样尊重 document 状态：inactive 文档下的相邻块不被拉入。"""
    async with session_factory() as session:
        blocks = await expand_neighbors(session, [("ck1", 0.5)])
    assert {block.chunk_id for block in blocks} == {"ck0", "ck1", "ck2"}  # control

    await _set_document_state(session_factory, status="inactive")
    async with session_factory() as session:
        blocks = await expand_neighbors(session, [("ck1", 0.5)])
    # The fused hit itself still materializes (the caller ranked it), but no
    # neighbor leaks out of the inactivated document.
    assert {block.chunk_id for block in blocks} == {"ck1"}


def test_pg_legs_join_document_status_filter_static():
    """PG-only SQL branches (unreachable on sqlite) must carry the same
    document-status defense: join retrieval_documents, require
    d.status='active' AND d.deleted_at IS NULL."""
    import inspect

    from proseforge.application.retrieval import search

    vector_source = inspect.getsource(search.vector_leg)
    assert "JOIN retrieval_documents d ON d.id = c.document_id" in vector_source
    assert "d.status = 'active' AND d.deleted_at IS NULL" in vector_source
    keyword_source = inspect.getsource(search.keyword_leg)
    assert "JOIN retrieval_documents d ON d.id = c.document_id" in keyword_source
    assert "d.status = 'active' AND d.deleted_at IS NULL" in keyword_source
