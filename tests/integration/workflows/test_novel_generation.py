import pytest

from proseforge.domain.ports.model_provider import GenerationEvent
from proseforge.workflows.novel_generation import (
    generate_chapter_content,
    run_writer_editor_loop,
)


class Writer:
    def __init__(self):
        self.request = None

    async def stream(self, request):
        self.request = request
        if request.metadata["role"] == "writer":
            yield GenerationEvent("content.delta", "A chapter.")
        elif request.metadata["role"] == "editor":
            yield GenerationEvent("content.delta", '{"status":"PASS","summary":"ok","issues":[],"preserve":[],"rewrite_scope":[]}')


@pytest.mark.asyncio
async def test_writer_stream_is_collected_without_empty_success():
    provider = Writer()
    result = await generate_chapter_content(provider, model="writer-model", project_title="Book", chapter_title="Opening")

    assert result == "A chapter."
    assert provider.request.metadata["role"] == "writer"


@pytest.mark.asyncio
async def test_empty_writer_stream_is_rejected():
    class Empty:
        async def stream(self, request):
            if False:
                yield GenerationEvent("content.delta", "")

    with pytest.raises(ValueError, match="empty chapter"):
        await generate_chapter_content(Empty(), model="writer-model", project_title="Book", chapter_title="Opening")


@pytest.mark.asyncio
async def test_writer_editor_loop_requires_structured_editor_pass():
    content, rounds, review = await run_writer_editor_loop(Writer(), writer_model="writer-model", editor_model="editor-model", project_title="Book", chapter_title="Opening")

    assert content == "A chapter."
    assert rounds == 0
    assert review["status"] == "PASS"


@pytest.mark.asyncio
async def test_writer_prompt_includes_compiled_story_context():
    provider = Writer()
    await generate_chapter_content(provider, model="writer-model", project_title="Book", chapter_title="Opening", context_text="Mira remembers the lighthouse.")

    assert "Mira remembers the lighthouse." in provider.request.input_blocks[0]["text"]
