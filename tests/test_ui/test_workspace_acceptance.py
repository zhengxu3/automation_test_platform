"""UI 验收测试：半自动模式所有模块展示验证
覆盖：智能体日志/产出、记忆体评估结论、Case 列表、文档内容、对话功能
使用已有需求 req_3552c7fa（有完整产出链路数据）
"""
import pytest
from playwright.sync_api import sync_playwright, expect

BASE_URL = "http://localhost:3000"
REQ_ID = "req_87517071"  # 有完整产出链路的需求（PDF上传+分析+文档+记忆）
REQ_ID_DEVICE = "req_3552c7fa"  # 有设备执行结果的需求
WS_URL = f"{BASE_URL}/workspace/{REQ_ID}"
WS_URL_DEVICE = f"{BASE_URL}/workspace/{REQ_ID_DEVICE}"


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


# ==================== 基础页面 ====================

class TestWorkspaceBasic:
    def test_page_loads(self, page):
        """工作空间正确加载，显示需求标题和状态"""
        page.goto(WS_URL)
        page.wait_for_load_state("networkidle")
        expect(page.locator("text=女女匹配策略验证")).to_be_visible()
        expect(page.locator("text=就绪")).to_be_visible()

    def test_agent_card_shows_status(self, page):
        """智能体卡片显示完成状态和 token 消耗"""
        page.goto(WS_URL)
        page.wait_for_load_state("networkidle")
        expect(page.locator("text=已安装").first).to_be_visible()
        # UI 自动化智能体应该显示 completed
        expect(page.locator("text=已完成").first).to_be_visible()

    def test_stats_cards_have_data(self, page):
        """统计卡片有真实数据（非 0）"""
        page.goto(WS_URL)
        page.wait_for_load_state("networkidle")
        # 记忆点 > 0
        memory_card = page.get_by_text("🧠 记忆点", exact=True).locator("..")
        memory_value = memory_card.locator(".font-mono").text_content()
        assert int(memory_value) > 0, f"记忆点应该 > 0，实际: {memory_value}"


# ==================== 日志区域 ====================

class TestLogs:
    def test_logs_show_execution_flow(self, page):
        """日志区域展示完整执行流程"""
        page.goto(WS_URL)
        page.wait_for_load_state("networkidle")
        expect(page.locator("text=结论 & 日志")).to_be_visible()
        # 分析日志
        expect(page.locator("text=分析完成").first).to_be_visible()

    def test_logs_show_device_result(self, page):
        """日志展示设备执行结果"""
        page.goto(WS_URL_DEVICE)
        page.wait_for_load_state("networkidle")
        expect(page.locator("text=设备任务完成").first).to_be_visible()

    def test_logs_show_steward_conclusion(self, page):
        """日志展示记忆体主管评估结论"""
        page.goto(WS_URL_DEVICE)
        page.wait_for_load_state("networkidle")
        # 结论标签
        expect(page.locator("text=结论").first).to_be_visible()
        # 评估内容
        expect(page.locator("text=验证").first).to_be_visible()


# ==================== 记忆面板 ====================

class TestMemory:
    def test_memory_dialog_content(self, page):
        """记忆弹窗有评估记忆 + 分析记忆"""
        page.goto(WS_URL)
        page.wait_for_load_state("networkidle")
        page.get_by_text("🧠 记忆点", exact=True).click()
        page.wait_for_selector("h3:has-text('需求记忆')", timeout=5000)

        # 有记忆点
        items = page.locator("[class*='rounded-lg bg-dark-900 border']")
        assert items.count() >= 1, f"记忆点应该 >= 1，实际: {items.count()}"

    def test_memory_shows_source(self, page):
        """记忆点显示来源标识"""
        page.goto(WS_URL)
        page.wait_for_load_state("networkidle")
        page.get_by_text("🧠 记忆点", exact=True).click()
        page.wait_for_selector("h3:has-text('需求记忆')", timeout=5000)
        # 有来源标识（steward_evaluation 或 requirement_analysis）
        page.locator("text=steward_evaluation").or_(page.locator("text=requirement_analysis")).first.wait_for(timeout=3000)

    def test_memory_pin_button(self, page):
        """记忆点有钉选按钮可点击"""
        page.goto(WS_URL)
        page.wait_for_load_state("networkidle")
        page.get_by_text("🧠 记忆点", exact=True).click()
        page.wait_for_selector("h3:has-text('需求记忆')", timeout=5000)
        pin_btn = page.locator("button:has-text('钉选')").first
        expect(pin_btn).to_be_visible()
        # 点击钉选
        pin_btn.click()
        page.wait_for_timeout(500)
        # 应该变成"取消钉选"
        expect(page.locator("text=取消钉选").first).to_be_visible()


# ==================== 文档面板 ====================

class TestDocs:
    def test_docs_three_tabs(self, page):
        """文档弹窗有3个 Tab"""
        page.goto(WS_URL)
        page.wait_for_load_state("networkidle")
        page.get_by_text("📄 文档", exact=True).click()
        page.wait_for_selector("h3:has-text('需求文档')", timeout=5000)
        expect(page.locator("button:has-text('需求拆解')")).to_be_visible()
        expect(page.locator("button:has-text('测试用例')")).to_be_visible()
        expect(page.locator("button:has-text('测试策略')")).to_be_visible()

    def test_docs_content_not_empty(self, page):
        """文档内容非空且有实质内容"""
        page.goto(WS_URL)
        page.wait_for_load_state("networkidle")
        page.get_by_text("📄 文档", exact=True).click()
        page.wait_for_selector("h3:has-text('需求文档')", timeout=5000)
        content = page.locator(".leading-relaxed")
        text = content.text_content()
        assert len(text) > 100, f"文档内容太短: {len(text)} chars"

    def test_docs_tab_switch(self, page):
        """切换 Tab 内容会变化"""
        page.goto(WS_URL)
        page.wait_for_load_state("networkidle")
        page.get_by_text("📄 文档", exact=True).click()
        page.wait_for_selector("h3:has-text('需求文档')", timeout=5000)
        # 记录第一个 Tab 内容
        content1 = page.locator(".leading-relaxed").text_content()
        # 切到测试策略
        page.click("button:has-text('测试策略')")
        page.wait_for_timeout(300)
        content2 = page.locator(".leading-relaxed").text_content()
        assert content1 != content2, "切换 Tab 后内容应该不同"


# ==================== Case 面板 ====================

class TestCases:
    def test_cases_have_data(self, page):
        """Case 弹窗有用例数据"""
        page.goto(WS_URL)
        page.wait_for_load_state("networkidle")
        page.get_by_text("✓ Case", exact=True).click()
        page.wait_for_selector("h3:has-text('测试用例')", timeout=5000)
        expect(page.locator("text=总计")).to_be_visible()

    def test_cases_filter_works(self, page):
        """Case 筛选按钮可用"""
        page.goto(WS_URL)
        page.wait_for_load_state("networkidle")
        page.get_by_text("✓ Case", exact=True).click()
        page.wait_for_selector("h3:has-text('测试用例')", timeout=5000)
        # 点击筛选按钮
        expect(page.locator("button:has-text('有效')")).to_be_visible()
        page.click("button:has-text('有效')")


# ==================== 对话功能 ====================

class TestChat:
    def test_chat_input_enabled(self, page):
        """对话输入框可用"""
        page.goto(WS_URL)
        page.wait_for_load_state("networkidle")
        chat = page.locator("input[placeholder*='记忆体']")
        expect(chat).to_be_visible()
        expect(chat).to_be_enabled()

    def test_chat_send_receives_response(self, page):
        """发送消息能收到 AI 回复"""
        page.goto(WS_URL)
        page.wait_for_load_state("networkidle")
        chat = page.locator("input[placeholder*='记忆体']")
        chat.fill("这个需求目前的测试进度如何？")
        page.click("button:has-text('发送')")
        # 等待思考中 → 回复
        page.wait_for_timeout(1000)
        # 应该有 AI 回复内容出现
        page.locator("text=AI:").wait_for(timeout=30000)
        # 回复不为空
        ai_msgs = page.locator(".text-text-secondary:has-text('AI:')")
        assert ai_msgs.count() > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ==================== 智能体弹窗详细验证 ====================

class TestAgentDialog:
    """点击智能体卡片 → 弹窗内日志/产出/评估都有内容"""

    def test_agent_dialog_opens(self, page):
        """点击智能体卡片打开弹窗"""
        page.goto(WS_URL_DEVICE)
        page.wait_for_load_state("networkidle")
        page.locator("[class*='cursor-pointer']:has-text('UI 自动化')").first.click()
        page.wait_for_selector("button:has-text('日志')", timeout=5000)
        expect(page.locator("h3:has-text('UI 自动化验证')")).to_be_visible()

    def test_agent_overview_shows_status(self, page):
        """概览面板显示状态和 Token"""
        page.goto(WS_URL_DEVICE)
        page.wait_for_load_state("networkidle")
        page.locator("[class*='cursor-pointer']:has-text('UI 自动化')").first.click()
        page.wait_for_selector("button:has-text('日志')", timeout=5000)
        expect(page.locator("text=已完成").first).to_be_visible()
        expect(page.locator("text=Token").first).to_be_visible()

    def test_agent_logs_tab_has_content(self, page):
        """日志 Tab 有脚本生成和设备执行日志"""
        page.goto(WS_URL_DEVICE)
        page.wait_for_load_state("networkidle")
        page.locator("[class*='cursor-pointer']:has-text('UI 自动化')").first.click()
        page.wait_for_selector("button:has-text('日志')", timeout=5000)
        page.click("button:has-text('日志')")
        page.wait_for_timeout(500)
        # 日志内容可见
        expect(page.locator("text=开始生成").first).to_be_visible()
        expect(page.locator("text=设备任务完成").first).to_be_visible()

    def test_agent_output_tab_shows_generated_code(self, page):
        """产出 Tab 显示 AI 生成的测试脚本"""
        page.goto(WS_URL_DEVICE)
        page.wait_for_load_state("networkidle")
        page.locator("[class*='cursor-pointer']:has-text('UI 自动化')").first.click()
        page.wait_for_selector("button:has-text('产出')", timeout=5000)
        page.click("button:has-text('产出')")
        page.wait_for_timeout(1000)
        # 弹窗内容面板
        panel = page.locator(".flex-1.overflow-auto.p-5")
        text = panel.text_content()
        assert "import" in text, f"产出应该有代码: {text[:80]}"
        assert "uiautomator2" in text or "pytest" in text, f"应该有 uiautomator2/pytest"
        assert len(text) > 200, f"产出太短: {len(text)} chars"
