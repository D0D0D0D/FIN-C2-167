"""AGENTIC STAR Marketplace entry adapter for FIN-C2-167."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 has no Marketplace extra
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


try:
    from shared.secrets.env_provider import EnvProvider
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.secrets.env_provider":
        raise

    class EnvProvider(SecretProvider):  # type: ignore[no-redef]
        """Minimal process-environment provider used by pre-1.0.2 wheels."""

        def __init__(self, namespace: str, agent_name: str) -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from shared.services.llm.base_llm import BaseLLM

from src.entry_adapter import SCOPE_KEYS, STG_DEFAULT_SCOPE_ENVS
from src.graph.graph import ProspectusDisclosureReviewGraph
from src.payload_store import PayloadStore
from src.services.kb_service import KBService

_NAMESPACE = "fin"
_AGENT_NAME = "FIN-C2-167"
_PROVIDER_AGENT_NAME = "fin_c2_167"
_MARKETPLACE_SESSION_ID = "a-star-marketplace"

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_SCOPE_ENV_KEYS = tuple(STG_DEFAULT_SCOPE_ENVS[key] for key in SCOPE_KEYS)
_REQUIRED_SECRET_KEYS = (*_AZURE_OPENAI_SECRET_KEYS, *_SCOPE_ENV_KEYS)
_ALLOWED_SECRET_KEYS = frozenset(_REQUIRED_SECRET_KEYS)


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Expose only this single-agent container's declared environment values."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _ALLOWED_SECRET_KEYS:
            return default
        return super().get(key, default)


def _validate_scope_declaration() -> None:
    if set(STG_DEFAULT_SCOPE_ENVS) != set(SCOPE_KEYS):
        raise RuntimeError("STG_DEFAULT_SCOPE_ENVS keys must exactly match SCOPE_KEYS")
    for env_name in STG_DEFAULT_SCOPE_ENVS.values():
        if (
            not isinstance(env_name, str)
            or not env_name
            or env_name != env_name.strip()
            or any(character.isspace() for character in env_name)
        ):
            raise RuntimeError("STG_DEFAULT_SCOPE_ENVS contains an invalid environment name")


def _scope_from_environment(provider: SecretProvider) -> dict[str, str]:
    """Resolve the declared scope without repairing deployment mistakes."""

    _validate_scope_declaration()
    scope: dict[str, str] = {}
    for key in SCOPE_KEYS:
        env_name = STG_DEFAULT_SCOPE_ENVS[key]
        value = provider.require(env_name)
        if not value or any(character.isspace() for character in value) or ":" in value:
            raise ValueError(
                f"Marketplace scope environment variable {env_name!r} "
                "must be non-empty and contain neither whitespace nor ':'"
            )
        scope[key] = value
    return scope


def _require_clean_secret(provider: SecretProvider, key: str) -> str:
    value = provider.require(key)
    if not value.strip() or value != value.strip() or "\n" in value or "\r" in value:
        raise ValueError(f"Marketplace secret {key!r} must be a clean non-empty string")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean_secret(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


class _AzureMarketplaceLLM(BaseLLM):  # type: ignore[misc]
    """Create an Azure client per operation and retain no connection values."""

    def __init__(self, *, tools: tuple[Any, ...] = ()) -> None:
        if tools:
            self._tools = tools

    def _new_client(self) -> BaseLLM:
        provider = current_secrets()
        values = {key: _require_clean_secret(provider, key) for key in _AZURE_OPENAI_SECRET_KEYS}

        # Azure support is an optional SDK extra and is deliberately the only
        # invocation-time import. Framework/security imports stay module-level.
        from shared.services.llm.azure_openai_client import AzureOpenAIClient

        client: BaseLLM = AzureOpenAIClient(
            {
                "api_key": values["AZURE_OPENAI_API_KEY"],
                "azure_endpoint": values["AZURE_OPENAI_ENDPOINT"],
                "azure_deployment": values["AZURE_OPENAI_DEPLOYMENT"],
            }
        )
        tools = getattr(self, "_tools", ())
        return client.bind_tools(list(tools)) if tools else client

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._new_client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._new_client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> "_AzureMarketplaceLLM":
        return type(self)(tools=tuple(tools))


class MarketplaceGraph(ProspectusDisclosureReviewGraph):
    """Marketplace-only read path, scope, HITL, and Azure boundary."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # register_nodes() captures scope, session, payload store, and LLM at
        # compile time. Resolve credential-free dependencies before that step;
        # the runner provisions and validates secrets only after compile().
        scope = _scope_from_environment(_AllowlistedEnvironmentProvider())
        source = dict(config or {})
        payload_config = source.get("payload_store", {})
        ttl_seconds = payload_config.get("ttl_seconds", 300) if isinstance(payload_config, dict) else 300
        retrieval = source.get("retrieval", {})
        retrieval = retrieval if isinstance(retrieval, dict) else {}
        marketplace_config = {
            **source,
            "llm": _AzureMarketplaceLLM(),
            "kb_service": KBService(db=None),
            "payload_store": PayloadStore(scope_keys=SCOPE_KEYS, ttl_seconds=ttl_seconds),
            "trusted_scope": scope,
            "session_id": _MARKETPLACE_SESSION_ID,
            "top_k": retrieval.get("top_k_internal_sources", 8),
            "min_similarity": retrieval.get("min_similarity", 0.55),
        }
        super().__init__(config=marketplace_config)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Runner conversation history is untrusted free text outside this AG's
        # S-2 channel. Rebuild the adapter-owned context from scratch.
        del input_context, session_id
        if not isinstance(user_input, str):
            raise TypeError("Marketplace input must be one plain string")

        scope = _scope_from_environment(_AllowlistedEnvironmentProvider())
        payload_store = cast(PayloadStore, self.config["payload_store"])
        payload_ref = payload_store.put(
            user_input,
            scope=scope,
            session_id=_MARKETPLACE_SESSION_ID,
        )
        envelope = json.dumps(
            {
                "mode": "invoke",
                **scope,
                "payload_ref": payload_ref,
                "session_id": _MARKETPLACE_SESSION_ID,
            },
            ensure_ascii=False,
        )
        marketplace_ctx = (
            replace(
                ctx,
                session_id=_MARKETPLACE_SESSION_ID,
                caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
                hitl_allowed=False,
            )
            if ctx is not None
            else InvocationContext(
                session_id=_MARKETPLACE_SESSION_ID,
                caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
                hitl_allowed=False,
            )
        )
        try:
            return cast(
                dict[str, Any],
                super().invoke(
                    envelope,
                    session_id=_MARKETPLACE_SESSION_ID,
                    ctx=marketplace_ctx,
                    input_context={
                        "mode": "invoke",
                        **scope,
                        "payload_ref": payload_ref,
                        "session_id": _MARKETPLACE_SESSION_ID,
                    },
                ),
            )
        finally:
            payload_store.delete(
                payload_ref,
                scope=scope,
                session_id=_MARKETPLACE_SESSION_ID,
            )

    def provision_secrets(self, provider: SecretProvider) -> None:
        # Ignore the runner's potentially broader/dotenv-capable provider.
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _AZURE_OPENAI_SECRET_KEYS:
            _require_clean_secret(environment_provider, key)
        _scope_from_environment(environment_provider)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=load_agent_config(Path(__file__).resolve().parent),
    )


if __name__ == "__main__":
    main()
