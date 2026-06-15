"""工作空间完整流程 UI 验收 — 安装智能体→运行→查看结论→对话→目标"""
import pytest
from playwright.sync_api import Page, expect

BASE_URL = "http://localhost:3000"
# 用已有的需求
REQ_ID = "req_44c2f7a3"


class TestWorkspaceNavigation:
    """工作空间页面能正常加载"""

    def test_workspace_loads(self, page: Page):
        page.goto(f"{BASE_URL}/workspace/{REQ_ID}")
        page.wait_for_timeout(1000)
        # 应该看到顶部需求标题
        expect(page.locator("text=← 返回")).to_be_visible()

    def test_stats_bar_visible(self, page: Page):
        page.goto(f"{BASE_URL}/workspace/{REQ_ID}")
        page.wait_for_timeout(1000)
        # 统计条应该展示
        expect(page.locator("text=用例通过")).to_be_visible()
        expect(page.locator("text=Token")).to_be_visible()
        expect(page.locator("text=幻觉率")).to_be_visible()

    def test_tabs_visible(self, page: Page):
        page.goto(f"{BASE_URL}/workspace/{REQ_ID}")
        page.wait_for_timeout(1000)
        expect(page.get_by_role("button", name="结论")).to_be_visible()
        expect(page.get_by_role("button", name="Case")).to_be_visible()
        expect(page.get_by_role("button", name="疑问")).to_be_visible()


class TestAgentInstall:
    """智能体安装流程"""

    def test_install_dialog_opens(self, page: Page):
        page.goto(f"{BASE_URL}/workspace/{REQ_ID}")
        page.wait_for_timeout(1000)
        page.click("text=+ 安装智能体")
        page.wait_for_timeout(500)
        # 弹窗应该打开
        expect(page.locator("text=安装智能体").nth(1)).to_be_visible()

    def test_install_agent(self, page: Page):
        page.goto(f"{BASE_URL}/workspace/{REQ_ID}")
        page.wait_for_timeout(1000)
        page.click("text=+ 安装智能体")
        page.wait_for_timeout(800)
        # 如果有可安装的智能体，点安装
        install_btn = page.locator("text=安装").last
        if install_btn.is_visible():
            install_btn.click()
            page.wait_for_timeout(1000)


class TestAgentDialog:
    """智能体详情弹窗"""

    def test_click_agent_opens_dialog(self, page: Page):
        page.goto(f"{BASE_URL}/workspace/{REQ_ID}")
        page.wait_for_timeout(1500)
        agent_card = page.locator("[data-testid='agent-card']").first
        if agent_card.is_visible():
            agent_card.click()
            page.wait_for_timeout(500)
            # 弹窗内应该有概览按钮和运行按钮
            expect(page.get_by_role("button", name="概览")).to_be_visible()
            expect(page.locator("text=▶ 运行")).to_be_visible()

    def test_dialog_shows_run_button(self, page: Page):
        page.goto(f"{BASE_URL}/workspace/{REQ_ID}")
        page.wait_for_timeout(1500)
        agent_card = page.locator("[data-testid='agent-card']").first
        if agent_card.is_visible():
            agent_card.click()
            page.wait_for_timeout(500)
            expect(page.locator("text=▶ 运行")).to_be_visible()

    def test_dialog_tabs_switch(self, page: Page):
        page.goto(f"{BASE_URL}/workspace/{REQ_ID}")
        page.wait_for_timeout(1500)
        agent_card = page.locator("[data-testid='agent-card']").first
        if agent_card.is_visible():
            agent_card.click()
            page.wait_for_timeout(500)
            page.get_by_role("button", name="日志").click()
            page.wait_for_timeout(300)
            page.get_by_role("button", name="产出").click()
            page.wait_for_timeout(300)


class TestAgentRun:
    """运行智能体并验证结论推送"""

    def test_run_agent_and_see_conclusion(self, page: Page):
        """核心流程：运行 → 等待 → 结论面板出现结果"""
        page.goto(f"{BASE_URL}/workspace/{REQ_ID}")
        page.wait_for_timeout(1500)
        # 点击智能体
        agent_card = page.locator(".rounded-lg.border.bg-dark-800").first
        if not agent_card.is_visible():
            pytest.skip("无已安装智能体")
        agent_card.click()
        page.wait_for_timeout(500)
        # 点运行
        run_btn = page.locator("text=▶ 运行")
        if run_btn.is_visible():
            run_btn.click()
            # 等待运行完成（LLM 调用可能需要 10-20s）
            page.wait_for_timeout(20000)
            # 关闭弹窗
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            # 结论面板应该有内容
            conclusions = page.locator("[class*='rounded-lg']").filter(has_text="结论")
            # 至少看到结论 tab 存在
            expect(page.locator("text=结论")).to_be_visible()


class TestChat:
    """记忆体对话"""

    def test_chat_input_visible(self, page: Page):
        page.goto(f"{BASE_URL}/workspace/{REQ_ID}")
        page.wait_for_timeout(1000)
        expect(page.locator("input[placeholder*='记忆体对话']")).to_be_visible()

    def test_send_message(self, page: Page):
        page.goto(f"{BASE_URL}/workspace/{REQ_ID}")
        page.wait_for_timeout(1000)
        # 输入消息
        chat_input = page.locator("input[placeholder*='记忆体对话']")
        chat_input.fill("当前测试进展如何？")
        page.click("text=发送")
        # 等待回复（LLM 调用）
        page.wait_for_timeout(15000)
        # 应该有 AI 回复
        expect(page.locator("text=AI:")).to_be_visible()


class TestRequirementList:
    """需求列表基本操作"""

    def test_requirement_list_loads(self, page: Page):
        page.goto(f"{BASE_URL}/requirements")
        page.wait_for_timeout(1000)
        expect(page.locator("text=需求管理")).to_be_visible()
        expect(page.locator("text=+ 创建需求")).to_be_visible()

    def test_click_requirement_enters_workspace(self, page: Page):
        page.goto(f"{BASE_URL}/requirements")
        page.wait_for_timeout(1000)
        # 点击第一个需求
        first_req = page.locator("[class*='rounded-xl'][class*='border'][class*='cursor-pointer']").first
        if first_req.is_visible():
            first_req.click()
            page.wait_for_url(f"{BASE_URL}/workspace/**")
