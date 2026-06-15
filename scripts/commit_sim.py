"""提交代码模拟（钩子驱动版）—— 给定 goal_id，随机 5~10 轮，每轮向其监控的库注入【不同】缺陷，
走 webhook 回调激活任务，全程叙述：哪个库怎么改 / 何时调回调 / 激活了哪个任务 / 是否激活成功 /
本轮 LLM(代码分析) 抓到了什么 / 测试结论 / 通知的问题，然后等下一轮。

缺陷每轮不同且都带 MOCK_BUG（保证 mock 测试失败，逼系统抛出问题），用于检验 LLM 能否抓到并通知。

用法（ai-service 根目录，需连 DB；gateway/worker 在运行）：
  python scripts/commit_sim.py <goal_id> [--min 5] [--max 10] [--wait 100] [--gap 6]
"""
import argparse
import json
import os
import random
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.db import get_collection

GATEWAY = os.getenv("AI_GATEWAY_URL", "http://127.0.0.1:5010")
HOOK_TOKEN = os.getenv("GOAL_HOOK_TOKEN", "")
ACTIVE = ("discovering", "planning", "running", "verifying", "replanning")

# 每个 side 一组【不同】缺陷模板：(简述, 注入代码片段)。都含 MOCK_BUG 以保证 mock 失败。
BUGS = {
    "backend": [
        ("手机号校验位数写错(11→10)", "def validate_phone_bug(p):\n    return len(p) == 10  # MOCK_BUG 应为11位\n"),
        ("登录错误码拼写错误", "INVALID_PHONE_CODE = 'INVALD_PHON'  # MOCK_BUG 错误码拼写错\n"),
        ("订单创建漏了商品ID校验", "def create_order_bug(d):\n    return {'ok': True}  # MOCK_BUG 未校验 product_id\n"),
        ("成功响应字段名写错(ok→success)", "def login_resp_bug():\n    return {'success': True}  # MOCK_BUG 字段应为 ok\n"),
        ("金额计算用了 max 而非 sum", "def total_bug(items):\n    return max(i['price'] for i in items)  # MOCK_BUG 应为求和\n"),
        ("缺少 None 检查会 KeyError", "def pay_bug(d):\n    return d['amount'] * d['qty']  # MOCK_BUG 缺 None/缺键检查\n"),
        ("错误的 HTTP 状态码(200 当失败)", "def err_status_bug():\n    return ('fail', 200)  # MOCK_BUG 失败却返回200\n"),
        ("分页参数未做边界检查", "def page_bug(n):\n    return list(range(n))  # MOCK_BUG n 可为负导致异常\n"),
        ("token 过期未判断", "def auth_bug(t):\n    return True  # MOCK_BUG 未校验 token 过期\n"),
        ("并发下单未加锁", "def stock_bug():\n    pass  # MOCK_BUG 扣库存未加锁，超卖\n"),
    ],
    "web": [
        ("登录按钮未禁用导致重复提交", "// MOCK_BUG round-defect: 登录按钮提交后未禁用，可重复点击重复下单\n"),
        ("手机号前端校验正则写错", "// MOCK_BUG round-defect: 手机号正则漏了开头数字校验\n"),
        ("订单列表未处理空状态", "// MOCK_BUG round-defect: orders 为空时白屏，未显示空态\n"),
        ("错误提示文案未国际化/写死", "// MOCK_BUG round-defect: 错误提示硬编码中文，未走 i18n\n"),
        ("路由跳转后未清理表单状态", "// MOCK_BUG round-defect: 登录成功跳转后残留旧表单数据\n"),
        ("响应式布局在小屏溢出", "// MOCK_BUG round-defect: 小屏下登录卡片溢出视口\n"),
        ("接口失败未捕获导致页面崩", "// MOCK_BUG round-defect: 登录接口异常未 catch，页面崩溃\n"),
        ("token 存 localStorage 未设过期", "// MOCK_BUG round-defect: token 存本地未设过期，安全隐患\n"),
    ],
    "client": [
        ("// MOCK_BUG round-defect: 登录页输入框未做 11 位手机号校验\n", "// MOCK_BUG round-defect: 登录页输入框未做 11 位手机号校验\n"),
        ("// MOCK_BUG round-defect: 返回键未拦截导致登录态丢失\n", "// MOCK_BUG round-defect: 返回键未拦截导致登录态丢失\n"),
        ("// MOCK_BUG round-defect: 网络异常未提示，界面卡死\n", "// MOCK_BUG round-defect: 网络异常未提示，界面卡死\n"),
        ("// MOCK_BUG round-defect: 订单列表滑动卡顿(主线程IO)\n", "// MOCK_BUG round-defect: 订单列表滑动卡顿(主线程IO)\n"),
        ("// MOCK_BUG round-defect: 横竖屏切换后状态丢失\n", "// MOCK_BUG round-defect: 横竖屏切换后状态丢失\n"),
        ("// MOCK_BUG round-defect: 支付按钮可连续点击重复支付\n", "// MOCK_BUG round-defect: 支付按钮可连续点击重复支付\n"),
    ],
}
SIDE_EXT = {"backend": (".py",), "web": (".vue", ".ts", ".js"), "client": (".kt", ".swift", ".java")}


def _repo_side(repo_path):
    """探测库主要 side（看含哪类源文件）。"""
    found = set()
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "build", "dist", "__pycache__")]
        for fn in files:
            for side, exts in SIDE_EXT.items():
                if fn.endswith(exts):
                    found.add(side)
    for side in ("backend", "web", "client"):  # 优先级
        if side in found:
            return side
    return "backend"


def _target_file(repo_path, side):
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "build", "dist", "__pycache__")]
        for fn in sorted(files):
            if fn.endswith(SIDE_EXT[side]):
                return os.path.join(root, fn)
    return os.path.join(repo_path, "DEFECT.txt")


def _commit(repo, rel_file, snippet, msg):
    with open(rel_file, "a", encoding="utf-8") as f:
        f.write(f"\n{snippet}")
    env = ["git", "-c", "user.email=demo@local", "-c", "user.name=demo"]
    subprocess.run(env + ["add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(env + ["commit", "-q", "-m", msg], cwd=repo, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()


def _post_hook(repo_id, branch, commit):
    body = json.dumps({"repo_id": repo_id, "branch": branch or "main", "commit": commit}).encode()
    req = urllib.request.Request(f"{GATEWAY}/ai/goal/webhook", data=body,
                                 headers={"Content-Type": "application/json",
                                          **({"X-Hook-Token": HOOK_TOKEN} if HOOK_TOKEN else {})}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode()).get("data", {})


def _goal(gid):
    return get_collection("ai_goals").find_one({"goal_id": gid}, {"_id": 0}) or {}


def _wait_settle(gid, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _goal(gid).get("status") not in ACTIVE:
            break
        time.sleep(4)
    return _goal(gid).get("status")


def _llm_findings(gid, repo_id, since=0):
    """取本轮(since 之后) LLM 的代码分析(branch_review)报告 + 测试结论 + critic 通知的问题。"""
    arts = list(get_collection("ai_goal_artifacts").find(
        {"goal_id": gid, "type": "branch_review", "created_at": {"$gte": since}},
        {"_id": 0, "reasoning": 1, "summary": 1, "source_ref": 1, "created_at": 1}
    ).sort("created_at", -1).limit(5))
    review = next((a for a in arts if a.get("source_ref") == repo_id), arts[0] if arts else {})
    ev = list(get_collection("ai_goal_evidence").find(
        {"goal_id": gid, "created_at": {"$gte": since}},
        {"_id": 0, "type": 1, "verdict": 1, "summary": 1}).sort("created_at", -1).limit(6))
    crit = list(get_collection("ai_goal_events").find(
        {"goal_id": gid, "event": "critic_decision", "timestamp": {"$gte": since}},
        {"_id": 0, "payload": 1}).sort("timestamp", -1).limit(1))
    return review, ev, (crit[0]["payload"] if crit else {})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("goal_id")
    ap.add_argument("--min", type=int, default=5)
    ap.add_argument("--max", type=int, default=10)
    ap.add_argument("--wait", type=int, default=100)
    ap.add_argument("--gap", type=int, default=6, help="每轮之间间隔秒")
    a = ap.parse_args()

    g = _goal(a.goal_id)
    if not g:
        print(f"❌ Goal {a.goal_id} 不存在"); return
    repos = [s for s in g.get("sources", []) if s.get("type") == "repo" and s.get("local_path")]
    if not repos:
        print("❌ 该 goal 没有可操作的本地代码库"); return

    rounds = random.randint(a.min, a.max)
    print(f"════════ 提交代码模拟（钩子驱动）════════", flush=True)
    print(f"目标任务: {a.goal_id} | 监控库: {[r.get('repo_id') for r in repos]}", flush=True)
    print(f"🎲 本次随机注入 {rounds} 轮缺陷（每轮不同问题，均不让通过，检验 LLM 能否抓到并通知）\n", flush=True)

    # 为每个 side 备一份打乱的缺陷序列，保证每轮不同
    pools = {side: random.sample(items, len(items)) for side, items in BUGS.items()}
    cursors = {side: 0 for side in BUGS}

    for i in range(1, rounds + 1):
        repo = repos[(i - 1) % len(repos)]            # 多库时轮流改
        repo_id, repo_path, branch = repo.get("repo_id"), repo.get("local_path"), repo.get("branch", "main")
        side = _repo_side(repo_path)
        pool = pools[side]
        desc, snippet = pool[cursors[side] % len(pool)]
        cursors[side] += 1
        tgt = _target_file(repo_path, side)

        print(f"───── 第 {i}/{rounds} 轮 ─────", flush=True)
        round_start = int(time.time())
        print(f"① 改哪个库怎么改: 库[{repo_id}] ({side}) 文件 {os.path.relpath(tgt, repo_path)}", flush=True)
        print(f"   注入缺陷: {desc.strip().splitlines()[0][:60]}", flush=True)
        head = _commit(repo_path, tgt, snippet, f"round{i}: 注入缺陷 {desc[:20]}")
        print(f"   提交 commit={head[:10]}", flush=True)

        ts = time.strftime("%H:%M:%S")
        try:
            resp = _post_hook(repo_id, branch, head)
        except Exception as e:
            print(f"   ⚠️ 回调失败: {e}"); resp = {}
        print(f"② 调用回调(webhook) 时间: {ts} → 返回: action={resp.get('action')} "
              f"activated={resp.get('activated')} busy={resp.get('busy')}", flush=True)

        act = resp.get("activated") or []
        busy = resp.get("busy") or []
        if a.goal_id in act:
            print(f"③ 验证激活: ✅ 已激活本任务 {a.goal_id}", flush=True)
        elif a.goal_id in busy:
            print(f"③ 验证激活: ⏳ 本任务在途未激活(上一轮还在跑)，等下次", flush=True)
        else:
            print(f"③ 验证激活: ⚠️ 本任务未在激活/在途列表（action={resp.get('action')}）", flush=True)

        st = _wait_settle(a.goal_id, a.wait)
        # 等本轮 branch_review 产物落库（避免轮次重叠时读到上一轮的旧分析）
        review, ev, crit = {}, [], {}
        for _ in range(10):
            review, ev, crit = _llm_findings(a.goal_id, repo_id, since=round_start)
            if review or ev:
                break
            time.sleep(4)
        print(f"④ 监控结果: status={st}", flush=True)
        print(f"   🤖 LLM 代码分析(branch_review)抓到: {(review.get('reasoning') or review.get('summary') or '(无)')[:160]}", flush=True)
        print(f"   🧪 测试结论: " + " | ".join(f"{e.get('type')}={e.get('verdict')}" for e in ev) or "(无)", flush=True)
        print(f"   📣 质检/通知的问题: {crit.get('decision')} — {str(crit.get('reason',''))[:100]} unmet={crit.get('unmet')}", flush=True)
        print(flush=True)
        if i < rounds:
            time.sleep(a.gap)

    print(f"════════ 结束：{rounds} 轮缺陷已注入 ════════", flush=True)
    print(f"提示：模拟库已被注入缺陷；如需复位 git checkout/reset 对应库即可。", flush=True)


if __name__ == "__main__":
    main()
