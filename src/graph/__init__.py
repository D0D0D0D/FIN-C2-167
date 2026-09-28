"""AgentCore Platform v1.0

Re-export the outer graph class so the AgentRegistry manifest
(`config/agent.yaml` → `module: src.graph`, `class: ProspectusDisclosureReviewGraph`)
resolves against the package, matching the declared entry point.
"""

from src.graph.graph import ProspectusDisclosureReviewGraph

__all__ = ["ProspectusDisclosureReviewGraph"]
