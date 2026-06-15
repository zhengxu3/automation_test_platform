"""演示控制器：只监控 goal.round，第 11 轮把模拟 mock 的 .buggy 开关删掉（修复）。
不碰引擎/调度——纯翻模拟代码库的开关。replan 循环由系统自带 critic 自主驱动。"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.db import get_collection

gid = sys.argv[1]
FIX_AT_ROUND = int(sys.argv[2]) if len(sys.argv) > 2 else 11
BUGGY = os.path.expanduser("~/Documents/work_code/mock-login-service/.buggy")

print(f"[ctl] 监控 {gid}，第 {FIX_AT_ROUND} 轮删 .buggy 修复 mock", flush=True)
fixed = False
for _ in range(600):  # 最多盯 ~30 分钟
    g = get_collection("ai_goals").find_one({"goal_id": gid}, {"_id": 0, "status": 1, "round": 1, "replan_count": 1})
    if not g:
        time.sleep(2); continue
    rnd, st = g.get("round", 1), g.get("status")
    if not fixed and rnd >= FIX_AT_ROUND and os.path.exists(BUGGY):
        os.remove(BUGGY)
        fixed = True
        print(f"[ctl] 第 {rnd} 轮 → 删除 .buggy，mock 已修复，下一次 api_test 应通过", flush=True)
    if st in ("completed", "partial_completed", "failed", "cancelled"):
        print(f"[ctl] 终态={st} round={rnd} replan_count={g.get('replan_count')}（fixed={fixed}）", flush=True)
        break
    time.sleep(2)
print("[ctl] 结束", flush=True)
