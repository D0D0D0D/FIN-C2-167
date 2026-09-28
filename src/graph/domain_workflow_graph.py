"""Inner Cat2 workflow graph for the three FIN disclosure-review modes."""

from __future__ import annotations

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus
from framework.schemas.hitl_status import HitlStatus
from src.nodes.feedback_node import FeedbackValidateNode, KbUpdateReflectionNode
from src.nodes.hitl_review_node import HitlReviewNode
from src.nodes.ingest_nodes import IngestParseNode, IngestSanitizeNode, KbUpsertNode, TaxonomyTagNode
from src.nodes.invoke_stage_nodes import (
    Stage1DisclosureStructureNode,
    Stage2ScopedRetrievalNode,
    Stage3DraftNode,
    Stage4InconsistencyNode,
)
from src.nodes.mode_router_node import ModeRouterNode
from src.schemas.state import DisclosureDiffWorkflowState


class DisclosureDiffWorkflowGraph(BaseGraph):
    @property
    def name(self) -> str:
        return "disclosure_diff_workflow"

    @property
    def state_schema(self) -> type:
        return DisclosureDiffWorkflowState

    def _validate_config(self) -> None:
        pass

    def register_nodes(self) -> None:
        cfg = self.config
        self._nodes["mode_router"] = ModeRouterNode()
        self._nodes["parse"] = IngestParseNode(
            cfg.get("payload_store"), cfg.get("trusted_scope"), cfg.get("session_id")
        )
        self._nodes["sanitize"] = IngestSanitizeNode()
        self._nodes["taxonomy_tag"] = TaxonomyTagNode()
        self._nodes["kb_upsert"] = KbUpsertNode(
            cfg.get("kb_service"), cfg.get("payload_store"), cfg.get("trusted_scope"), cfg.get("session_id")
        )
        self._nodes["stage1"] = Stage1DisclosureStructureNode(
            cfg.get("payload_store"), cfg.get("trusted_scope"), cfg.get("session_id")
        )
        self._nodes["stage2"] = Stage2ScopedRetrievalNode(
            cfg.get("kb_service"), cfg.get("top_k", 8), cfg.get("min_similarity", 0.55)
        )
        self._nodes["stage3"] = Stage3DraftNode(cfg.get("llm"))
        self._nodes["hitl_review"] = HitlReviewNode()
        self._nodes["stage4"] = Stage4InconsistencyNode()
        self._nodes["validate_feedback"] = FeedbackValidateNode(
            cfg.get("payload_store"), cfg.get("trusted_scope"), cfg.get("session_id")
        )
        self._nodes["kb_update_reflection"] = KbUpdateReflectionNode(cfg.get("kb_service"))

    def add_edges(self) -> None:
        self._sg.add_edge(START, "mode_router")
        self._sg.add_conditional_edges(
            "mode_router",
            self.route_mode,
            {"ingest": "parse", "invoke": "stage1", "feedback": "validate_feedback", "error": END},
        )
        self._sg.add_edge("parse", "sanitize")
        self._sg.add_edge("sanitize", "taxonomy_tag")
        self._sg.add_edge("taxonomy_tag", "kb_upsert")
        self._sg.add_edge("kb_upsert", END)
        self._sg.add_edge("stage1", "stage2")
        self._sg.add_edge("stage2", "stage3")
        self._sg.add_edge("stage3", "hitl_review")
        self._sg.add_conditional_edges("hitl_review", self.route_after_review, {"proceed": "stage4", "stop": END})
        self._sg.add_edge("stage4", END)
        self._sg.add_edge("validate_feedback", "kb_update_reflection")
        self._sg.add_edge("kb_update_reflection", END)

    def route(self, state: DisclosureDiffWorkflowState) -> str:
        return self.route_mode(state)

    def route_mode(self, state: DisclosureDiffWorkflowState) -> str:
        return (
            "error"
            if state.get("status") == AgentStatus.ERROR.value
            else state.get("mode")
            if state.get("mode") in {"ingest", "invoke", "feedback"}
            else "error"
        )

    def route_after_review(self, state: DisclosureDiffWorkflowState) -> str:
        if state.get("status") in {AgentStatus.ERROR.value, AgentStatus.CANCELLED.value}:
            return "stop"
        if state.get("hitl_feedback") is not None and state.get("hitl_status") == HitlStatus.REJECTED:
            return "stop"
        return "proceed"

    def get_output(self, state: DisclosureDiffWorkflowState) -> dict:
        keys = (
            "status",
            "mode",
            "gate_kind",
            "output",
            "evidence_map",
            "cited_source_ids",
            "retrieved_records",
            "inconsistency_flags",
            "missing_data_flags",
            "requires_human_review",
            "degradation_reason",
            "error_code",
            "error_type",
            "ingest_summary",
            "feedback_summary",
            "hitl_draft",
            "hitl_feedback",
            "hitl_status",
            "error_log",
        )
        return {key: state.get(key) for key in keys if state.get(key) is not None}
