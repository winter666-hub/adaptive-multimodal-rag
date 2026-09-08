from __future__ import annotations

from unittest.mock import Mock

from furiosa_rag.config import ModelEndpoint
from furiosa_rag.models import Chunk, RetrievedChunk
from furiosa_rag.router import QueryRoute, RetrievalAwareAdaptiveRouter


def _source(text: str, page: int = 2) -> RetrievedChunk:
    return RetrievedChunk(Chunk(f"page-{page}-chunk-1", page, text), 0.8, 0.9)


def _router(output: str) -> tuple[RetrievalAwareAdaptiveRouter, Mock]:
    client = Mock()
    client.post_json.return_value = {
        "choices": [{"message": {"content": output}}]
    }
    endpoint = ModelEndpoint("llm", "http://llm.example/v1", "router-model")
    return RetrievalAwareAdaptiveRouter(endpoint, client), client


def test_retrieval_aware_router_sends_question_and_top_three_evidence() -> None:
    router, client = _router("TEXT_ONLY")
    evidence = tuple(_source(f"evidence-{index}", index) for index in range(1, 5))

    decision = router.route("What is the value?", evidence)

    payload = client.post_json.call_args.args[2]
    user_prompt = payload["messages"][1]["content"]
    assert "What is the value?" in user_prompt
    assert "evidence-1" in user_prompt
    assert "evidence-2" in user_prompt
    assert "evidence-3" in user_prompt
    assert "evidence-4" not in user_prompt
    assert "page 1" in user_prompt
    assert payload["temperature"] == 0
    assert payload["max_tokens"] == 4
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert decision.route is QueryRoute.TEXT_ONLY


def test_retrieval_aware_router_can_require_visual_evidence() -> None:
    router, _ = _router("VISUAL_REQUIRED")

    decision = router.route(
        "What value is shown?",
        (_source("The result is shown in Figure 3, but no value is extracted."),),
    )

    assert decision.route is QueryRoute.VISUAL_REQUIRED
    assert decision.used_llm_router is True
