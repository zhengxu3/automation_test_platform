"""从 Confluence Wiki 同步 Case 到 MongoDB（本地 Mac 执行，线上 Worker 只读 DB）。

用法:
  .venv/bin/python scripts/sync_wiki_cases.py --goal-id goal_b04aec33 --page-id 135137162
  .venv/bin/python scripts/sync_wiki_cases.py --goal-id goal_b04aec33 --page-id 135137162 --force

逻辑:
  1. 拉 Confluence 页面内容
  2. 算 MD5 → 对比 DB 存的上一次 MD5
  3. 没变 → 跳过（秒退）
  4. 有变 → 解析 Case → 更新 ai_goal_cases → 存新 MD5

可以 cron 定时跑（如每 10 分钟），或手动触发。
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CONFLUENCE_URL = "https://confluence.holla.cool"
CONFLUENCE_USER = "xu.zheng"
CONFLUENCE_PASS = "qgu1ZXH2a9cQ18912"


def fetch_page_body(page_id: str) -> str:
    """拉取 Confluence 页面 HTML body。"""
    url = f"{CONFLUENCE_URL}/rest/api/content/{page_id}?expand=body.storage,version"
    resp = requests.get(url, auth=(CONFLUENCE_USER, CONFLUENCE_PASS), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("body", {}).get("storage", {}).get("value", "")


def md5_of(content: str) -> str:
    return hashlib.md5(content.encode()).hexdigest()


def get_stored_md5(goal_id: str, page_id: str) -> str:
    """从 DB 读上次同步的 MD5。"""
    from common.db import get_collection
    doc = get_collection("ai_wiki_sync").find_one(
        {"goal_id": goal_id, "page_id": page_id}, {"md5": 1})
    return (doc or {}).get("md5", "")


def save_md5(goal_id: str, page_id: str, md5: str, case_count: int):
    from common.db import get_collection
    get_collection("ai_wiki_sync").update_one(
        {"goal_id": goal_id, "page_id": page_id},
        {"$set": {"md5": md5, "case_count": case_count, "synced_at": int(time.time())}},
        upsert=True)


def parse_cases_from_html(html: str, goal_id: str, page_id: str) -> list:
    """从 Confluence HTML 中解析 Case 列表。

    支持多种格式：
    - 表格（<table>）：第一行为表头，后续行为 Case
    - 列表（<li>）：每条为一个 Case
    - 标题+内容结构
    """
    from html.parser import HTMLParser

    # 简单 HTML → 纯文本提取
    class TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows = []
            self._current_row = []
            self._current_cell = ""
            self._in_cell = False
            self._in_table = False

        def handle_starttag(self, tag, attrs):
            if tag == "table":
                self._in_table = True
            if tag in ("td", "th"):
                self._in_cell = True
                self._current_cell = ""
            if tag == "tr":
                self._current_row = []

        def handle_endtag(self, tag):
            if tag in ("td", "th"):
                self._in_cell = False
                self._current_row.append(self._current_cell.strip())
            if tag == "tr" and self._current_row:
                self.rows.append(self._current_row)
            if tag == "table":
                self._in_table = False

        def handle_data(self, data):
            if self._in_cell:
                self._current_cell += data

    extractor = TextExtractor()
    extractor.feed(html)

    cases = []
    if extractor.rows and len(extractor.rows) > 1:
        # 表格模式：第一行为表头
        headers = [h.lower().strip() for h in extractor.rows[0]]
        for row in extractor.rows[1:]:
            if len(row) < 2:
                continue
            case = {"goal_id": goal_id, "source": f"wiki:{page_id}"}
            for i, h in enumerate(headers):
                val = row[i] if i < len(row) else ""
                if "id" in h or "编号" in h:
                    case["case_id"] = val
                elif "标题" in h or "title" in h or "名称" in h:
                    case["title"] = val
                elif "模块" in h or "module" in h or "功能" in h:
                    case["module"] = val
                elif "优先" in h or "priority" in h:
                    case["priority"] = val
                elif "步骤" in h or "step" in h:
                    case["steps"] = val
                elif "预期" in h or "expect" in h:
                    case["expected"] = val
                elif "接口" in h or "api" in h or "endpoint" in h:
                    case["api_info"] = {"endpoint": val}
            if not case.get("case_id"):
                case["case_id"] = f"WC-{len(cases)+1:03d}"
            if case.get("title") or case.get("module"):
                cases.append(case)

    if not cases:
        # 非表格：尝试按行解析
        text = re.sub(r'<[^>]+>', '\n', html)
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        for i, line in enumerate(lines):
            # 匹配 "TC-001 xxxx" 或 "1. xxxx" 格式
            m = re.match(r'^(TC-\d+|WC-\d+|\d+[\.\)、])\s*(.+)', line)
            if m:
                cases.append({
                    "case_id": m.group(1).rstrip('.）、)'),
                    "title": m.group(2)[:100],
                    "goal_id": goal_id,
                    "source": f"wiki:{page_id}",
                })

    return cases


def sync(goal_id: str, page_id: str, force: bool = False):
    print(f"📖 拉取 Confluence page {page_id}...")
    body = fetch_page_body(page_id)
    if not body:
        print("⚠️ 页面内容为空")
        return

    current_md5 = md5_of(body)
    stored_md5 = get_stored_md5(goal_id, page_id)

    if current_md5 == stored_md5 and not force:
        print(f"✅ 无变化（MD5={current_md5[:8]}），跳过")
        return

    print(f"🔄 内容有变化（旧={stored_md5[:8]} → 新={current_md5[:8]}），解析 Case...")
    cases = parse_cases_from_html(body, goal_id, page_id)
    print(f"📋 解析出 {len(cases)} 条 Case")

    if not cases:
        print("⚠️ 未解析出任何 Case，跳过写入")
        return

    # 写入 DB：按 source 删旧 + 插新
    from common.db import get_collection
    col = get_collection("ai_goal_cases")
    col.delete_many({"goal_id": goal_id, "source": f"wiki:{page_id}"})
    for c in cases:
        c.setdefault("priority", "P1")
        c.setdefault("module", "")
        c.setdefault("title", "")
        c.setdefault("steps", "")
        c.setdefault("expected", "")
        c.setdefault("api_info", {})
        c["synced_at"] = int(time.time())
    col.insert_many(cases)

    save_md5(goal_id, page_id, current_md5, len(cases))
    print(f"✅ 已同步 {len(cases)} 条 Case 到 DB（goal={goal_id}）")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="同步 Confluence Case 到 MongoDB")
    parser.add_argument("--goal-id", required=True)
    parser.add_argument("--page-id", required=True)
    parser.add_argument("--force", action="store_true", help="强制更新，忽略 MD5 对比")
    args = parser.parse_args()
    sync(args.goal_id, args.page_id, args.force)
