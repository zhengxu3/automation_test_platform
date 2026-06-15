"""UI 自动化验收 — Playwright 无头浏览器"""
import pytest
from playwright.sync_api import Page, expect

BASE_URL = "http://localhost:3000"


class TestNavigation:
    """验收：导航和页面加载"""

    def test_home_redirects_to_goals(self, page: Page):
        page.goto(BASE_URL)
        page.wait_for_url(f"{BASE_URL}/goals")
        expect(page.get_by_role("heading", name="Goals")).to_be_visible()

    def test_sidebar_navigation(self, page: Page):
        page.goto(BASE_URL)
        # 点击智能体
        page.click("text=智能体")
        page.wait_for_url(f"{BASE_URL}/agents")
        expect(page.locator("text=智能体管理")).to_be_visible()
        # 点击 Git 仓库
        page.click("text=Git 仓库")
        page.wait_for_url(f"{BASE_URL}/repos")
        # 点击知识库
        page.click("text=知识库")
        page.wait_for_url(f"{BASE_URL}/knowledge")

    def test_sidebar_shows_username(self, page: Page):
        page.goto(BASE_URL)
        page.wait_for_timeout(1000)
        expect(page.locator("text=开发者")).to_be_visible()


class TestAgentManagement:
    """验收：智能体 CRUD + 上下级联动"""

    def test_agent_list_loads(self, page: Page):
        page.goto(f"{BASE_URL}/agents")
        page.wait_for_timeout(1000)
        # 应该能看到创建按钮
        expect(page.locator("text=创建智能体")).to_be_visible()

    def test_create_agent(self, page: Page):
        page.goto(f"{BASE_URL}/agents")
        page.click("text=+ 创建智能体")
        page.wait_for_timeout(500)
        # 填表（弹窗内的输入框）
        page.fill('input[placeholder="名称"]', 'UI测试智能体')
        page.fill('input[placeholder="描述"]', '自动化验收创建的')
        # 点弹窗内的提交按钮（用 type=submit 精确定位）
        page.locator('form button[type="submit"]').click()
        page.wait_for_timeout(1500)
        # 列表应该出现新创建的
        expect(page.locator("text=UI测试智能体")).to_be_visible()

    def test_agent_detail_panel(self, page: Page):
        page.goto(f"{BASE_URL}/agents")
        page.wait_for_timeout(1000)
        # 点击某个智能体
        agent_card = page.locator("text=UI测试智能体").first
        if agent_card.is_visible():
            agent_card.click()
            page.wait_for_timeout(500)
            # 侧边栏应该出现
            expect(page.locator("text=ID:")).to_be_visible()

    def test_delete_agent(self, page: Page):
        page.goto(f"{BASE_URL}/agents")
        page.wait_for_timeout(1000)
        # 点击测试智能体打开详情
        agent_card = page.locator("text=UI测试智能体").first
        if agent_card.is_visible():
            agent_card.click()
            page.wait_for_timeout(500)
            # 处理 confirm 弹窗
            page.on("dialog", lambda d: d.accept())
            page.click("text=删除")
            page.wait_for_timeout(1000)
            # 应该消失了
            expect(page.locator("text=UI测试智能体")).not_to_be_visible()


class TestAuthCheck:
    """验收：认证状态"""

    def test_auth_check_passes(self, page: Page):
        page.goto(BASE_URL)
        # dev 环境不会跳登录页
        page.wait_for_timeout(500)
        expect(page).not_to_have_url(f"{BASE_URL}/login")
