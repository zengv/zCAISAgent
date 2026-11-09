from collections.abc import Awaitable, Callable
from typing import Any

from zcaisagent.domain.enums import AutonomyLevel
from zcaisagent.domain.models import RunState
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.graph import Edge, GraphSpec
from zcaisagent.orchestration.nodes.abstain import abstain_node
from zcaisagent.orchestration.nodes.act import act_node
from zcaisagent.orchestration.nodes.analyze import analyze_node
from zcaisagent.orchestration.nodes.direct_answer import direct_answer_node
from zcaisagent.orchestration.nodes.grade import grade_node
from zcaisagent.orchestration.nodes.observe import observe_node
from zcaisagent.orchestration.nodes.plan import plan_node
from zcaisagent.orchestration.nodes.reflect import reflect_node
from zcaisagent.orchestration.nodes.repair import repair_node
from zcaisagent.orchestration.nodes.respond import respond_node
from zcaisagent.orchestration.nodes.retrieve import retrieve_node
from zcaisagent.orchestration.nodes.route import route_node
from zcaisagent.orchestration.nodes.synthesize import synthesize_node
from zcaisagent.orchestration.nodes.verify import verify_node


def build_default_graph() -> GraphSpec:
    nodes: dict[str, Callable[[RunState, Any], Awaitable[StatePatch]]] = {
        "analyze": analyze_node,
        "route": route_node,
        "plan": plan_node,
        "act": act_node,
        "observe": observe_node,
        "reflect": reflect_node,
        "direct_answer": direct_answer_node,
        "retrieve": retrieve_node,
        "grade": grade_node,
        "repair": repair_node,
        "synthesize": synthesize_node,
        "verify": verify_node,
        "respond": respond_node,
        "abstain": abstain_node,
    }
    edges = [
        Edge("analyze", "route"),
        Edge("route", "abstain", lambda s: s.analysis.get("intent") == "不安全"),
        Edge("route", "direct_answer", lambda s: s.level == AutonomyLevel.L0_DIRECT),
        # L2/L3 走通用循环；L1 维持知识快路径。
        Edge(
            "route",
            "plan",
            lambda s: s.level in {AutonomyLevel.L2_STANDARD, AutonomyLevel.L3_DEEP},
        ),
        Edge("route", "retrieve"),
        Edge("plan", "act"),
        Edge("act", "observe"),
        Edge("observe", "reflect"),
        Edge(
            "reflect",
            "grade",
            lambda s: (s.analysis.get("loop") or {}).get("decision") == "complete",
        ),
        Edge("reflect", "act"),
        Edge("retrieve", "grade"),
        Edge("grade", "abstain", lambda s: not s.evidence),
        Edge(
            "grade",
            "synthesize",
            lambda s: (s.analysis.get("sufficiency") or {}).get("decision")
            in {"sufficient", "conflict"},
        ),
        Edge("grade", "abstain", lambda s: bool(s.analysis.get("no_progress"))),
        Edge("grade", "repair"),
        Edge("synthesize", "verify"),
        Edge("verify", "respond", lambda s: bool(s.verdict and s.verdict.passed)),
        Edge(
            "verify",
            "abstain",
            lambda s: bool(s.verdict and s.verdict.suggested_action == "abstain"),
        ),
        Edge("verify", "repair"),
        Edge(
            "repair",
            "retrieve",
            lambda s: (s.analysis.get("repair_action") or "") == "retrieve",
        ),
        Edge(
            "repair",
            "synthesize",
            lambda s: (s.analysis.get("repair_action") or "") == "rewrite",
        ),
        Edge(
            "repair",
            "respond",
            lambda s: (s.analysis.get("repair_action") or "") == "degrade",
        ),
        Edge("repair", "abstain"),
    ]
    return GraphSpec(nodes=nodes, edges=edges, entry="analyze")
