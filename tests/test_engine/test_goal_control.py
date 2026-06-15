"""Goal 级 pause/resume/cancel 控制入口回归测试。"""
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from common.db import get_collection
from engine import goal_scheduler
from engine import goal_runtime


COLS = [
    "ai_goals", "ai_goal_steps", "ai_goal_events", "ai_goal_evidence",
    "ai_goal_artifacts", "ai_goal_summary", "ai_task_queue", "ai_memory_points",
    "ai_workspace_agents",
]


def _cleanup(goal_id):
    for c in COLS:
        get_collection(c).delete_many({"goal_id": goal_id})


def _insert_goal(status="running"):
    gid = f"goal_ctrl_{uuid.uuid4().hex[:6]}"
    now = int(time.time())
    get_collection("ai_goals").insert_one({
        "goal_id": gid,
        "title": "控制入口测试",
        "goal_statement": "x",
        "status": status,
        "completion_policy": "auto_complete",
        "current_plan_kind": "objective",
        "acceptance": [],
        "created_at": now,
        "updated_at": now,
    })
    return gid


def test_pause_and_resume_goal(monkeypatch):
    gid = _insert_goal("running")
    calls = []
    monkeypatch.setattr(goal_scheduler, "advance", lambda goal_id: calls.append(goal_id) or {"ok": True})
    try:
        paused = goal_runtime.pause_goal(gid, reason="演示暂停", actor="tester")
        assert paused["ok"] and paused["status"] == "paused"
        goal = get_collection("ai_goals").find_one({"goal_id": gid}, {"_id": 0})
        assert goal["status"] == "paused"

        resumed = goal_runtime.resume_goal(gid, reason="继续", actor="tester")
        assert resumed["ok"] and resumed["status"] == "running"
        assert calls == [gid]
        events = [e["event"] for e in get_collection("ai_goal_events").find({"goal_id": gid}, {"_id": 0})]
        assert "goal_paused" in events and "goal_resumed" in events
    finally:
        _cleanup(gid)


def test_cancel_goal_marks_active_unfinished_steps_and_agents():
    gid = _insert_goal("running")
    get_collection("ai_goal_steps").insert_many([
        {"goal_id": gid, "step_id": "s_pending", "status": "pending", "plan_version": 1},
        {"goal_id": gid, "step_id": "s_running", "status": "running", "plan_version": 1},
        {"goal_id": gid, "step_id": "s_done", "status": "completed", "plan_version": 1},
    ])
    get_collection("ai_workspace_agents").insert_one({
        "goal_id": gid, "agent_id": "agent_x", "status": "running",
    })
    try:
        result = goal_runtime.cancel_goal(gid, reason="不再需要", actor="tester")

        assert result["ok"] and result["status"] == "cancelled"
        goal = get_collection("ai_goals").find_one({"goal_id": gid}, {"_id": 0})
        assert goal["status"] == "cancelled"
        s_pending = get_collection("ai_goal_steps").find_one({"goal_id": gid, "step_id": "s_pending"}, {"_id": 0})
        s_running = get_collection("ai_goal_steps").find_one({"goal_id": gid, "step_id": "s_running"}, {"_id": 0})
        s_done = get_collection("ai_goal_steps").find_one({"goal_id": gid, "step_id": "s_done"}, {"_id": 0})
        assert s_pending["status"] == "cancelled"
        assert s_running["status"] == "skipped"
        assert s_done["status"] == "completed"
        agent = get_collection("ai_workspace_agents").find_one({"goal_id": gid, "agent_id": "agent_x"}, {"_id": 0})
        assert agent["status"] == "cancelled"
        ev = get_collection("ai_goal_events").find_one({"goal_id": gid, "event": "goal_cancelled"}, {"_id": 0})
        assert ev and ev["payload"]["steps_marked"] == 2
    finally:
        _cleanup(gid)
