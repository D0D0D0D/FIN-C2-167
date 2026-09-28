"""The prompt and validator share one machine-readable LLM response contract."""

from __future__ import annotations

import json

import pytest
from framework.errors import SecurityViolationError

from src.nodes.invoke_stage_nodes import Stage3DraftNode, _project_draft, _prompt, _response_contract


def _example_from_schema(schema: dict) -> object:
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema["type"]
    if kind == "object":
        return {name: _example_from_schema(field) for name, field in schema["properties"].items()}
    if kind == "array":
        return [_example_from_schema(schema["items"])]
    if kind == "string":
        return "source_1"
    raise AssertionError(f"test generator does not support declared type: {kind}")


def test_declared_contract_mechanically_builds_a_response_accepted_by_validator() -> None:
    generated = _example_from_schema(_response_contract())
    projected = _project_draft(generated)
    assert projected["unreflected_facts"] and projected["review_questions"]
    prompt = _prompt({"structured_draft": [], "retrieved_records": [{"source_id": "source_1"}]})
    encoded_contract = prompt.split("RESPONSE_CONTRACT:\n", 1)[1].split("\nUNTRUSTED_DATA:\n", 1)[0]
    assert json.loads(encoded_contract) == _response_contract()

    class ContractDrivenLlm:
        def complete(self, _messages):
            return {"content": json.dumps(generated)}

    stage_result = Stage3DraftNode(ContractDrivenLlm()).execute(
        {"retrieved_records": [{"source_id": "source_1"}]}
    )
    assert stage_result["status"] == "success" and stage_result["diff_draft"] == projected


@pytest.mark.parametrize(
    "invalid",
    [
        {"unreflected_facts": [{"source_id": "source_1", "candidate": {"raw_document_text": "secret"}}],
         "review_questions": ["question"]},
        {"unreflected_facts": [{"source_id": "source_1", "candidate": "candidate", "raw": "secret"}],
         "review_questions": ["question"]},
        {"findings": [], "questions": []},
    ],
)
def test_typed_projection_rejects_wrong_types_and_undeclared_keys(invalid: dict) -> None:
    with pytest.raises(SecurityViolationError) as caught:
        _project_draft(invalid)
    assert "secret" not in str(caught.value)


class _ProviderFailure:
    def complete(self, _messages):
        raise RuntimeError("provider-secret-marker")


def test_stage3_preserves_stable_llm_error_without_provider_text() -> None:
    result = Stage3DraftNode(_ProviderFailure()).execute({"retrieved_records": []})
    assert result["status"] == "error" and result["error_code"] == "llm_call_failed"
    assert "provider-secret-marker" not in json.dumps(result)
