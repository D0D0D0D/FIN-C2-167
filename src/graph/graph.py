"""Outer fixed AgentBaseGraph for FIN-C2-167."""

from typing import Any

from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.main_node import DisclosureDiffWorkflowGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State


class ProspectusDisclosureReviewGraph(AgentBaseGraph):
    @property
    def name(self) -> str:
        return "Corporate Bond Prospectus Disclosure-Control Diff Review Agent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()
        ingest = self.config.get("ingest", {})
        self._nodes["pre_process"] = PreProcessNode(
            payload_store=self.config.get("payload_store"),
            max_file_bytes=ingest.get("max_file_bytes", 10_485_760),
            max_batch_records=ingest.get("max_batch_records", 500),
            max_free_text_chars=ingest.get("max_free_text_chars", 20_000),
            trusted_scope=self.config.get("trusted_scope"),
            session_id=self.config.get("session_id"),
        )
        self._nodes["main"] = DisclosureDiffWorkflowGraphNode(
            llm=self.config.get("llm"), kb_service=self.config.get("kb_service"),
            payload_store=self.config.get("payload_store"), top_k=self.config.get("top_k", 8),
            min_similarity=self.config.get("min_similarity", 0.55),
            trusted_scope=self.config.get("trusted_scope"), session_id=self.config.get("session_id"),
        )
        self._nodes["post_process"] = PostProcessNode()

    def get_output(self, state: State) -> dict[str, Any]:
        output = {
            "output": state.get("formatted_output") or state.get("result"),
            "status": state.get("status"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
        error_log = state.get("error_log")
        if isinstance(error_log, list):
            output["error_log"] = list(dict.fromkeys(error_log))
        if state.get("error_code") is not None:
            output["error_code"] = state.get("error_code")
        if state.get("error_type") is not None:
            output["error_type"] = state.get("error_type")
        return output
