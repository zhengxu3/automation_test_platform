"""AgentRuntime — Goal 与半自动共用的智能体 安装/运行 原语

主线关键连接件：Goal 不再自造简化执行，而是
1. install_agent      把 ai_agents 能力实例化进工作集合 ai_workspace_agents（支持 goal_id + phase）
2. enqueue_agent_task 组装完整 payload(capability/handler_class/model/inputs + goal/step/phase) → 入队(executor 路由)
3. collect_probe_outputs 汇聚某 goal 的探查产物 + 完成状态

执行入口统一走 GoalCapabilityTask(task_type=40)；其内部按 capability 调真实 handler。
探查轮(phase=probe) 与 plan 执行(phase=step) 都走这套。确定性，零 LLM。
"""
import time
import uuid

from common.db import get_collection
from engine.contracts import get_executor

GOAL_TASK_TYPE = 40  # GoalCapabilityTask 统一入口

# 实例终态（探查完成判定用）
_INSTANCE_TERMINAL = ("completed", "error", "degraded", "failed")


def agent_by_capability(capability_key: str) -> dict:
    """按能力取一个 active 智能体（Goal 用它的 handler/model/prompt）。"""
    return get_collection("ai_agents").find_one(
        {"capability_key": capability_key, "status": "active"}, {"_id": 0}
    )


def _registry_key(agent_id: str, goal_id: str = None, req_id: str = None) -> dict:
    """工作集合=智能体注册表主键：一个 goal(或 req) 内每个真实智能体一个持久实例。
    不再按 step 建实例（避免同 step 重跑覆盖），每步执行明细落 step+artifact。"""
    if goal_id:
        return {"goal_id": goal_id, "agent_id": agent_id}
    if req_id:
        return {"req_id": req_id, "agent_id": agent_id}
    return {"agent_id": agent_id}


def register_candidate_agents(goal_id: str, capabilities: list) -> list:
    """注册阶段：把本 goal 可用的真实智能体快照进工作集合（持久注册表）。
    管家/规划器据此思考"有什么智能体、能用什么"。append-only：只 upsert，永不删除。
    capabilities 来自 planner.discover_capabilities（真实 ai_agents 过滤后的候选）。
    """
    col = get_collection("ai_workspace_agents")
    registered = []
    for c in capabilities:
        aid = c.get("agent_id")
        if not aid:
            continue
        col.update_one(
            {"goal_id": goal_id, "agent_id": aid},
            {"$set": {
                "goal_id": goal_id, "agent_id": aid,
                "agent_name": c.get("agent_name", ""),
                "capability_key": c.get("capability_key", ""),
                "purpose": c.get("purpose", ""),
                "produces_evidence": c.get("produces_evidence", []) or [],
                "risk_level": c.get("risk_level", "low"),
                "can_execute": c.get("can_execute_now", False),
                "updated_at": int(time.time()),
            },
             "$setOnInsert": {
                "status": "registered", "runs": 0, "served_steps": [],
                "registered_at": int(time.time()),
            }},
            upsert=True,
        )
        registered.append(aid)
    return registered


def install_agent(agent: dict, *, goal_id: str = None, req_id: str = None,
                  phase: str = "step", step_id: str = None) -> dict:
    """确保真实智能体已注册进工作集合（持久, append-only；不按 step 覆盖、不删除）。返回主键。"""
    key = _registry_key(agent["agent_id"], goal_id, req_id)
    get_collection("ai_workspace_agents").update_one(
        key,
        {"$set": {
            **key,
            "agent_name": agent.get("agent_name", ""),
            "category": agent.get("category", ""),
            "capability_key": agent.get("capability_key", ""),
            "handler_class": agent.get("handler_class", ""),
            "model_id": agent.get("model_id", "gemini_flash"),
            "updated_at": int(time.time()),
        },
         "$setOnInsert": {
            "status": "registered", "runs": 0, "served_steps": [],
            "registered_at": int(time.time()),
        }},
        upsert=True,
    )
    return key


def enqueue_agent_task(agent: dict, inputs: dict, *, goal_id: str = None, step_id: str = None,
                       req_id: str = None, phase: str = "step", idempotency_key: str = None) -> str:
    """组装完整 payload + 入队（executor 路由 ai_task_queue/device_task_queue）。返回 task_id。
    同时：per-step 输入快照落 step 文档；注册表标记该智能体运行中 + 累计运行 + 参与的 step。
    """
    capability = agent.get("capability_key", "")
    executor = get_executor(capability)
    queue = "device_task_queue" if executor == "device_worker" else "ai_task_queue"

    task_id = f"gtask_{uuid.uuid4().hex[:8]}"
    payload = {
        "capability_key": capability,
        "agent_id": agent["agent_id"],
        "handler_class": agent.get("handler_class", ""),
        "system_prompt": agent.get("system_prompt", ""),
        "model_id": agent.get("model_id", "gemini_flash"),
        "inputs": inputs,
        "phase": phase,
    }
    if goal_id:
        payload["goal_id"] = goal_id
    if step_id:
        payload["step_id"] = step_id
    if req_id:
        payload["req_id"] = req_id
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key

    task_doc = {
        "task_id": task_id,
        "task_type": GOAL_TASK_TYPE,
        "payload": payload,
        "status": 1,
        "created_at": int(time.time()),
    }
    if goal_id:
        task_doc["goal_id"] = goal_id
    if step_id:
        task_doc["step_id"] = step_id
    if idempotency_key:
        task_doc["idempotency_key"] = idempotency_key
    get_collection(queue).insert_one(task_doc)

    # per-step 输入快照落 step 文档（不放共享注册表，避免多 repo 并行同 agent 互相覆盖；幻觉可查地基）
    if goal_id and step_id:
        get_collection("ai_goal_steps").update_one(
            {"goal_id": goal_id, "step_id": step_id, "superseded_by": {"$exists": False}},
            {"$set": {"inputs_snapshot": inputs}}
        )
    # 注册表：运行中 + 累计运行次数 + 参与的 step（原子操作, 并行安全, append-only 不覆盖历史）
    key = _registry_key(agent["agent_id"], goal_id, req_id)
    update = {"$set": {"status": "running", "last_step": step_id or "", "started_at": int(time.time())},
              "$inc": {"runs": 1}}
    if step_id:
        update["$addToSet"] = {"served_steps": step_id}
    get_collection("ai_workspace_agents").update_one(key, update)
    return task_id


def mark_agent(status: str, *, agent_id: str, goal_id: str = None, req_id: str = None,
               step_id: str = None, phase: str = "step"):
    """更新注册表实例状态（worker 回调后调）。按 (goal_id, agent_id) 持久实例。"""
    key = _registry_key(agent_id, goal_id, req_id)
    get_collection("ai_workspace_agents").update_one(
        key, {"$set": {"status": status, "updated_at": int(time.time())}}
    )


def collect_probe_outputs(goal_id: str) -> dict:
    """汇聚某 goal 探查产物 + 完成状态（兼容旧路径；新流程探查已是 discovery plan 由调度器推进）。"""
    agents = list(get_collection("ai_workspace_agents").find(
        {"goal_id": goal_id}, {"_id": 0}
    ))
    artifacts = list(get_collection("ai_goal_artifacts").find(
        {"goal_id": goal_id, "phase": "probe"}, {"_id": 0}
    ))
    all_done = bool(agents) and all(a.get("status") in _INSTANCE_TERMINAL for a in agents)
    return {"all_done": all_done, "outputs": artifacts, "agents": agents}
