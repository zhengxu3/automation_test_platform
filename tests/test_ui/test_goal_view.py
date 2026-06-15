"""Goal 模式 UI 自动化测试 — 验证 GoalList + GoalDetail 可视化

覆盖：
- Goal 列表页加载 + 创建弹窗
- 创建 Goal（doc_only）→ 跳转详情
- 可行性画像屏展示
- DAG 步骤卡片
- 记忆体决策流
- Step 详情弹窗
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import uuid
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


@pytest.fixture(scope="module")
def prepared_goal():
    """预置一个已完成规划的 Goal（doc_only，直接造数据避免等 LLM）"""
    goal_id = f"goal_uitest_{uuid.uuid4().hex[:6]}"
    now = int(time.time())
    get_collection("ai_goals").insert_one({
        "goal_id": goal_id,
        "title": "UI测试-登录验证Goal",
        "trigger": "manual",
        "completion_policy": "auto_complete",
        "status": "running",
        "goal_statement": "登录流程稳定可用，崩溃问题修复",
        "goal_confidence": 0.85,
        "acceptance": [
            {"id": "a1", "desc": "需求拆解完成", "evidence_type": "doc_review", "bound_to": None, "verdict": "pending"},
            {"id": "a2", "desc": "测试用例生成", "evidence_type": "testcase_generated", "bound_to": None, "verdict": "pending"},
        ],
        "feasibility": {
            "input_mode": "doc_only",
            "allowed_evidence_types": ["doc_review", "testcase_generated"],
            "blocked_evidence_types": ["static_analysis", "api_test", "device_test", "e2e_test"],
            "executable": False,
        },
        "created_at": now, "updated_at": now,
    })
    get_collection("ai_goal_steps").insert_many([
        {"goal_id": goal_id, "step_id": "s1", "name": "需求分析与用例生成", "capability_key": "requirement_analysis",
         "agent_id": "agent_req_analysis", "depends_on": [], "serves_acceptance": ["a1", "a2"],
         "evidence_type": "doc_review", "can_execute": True, "requires_approval": False,
         "risk_level": "low", "needs_upgrade": False, "rationale": "解析需求拆出验收点",
         "status": "completed", "attempts": [{"attempt_no": 1, "status": "completed", "output_summary": "拆出2个验收点"}],
         "plan_version": 1, "created_at": now},
    ])
    get_collection("ai_goal_events").insert_many([
        {"goal_id": goal_id, "event": "profiling_started", "actor": "system", "payload": {}, "timestamp": now},
        {"goal_id": goal_id, "event": "feasibility_profiled", "actor": "profiler",
         "payload": {"input_mode": "doc_only", "executable": False}, "timestamp": now + 1},
        {"goal_id": goal_id, "event": "goal_generated", "actor": "steward",
         "payload": {"acceptance_count": 2, "confidence": 0.85}, "timestamp": now + 2},
        {"goal_id": goal_id, "event": "plan_generated", "actor": "planner",
         "payload": {"step_count": 1, "plan_summary": "单步需求分析"}, "timestamp": now + 3},
    ])
    get_collection("ai_goal_evidence").insert_one({
        "evidence_id": "ev_test1", "goal_id": goal_id, "step_id": "s1", "acceptance_id": "a1",
        "type": "doc_review", "verdict": "pass", "summary": "需求拆解评审通过",
        "confidence": 0.9, "plan_version": 1, "created_at": now,
    })
    get_collection("ai_memory_points").insert_one({
        "point_id": "mp_test1", "goal_id": goal_id, "step_id": "s1", "agent_id": "agent_req_analysis",
        "summary": "登录模块需求已拆解为2个验收点", "layer": "inference", "verified": False,
        "source": "steward_evaluation", "created_at": now,
    })

    yield goal_id

    for c in ["ai_goals", "ai_goal_steps", "ai_goal_events", "ai_goal_evidence", "ai_memory_points"]:
        get_collection(c).delete_many({"goal_id": goal_id})


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    yield pg
    ctx.close()


class TestGoalList:
    def test_list_page_loads(self, page):
        page.goto(f"{BASE_URL}/goals")
        page.wait_for_load_state("networkidle")
        expect(page.locator("text=Goal 自主任务")).to_be_visible()
        expect(page.locator("text=创建 Goal")).to_be_visible()

    def test_create_dialog(self, page):
        page.goto(f"{BASE_URL}/goals")
        page.wait_for_load_state("networkidle")
        page.click("text=创建 Goal")
        page.wait_for_selector("[data-testid='goal-title']", timeout=5000)
        # 完成策略选项
        expect(page.locator("text=达标即停")).to_be_visible()
        expect(page.locator("text=持续守护")).to_be_visible()


class TestGoalDetail:
    def test_detail_loads(self, page, prepared_goal):
        page.goto(f"{BASE_URL}/goal/{prepared_goal}")
        page.wait_for_load_state("networkidle")
        expect(page.locator("text=UI测试-登录验证Goal")).to_be_visible()
        expect(page.locator("text=执行中")).to_be_visible()

    def test_feasibility_panel(self, page, prepared_goal):
        """点击受限卡片 → 可行性画像弹窗"""
        page.goto(f"{BASE_URL}/goal/{prepared_goal}")
        page.wait_for_load_state("networkidle")
        page.locator("[data-testid='stat-feasibility']").click()
        page.wait_for_selector("[data-testid='feasibility-panel']", timeout=5000)
        expect(page.locator("text=本次可以证明")).to_be_visible()
        expect(page.locator("text=暂时无法证明")).to_be_visible()

    def test_plan_list_left(self, page, prepared_goal):
        """列1 Plan 叙事列表"""
        page.goto(f"{BASE_URL}/goal/{prepared_goal}")
        page.wait_for_load_state("networkidle")
        expect(page.locator("text=执行计划").first).to_be_visible()
        items = page.locator("[data-testid='plan-item']")
        assert items.count() >= 1
        expect(page.locator("text=需求分析与用例生成").first).to_be_visible()

    def test_agent_column(self, page, prepared_goal):
        """列2 现有智能体（解释 plan 执行）"""
        page.goto(f"{BASE_URL}/goal/{prepared_goal}")
        page.wait_for_load_state("networkidle")
        expect(page.locator("text=🤖 智能体").first).to_be_visible()
        agents = page.locator("[data-testid='agent-item']")
        assert agents.count() >= 1

    def test_track_node(self, page, prepared_goal):
        """执行跑道横向节点"""
        page.goto(f"{BASE_URL}/goal/{prepared_goal}")
        page.wait_for_load_state("networkidle")
        expect(page.locator("text=执行跑道")).to_be_visible()
        nodes = page.locator("[data-testid='track-node']")
        assert nodes.count() >= 1

    def test_evidence_dialog(self, page, prepared_goal):
        """点击证据卡片 → 弹窗"""
        page.goto(f"{BASE_URL}/goal/{prepared_goal}")
        page.wait_for_load_state("networkidle")
        page.locator("[data-testid='stat-evidence']").click()
        page.wait_for_timeout(500)
        expect(page.locator("h3:has-text('证据')")).to_be_visible()

    def test_memory_dialog(self, page, prepared_goal):
        """点击记忆卡片 → 弹窗"""
        page.goto(f"{BASE_URL}/goal/{prepared_goal}")
        page.wait_for_load_state("networkidle")
        page.locator("[data-testid='stat-memory']").click()
        page.wait_for_timeout(500)
        expect(page.locator("h3:has-text('记忆')")).to_be_visible()

    def test_decision_stream(self, page, prepared_goal):
        """记忆体工作流（大决策流）"""
        page.goto(f"{BASE_URL}/goal/{prepared_goal}")
        page.wait_for_load_state("networkidle")
        expect(page.locator("text=记忆体工作流").first).to_be_visible()
        items = page.locator("[data-testid='event-item']")
        assert items.count() >= 1
        # actor 名称展示
        expect(page.locator("text=可行性画像").first).to_be_visible()

    def test_chat_input(self, page, prepared_goal):
        """与记忆体交谈输入框"""
        page.goto(f"{BASE_URL}/goal/{prepared_goal}")
        page.wait_for_load_state("networkidle")
        chat = page.locator("[data-testid='chat-input']")
        expect(chat).to_be_visible()
        expect(chat).to_be_enabled()

    def test_step_detail_dialog(self, page, prepared_goal):
        """点击 plan 项打开详情"""
        page.goto(f"{BASE_URL}/goal/{prepared_goal}")
        page.wait_for_load_state("networkidle")
        page.locator("[data-testid='plan-item']").first.click()
        page.wait_for_timeout(500)
        expect(page.locator("text=规划理由")).to_be_visible()
        expect(page.locator("text=执行记录").first).to_be_visible()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
