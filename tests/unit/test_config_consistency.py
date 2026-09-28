from pathlib import Path

import yaml
from framework.schemas.trust_level import TrustLevel

from src.api.server import _build_agent
from src.entry_adapter import AuthContext, AuthSource


def test_hitl_enabled_matches_manifest_and_runtime_config() -> None:
    root = Path(__file__).parents[2]
    manifest = yaml.safe_load((root / "config/agent.yaml").read_text(encoding="utf-8"))
    runtime = yaml.safe_load((root / "config/config.yaml").read_text(encoding="utf-8"))
    assert manifest["hitl"]["enabled"] is runtime["hitl"]["enabled"] is True


def test_runtime_limits_are_wired_into_request_scoped_nodes() -> None:
    root = Path(__file__).parents[2]
    runtime = yaml.safe_load((root / "config/config.yaml").read_text(encoding="utf-8"))
    auth = AuthContext(
        trust=TrustLevel.VERIFIED_EXTERNAL,
        scope={"company_id": "company", "issuance_id": "issue"},
        principal={"caller_id": "reviewer"},
        auth_source=AuthSource.MIDDLEWARE,
    )

    graph = _build_agent(auth, session_id="session")
    pre_process = graph._nodes["pre_process"]
    main = graph._nodes["main"]

    assert pre_process._max_batch_records == runtime["ingest"]["max_batch_records"]
    assert pre_process._max_free_text_chars == runtime["ingest"]["max_free_text_chars"]
    assert main._fin_config["min_similarity"] == runtime["retrieval"]["min_similarity"]
    stage2 = main.get_subgraph()._nodes["stage2"]
    assert stage2._min_similarity == runtime["retrieval"]["min_similarity"]
