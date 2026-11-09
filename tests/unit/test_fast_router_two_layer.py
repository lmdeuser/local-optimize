import pytest
from unittest.mock import patch

import fast_router
from fast_router import AdaptiveModelRouter, preload_local_model

pytestmark = pytest.mark.unit


def test_preload_local_model_uses_shared_transport():
    with patch("fast_router.query_local_ollama", return_value="OK") as generate:
        assert preload_local_model(fast_router.OLLAMA_MODEL) is True
    assert generate.call_args.kwargs["model"] == fast_router.OLLAMA_MODEL


def test_query_local_ollama_uses_shared_guarded_transport():
    with patch("local_inference.generate", return_value="Ответ") as generate:
        assert fast_router.query_local_ollama("Тест") == "Ответ"
    assert generate.call_args.args[1] == fast_router.OLLAMA_MODEL
    assert generate.call_args.kwargs["num_ctx"] == fast_router.OLLAMA_NUM_CTX


def test_layer_1_local_resolution_saves_cloud_tokens():
    router = AdaptiveModelRouter(confidence_threshold=0.5)

    with patch.object(fast_router, "query_local_ollama", return_value="Локальный ответ"):
        result = router.evaluate(
            query="Простой вопрос",
            context="Контекст фактов",
            rag_score=0.85,
        )

        assert result["decision"] == "respond"
        assert result["layer"] == "layer_1_local"
        assert result["response"] == "Локальный ответ"
        # On local resolution, optimized_context is None so cloud model spends 0 tokens
        assert result["optimized_context"] is None
        assert result["escalation_reason"] is None


def test_layer_2_cloud_escalation_compacts_context_to_save_tokens():
    router = AdaptiveModelRouter(confidence_threshold=0.8)

    # Bloated repeating context
    redundant_lines = ["Факт номер 1", "Факт номер 1", "Факт номер 2", "Факт номер 2"]
    bloated_context = "\n".join(redundant_lines * 50)

    result = router.evaluate(
        query="Сложная архитектурная задача",
        context=bloated_context,
        rag_score=0.4,
    )

    assert result["decision"] == "escalate"
    assert result["layer"] == "layer_2_cloud"
    assert result["response"] is None
    assert "ниже адаптивного порога" in result["escalation_reason"]
    assert "0.40" not in result["escalation_reason"]
    assert "0.80" not in result["escalation_reason"]
    assert "сложность=" not in result["escalation_reason"]

    # Retrieved RAG text and scores must not be attached to the cloud-escalation signal.
    assert result["optimized_context"] is None


def test_layer_2_cloud_escalation_on_local_model_insufficient_data():
    router = AdaptiveModelRouter(confidence_threshold=0.5)

    with patch.object(fast_router, "query_local_ollama", return_value="НЕДОСТАТОЧНО ДАННЫХ для ответа"):
        result = router.evaluate(
            query="Специфичный вопрос",
            context="Краткий контекст",
            rag_score=0.9,
        )

        assert result["decision"] == "escalate"
        assert result["layer"] == "layer_2_cloud"
        assert result["response"] is None
        assert "INSUFFICIENT DATA" in result["escalation_reason"]
        assert result["optimized_context"] is None


def test_generated_memory_is_not_accepted_as_a_primary_source():
    router = AdaptiveModelRouter(confidence_threshold=0.5)
    generated_only = (
        "[Evidence 1 | chunk_id=56 | source_uri=agent_generated "
        "| source_type=llm_output | validation_status=validated "
        "| source_is_primary=false]\nA generated summary."
    )

    result = router.evaluate(
        query="Что указано в проекте?",
        context=generated_only,
        rag_score=0.95,
    )

    assert result["decision"] == "escalate"
    assert result["evidence_gate_failed"] is True
    assert "первоисточника" in result["escalation_reason"]


def test_answer_cannot_cite_generated_memory_when_primary_evidence_exists():
    router = AdaptiveModelRouter(confidence_threshold=0.5)
    context = (
        "[Evidence 1 | chunk_id=55 | source_uri=fixture://document "
        "| source_type=document | validation_status=unverified "
        "| source_is_primary=true]\nPrimary source.\n\n"
        "[Evidence 2 | chunk_id=56 | source_uri=agent_generated "
        "| source_type=llm_output | validation_status=validated "
        "| source_is_primary=false]\nGenerated summary."
    )

    with patch.object(fast_router, "query_local_ollama", return_value="Claim [Evidence 2]."):
        result = router.evaluate(
            query="Что известно?",
            context=context,
            rag_score=0.95,
        )

    assert result["decision"] == "escalate"
    assert "проверяемых ссылок" in result["escalation_reason"]


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ("Сервер проверяет запрос.", "missing_citation"),
        ("Сервер проверяет запрос [Evidence 9].", "unknown_evidence_id"),
        ("Сервер проверяет запрос [Evidence 2].", "non_primary_source"),
        ("Сервер проверяет запрос [Evidence 1 | chunk_id=55].", "malformed_citation"),
    ],
)
def test_privacy_fallback_exposes_citation_validation_reason(answer, reason):
    router = AdaptiveModelRouter(confidence_threshold=0.5)
    context = (
        "[Evidence 1 | chunk_id=55 | source_uri=fixture://document "
        "| source_type=document | validation_status=validated "
        "| source_is_primary=true]\nPrimary source.\n\n"
        "[Evidence 2 | chunk_id=56 | source_uri=agent_generated "
        "| source_type=llm_output | validation_status=validated "
        "| source_is_primary=false]\nGenerated summary."
    )

    with patch.object(fast_router, "query_local_ollama", return_value=answer):
        result = router.evaluate(
            query="Что делает сервер?",
            context=context,
            rag_score=0.95,
            privacy_mode=True,
        )

    assert result["response_kind"] == "policy_fallback"
    assert result["answer_diagnostics"]["stage"] == "citation_validation"
    assert result["answer_diagnostics"]["reason"] == reason


def test_privacy_fallback_reports_model_insufficient_data():
    router = AdaptiveModelRouter(confidence_threshold=0.5)
    context = (
        "[Evidence 1 | chunk_id=55 | source_uri=fixture://document "
        "| source_type=document | validation_status=validated "
        "| source_is_primary=true]\nPrimary source."
    )

    with patch.object(fast_router, "query_local_ollama", return_value="НЕДОСТАТОЧНО ДАННЫХ"):
        result = router.evaluate(
            query="Что делает сервер?",
            context=context,
            rag_score=0.95,
            privacy_mode=True,
        )

    assert result["response_kind"] == "policy_fallback"
    assert result["answer_diagnostics"] == {
        "stage": "local_generation",
        "status": "refused",
        "reason": "model_insufficient_data",
    }
