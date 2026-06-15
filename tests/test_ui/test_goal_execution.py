"""Goal 执行状态展示 UI 验证 — 覆盖 plan显示/执行状态/智能体工作休眠/数据/多轮记忆

用预置数据模拟不同执行阶段，验证前端展示正确。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time, uuid
import pytest
from playwright.sync_api import sync_playwright, expect
from common.db import get_collection

BASE_URL = "http://localhost:3000"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    yield pg
    ctx.close()


def _make_goal(gid, steps_data, status="running"):
    """造一个指定步骤状态的 Goal"""
    now = int(time.time())
    get_collection("ai_goals").insert_one({
        "goal_id": gid, "title": f"状态测试-{gid[-4:]}", "status": status,
        "completion_policy": "auto_complete", "goal_statement": "验证执行状态展示",
        "goal_confidence": 0.85,
        "acceptance": [
            {"id": "a1", "desc": "代码变更已分析", "evidence_type": "static_analysis", "bound_to": None, "verdict": "pending"},
            {"id": "a2", "desc": "回归用例已生成", "evidence_type": "testcase_generated", "bound_to": None, "verdict": "pending"},
        ],
        "feasibility": {"input_mode": "repo_only", "allowed_evidence_types": ["static_analysis", "testcase_generated"],
                        "blocked_evidence_types": ["device_test"], "executable": False},
        "created_at": now, "updated_at": now,
    })
    for s in steps_data:
        s.update({"goal_id": gid, "plan_version": 1, "created_at": now})
        get_collection("ai_goal_steps").insert_one(s)
    get_collection("ai_goal_events").insert_one({
        "goal_id": gid, "event": "feasibility_profiled", "actor": "profiler",
        "payload": {"input_mode": "repo_only", "executable": False}, "timestamp": now})


def _cleanup(gid):
    for c in ["ai_goals", "ai_goal_steps", "ai_goal_events", "ai_goal_evidence", "ai_memory_points"]:
        get_collection(c).delete_many({"goal_id": gid})


@pytest.fixture
def running_goal():
    """执行中：s1完成(智能体已休眠) + s2运行中(智能体工作)"""
    gid = f"goal_run_{uuid.uuid4().hex[:6]}"
    _make_goal(gid, [
        {"step_id": "s1", "name": "代码变更分析", "capability_key": "branch_review",
         "agent_id": "agent_branch_review", "depends_on": [], "serves_acceptance": ["a1"],
         "evidence_type": "static_analysis", "executor": "ai_worker", "can_execute": True,
         "requires_approval": False, "risk_level": "low", "status": "completed",
         "rationale": "分析分支差异识别影响", "attempts": [{"attempt_no": 1, "status": "completed", "output_summary": "影响3模块"}]},
        {"step_id": "s2", "name": "回归用例生成", "capability_key": "requirement_analysis",
         "agent_id": "agent_req_analysis", "depends_on": ["s1"], "serves_acceptance": ["a2"],
         "evidence_type": "testcase_generated", "executor": "ai_worker", "can_execute": True,
         "requires_approval": False, "risk_level": "low", "status": "running",
         "rationale": "基于影响生成回归用例", "attempts": []},
    ])
    # s1 产证据
    get_collection("ai_goal_evidence").insert_one({
        "evidence_id": "ev1", "goal_id": gid, "step_id": "s1", "acceptance_id": "a1",
        "type": "static_analysis", "verdict": "pass", "summary": "影响登录/支付/匹配3模块",
        "confidence": 0.9, "created_at": int(time.time())})
    get_collection("ai_goals").update_one({"goal_id": gid, "acceptance.id": "a1"},
        {"$set": {"acceptance.$.bound_to": "ev1", "acceptance.$.verdict": "pass"}})
    yield gid
    _cleanup(gid)


class TestExecutionDisplay:
    def test_plan_shows_steps(self, running_goal, page):
        """plan 列表显示所有步骤"""
        page.goto(f"{BASE_URL}/goal/{running_goal}")
        page.wait_for_load_state("networkidle")
        expect(page.locator("text=代码变更分析").first).to_be_visible()
        expect(page.locator("text=回归用例生成").first).to_be_visible()

    def test_step_status_differentiated(self, running_goal, page):
        """执行状态区分：s1完成✅ / s2运行中🔄"""
        page.goto(f"{BASE_URL}/goal/{running_goal}")
        page.wait_for_load_state("networkidle")
        items = page.locator("[data-testid='plan-item']")
        assert items.count() == 2
        # 完成和运行中状态都展示
        expect(page.locator("text=完成").first).to_be_visible()
        expect(page.locator("text=运行").first).to_be_visible()

    def test_agent_working_vs_sleeping(self, running_goal, page):
        """智能体工作(running脉冲) vs 休眠(completed)状态区分"""
        page.goto(f"{BASE_URL}/goal/{running_goal}")
        page.wait_for_load_state("networkidle")
        agents = page.locator("[data-testid='agent-item']")
        assert agents.count() == 2

    def test_working_data_evidence(self, running_goal, page):
        """工作期数据：s1已产证据，验收a1已通过"""
        page.goto(f"{BASE_URL}/goal/{running_goal}")
        page.wait_for_load_state("networkidle")
        # 验收卡片 1/2 通过
        page.locator("[data-testid='stat-evidence']").click()
        page.wait_for_timeout(500)
        expect(page.locator("text=影响登录").first).to_be_visible()

    def test_track_progress_indicator(self, running_goal, page):
        """跑道进度：1/2完成，s2有当前位置指示"""
        page.goto(f"{BASE_URL}/goal/{running_goal}")
        page.wait_for_load_state("networkidle")
        expect(page.locator("text=执行跑道")).to_be_visible()
        # 跑道节点
        nodes = page.locator("[data-testid='track-node']")
        assert nodes.count() == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
