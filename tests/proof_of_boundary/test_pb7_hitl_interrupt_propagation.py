"""PB-7: D6 interrupt signal is not swallowed; false guard skips it."""

import pathlib
import warnings

import pytest
from framework.schemas.trust_level import TrustLevel
from langgraph.errors import GraphInterrupt

import src.nodes.hitl_review_node as module
from src.nodes.hitl_review_node import HitlReviewNode


# ---------------------------------------------------------------------------
# Scaffold contract: PB-7 exposes `_CONFIG_PATH` and `_hitl_enabled()` so a
# template can be told apart as HITL or non-HITL.
#
# This template declares HITL in config/config.yaml rather than at the top level
# of the manifest, so `_hitl_enabled()` reports False here and the tests below
# are deliberately NOT gated on it -- this agent is HITL-enabled and its PB-7
# coverage must always run. Reconciling the two manifest shapes would start
# running PB-7 where it is currently skipped, so it needs its own blast-radius
# assessment and is out of scope here.
# ---------------------------------------------------------------------------

_CONFIG_PATH = pathlib.Path(__file__).parents[2] / "config" / "config.yaml"


def _hitl_enabled() -> bool:
    """Return True when config/config.yaml declares hitl.enabled: true.

    An absent or unreadable config.yaml warns instead of skipping silently —
    either case is ambiguous (never shipped / broken
    vs. genuinely non-HITL) and should not look like a clean auto-waiver.
    """
    if not _CONFIG_PATH.exists():
        warnings.warn(
            f"{_CONFIG_PATH} not found — PB-7 skipped without verifying hitl.enabled. "
            "If this template calls interrupt(), ship config/config.yaml before release.",
            stacklevel=2,
        )
        return False
    try:
        import yaml  # pyyaml==6.0.1 — pinned in scaffold/pyproject.toml dependencies

        data = yaml.safe_load(_CONFIG_PATH.read_text())
    except Exception as exc:
        warnings.warn(
            f"{_CONFIG_PATH} could not be read as YAML ({exc}) — PB-7 skipped without "
            "verifying hitl.enabled. If this template calls interrupt(), fix config/config.yaml "
            "before release.",
            stacklevel=2,
        )
        return False
    hitl = (data or {}).get("hitl", {}) if isinstance(data, dict) else None
    if not isinstance(hitl, dict):
        warnings.warn(
            f"{_CONFIG_PATH} does not have the expected 'hitl:' mapping shape — PB-7 skipped "
            "without verifying hitl.enabled. If this template calls interrupt(), fix "
            "config/config.yaml before release.",
            stacklevel=2,
        )
        return False
    return bool(hitl.get("enabled", False))


pytestmark = pytest.mark.skipif(
    not _hitl_enabled(),
    reason="config/config.yaml does not set hitl.enabled: true — PB-7 not applicable",
)

def _state(**updates):
    value = {
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "correlation_id": "pb7",
        "diff_draft": {
            "unreflected_facts": [
                {
                    "source_id": "source_1",
                    "candidate": "社内資料との反映要否を確認してください。",
                    "section": "risk_factors",
                }
            ],
            "review_questions": ["記載内容を確認してください。"],
        },
        "retrieved_records": [{"source_id": "source_1"}],
        "cited_source_ids": ["source_1"],
        "evidence_map": [
            {
                "claim_id": "claim_1",
                "claim_text": "反映要否の確認",
                "source_ids": ["source_1"],
                "asserted_attrs": {},
            }
        ],
        "hitl_allowed": True,
    }
    value.update(updates)
    return value


def test_pb7_hitl_interrupt_propagates(monkeypatch) -> None:
    def raises(_payload):
        raise GraphInterrupt(())

    monkeypatch.setattr(module, "interrupt", raises)
    with pytest.raises(GraphInterrupt):
        HitlReviewNode()(_state())


def test_pb7_hitl_allowed_false_skips_interrupt(monkeypatch) -> None:
    monkeypatch.setattr(module, "interrupt", lambda _payload: pytest.fail("interrupt must not run"))
    result = HitlReviewNode()(_state(hitl_allowed=False))
    assert result["status"] == "success"
    assert result["hitl_draft"] == _state()["diff_draft"]


@pytest.mark.parametrize(
    "draft,state_update",
    [
        (
            {
                "unreflected_facts": [
                    {
                        "source_id": "source_1",
                        "candidate": "開示は適正。",
                        "section": "risk_factors",
                    }
                ],
                "review_questions": ["記載内容を確認してください。"],
            },
            {},
        ),
        (
            {
                "unreflected_facts": [
                    {
                        "source_id": "source_1",
                        "candidate": "原文の連絡先 person@example.com",
                        "section": "risk_factors",
                    }
                ],
                "review_questions": ["記載内容を確認してください。"],
            },
            {},
        ),
        (
            {
                "unreflected_facts": [
                    {
                        "source_id": "unknown_source",
                        "candidate": "社内資料との反映要否を確認してください。",
                        "section": "risk_factors",
                    }
                ],
                "review_questions": ["記載内容を確認してください。"],
            },
            {},
        ),
    ],
    ids=("assertive-verdict", "raw-pii", "unverified-source-id"),
)
def test_pb7_hitl_draft_is_gated_before_interrupt(monkeypatch, draft, state_update) -> None:
    monkeypatch.setattr(module, "interrupt", lambda _payload: pytest.fail("unsafe draft reached interrupt"))
    result = HitlReviewNode()(_state(diff_draft=draft, **state_update))
    assert result["status"] == "error"
    assert result["error_log"]
