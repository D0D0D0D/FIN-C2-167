"""AGENTIC STAR Marketplace entry-adapter boundary tests."""

from __future__ import annotations

import json
import sys
from types import ModuleType

import pytest
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import MissingSecret, NullProvider
from framework.secrets.context import bound_secrets

import cli
from src.graph.graph import ProspectusDisclosureReviewGraph


def _set_required_environment(monkeypatch, suffix: str = "one") -> dict[str, str]:
    values = {key: f"test-{suffix}-{key.lower()}" for key in cli._REQUIRED_SECRET_KEYS}
    # The startup gate requires a bare Azure resource endpoint, so
    # this value cannot be a placeholder like the other secrets.
    values["AZURE_OPENAI_ENDPOINT"] = f"https://{suffix}.services.ai.azure.example"
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def _contains(value, needle: str) -> bool:
    if isinstance(value, dict):
        return any(_contains(key, needle) or _contains(item, needle) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains(item, needle) for item in value)
    return needle in value if isinstance(value, str) else False


def test_marketplace_discards_runner_context_forces_read_mode_and_external_trust(monkeypatch):
    values = _set_required_environment(monkeypatch)
    sentinel = "UNSCANNED-CONVERSATION-HISTORY-167"
    captured = {}

    def parent_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        captured.update(
            user_input=user_input,
            session_id=session_id,
            ctx=ctx,
            input_context=input_context,
        )
        return {"status": "success"}

    monkeypatch.setattr(ProspectusDisclosureReviewGraph, "invoke", parent_invoke)
    original_ctx = InvocationContext(
        caller_id="marketplace-user",
        caller_trust_level=TrustLevel.INTERNAL,
        hitl_allowed=True,
    )
    result = cli.MarketplaceGraph().invoke(
        json.dumps({"mode": "ingest", "rows": [sentinel]}),
        session_id="runner-session",
        ctx=original_ctx,
        input_context={
            "conversation_history": [{"role": "user", "content": sentinel}],
            "company_id": "attacker-company",
            "mode": "feedback",
        },
    )

    expected_scope = {
        "company_id": values["STG_DEFAULT_COMPANY_ID"],
        "issuance_id": values["STG_DEFAULT_ISSUANCE_ID"],
    }
    envelope = json.loads(captured["user_input"])
    assert result == {"status": "success"}
    assert captured["input_context"] == {
        "mode": "invoke",
        **expected_scope,
        "payload_ref": envelope["payload_ref"],
        "session_id": cli._MARKETPLACE_SESSION_ID,
    }
    assert not _contains(captured["input_context"], sentinel)
    assert captured["session_id"] == cli._MARKETPLACE_SESSION_ID
    assert envelope["mode"] == "invoke"
    assert captured["ctx"] is not original_ctx
    assert captured["ctx"].caller_trust_level is TrustLevel.VERIFIED_EXTERNAL
    assert captured["ctx"].hitl_allowed is False


def test_conversation_history_never_enters_real_graph_state(monkeypatch):
    _set_required_environment(monkeypatch)
    sentinel = "UNSCANNED-CONVERSATION-STATE-167"
    agent = cli.MarketplaceGraph()
    agent.compile()
    captured = {}

    def capture_state(initial_state, config=None):
        del config
        captured["initial"] = initial_state
        return {
            **initial_state,
            "formatted_output": {"mode": "invoke"},
            "status": "success",
        }

    agent._compiled.invoke = capture_state
    agent.invoke(
        "business risk_factors",
        ctx=InvocationContext(hitl_allowed=True),
        input_context={"conversation_history": [{"role": "user", "content": sentinel}]},
    )

    assert not _contains(captured["initial"], sentinel)
    assert captured["initial"]["caller_trust_level"] == TrustLevel.VERIFIED_EXTERNAL.value
    assert captured["initial"]["hitl_allowed"] is False


@pytest.mark.parametrize(
    "invalid_scope",
    [None, "", "   ", "company id", "company\tid", "company\nid", "company:id"],
)
def test_invalid_scope_fails_during_startup(monkeypatch, invalid_scope):
    _set_required_environment(monkeypatch)
    key = "STG_DEFAULT_COMPANY_ID"
    if invalid_scope is None:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, invalid_scope)

    with pytest.raises((MissingSecret, ValueError), match=key):
        cli.MarketplaceGraph()


@pytest.mark.parametrize("missing_key", cli._AZURE_OPENAI_SECRET_KEYS)
def test_compile_precedes_provision_and_missing_secret_fails_startup(monkeypatch, missing_key):
    _set_required_environment(monkeypatch)
    agent = cli.MarketplaceGraph()
    agent.compile()
    monkeypatch.delenv(missing_key)

    with pytest.raises(MissingSecret, match=missing_key):
        agent.provision_secrets(NullProvider())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    _set_required_environment(monkeypatch)
    agent = cli.MarketplaceGraph()
    agent.compile()
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "gate-test-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gate-test-deployment")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        agent.provision_secrets(NullProvider())


def test_allowlisted_provider_hides_unrelated_environment(monkeypatch):
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("UNRELATED_PLATFORM_SECRET", "must-not-be-visible")
    agent = cli.MarketplaceGraph()

    agent.provision_secrets(NullProvider())

    assert agent._secrets_provider.get("UNRELATED_PLATFORM_SECRET") is None
    assert isinstance(agent.config["llm"], cli._AzureMarketplaceLLM)


def test_azure_llm_resolves_fresh_credentials_without_caching_client(monkeypatch):
    configs = []

    class FakeAzureOpenAIClient:
        def __init__(self, config):
            configs.append(dict(config))

        def complete(self, messages):
            return {"content": "ok", "messages": messages}

        def stream(self, messages):
            del messages
            yield "ok"

        def bind_tools(self, tools):
            del tools
            return self

    azure_module = ModuleType("shared.services.llm.azure_openai_client")
    azure_module.AzureOpenAIClient = FakeAzureOpenAIClient
    monkeypatch.setitem(sys.modules, azure_module.__name__, azure_module)

    provider = cli._AllowlistedEnvironmentProvider()
    llm = cli._AzureMarketplaceLLM()
    assert vars(llm) == {}
    with bound_secrets(provider):
        first = _set_required_environment(monkeypatch, "first")
        llm.complete([{"role": "user", "content": "first"}])
        assert vars(llm) == {}
        second = _set_required_environment(monkeypatch, "second")
        llm.complete([{"role": "user", "content": "second"}])
        assert vars(llm) == {}

    assert configs == [
        {
            "api_key": first["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": first["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": first["AZURE_OPENAI_DEPLOYMENT"],
        },
        {
            "api_key": second["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": second["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": second["AZURE_OPENAI_DEPLOYMENT"],
        },
    ]


def test_marketplace_hitl_is_suppressed_through_real_graph(monkeypatch):
    _set_required_environment(monkeypatch)

    def valid_completion(self, messages):
        del self, messages
        return {
            "content": json.dumps(
                {
                    "unreflected_facts": [],
                    "review_questions": ["根拠資料との対応を確認してください。"],
                },
                ensure_ascii=False,
            )
        }

    monkeypatch.setattr(cli._AzureMarketplaceLLM, "complete", valid_completion)
    agent = cli.MarketplaceGraph()
    agent.compile()

    result = agent.invoke(
        "business risk_factors material_contracts",
        ctx=InvocationContext(thread_id="hitl", hitl_allowed=True),
    )

    assert result["status"] == "success"
    assert result["status"] != "awaiting_human"


def test_marketplace_never_exposes_awaiting_human(monkeypatch):
    _set_required_environment(monkeypatch)

    def hitl_sensitive_parent(self, user_input, session_id="", ctx=None, input_context=None):
        del self, user_input, session_id, input_context
        return {"status": "awaiting_human" if ctx.hitl_allowed else "success"}

    monkeypatch.setattr(ProspectusDisclosureReviewGraph, "invoke", hitl_sensitive_parent)
    result = cli.MarketplaceGraph().invoke(
        "business risk_factors",
        ctx=InvocationContext(hitl_allowed=True),
    )

    assert result["status"] == "success"


def test_main_passes_repo_config_to_marketplace_runner(monkeypatch):
    called = {}

    def fake_runner(agent_cls, **kwargs):
        called["agent_cls"] = agent_cls
        called.update(kwargs)

    monkeypatch.setattr(cli, "run_agent_marketplace", fake_runner)
    cli.main()

    assert called["agent_cls"] is cli.MarketplaceGraph
    assert called["agent_name"] == "FIN-C2-167"
    assert called["namespace"] == "fin"
    assert called["config"]["hitl"]["enabled"] is True
