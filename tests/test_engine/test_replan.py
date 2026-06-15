"""多轮 replan 闭环集成测试 — 计划生成 → 多轮执行（每轮 plan 不同）

不依赖真实 LLM：stub planner.generate_plan / discover_capabilities + steward。
验证：
- 一轮没拿全验收 → Critic 判 replan → 生成针对缺口的【不同】新 plan（plan_version 递增）
- 旧 plan append-only 保留（superseded_by）；调度只看活跃 step
- 多轮直到全验收通过 → completed
- 超 max_replans 预算 → 诚实停在 partial_completed，不空转
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import uuid
import pytest

from common.db import get_collection
from engine import goal_scheduler as sched
from engine import planner
from engine import steward


def _cap(can_execute=True):
    """stub 能力清单：用验证级证据 static_analysis（才构成业务 pass，支撑多轮 replan/完成）"""
    return [{
        "capability_key": "requirement_analysis",
        "agent_id": "agent_req",
        "purpose": "需求分析",
        "required_sources": [],
        "produces_evidence": ["static_analysis"],
        "risk_level": "low",
        "requires_approval": False,
        "fallback": None,
        "can_execute_now": can_execute,
    }]


def _make_goal(goal_id, acceptance, max_replans=3, allowed=None):
    get_collection("ai_goals").insert_one({
        "goal_id": goal_id, "title": "多轮测试", "goal_statement": "验证多轮 replan",
        "completion_policy": "auto_complete", "status": "running",
        "feasibility": {"allowed_evidence_types": allowed or ["static_analysis"]},
        "budget": {"max_replans": max_replans},
        "plan_version": 1, "round": 1, "replan_count": 0,
        "acceptance": acceptance, "created_at": int(time.time()),
    })


def _make_step(goal_id, step_id, serves, can_execute=True, pv=1):
    return {
        "goal_id": goal_id, "step_id": step_id, "name": f"{step_id}-pv{pv}",
        "capability_key": "requirement_analysis", "agent_id": "agent_req",
        "depends_on": [], "serves_acceptance": serves, "evidence_type": "static_analysis",
        "can_execute": can_execute, "requires_approval": False, "risk_level": "low",
        "retryable": True, "fallback": "doc_review", "status": "pending",
        "attempts": [], "plan_version": pv,
    }


def _cleanup(goal_id):
    for c in ["ai_goals", "ai_goal_steps", "ai_goal_events", "ai_goal_evidence",
              "ai_goal_artifacts", "ai_goal_summary", "ai_task_queue", "ai_memory_points"]:
        get_collection(c).delete_many({"goal_id": goal_id})


@pytest.fixture(autouse=True)
def stub_llm(monkeypatch):
    """统一桩掉所有 LLM 调用，保证确定性。"""
    monkeypatch.setattr(steward, "evaluate_and_remember", lambda *a, **k: {"conclusion": "stub"})
    monkeypatch.setattr(steward, "retrieve_memory", lambda *a, **k: "")
    monkeypatch.setattr(planner, "discover_capabilities", lambda profile: _cap(True))


class TestMultiRoundReplan:
    def test_two_rounds_complete_with_different_plans(self, monkeypatch):
        """R1 只拿下 a1 → replan → R2 针对 a2 出【不同】plan → 全过 → completed"""
        goal_id = f"goal_mr_{uuid.uuid4().hex[:6]}"
        _make_goal(goal_id, [
            {"id": "a1", "desc": "拆解登录", "evidence_type": "static_analysis", "bound_to": None, "verdict": "pending"},
            {"id": "a2", "desc": "拆解支付", "evidence_type": "static_analysis", "bound_to": None, "verdict": "pending"},
        ])
        # R1 计划：只服务 a1（故意漏 a2，制造缺口）
        get_collection("ai_goal_steps").insert_one(_make_step(goal_id, "s1", ["a1"], pv=1))

        # 重规划时 Planner 产出【不同】的 plan：R2 针对未达成的 a2
        plan_calls = {"n": 0}
        def fake_plan(goal_statement, acceptance, profile, capabilities,
                      memory_context="", prior_context="", model_id="gemini_pro"):
            plan_calls["n"] += 1
            # 断言重规划确实拿到了上一轮上下文（含未达成 a2）
            assert "a2" in prior_context
            return {"steps": [{"step_id": "s1", "name": "R2-攻克支付", "capability_key": "requirement_analysis",
                               "depends_on": [], "serves_acceptance": ["a2"], "rationale": "针对a2"}],
                    "plan_summary": "round2 plan", "confidence": 0.9, "_meta": {}}
        monkeypatch.setattr(planner, "generate_plan", fake_plan)

        try:
            # R1 执行
            sched.advance(goal_id)
            sched.on_step_done(goal_id, "s1", {"acceptance_points": ["登录"], "summary": "R1完成a1"})
            # → a1 pass、a2 未达成 → critic replan → R2 plan 生成并 advance（R2 s1 running）

            goal = get_collection("ai_goals").find_one({"goal_id": goal_id})
            assert goal["status"] == "running", "replan 后应回到 running 跑第二轮"
            assert goal["plan_version"] == 2 and goal["round"] == 2 and goal["replan_count"] == 1

            # 旧 plan append-only 保留
            r1 = get_collection("ai_goal_steps").find_one({"goal_id": goal_id, "step_id": "s1", "plan_version": 1})
            assert r1["superseded_by"] == 2
            # 活跃 step 是 R2 的（服务 a2，且 plan 内容不同）
            active = get_collection("ai_goal_steps").find_one(
                {"goal_id": goal_id, "step_id": "s1", "superseded_by": {"$exists": False}})
            assert active["plan_version"] == 2 and active["serves_acceptance"] == ["a2"]
            assert active["name"] != r1["name"], "每轮 plan 应不同"

            # R2 执行 → 全过 → completed
            sched.on_step_done(goal_id, "s1", {"acceptance_points": ["支付"], "summary": "R2完成a2"})
            goal = get_collection("ai_goals").find_one({"goal_id": goal_id})
            assert goal["status"] == "completed"
            assert all(a["verdict"] == "pass" for a in goal["acceptance"])

            # 事件留痕含 replan_triggered + critic_decision
            events = [e["event"] for e in get_collection("ai_goal_events").find({"goal_id": goal_id})]
            assert "replan_triggered" in events
            assert "critic_decision" in events
        finally:
            _cleanup(goal_id)

    def test_replan_budget_exhausted_then_partial(self, monkeypatch):
        """验收永远拿不下（步骤降级）+ max_replans=1 → 第2轮后诚实停 partial_completed，不空转"""
        goal_id = f"goal_mb_{uuid.uuid4().hex[:6]}"
        _make_goal(goal_id, [
            {"id": "a1", "desc": "真机验证", "evidence_type": "static_analysis", "bound_to": None, "verdict": "pending"},
        ], max_replans=1)
        # R1 step 不可执行 → 同步降级 → a1 永远拿不到证据
        get_collection("ai_goal_steps").insert_one(_make_step(goal_id, "s1", ["a1"], can_execute=False, pv=1))

        # 能力始终不可执行 → 每轮 plan 都降级
        monkeypatch.setattr(planner, "discover_capabilities", lambda profile: _cap(False))
        monkeypatch.setattr(planner, "generate_plan", lambda *a, **k: {
            "steps": [{"step_id": "s1", "name": "再试一次", "capability_key": "requirement_analysis",
                       "depends_on": [], "serves_acceptance": ["a1"], "rationale": "retry"}],
            "plan_summary": "retry plan", "confidence": 0.5, "_meta": {}})

        try:
            # 一次 advance 即驱动：R1 降级→verify→replan(count0<1)→R2 降级→verify→partial(count1>=1)
            sched.advance(goal_id)

            goal = get_collection("ai_goals").find_one({"goal_id": goal_id})
            assert goal["status"] == "partial_completed", "超预算应诚实停在部分完成"
            assert goal["replan_count"] == 1 and goal["round"] == 2
            assert goal["plan_version"] == 2

            # 没有无限重规划（replan_triggered 恰好 1 次）
            replan_events = [e for e in get_collection("ai_goal_events").find(
                {"goal_id": goal_id, "event": "replan_triggered"})]
            assert len(replan_events) == 1
        finally:
            _cleanup(goal_id)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
