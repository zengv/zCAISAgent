import asyncio

from zcaisagent.orchestration.nodes.retrieve import retrieve_node
from tests.conftest import make_chunk, make_ctx, make_state


def test_retrieve_uses_rewritten_variants_for_multi_hop() -> None:
    ctx = make_ctx(
        [
            make_chunk("kb#0", "一线城市住宿标准为 800 元/晚。"),
            make_chunk("kb#1", "年假天数为 5 天。"),
        ],
        top_k=3,
    )
    state = make_state("住宿标准以及年假天数是多少？").model_copy(
        update={"analysis": {"complexity": "multi_hop", "intent": "查事实"}}
    )

    patch = asyncio.run(retrieve_node(state, ctx))

    evidence_ids = {item.id for item in patch.append_evidence or []}
    assert {"kb#0", "kb#1"} <= evidence_ids
    notes = patch.append_notes or []
    assert any(note.startswith("rewrite:multi_") for note in notes)


def test_retry_query_takes_precedence_over_rewrite() -> None:
    ctx = make_ctx([make_chunk("kb#0", "年假天数为 5 天。")], top_k=3)
    state = make_state("住宿标准以及年假天数是多少？").model_copy(
        update={
            "analysis": {
                "complexity": "multi_hop",
                "intent": "查事实",
                "retry_query": "年假天数",
            }
        }
    )

    patch = asyncio.run(retrieve_node(state, ctx))

    notes = patch.append_notes or []
    assert any(":retry:" in note for note in notes)
    assert not any(note.startswith("rewrite:") for note in notes)
