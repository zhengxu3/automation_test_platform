"""演示检查/手动触发小工具（无任何外部 force 开关，不污染 goal 流程）。

  status  <goal_id>                  查看 goal 状态/轮次/验收点
  trigger <goal_id> [--sides a,b]    手动起一轮（sides 可省略→由各 repo 的 git diff 自动判定爆炸范围）

正常演示请用 scripts/commit_sim.py（提交代码模拟）。本工具只用于排查/手动推进。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.db import get_collection
from engine import goal_runtime


def cmd_status(gid):
    g = get_collection("ai_goals").find_one({"goal_id": gid}, {"_id": 0}) or {}
    if not g:
        print("Goal 不存在"); return
    print(f"status={g.get('status')} round={g.get('round')} pv={g.get('plan_version')} "
          f"replan_count={g.get('replan_count')} auto_replan={g.get('auto_replan')}")
    for a in g.get("acceptance", []):
        print(f"  [{a.get('verdict','?'):12}] {a.get('id'):6} {a.get('evidence_type','?'):16} "
              f"side={a.get('side','')} src={a.get('source_ref','')}  {a.get('desc','')[:40]}")


def cmd_trigger(gid, sides):
    side_list = [s.strip() for s in (sides or "").split(",") if s.strip()] or None
    r = goal_runtime.trigger_code_update_round(gid, reason="手动触发", sides=side_list)
    print(f"🚀 sides={side_list} → ok={r.get('ok')} pv={r.get('plan_version')} "
          f"next={r.get('next_state')} skip={r.get('skipped')} reason={r.get('reason','')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["status", "trigger"])
    ap.add_argument("goal_id")
    ap.add_argument("--sides", default="")
    a = ap.parse_args()
    if a.action == "status":
        cmd_status(a.goal_id)
    else:
        cmd_trigger(a.goal_id, a.sides)


if __name__ == "__main__":
    main()
