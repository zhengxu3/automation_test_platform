"""调度死锁回归：步骤失败且有后继依赖时，不能卡死——级联 skipped 解开，进入验收。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import time, uuid, pytest
from common.db import get_collection
from engine import goal_scheduler as sched


@pytest.fixture
def goal_failed_with_dependent():
    gid = f"goal_dl_{uuid.uuid4().hex[:6]}"
    get_collection("ai_goals").insert_one({
        "goal_id": gid, "title": "死锁回归", "goal_statement": "x", "status": "running",
        "completion_policy": "auto_complete", "current_plan_kind": "objective",
        "feasibility": {"allowed_evidence_types": ["static_analysis"], "producible_evidence_types": ["static_analysis"]},
        "budget": {"max_replans": 0}, "plan_version": 1, "round": 1, "replan_count": 0,
        "acceptance": [{"id": "a1", "desc": "x", "evidence_type": "static_analysis", "bound_to": None, "verdict": "pending"}],
        "created_at": int(time.time()),
    })
    # s1 已失败；s2 依赖 s1 且 pending（旧逻辑会永久卡）
    get_collection("ai_goal_steps").insert_many([
        {"goal_id": gid, "step_id": "s1", "name": "失败步", "capability_key": "api_test",
         "agent_id": "agent_api_test", "depends_on": [], "serves_acceptance": ["a1"],
         "evidence_type": "api_test", "can_execute": True, "status": "failed", "attempts": [],
         "plan_version": 1, "plan_kind": "objective"},
        {"goal_id": gid, "step_id": "s2", "name": "后继步", "capability_key": "api_test",
         "agent_id": "agent_api_test", "depends_on": ["s1"], "serves_acceptance": ["a1"],
         "evidence_type": "api_test", "can_execute": True, "status": "pending", "attempts": [],
         "plan_version": 1, "plan_kind": "objective"},
    ])
    yield gid
    for c in ["ai_goals", "ai_goal_steps", "ai_goal_events", "ai_goal_evidence",
              "ai_goal_artifacts", "ai_goal_summary", "ai_task_queue", "ai_memory_points",
              "ai_workspace_agents"]:
        get_collection(c).delete_many({"goal_id": gid})


def test_failed_step_with_dependent_does_not_deadlock(goal_failed_with_dependent, monkeypatch):
    monkeypatch.setattr(sched.steward, "evaluate_and_remember", lambda *a, **k: {"conclusion": "stub"})
    gid = goal_failed_with_dependent
    sched.advance(gid)

    s2 = get_collection("ai_goal_steps").find_one({"goal_id": gid, "step_id": "s2"})
    assert s2["status"] == "skipped", "依赖失败的后继应级联 skipped，而非永久 pending"
    goal = get_collection("ai_goals").find_one({"goal_id": gid})
    # 不再卡 running：max_replans=0 → 直接诚实 partial（关键是没卡死）
    assert goal["status"] in ("partial_completed", "verifying", "completed"), f"不该卡死，实际 {goal['status']}"


def test_watchdog_advances_running_goal_when_all_steps_terminal():
    """gateway 重启窗口可能导致 step 全终态但 goal 仍 running；看门狗应补 advance。"""
    gid = f"goal_wd_{uuid.uuid4().hex[:6]}"
    old = int(time.time()) - 300
    get_collection("ai_goals").insert_one({
        "goal_id": gid, "title": "看门狗自愈", "goal_statement": "x", "status": "running",
        "completion_policy": "auto_complete", "current_plan_kind": "objective",
        "feasibility": {"allowed_evidence_types": ["static_analysis"],
                        "producible_evidence_types": ["static_analysis"]},
        "budget": {"max_replans": 0}, "plan_version": 1, "round": 1, "replan_count": 0,
        "acceptance": [{"id": "a1", "desc": "x", "evidence_type": "static_analysis",
                        "bound_to": None, "verdict": "pending"}],
        "created_at": old, "updated_at": old,
    })
    get_collection("ai_goal_steps").insert_one({
        "goal_id": gid, "step_id": "s1", "name": "已完成但未验收",
        "capability_key": "branch_review", "agent_id": "agent_branch_review",
        "depends_on": [], "serves_acceptance": ["a1"], "evidence_type": "static_analysis",
        "can_execute": True, "status": "completed", "attempts": [],
        "plan_version": 1, "plan_kind": "objective", "updated_at": old,
    })
    try:
        result = sched.recover_stuck_goals(stale_seconds=60)

        assert any(x["goal_id"] == gid for x in result["recovered"])
        goal = get_collection("ai_goals").find_one({"goal_id": gid}, {"_id": 0})
        assert goal["status"] != "running"
        ev = get_collection("ai_goal_events").find_one(
            {"goal_id": gid, "event": "watchdog_advance"}, {"_id": 0})
        assert ev and ev["actor"] == "watchdog"
    finally:
        for c in ["ai_goals", "ai_goal_steps", "ai_goal_events", "ai_goal_evidence",
                  "ai_goal_artifacts", "ai_goal_summary", "ai_task_queue", "ai_memory_points",
                  "ai_workspace_agents"]:
            get_collection(c).delete_many({"goal_id": gid})


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
