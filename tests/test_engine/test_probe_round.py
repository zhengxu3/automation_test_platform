"""Discovery Plan 端到端集成测试

验证目标发现计划完整异步流（doc 场景）：
  discover_and_plan(doc goal)
    → profile（含 producible_evidence_types）
    → probe_planner 选 requirement_analysis
    → 落 plan_kind=discovery 的 ai_goal_steps
    → scheduler 正常提交 step 任务
  on_step_done(worker 回调)
    → 存 discovery artifact
    → discovery plan 全完成后 synthesize_goal_from_probe(stub)
    → 生成 plan_kind=objective 的下一版 plan → running → advance 入队目标执行 step

Steward/Planner 的 LLM 调用打桩，聚焦确定性编排链路。
另含 Critic 真实可达天花板(producible_evidence_types)判定的纯函数测试。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import time
import uuid
import pytest

from common.db import get_collection
from engine import goal_runtime
from engine import goal_scheduler as sched
from engine import critic


# ==================== 端到端探查轮 ====================

@pytest.fixture
def doc_goal_with_agent():
    """discovering 状态的 doc-only Goal + 一个 active requirement_analysis 智能体。"""
    goal_id = f"goal_probe_{uuid.uuid4().hex[:6]}"
    agent_id = f"agent_req_{uuid.uuid4().hex[:6]}"

    get_collection("ai_agents").insert_one({
        "agent_id": agent_id, "agent_name": "需求分析", "category": "analysis",
        "capability_key": "requirement_analysis", "handler_class": "requirement_analysis",
        "model_id": "gemini_flash", "system_prompt": "拆需求", "status": "active",
        "capability_contract": {
            "purpose": "解析需求文档，拆出验收点和测试用例",
            "required_sources": ["doc"],
            "produces_evidence": ["doc_review", "testcase_generated"],
            "risk_level": "low", "requires_approval": False, "mutates": False,
            "timeout_sec": 300, "retryable": True, "fallback": None,
        },
    })
    get_collection("ai_goals").insert_one({
        "goal_id": goal_id,
        "title": "登录需求验证",
        "status": "discovering",
        "completion_policy": "auto_complete",
        "sources": [{"type": "doc", "content": "用户登录功能需求：手机号+验证码登录，错误提示。"}],
        "acceptance": [],
        "created_at": int(time.time()),
    })

    yield goal_id, agent_id

    get_collection("ai_agents").delete_many({"agent_id": agent_id})
    for c in ["ai_goals", "ai_goal_steps", "ai_goal_events", "ai_goal_evidence",
              "ai_goal_artifacts", "ai_goal_summary", "ai_task_queue", "ai_memory_points",
              "ai_workspace_agents"]:
        get_collection(c).delete_many({"goal_id": goal_id})


def _stub_steward_planner(monkeypatch):
    """打桩 Steward 综合目标 + Planner 规划（避免真实 LLM）。"""
    def fake_synthesize(title, probe_outputs, input_mode="doc_only",
                        allowed_evidence=None, memory_context="", model_id="gemini_flash"):
        # 断言探查产物确实传进来了
        assert probe_outputs and probe_outputs[0]["type"] == "requirement_analysis"
        return {
            "goal_statement": "验证手机号+验证码登录全流程",
            "acceptance": [
                {"id": "a1", "desc": "登录成功路径可验证", "evidence_type": "doc_review",
                 "bound_to": None, "verdict": "pending"},
            ],
            "rationale": "综合自需求拆解探查产物",
            "confidence": 0.82,
        }

    def fake_generate_plan(goal_statement, acceptance, profile, capabilities,
                           memory_context="", prior_context="", model_id="gemini_pro"):
        return {
            "steps": [
                {"step_id": "s1", "name": "需求分析", "capability_key": "requirement_analysis",
                 "depends_on": [], "serves_acceptance": ["a1"], "needs_upgrade": False,
                 "rationale": "拆解验收点"},
            ],
            "plan_summary": "单步需求分析",
            "confidence": 0.8,
            "_meta": {"ok": True, "attempts": 1, "degraded": False},
        }

    monkeypatch.setattr(goal_runtime.steward, "synthesize_goal_from_probe", fake_synthesize)
    monkeypatch.setattr(goal_runtime.planner, "generate_plan", fake_generate_plan)
    monkeypatch.setattr(sched.steward, "evaluate_and_remember", lambda *a, **k: {"conclusion": "stub"})


class TestProbeRoundEndToEnd:
    def test_discover_enqueues_probe_then_synthesizes_and_runs(self, doc_goal_with_agent, monkeypatch):
        goal_id, agent_id = doc_goal_with_agent
        _stub_steward_planner(monkeypatch)

        # ===== 1. discover_and_plan → 启动 discovery plan =====
        result = goal_runtime.discover_and_plan(goal_id)
        assert result.get("stage") == "discovery"
        assert result.get("plan_kind") == "discovery"

        # 画像写了真实可达天花板
        goal = get_collection("ai_goals").find_one({"goal_id": goal_id})
        assert goal["status"] == "running"
        assert goal["target_state"] == "discovering"
        assert goal["current_plan_kind"] == "discovery"
        producible = goal["feasibility"]["producible_evidence_types"]
        assert "doc_review" in producible  # requirement_analysis 智能体真实可产

        # discovery step 可见、可回放，并以普通 step 任务入队
        d1 = get_collection("ai_goal_steps").find_one({"goal_id": goal_id, "step_id": "d1"})
        assert d1 is not None
        assert d1["plan_kind"] == "discovery"
        assert d1["plan_version"] == 1
        assert d1["status"] == "running"

        discovery_task = get_collection("ai_task_queue").find_one(
            {"goal_id": goal_id, "payload.step_id": "d1"})
        assert discovery_task is not None
        assert discovery_task["payload"]["phase"] == "step"
        assert discovery_task["payload"]["handler_class"] == "requirement_analysis"

        # 智能体注册表(goal_id+agent_id 持久实例)：运行中，served_steps 含 d1
        inst = get_collection("ai_workspace_agents").find_one(
            {"goal_id": goal_id, "served_steps": "d1"})
        assert inst is not None and inst["status"] == "running"

        # ===== 2. 模拟 worker 完成 discovery step 回调 =====
        discovery_output = {
            "capability_key": "requirement_analysis",
            "acceptance_points": ["登录成功", "错误提示正确"],
            "test_cases": ["c1", "c2"],
            "summary": "拆出2个验收点",
            "confidence": 0.85,
        }
        done = sched.on_step_done(goal_id, "d1", discovery_output)
        assert done["ok"]

        # discovery 产物落库，可作为主管综合目标的输入
        discovery_art = get_collection("ai_goal_artifacts").find_one(
            {"goal_id": goal_id, "step_id": "d1", "plan_kind": "discovery"})
        assert discovery_art is not None and discovery_art["type"] == "requirement_analysis"

        # ===== 3. discovery 全完成 → synthesize → objective plan → running =====
        goal = get_collection("ai_goals").find_one({"goal_id": goal_id})
        assert goal["status"] == "running"
        assert goal["target_state"] == "confirmed"
        assert goal["current_plan_kind"] == "objective"
        assert goal["plan_version"] == 2
        assert goal["goal_statement"] == "验证手机号+验证码登录全流程"
        assert len(goal["acceptance"]) == 1 and goal["acceptance"][0]["id"] == "a1"

        # objective plan step 落库 + 被 advance 提交执行
        steps = list(get_collection("ai_goal_steps").find({"goal_id": goal_id}))
        assert {s["step_id"] for s in steps} == {"d1", "s1"}
        s1 = get_collection("ai_goal_steps").find_one({"goal_id": goal_id, "step_id": "s1"})
        assert s1["plan_kind"] == "objective"
        assert s1["plan_version"] == 2
        assert s1["status"] == "running"
        step_task = get_collection("ai_task_queue").find_one(
            {"goal_id": goal_id, "payload.step_id": "s1"})
        assert step_task is not None
        assert step_task["payload"]["step_id"] == "s1"

    def test_probe_waits_until_all_done(self, doc_goal_with_agent, monkeypatch):
        """discovery plan 未全完成时，不提前生成目标。"""
        goal_id, agent_id = doc_goal_with_agent
        _stub_steward_planner(monkeypatch)

        goal_runtime.discover_and_plan(goal_id)
        # 人为再挂一个未完成 discovery step，模拟"还有一个分析智能体没回调"
        get_collection("ai_goal_steps").insert_one({
            "goal_id": goal_id, "step_id": "d2", "name": "代码画像扫描",
            "capability_key": "code_scan", "agent_id": "agent_code_scan",
            "depends_on": [], "serves_acceptance": [], "evidence_type": None,
            "can_execute": True, "requires_approval": False, "risk_level": "low",
            "retryable": True, "status": "running", "attempts": [],
            "plan_version": 1, "plan_kind": "discovery", "created_at": int(time.time()),
        })

        done = sched.on_step_done(goal_id, "d1", {
            "capability_key": "requirement_analysis",
            "acceptance_points": ["x"], "test_cases": ["c1"], "summary": "s",
        })
        assert done["ok"] is True

        goal = get_collection("ai_goals").find_one({"goal_id": goal_id})
        assert goal["status"] == "running"
        assert goal["target_state"] == "discovering"
        assert "goal_statement" not in goal  # 目标尚未生成


# ==================== Critic 真实可达天花板 ====================

class TestCriticProducibleCeiling:
    def _goal(self, unmet_evidence, producible, allowed, replan_count=0):
        return {
            "goal_id": "g_critic",
            "acceptance": [
                {"id": "a1", "desc": "已过", "evidence_type": "doc_review", "verdict": "pass"},
                {"id": "a2", "desc": "未过", "evidence_type": unmet_evidence, "verdict": "pending"},
            ],
            "feasibility": {
                "producible_evidence_types": producible,
                "allowed_evidence_types": allowed,
            },
            "replan_count": replan_count,
            "budget": {"max_replans": 3},
        }

    def test_unproducible_unmet_stops_partial_not_replan(self):
        """未达成验收的证据类型源理论可产(allowed)、但无智能体能产(不在 producible)
        → 不空转 replan，诚实停 partial。"""
        goal = self._goal(unmet_evidence="device_test",
                          producible=["doc_review"],
                          allowed=["doc_review", "device_test"])
        d = critic.decide_after_verify(goal)
        assert d["decision"] == "partial"
        assert d["achievable_unmet"] == []

    def test_producible_unmet_triggers_replan(self):
        """未达成验收的证据类型是验证级 + 有智能体能产(∈producible) + 预算未尽 → replan 再攻一轮。"""
        goal = self._goal(unmet_evidence="static_analysis",
                          producible=["static_analysis"],
                          allowed=["static_analysis"])
        d = critic.decide_after_verify(goal)
        assert d["decision"] == "replan"
        assert "a2" in d["achievable_unmet"]

    def test_fallback_to_allowed_when_no_producible(self):
        """老数据无 producible_evidence_types 字段 → 回退用 allowed_evidence_types。"""
        goal = {
            "goal_id": "g_old",
            "acceptance": [
                {"id": "a1", "desc": "未过", "evidence_type": "static_analysis", "verdict": "pending"},
            ],
            "feasibility": {"allowed_evidence_types": ["static_analysis"]},
            "replan_count": 0, "budget": {"max_replans": 3},
        }
        d = critic.decide_after_verify(goal)
        assert d["decision"] == "replan"

    def test_repeated_same_unmet_blocks_auto_replan(self):
        """连续两轮同一未达标集合 → blocked，防止自动重规划无限震荡。"""
        goal = self._goal(unmet_evidence="static_analysis",
                          producible=["static_analysis"],
                          allowed=["static_analysis"],
                          replan_count=0)
        first = critic.decide_after_verify(goal)
        assert first["decision"] == "replan"

        goal["replan_count"] = 1
        goal["last_replan_unmet_signature"] = first["unmet_signature"]
        second = critic.decide_after_verify(goal)
        assert second["decision"] == "blocked"
        assert "连续两轮" in second["reason"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
