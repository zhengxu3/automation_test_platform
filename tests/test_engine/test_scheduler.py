"""Goal Scheduler 调度测试 — DAG 推进/依赖就绪/证据绑定/验收

模拟 step 完成回调，验证调度器的确定性逻辑（不依赖真实 worker）。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import uuid
import pytest
from common.db import get_collection
from engine import goal_scheduler as sched
from engine import state


@pytest.fixture
def goal_with_dag():
    """造一个 running 状态的 Goal + 3步 DAG（s1 → s2,s3）"""
    goal_id = f"goal_sched_{uuid.uuid4().hex[:6]}"
    get_collection("ai_goals").insert_one({
        "goal_id": goal_id,
        "title": "调度测试",
        "goal_statement": "验证调度推进",
        "completion_policy": "auto_complete",
        "status": "running",
        "acceptance": [
            {"id": "a1", "desc": "需求拆解完成", "evidence_type": "doc_review", "bound_to": None, "verdict": "pending"},
            {"id": "a2", "desc": "用例生成完成", "evidence_type": "testcase_generated", "bound_to": None, "verdict": "pending"},
        ],
        "created_at": int(time.time()),
    })
    steps = [
        {"goal_id": goal_id, "step_id": "s1", "name": "需求分析", "capability_key": "requirement_analysis",
         "agent_id": "agent_req_analysis", "depends_on": [], "serves_acceptance": ["a1"],
         "evidence_type": "doc_review", "can_execute": True, "requires_approval": False,
         "risk_level": "low", "retryable": True, "status": "pending", "attempts": [], "plan_version": 1},
        {"goal_id": goal_id, "step_id": "s2", "name": "用例生成", "capability_key": "requirement_analysis",
         "agent_id": "agent_req_analysis", "depends_on": ["s1"], "serves_acceptance": ["a2"],
         "evidence_type": "testcase_generated", "can_execute": True, "requires_approval": False,
         "risk_level": "low", "retryable": True, "status": "pending", "attempts": [], "plan_version": 1},
    ]
    get_collection("ai_goal_steps").insert_many([dict(s) for s in steps])

    yield goal_id

    # 清理
    for c in ["ai_goals", "ai_goal_steps", "ai_goal_events", "ai_goal_evidence",
              "ai_goal_artifacts", "ai_goal_summary", "ai_task_queue"]:
        get_collection(c).delete_many({"goal_id": goal_id})


class TestScheduler:
    def test_advance_submits_ready_step(self, goal_with_dag):
        """推进：只提交依赖就绪的 step（s1 无依赖先提交，s2 等 s1）"""
        result = sched.advance(goal_with_dag)
        assert result["ok"]
        assert "s1" in result["submitted"]
        assert "s2" not in result["submitted"]  # s2 依赖 s1，未就绪

        # s1 应该进 running，且有任务入队
        s1 = get_collection("ai_goal_steps").find_one({"goal_id": goal_with_dag, "step_id": "s1"})
        assert s1["status"] == "running"
        task = get_collection("ai_task_queue").find_one({"goal_id": goal_with_dag, "step_id": "s1"})
        assert task is not None
        assert task["idempotency_key"]  # 幂等键存在

        ev = get_collection("ai_goal_events").find_one(
            {"goal_id": goal_with_dag, "event": "step_submitted", "payload.step_id": "s1"},
            {"_id": 0},
        )
        assert ev is not None
        payload = ev["payload"]
        assert payload["agent_id"] == "agent_req_analysis"
        assert payload["step_name"] == "需求分析"
        assert payload["serves_acceptance"][0]["id"] == "a1"
        assert payload["serves_acceptance"][0]["desc"] == "需求拆解完成"
        assert payload["activation_reason"]
        assert "doc_content" in payload["input_keys"]

    def test_step_done_binds_evidence(self, goal_with_dag):
        """step 完成 → 绑定证据到 acceptance（准备级证据 doc_review → verdict=prepared，非 pass）"""
        sched.advance(goal_with_dag)  # 提交 s1
        # 模拟 s1 完成
        sched.on_step_done(goal_with_dag, "s1",
                           {"acceptance_points": ["登录", "崩溃"], "summary": "需求拆解完成", "confidence": 0.9})

        # a1 应该绑定了证据；doc_review 是准备级 → prepared（生成≠业务通过）
        goal = get_collection("ai_goals").find_one({"goal_id": goal_with_dag})
        a1 = next(a for a in goal["acceptance"] if a["id"] == "a1")
        assert a1["bound_to"] is not None
        assert a1["verdict"] == "prepared"

        # s2 应该被推进（s1 完成后依赖就绪）
        s2 = get_collection("ai_goal_steps").find_one({"goal_id": goal_with_dag, "step_id": "s2"})
        assert s2["status"] == "running"

    def test_full_completion(self, goal_with_dag):
        """全流程：s1/s2 完成，但只产准备级证据(doc_review/testcase_generated)
        → 验收点只是 prepared（用例已生成、未业务验证）→ 诚实停在 partial_completed，不假装 completed"""
        sched.advance(goal_with_dag)
        sched.on_step_done(goal_with_dag, "s1",
                           {"acceptance_points": ["x"], "summary": "s1完成", "confidence": 0.9})
        sched.on_step_done(goal_with_dag, "s2",
                           {"acceptance_points": ["c1"], "test_cases": ["c1"], "summary": "s2完成", "confidence": 0.9})

        goal = get_collection("ai_goals").find_one({"goal_id": goal_with_dag})
        assert goal["status"] == "partial_completed"

        # 生成了总结快照
        summary = get_collection("ai_goal_summary").find_one({"goal_id": goal_with_dag})
        assert summary is not None
        assert summary["final_status"] == "partial_completed"
        assert summary["execution_stats"]["completed"] == 2
        assert summary["evidence_collected"] >= 2

        # 输入画像和本轮结果画像分开：只产准备级证据，不应被标为实际执行过。
        goal = get_collection("ai_goals").find_one({"goal_id": goal_with_dag})
        feasibility = goal.get("feasibility", {})
        assert feasibility["runtime_executable"] is False
        assert feasibility["runtime_execution_evidence_types"] == []
        assert set(feasibility["runtime_evidence_types"]) == {"doc_review", "testcase_generated"}

    def test_idempotency_key_deterministic(self, goal_with_dag):
        """幂等键：相同输入产生相同 key"""
        k1 = sched._idempotency_key(goal_with_dag, "s1", 1, [])
        k2 = sched._idempotency_key(goal_with_dag, "s1", 1, [])
        assert k1 == k2
        # 不同 step 不同 key
        k3 = sched._idempotency_key(goal_with_dag, "s2", 1, [])
        assert k1 != k3


class TestDegradation:
    def test_non_executable_degrades(self):
        """不可执行的 step → 降级（走 fallback）"""
        goal_id = f"goal_deg_{uuid.uuid4().hex[:6]}"
        get_collection("ai_goals").insert_one({
            "goal_id": goal_id, "title": "降级测试", "status": "running",
            "completion_policy": "auto_complete",
            "acceptance": [{"id": "a1", "desc": "真机验证", "evidence_type": "device_test", "bound_to": None, "verdict": "pending"}],
            "created_at": int(time.time()),
        })
        get_collection("ai_goal_steps").insert_one({
            "goal_id": goal_id, "step_id": "s1", "name": "UI验证", "capability_key": "script_gen",
            "agent_id": "agent_ui_automation", "depends_on": [], "serves_acceptance": ["a1"],
            "evidence_type": "device_test", "can_execute": False,  # 不可执行
            "requires_approval": True, "risk_level": "high", "retryable": True,
            "fallback": "doc_review", "status": "pending", "attempts": [], "plan_version": 1,
        })

        sched.advance(goal_id)
        s1 = get_collection("ai_goal_steps").find_one({"goal_id": goal_id, "step_id": "s1"})
        assert s1["status"] == "degraded"
        assert s1.get("fallback_applied") == "doc_review"

        # 清理
        for c in ["ai_goals", "ai_goal_steps", "ai_goal_events", "ai_goal_summary"]:
            get_collection(c).delete_many({"goal_id": goal_id})

    def test_degraded_cascade_does_not_stall(self):
        """同步降级级联：s1 降级(终态) → advance 同轮应解锁依赖 s1 的 s2，不卡死在 pending。

        s1/s2 都不可执行（都降级），advance 一次调用应推进到全终态并进入验收。
        """
        goal_id = f"goal_casc_{uuid.uuid4().hex[:6]}"
        get_collection("ai_goals").insert_one({
            "goal_id": goal_id, "title": "降级级联", "status": "running",
            "completion_policy": "auto_complete",
            "acceptance": [{"id": "a1", "desc": "真机验证", "evidence_type": "device_test",
                            "bound_to": None, "verdict": "pending"}],
            "created_at": int(time.time()),
        })
        get_collection("ai_goal_steps").insert_many([
            {"goal_id": goal_id, "step_id": "s1", "name": "UI验证1", "capability_key": "script_gen",
             "agent_id": "agent_ui", "depends_on": [], "serves_acceptance": ["a1"],
             "evidence_type": "device_test", "can_execute": False, "requires_approval": True,
             "risk_level": "high", "retryable": True, "fallback": "doc_review",
             "status": "pending", "attempts": [], "plan_version": 1},
            {"goal_id": goal_id, "step_id": "s2", "name": "UI验证2", "capability_key": "script_gen",
             "agent_id": "agent_ui", "depends_on": ["s1"], "serves_acceptance": ["a1"],
             "evidence_type": "device_test", "can_execute": False, "requires_approval": True,
             "risk_level": "high", "retryable": True, "fallback": "doc_review",
             "status": "pending", "attempts": [], "plan_version": 1},
        ])

        sched.advance(goal_id)  # 单次调用应级联推进 s1→s2

        s1 = get_collection("ai_goal_steps").find_one({"goal_id": goal_id, "step_id": "s1"})
        s2 = get_collection("ai_goal_steps").find_one({"goal_id": goal_id, "step_id": "s2"})
        assert s1["status"] == "degraded"
        assert s2["status"] == "degraded", "依赖降级 step 的后继必须被解锁，不能卡在 pending"

        # 全终态 → 已进入验收并判定（无 pass 证据 → partial_completed）
        goal = get_collection("ai_goals").find_one({"goal_id": goal_id})
        assert goal["status"] in ("partial_completed", "verifying")

        for c in ["ai_goals", "ai_goal_steps", "ai_goal_events", "ai_goal_summary"]:
            get_collection(c).delete_many({"goal_id": goal_id})


class TestRuntimeFeasibility:
    def test_api_evidence_marks_runtime_executable_even_when_failed(self):
        """本轮只要真实产出执行级证据，即使业务 fail，也应标记为已执行过。"""
        goal_id = f"goal_runtime_feas_{uuid.uuid4().hex[:6]}"
        now = int(time.time())
        get_collection("ai_goals").insert_one({
            "goal_id": goal_id,
            "title": "运行画像测试",
            "status": "running",
            "completion_policy": "auto_complete",
            "current_plan_kind": "objective",
            "feasibility": {"executable": True, "allowed_evidence_types": ["api_test"]},
            "acceptance": [{
                "id": "a_api",
                "desc": "接口验证",
                "evidence_type": "api_test",
                "bound_to": "ev_api",
                "verdict": "fail",
            }],
            "created_at": now,
            "updated_at": now,
        })
        get_collection("ai_goal_evidence").insert_one({
            "goal_id": goal_id,
            "evidence_id": "ev_api",
            "step_id": "s_api",
            "acceptance_id": "a_api",
            "type": "api_test",
            "verdict": "fail",
            "created_at": now,
        })

        try:
            snapshot = sched._refresh_runtime_feasibility(goal_id)

            assert snapshot["runtime_executable"] is True
            assert snapshot["runtime_execution_evidence_types"] == ["api_test"]
            assert snapshot["runtime_evidence_counts"]["fail"] == 1

            goal = get_collection("ai_goals").find_one({"goal_id": goal_id})
            feasibility = goal["feasibility"]
            assert feasibility["executable"] is True
            assert feasibility["runtime_executable"] is True
        finally:
            for c in ["ai_goals", "ai_goal_steps", "ai_goal_events", "ai_goal_evidence",
                      "ai_goal_artifacts", "ai_goal_summary", "ai_task_queue"]:
                get_collection(c).delete_many({"goal_id": goal_id})


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
