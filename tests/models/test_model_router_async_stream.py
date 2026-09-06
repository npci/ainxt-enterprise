from collections.abc import AsyncIterator, Iterator

import anyio
import pytest

from models.model_router import CLAUDE_PRIMARY_MODEL, ModelRouter


class _ClaudeGateway:
    def generate(self, prompt: str, *, model: str) -> Iterator[str]:
        del prompt, model
        yield "ROUTING_OK"


async def _collect_stream(router: ModelRouter) -> list[str | dict[str, dict[str, str | int]]]:
    stream: AsyncIterator[str | dict[str, dict[str, str | int]]] = router.async_stream(
        "hello",
        model_hint="medium",
    )
    return [chunk async for chunk in stream]


def test_async_stream_reports_actual_claude_fallback_when_openai_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: Auto resolved to the medium/OpenAI tier, but only Claude is available.
    router = ModelRouter()
    monkeypatch.setattr(router, "_get_openai", lambda: None)
    monkeypatch.setattr(router, "_get_claude", lambda: _ClaudeGateway())

    # When: the async web-chat stream falls back through its sync worker thread.
    chunks = anyio.run(_collect_stream, router)
    stream_meta = next(
        chunk["__stream_meta__"]
        for chunk in chunks
        if isinstance(chunk, dict) and "__stream_meta__" in chunk
    )

    # Then: metadata identifies the provider that produced the answer, not the failed route.
    assert "".join(chunk for chunk in chunks if isinstance(chunk, str)) == "ROUTING_OK"
    assert stream_meta["model_id"] == CLAUDE_PRIMARY_MODEL
    assert "[fallback]" in str(stream_meta["model_label"])
