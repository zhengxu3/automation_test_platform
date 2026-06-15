"""追踪某 Goal 的真实运行轨迹：状态/事件/智能体实例/步骤/证据/记忆。"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.db import get_collection

goal_id = sys.argv[1]
g = get_collection("ai_goals").find_one({"goal_id": goal_id}, {"_id": 0})
print(f"=== Goal {goal_id} ===")
print(f"status={g.get('status')}  round={g.get('round')}  plan_version={g.get('plan_version')}")
print(f"statement={g.get('goal_statement','')[:80]}")
print(f"acceptance={[(a.get('id'),a.get('verdict')) for a in g.get('acceptance',[])]}")

print("\n--- 智能体实例 (ai_workspace_agents) [真实安装] ---")
for a in get_collection("ai_workspace_agents").find({"goal_id": goal_id}, {"_id": 0}).sort([("phase",1),("installed_at",1)]):
    print(f"  phase={a.get('phase'):6} {a.get('agent_id'):20} cap={a.get('capability_key'):20} status={a.get('status')}")

print("\n--- 步骤 (ai_goal_steps) [plan 派活] ---")
for s in get_collection("ai_goal_steps").find({"goal_id": goal_id}, {"_id": 0}).sort("step_id",1):
    print(f"  {s.get('step_id')} {s.get('name','')[:24]:24} cap={s.get('capability_key'):16} phase={s.get('phase','?'):11} status={s.get('status'):10} pv={s.get('plan_version')}")

print("\n--- 证据 (ai_goal_evidence) [记忆体收/绑定] ---")
for e in get_collection("ai_goal_evidence").find({"goal_id": goal_id}, {"_id": 0}):
    print(f"  acc={e.get('acceptance_id')} type={e.get('type')} verdict={e.get('verdict')}")

print("\n--- 记忆点 (ai_memory_points) [记忆体沉淀] ---")
for m in get_collection("ai_memory_points").find({"goal_id": goal_id}, {"_id": 0}).limit(10):
    print(f"  [{m.get('layer')}] {m.get('summary','')[:70]}")

print("\n--- 事件流 (ai_goal_events) [管家判断/状态转移] ---")
for ev in get_collection("ai_goal_events").find({"goal_id": goal_id}, {"_id": 0}).sort("timestamp",1):
    actor = ev.get("actor","")
    line = ev.get("event","")
    extra = ""
    p = ev.get("payload",{})
    if "decision" in p: extra = f" decision={p['decision']} reason={p.get('reason','')[:40]}"
    elif "conclusion" in p: extra = f" {p['conclusion'][:40]}"
    print(f"  [{actor:9}] {line}{extra}")
