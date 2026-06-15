"""产物留痕自包含测试 — 锁死"幻觉可查"地基

验证 step 执行产物自包含：谁产出(agent_id/agent_name) + 喂了什么(inputs_snapshot)
+ 怎么分析(reasoning) + 产出什么(summary/data)，缺一不可——否则无法对照核查幻觉。

风格对齐 test_probe_round / test_replan：真实 test DB + 显式 cleanup + stub LLM。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import time
import uuid
import pytest

from common.db import get_collection
from engine import goal_scheduler as sched
from engine import planner
from engine import steward


@pytest.fixture
def running_goal_with_agent():
    """running 状态 goal + 一个 active 智能体 + 一个绑定它的 pending step（含 doc 源）。"""
    goal_id = f"goal_prov_{uuid.uuid4().hex[:6]}"
    agent_id = f"agent_req_{uuid.uuid4().hex[:6]}"

    get_collection("ai_agents").insert_one({
        "agent_id": agent_id, "agent_name": "需求分析智能体", "category": "analysis",
        "capability_key": "requirement_analysis", "handler_class": "requirement_analysis",
        "model_id": "gemini_flash", "system_prompt": "拆需求", "status": "active",
        "capability_contract": {"purpose": "解析需求", "required_sources": ["doc"],
                                "produces_evidence": ["doc_review"], "risk_level": "low",
                                "requires_approval": False, "mutates": False,
                                "timeout_sec": 300, "retryable": True, "fallback": None},
    })
    get_collection("ai_goals").insert_one({
        "goal_id": goal_id, "title": "留痕测试", "goal_statement": "验证产物自包含",
        "status": "running", "completion_policy": "auto_complete",
        "feasibility": {"allowed_evidence_types": ["doc_review"],
                        "producible_evidence_types": ["doc_review"]},
        "budget": {"max_replans": 3}, "plan_version": 1, "round": 1, "replan_count": 0,
        "sources": [{"type": "doc", "content": "登录需求：手机号+验证码，错误提示要明确。"}],
        "acceptance": [{"id": "a1", "desc": "登录主流程可验证", "evidence_type": "doc_review",
                        "bound_to": None, "verdict": "pending"}],
        "created_at": int(time.time()),
    })
    get_collection("ai_goal_steps").insert_one({
        "goal_id": goal_id, "step_id": "s1", "name": "需求分析",
        "capability_key": "requirement_analysis", "agent_id": agent_id,
        "depends_on": [], "serves_acceptance": ["a1"], "evidence_type": "doc_review",
        "can_execute": True, "requires_approval": False, "risk_level": "low",
        "retryable": True, "fallback": None, "status": "pending", "attempts": [], "plan_version": 1,
    })

    yield goal_id, agent_id

    get_collection("ai_agents").delete_many({"agent_id": agent_id})
    for c in ["ai_goals", "ai_goal_steps", "ai_goal_events", "ai_goal_evidence",
              "ai_goal_artifacts", "ai_goal_summary", "ai_task_queue", "ai_memory_points",
              "ai_workspace_agents"]:
        get_collection(c).delete_many({"goal_id": goal_id})


def test_step_artifact_is_self_contained(running_goal_with_agent, monkeypatch):
    """advance 提交（落输入快照）→ on_step_done → 产物含 谁/喂了什么/怎么分析/产出。"""
    goal_id, agent_id = running_goal_with_agent
    monkeypatch.setattr(steward, "evaluate_and_remember", lambda *a, **k: {"conclusion": "stub"})
    monkeypatch.setattr(steward, "retrieve_memory", lambda *a, **k: "")
    monkeypatch.setattr(planner, "discover_capabilities", lambda profile: [{
        "capability_key": "requirement_analysis", "agent_id": agent_id, "purpose": "需求分析",
        "required_sources": [], "produces_evidence": ["doc_review"], "risk_level": "low",
        "requires_approval": False, "fallback": None, "can_execute_now": True}])
    monkeypatch.setattr(planner, "generate_plan", lambda *a, **k: {
        "steps": [], "plan_summary": "", "confidence": 0.0, "_meta": {}})  # replan 不再加步骤

    # 提交：install_agent 注册持久实例(agent_name)，enqueue 把 inputs_snapshot 落到 step 文档
    sched.advance(goal_id)
    reg = get_collection("ai_workspace_agents").find_one({"goal_id": goal_id, "agent_id": agent_id})
    assert reg is not None and reg["agent_name"] == "需求分析智能体"
    stp = get_collection("ai_goal_steps").find_one({"goal_id": goal_id, "step_id": "s1"})
    assert stp["inputs_snapshot"]["doc_content"].startswith("登录需求")  # resolver 喂的 doc 被快照到 step

    # 完成回调：handler 产出带 report（思考流程）
    output = {
        "acceptance_points": ["登录成功", "错误提示明确"],
        "summary": "拆出2个验收点",
        "report": "## 分析过程\n基于文档识别登录主流程...\n## 验收点\n1.登录成功 2.错误提示",
    }
    sched.on_step_done(goal_id, "s1", output)

    # ===== 产物自包含断言 =====
    art = get_collection("ai_goal_artifacts").find_one({"goal_id": goal_id, "step_id": "s1"})
    assert art is not None
    # 谁产出
    assert art["agent_id"] == agent_id
    assert art["agent_name"] == "需求分析智能体"
    # 喂了什么（输入快照可对照）
    assert art["inputs_snapshot"]["doc_content"].startswith("登录需求")
    # 怎么分析（思考流程留痕）
    assert "分析过程" in art["reasoning"]
    # 产出什么
    assert art["summary"] == "拆出2个验收点"
    assert art["data"]["acceptance_points"] == ["登录成功", "错误提示明确"]
    # 来源字段存在（多 repo 扇出后填，现可为空）
    assert "source_ref" in art
    # 自包含：谁+输入+推理+产出齐全 → 可做幻觉核查
    for k in ("agent_id", "agent_name", "inputs_snapshot", "reasoning", "data"):
        assert k in art, f"留痕缺字段 {k}，幻觉不可查"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
