"""Worker → Scheduler 回调对接测试

验证：worker 执行完 handler 后拿到的结构化产出，能通过契约校验、绑定证据、推进 DAG。
覆盖两层：
1. 契约层 — 3 个执行层 handler 的产出形状满足各自契约 success 判定
2. 回调层 — _notify_goal_scheduler 正确网关（goal 任务才回调）+ on_step_done 全链路推进
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import asyncio
import time
import uuid
import pytest

from common.db import get_collection
from engine import goal_scheduler as sched
from engine.contracts import check_success


# ==================== 契约层：handler 产出形状满足契约 ====================

class TestHandlerOutputContracts:
    def test_requirement_analysis_output_passes(self):
        """requirement_analysis 产出（有验收点）→ 契约通过"""
        output = {
            "acceptance_points": ["登录3步通过", "异常提示正确"],
            "test_cases": ["c1", "c2"],
            "docs": ["需求拆解", "测试用例"],
            "summary": "需求分析完成",
            "confidence": 0.85,
        }
        assert check_success("requirement_analysis", output) is True

    def test_requirement_analysis_empty_fails(self):
        """无验收点 → 契约判失败（不假装成功）"""
        assert check_success("requirement_analysis", {"acceptance_points": []}) is False

    def test_branch_review_with_cases_passes(self):
        """branch_review 有回归用例 → 通过"""
        output = {"change_summary": "改了登录", "regression_cases": ["回归登录"], "no_change": False}
        assert check_success("branch_review", output) is True

    def test_branch_review_no_change_passes(self):
        """branch_review 明确无变更 → 也算成功（诚实表达）"""
        output = {"change_summary": "无文件变更", "regression_cases": [], "no_change": True}
        assert check_success("branch_review", output) is True

    def test_branch_review_empty_fails(self):
        """既无回归用例又非无变更 → 失败"""
        assert check_success("branch_review", {"regression_cases": [], "no_change": False}) is False

    def test_script_gen_output_passes(self):
        """script_gen 产出脚本路径 → 通过"""
        output = {"script_path": "/x/test_main.py", "covered_cases": ["登录"]}
        assert check_success("script_gen", output) is True

    def test_script_gen_no_path_fails(self):
        assert check_success("script_gen", {"script_path": ""}) is False


# ==================== 回调层：on_step_done 全链路 ====================

@pytest.fixture
def goal_with_dag():
    """running 状态 Goal + 2 步 DAG（s1 需求分析 → s2 用例生成）"""
    goal_id = f"goal_cb_{uuid.uuid4().hex[:6]}"
    get_collection("ai_goals").insert_one({
        "goal_id": goal_id,
        "title": "worker回调测试",
        "goal_statement": "验证 worker 回调推进",
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

    for c in ["ai_goals", "ai_goal_steps", "ai_goal_events", "ai_goal_evidence",
              "ai_goal_artifacts", "ai_goal_summary", "ai_task_queue", "ai_memory_points"]:
        get_collection(c).delete_many({"goal_id": goal_id})


class TestWorkerCallbackChain:
    def test_handler_output_binds_evidence_and_advances(self, goal_with_dag, monkeypatch):
        """模拟 worker：handler 产出 → on_step_done → 契约通过 + evidence 绑定 + s2 推进。

        Steward 评估走 LLM，这里桩掉以保持聚焦确定性调度逻辑。
        """
        monkeypatch.setattr(sched.steward, "evaluate_and_remember",
                            lambda *a, **k: {"conclusion": "stub"})

        sched.advance(goal_with_dag)  # 提交 s1

        # worker 执行完 requirement_analysis handler 的真实产出形状
        handler_output = {
            "acceptance_points": ["登录拆解", "异常拆解"],
            "test_cases": ["c1"],
            "docs": ["需求拆解"],
            "summary": "需求分析完成：1个文档，1条用例",
            "confidence": 0.85,
        }
        result = sched.on_step_done(goal_with_dag, "s1", handler_output)
        assert result["ok"]

        # a1 绑定证据；doc_review 是准备级 → verdict=prepared（用例已生成≠业务通过）
        goal = get_collection("ai_goals").find_one({"goal_id": goal_with_dag})
        a1 = next(a for a in goal["acceptance"] if a["id"] == "a1")
        assert a1["bound_to"] is not None
        assert a1["verdict"] == "prepared"

        # s1 completed，s2 被推进到 running
        s1 = get_collection("ai_goal_steps").find_one({"goal_id": goal_with_dag, "step_id": "s1"})
        s2 = get_collection("ai_goal_steps").find_one({"goal_id": goal_with_dag, "step_id": "s2"})
        assert s1["status"] == "completed"
        assert s2["status"] == "running"

        # 产出落 artifact
        art = get_collection("ai_goal_artifacts").find_one({"goal_id": goal_with_dag, "step_id": "s1"})
        assert art is not None

    def test_failed_output_does_not_bind_evidence(self, goal_with_dag, monkeypatch):
        """handler 产出不满足契约（无验收点）→ 不绑证据，走失败/重试路径"""
        monkeypatch.setattr(sched.steward, "evaluate_and_remember",
                            lambda *a, **k: {"conclusion": "stub"})
        sched.advance(goal_with_dag)

        # 空产出，契约判失败
        sched.on_step_done(goal_with_dag, "s1", {"acceptance_points": []})

        goal = get_collection("ai_goals").find_one({"goal_id": goal_with_dag})
        a1 = next(a for a in goal["acceptance"] if a["id"] == "a1")
        assert a1["bound_to"] is None  # 失败不绑证据


# ==================== 网关层：_notify_goal_scheduler ====================

class TestNotifyGateway:
    def test_skips_when_not_goal_task(self, monkeypatch):
        """req 模式任务（payload 无 goal_id）→ 不回调调度器"""
        from engine import goal_scheduler
        called = {"n": 0}
        monkeypatch.setattr(goal_scheduler, "on_step_done",
                            lambda *a, **k: called.__setitem__("n", called["n"] + 1))

        from ai_worker.main import _notify_goal_scheduler
        asyncio.run(_notify_goal_scheduler({"req_id": "req_x"}, {"x": 1}, success=True))
        assert called["n"] == 0

    def test_calls_when_goal_task(self, monkeypatch):
        """goal 任务（payload 含 goal_id+step_id）→ 透传 output/success 回调调度器"""
        from engine import goal_scheduler
        seen = {}
        def fake(goal_id, step_id, output, success):
            seen.update(goal_id=goal_id, step_id=step_id, output=output, success=success)
            return {"ok": True}
        monkeypatch.setattr(goal_scheduler, "on_step_done", fake)

        from ai_worker.main import _notify_goal_scheduler
        payload = {"goal_id": "g1", "step_id": "s1", "agent_id": "a"}
        out = {"acceptance_points": ["x"]}
        asyncio.run(_notify_goal_scheduler(payload, out, success=True))
        assert seen == {"goal_id": "g1", "step_id": "s1", "output": out, "success": True}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
