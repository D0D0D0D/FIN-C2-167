"""Focused GraphNode boundary tests (business graph is integration-tested)."""

from framework.schemas.agent_status import AgentStatus
from src.nodes.main_node import DisclosureDiffWorkflowGraphNode, MainNode


def test_main_node_is_the_fin_graphnode_contract() -> None:
    assert MainNode is DisclosureDiffWorkflowGraphNode
    assert MainNode.propagate_hitl is True


def test_main_node_passes_upstream_error_without_subgraph() -> None:
    node = MainNode()
    result = node.execute({"status": AgentStatus.ERROR.value})
    assert result == {}


def test_main_node_rejects_missing_validated_envelope() -> None:
    node = MainNode()
    result = node.execute({"correlation_id": "unit", "mode": "invoke"})
    assert result["status"] == AgentStatus.ERROR.value
    assert "validated_input" in result["error_log"][0]
