"""UI 自动化测试：验证半自动模式工作空间弹窗功能"""
import pytest
from playwright.sync_api import sync_playwright, expect
import time

BASE_URL = "http://localhost:3000"
EXISTING_REQ_ID = "req_87517071"
WS_URL = f"{BASE_URL}/workspace/{EXISTING_REQ_ID}"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    """每个测试用新 page"""
    ctx = browser.new_context()
    pg = ctx.new_page()
    yield pg
    ctx.close()


def test_01_requirements_page(page):
    """需求列表页加载 + 有创建弹窗的上传功能"""
    page.goto(BASE_URL + "/requirements")
    page.wait_for_load_state("networkidle")
    expect(page.locator("h2:has-text('需求管理')")).to_be_visible()
    expect(page.locator("text=女女匹配策略验证").first).to_be_visible()

    # 打开创建弹窗
    page.click("button:has-text('创建需求')")
    page.wait_for_selector("h3:has-text('创建需求')")
    expect(page.locator("text=上传需求文档")).to_be_visible()
    page.click("button:has-text('取消')")


def test_02_workspace_loads(page):
    """工作空间页面加载正确"""
    page.goto(WS_URL)
    page.wait_for_load_state("networkidle")
    expect(page.locator("text=女女匹配策略验证")).to_be_visible()
    expect(page.locator("text=就绪")).to_be_visible()
    expect(page.locator("text=已安装").first).to_be_visible()


def test_03_stats_and_logs(page):
    """统计卡片 + 日志可见"""
    page.goto(WS_URL)
    page.wait_for_load_state("networkidle")
    # 统计卡片
    expect(page.get_by_text("🧠 记忆点", exact=True)).to_be_visible()
    expect(page.get_by_text("✓ Case", exact=True)).to_be_visible()
    expect(page.get_by_text("📄 文档", exact=True)).to_be_visible()
    # 日志
    expect(page.locator("text=结论 & 日志")).to_be_visible()
    expect(page.locator("text=分析完成").first).to_be_visible()


def test_04_docs_dialog(page):
    """文档弹窗：3个文档可切换查看"""
    page.goto(WS_URL)
    page.wait_for_load_state("networkidle")

    # 点击文档统计卡片
    page.get_by_text("📄 文档", exact=True).click()
    page.wait_for_selector("h3:has-text('需求文档')", timeout=5000)

    # 3个 Tab
    expect(page.locator("button:has-text('需求拆解')")).to_be_visible()
    expect(page.locator("button:has-text('测试用例')")).to_be_visible()
    expect(page.locator("button:has-text('测试策略')")).to_be_visible()

    # 有内容（弹窗里有 leading-relaxed 的内容区）
    content = page.locator(".leading-relaxed")
    expect(content).to_be_visible()
    assert len(content.text_content()) > 20


def test_05_cases_dialog(page):
    """Case 弹窗：有用例列表和筛选"""
    page.goto(WS_URL)
    page.wait_for_load_state("networkidle")

    page.get_by_text("✓ Case", exact=True).click()
    page.wait_for_selector("h3:has-text('测试用例')", timeout=5000)

    # 统计
    expect(page.locator("text=总计")).to_be_visible()
    # 筛选
    expect(page.locator("button:has-text('全部')")).to_be_visible()
    expect(page.locator("button:has-text('有效')")).to_be_visible()


def test_06_memory_dialog(page):
    """记忆弹窗：有记忆点 + 钉选"""
    page.goto(WS_URL)
    page.wait_for_load_state("networkidle")

    page.get_by_text("🧠 记忆点", exact=True).click()
    page.wait_for_selector("h3:has-text('需求记忆')", timeout=5000)

    # 有记忆
    expect(page.locator("text=需求分析").first).to_be_visible()
    # 钉选按钮
    expect(page.locator("button:has-text('钉选')").first).to_be_visible()


def test_07_chat_input(page):
    """对话框可用"""
    page.goto(WS_URL)
    page.wait_for_load_state("networkidle")
    chat = page.locator("input[placeholder*='记忆体']")
    expect(chat).to_be_visible()
    expect(chat).to_be_enabled()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
