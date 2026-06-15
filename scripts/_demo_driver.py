"""演示驱动：先观察第一轮，然后每 3 分钟提交一次代码更新 + 触发新一轮 objective plan（共 5 轮）。"""
import os, sys, time, subprocess
import urllib.request, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.db import get_collection
from engine import goal_runtime

gid = sys.argv[1]
REPO = "/Users/admin/Documents/work_code/mock-login-service"
LOGIN = os.path.join(REPO, "auth/login.py")
INTERVAL = 180


def status():
    g = get_collection("ai_goals").find_one({"goal_id": gid}, {"_id": 0, "status": 1, "round": 1, "plan_version": 1}) or {}
    return g


def approve_if_needed():
    """无人值守：计划卡在待审批时自动批准（模拟页面点批准）。"""
    for _ in range(6):
        st = status()
        if st.get("status") != "awaiting_approval":
            return
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:5010/ai/goal/approve",
                data=json.dumps({"goal_id": gid}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=15).read()
            print("[driver] 自动批准计划", flush=True)
        except Exception as e:
            print(f"[driver] 批准异常: {e}", flush=True)
        time.sleep(5)


def commit(i):
    with open(LOGIN, "r") as f:
        src = f.read()
    src = src.rstrip() + f"\n# code update round {i} @ {int(time.time())}\n"
    with open(LOGIN, "w") as f:
        f.write(src)
    env = ["git", "-c", "user.email=mock@local", "-c", "user.name=mock"]
    subprocess.run(env + ["add", "-A"], cwd=REPO, capture_output=True)
    subprocess.run(env + ["commit", "-q", "-m", f"chore: 代码更新第{i}轮"], cwd=REPO, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    print(f"[driver] 提交代码更新 round {i}, HEAD={head}", flush=True)


print(f"[driver] 启动 goal={gid}；先等 {INTERVAL}s 让用户观察第一轮(discovery→objective)", flush=True)
approve_if_needed()   # 第一轮 objective 若卡审批，先放行
time.sleep(INTERVAL)
for i in range(1, 6):
    # 等上一轮 settle（最多 ~4 分钟）
    for _ in range(8):
        st = status()
        if st.get("status") not in ("discovering", "planning", "running", "verifying", "replanning"):
            break
        print(f"[driver] 上一轮仍在 {st.get('status')}，等 30s", flush=True)
        time.sleep(30)
    commit(i)
    r = goal_runtime.trigger_code_update_round(gid, reason=f"第{i}次代码更新")
    print(f"[driver] round{i} 触发 → ok={r.get('ok')} pv={r.get('plan_version')} "
          f"next={r.get('next_state')} skip={r.get('skipped')} reason={r.get('reason','')}", flush=True)
    approve_if_needed()   # 新一轮若含高风险步骤(api_test)卡审批，自动放行
    time.sleep(INTERVAL)
print("[driver] 演示结束（5 轮代码更新已驱动）", flush=True)
