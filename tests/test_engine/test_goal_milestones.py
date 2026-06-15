"""终态里程碑事件 + summary 测试（确定性：全 pass 验收，不经 LLM）。

覆盖 _verify_goal 的 complete 分支：
  - auto_complete → completed + goal_completed 事件 + summary；
  - continuous   → guarding + goal_guarding 事件 + summary。
以及 cancel_goal 写终态 summary。
"""
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from engine import goal_scheduler, goal_runtime
from common.db import get_collection


def _setup_goal(policy: str) -> str:
    gid = f"goal_milestone_{uuid.uuid4().hex[:8]}"
    get_collection("ai_goals").insert_one({
        "goal_id": gid,
        "title": "里程碑测试",
        "status": "running",
        "completion_policy": policy,
        "auto_replan": True,
        "current_plan_kind": "objective",
        "sources": [],
        "acceptance": [
            {"id": "a1", "desc": "API 通过", "evidence_type": "api_test",
             "verdict": "pass", "bound_to": "ev1"},
        ],
        "goal_statement": "x",
        "feasibility": {"allowed_evidence_types": ["api_test"]},
        "plan_version": 1, "round": 1, "replan_count": 0,
        "created_at": int(time.time()),
    })
    get_collection("ai_goal_steps").insert_one({
        "goal_id": gid, "step_id": "s1", "status": "completed", "plan_version": 1,
    })
    return gid


def _events(gid):
    return [e["event"] for e in get_collection("ai_goal_events").find({"goal_id": gid})]


def _cleanup(gid):
    for c in ["ai_goals", "ai_goal_steps", "ai_goal_events", "ai_goal_evidence",
              "ai_goal_artifacts", "ai_goal_summary", "ai_memory_points"]:
        get_collection(c).delete_many({"goal_id": gid})


def test_auto_complete_emits_goal_completed_and_summary():
    gid = _setup_goal("auto_complete")
    try:
        result = goal_scheduler._verify_goal(gid)
        assert result["status"] == "completed"
        assert "goal_completed" in _events(gid)
        summary = get_collection("ai_goal_summary").find_one({"goal_id": gid}, {"_id": 0})
        assert summary and summary["final_status"] == "completed"
        assert summary["acceptance_summary"][0]["verdict"] == "pass"
    finally:
        _cleanup(gid)


def test_continuous_emits_goal_guarding_and_summary():
    gid = _setup_goal("continuous")
    try:
        result = goal_scheduler._verify_goal(gid)
        assert result["status"] == "guarding"
        assert "goal_guarding" in _events(gid)
        summary = get_collection("ai_goal_summary").find_one({"goal_id": gid}, {"_id": 0})
        assert summary and summary["final_status"] == "guarding"
    finally:
        _cleanup(gid)


def test_cancel_writes_terminal_summary():
    gid = _setup_goal("auto_complete")
    try:
        result = goal_runtime.cancel_goal(gid, reason="测试取消")
        assert result["status"] == "cancelled"
        assert "goal_cancelled" in _events(gid)
        summary = get_collection("ai_goal_summary").find_one({"goal_id": gid}, {"_id": 0})
        assert summary and summary["final_status"] == "cancelled"
    finally:
        _cleanup(gid)
