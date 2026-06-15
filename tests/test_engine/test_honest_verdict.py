"""诚实 verdict 测试 — 真跑暴露的"假 pass"修复回归

第一性原理：验证级证据必须真正"验证过"才算 pass。
真跑铁证：branch_review 在无 diff 仓库上报 no_change=True，啥都没实证，
旧逻辑却把全库验收点判 pass→Goal 假 completed。本测试锁死诚实行为：
1. no_change → 证据 verdict=not_applicable（非 pass）
2. critic：not_applicable 不算可达成 → 不空转 replan，诚实停 partial
3. 硬约束：无平台真实智能体绑定能力 → 拒绝合成假 agent，降级+告警，不入队
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import time
import uuid
import pytest

from common.db import get_collection
from engine import goal_scheduler as sched
from engine import critic


# ==================== 1. _bind_evidence 诚实 verdict ====================

@pytest.fixture
def goal_branch_review():
    """running Goal + 单 branch_review step（static_analysis 验证级证据）服务 a1。"""
    goal_id = f"goal_hv_{uuid.uuid4().hex[:6]}"
    get_collection("ai_goals").insert_one({
        "goal_id": goal_id, "title": "诚实verdict测试", "goal_statement": "验证无变更不假pass",
        "completion_policy": "auto_complete", "status": "running",
        "feasibility": {"allowed_evidence_types": ["static_analysis"],
                        "producible_evidence_types": ["static_analysis"]},
        "budget": {"max_replans": 3},
        "acceptance": [{"id": "a1", "desc": "全库静态合规", "evidence_type": "static_analysis",
                        "bound_to": None, "verdict": "pending"}],
        "plan_version": 1, "round": 1, "replan_count": 0, "created_at": int(time.time()),
    })
    get_collection("ai_goal_steps").insert_one({
        "goal_id": goal_id, "step_id": "s1", "name": "静态分析", "capability_key": "branch_review",
        "agent_id": "agent_branch_review", "depends_on": [], "serves_acceptance": ["a1"],
        "evidence_type": "static_analysis", "can_execute": True, "requires_approval": False,
        "risk_level": "low", "retryable": True, "status": "pending", "attempts": [], "plan_version": 1,
    })
    yield goal_id
    for c in ["ai_goals", "ai_goal_steps", "ai_goal_events", "ai_goal_evidence",
              "ai_goal_artifacts", "ai_goal_summary", "ai_task_queue", "ai_memory_points",
              "ai_workspace_agents"]:
        get_collection(c).delete_many({"goal_id": goal_id})


class TestHonestVerdict:
    def test_no_change_binds_not_applicable_not_pass(self, goal_branch_review, monkeypatch):
        """branch_review 报 no_change=True → 证据 verdict=not_applicable，绝不 pass。"""
        monkeypatch.setattr(sched.steward, "evaluate_and_remember",
                            lambda *a, **k: {"conclusion": "stub"})
        sched.advance(goal_branch_review)  # 提交 s1（agent_branch_review 真实存在）

        # 真跑形状：无文件变更
        no_change_output = {"change_summary": "无文件变更", "regression_cases": [],
                            "risk_points": [], "no_change": True}
        sched.on_step_done(goal_branch_review, "s1", no_change_output)

        goal = get_collection("ai_goals").find_one({"goal_id": goal_branch_review})
        a1 = next(a for a in goal["acceptance"] if a["id"] == "a1")
        assert a1["verdict"] == "not_applicable", "无变更不该假 pass"
        assert a1["verdict"] != "pass"

        ev = get_collection("ai_goal_evidence").find_one(
            {"goal_id": goal_branch_review, "acceptance_id": "a1"})
        assert ev is not None and ev["verdict"] == "not_applicable"

    def test_no_change_goal_stops_partial_not_completed(self, goal_branch_review, monkeypatch):
        """全链路：无变更 → 不假 completed，诚实停在 partial_completed。"""
        monkeypatch.setattr(sched.steward, "evaluate_and_remember",
                            lambda *a, **k: {"conclusion": "stub"})
        sched.advance(goal_branch_review)
        sched.on_step_done(goal_branch_review, "s1",
                           {"change_summary": "无文件变更", "regression_cases": [], "no_change": True})

        goal = get_collection("ai_goals").find_one({"goal_id": goal_branch_review})
        assert goal["status"] == "partial_completed", "假 pass 已修，应诚实停 partial"
        assert goal["status"] != "completed"
        # 没有空转 replan
        replans = list(get_collection("ai_goal_events").find(
            {"goal_id": goal_branch_review, "event": "replan_triggered"}))
        assert len(replans) == 0, "not_applicable 不该触发空转 replan"

    def test_real_change_still_passes(self, goal_branch_review, monkeypatch):
        """对照：有真实变更（no_change != True）→ 仍正常判 pass，不误伤。"""
        monkeypatch.setattr(sched.steward, "evaluate_and_remember",
                            lambda *a, **k: {"conclusion": "stub"})
        sched.advance(goal_branch_review)
        sched.on_step_done(goal_branch_review, "s1",
                           {"change_summary": "改了登录", "regression_cases": ["回归登录"], "no_change": False})

        goal = get_collection("ai_goals").find_one({"goal_id": goal_branch_review})
        a1 = next(a for a in goal["acceptance"] if a["id"] == "a1")
        assert a1["verdict"] == "pass"
        assert goal["status"] == "completed"


# ==================== 2. critic：not_applicable 不空转 replan ====================

class TestCriticNotApplicable:
    def _goal(self, verdict_a2, replan_count=0):
        return {
            "goal_id": "g_na",
            "acceptance": [
                {"id": "a1", "desc": "已过", "evidence_type": "static_analysis", "verdict": "pass"},
                {"id": "a2", "desc": "无变更", "evidence_type": "static_analysis", "verdict": verdict_a2},
            ],
            "feasibility": {"producible_evidence_types": ["static_analysis"],
                            "allowed_evidence_types": ["static_analysis"]},
            "replan_count": replan_count, "budget": {"max_replans": 3},
        }

    def test_not_applicable_stops_partial(self):
        """未达成验收 verdict=not_applicable → 不算可达成，诚实停 partial（不 replan）。"""
        d = critic.decide_after_verify(self._goal("not_applicable"))
        assert d["decision"] == "partial"
        assert d["achievable_unmet"] == []
        assert "a2" in d["unmet"]

    def test_pending_still_replans(self):
        """对照：同证据类型但 verdict=pending（真可再攻）→ 仍 replan，不误伤多轮闭环。"""
        d = critic.decide_after_verify(self._goal("pending"))
        assert d["decision"] == "replan"
        assert "a2" in d["achievable_unmet"]


# ==================== 3. 硬约束：拒绝合成假 agent ====================

@pytest.fixture
def goal_ghost_cap():
    """running Goal + step 绑定一个平台不存在的能力/agent（无真实智能体）。"""
    goal_id = f"goal_ghost_{uuid.uuid4().hex[:6]}"
    get_collection("ai_goals").insert_one({
        "goal_id": goal_id, "title": "无真实agent测试", "goal_statement": "x",
        "completion_policy": "auto_complete", "status": "running",
        "feasibility": {"allowed_evidence_types": []},
        "acceptance": [{"id": "a1", "desc": "x", "evidence_type": "static_analysis",
                        "bound_to": None, "verdict": "pending"}],
        "plan_version": 1, "round": 1, "replan_count": 0, "created_at": int(time.time()),
    })
    get_collection("ai_goal_steps").insert_one({
        "goal_id": goal_id, "step_id": "s1", "name": "幽灵能力",
        "capability_key": "ghost_capability_xyz", "agent_id": "ghost_agent_xyz",
        "depends_on": [], "serves_acceptance": ["a1"], "evidence_type": "static_analysis",
        "can_execute": True, "requires_approval": False, "risk_level": "low",
        "retryable": True, "status": "pending", "attempts": [], "plan_version": 1,
    })
    yield goal_id
    for c in ["ai_goals", "ai_goal_steps", "ai_goal_events", "ai_goal_evidence",
              "ai_goal_artifacts", "ai_goal_summary", "ai_task_queue", "ai_memory_points",
              "ai_workspace_agents"]:
        get_collection(c).delete_many({"goal_id": goal_id})


class TestRequireRealAgent:
    def test_no_real_agent_degrades_and_warns_not_enqueue(self, goal_ghost_cap):
        """无平台真实智能体绑定能力 → 拒绝合成假 agent：step 降级 + 告警事件 + 不入队。"""
        sched.advance(goal_ghost_cap)

        s1 = get_collection("ai_goal_steps").find_one({"goal_id": goal_ghost_cap, "step_id": "s1"})
        assert s1["status"] == "degraded", "无真实 agent 应诚实降级，不派活给假 agent"

        warn = get_collection("ai_goal_events").find_one(
            {"goal_id": goal_ghost_cap, "event": "step_no_real_agent"})
        assert warn is not None, "应告警拒绝合成假 agent"

        # 没有往任务队列派活（没有合成假 agent 入队）
        q = list(get_collection("ai_task_queue").find({"goal_id": goal_ghost_cap}))
        assert q == [], "拒绝合成假 agent 后不应有任务入队"
        # 也没有装出假的 workspace 实例
        inst = list(get_collection("ai_workspace_agents").find(
            {"goal_id": goal_ghost_cap, "step_id": "s1"}))
        assert inst == [], "不应安装假 agent 实例"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
