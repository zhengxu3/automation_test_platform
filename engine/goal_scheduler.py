"""Goal Scheduler — DAG 执行调度（确定性状态机，零 LLM）

职责：推进 DAG、检测依赖就绪、提交任务、处理完成回调、验收。
事件驱动：被 step 完成事件唤醒，不轮询 LLM。

调度层是确定性代码（选 step、查依赖、提交任务），思考层（Steward 评估）才花 token。
"""
import time
import uuid
import hashlib

from common.db import get_collection
from engine import state
from engine import steward
from engine.contracts import check_success


# 活跃 step 过滤：replan 会把旧 plan 的 step 标 superseded_by（append-only 保留），
# 调度只看当前轮次未被取代的 step。
_ACTIVE = {"superseded_by": {"$exists": False}}
_STEP_TERMINAL_STATES = ("completed", "degraded", "skipped", "failed")
_EXECUTION_EVIDENCE_TYPES = ("api_test", "web_test", "device_test", "e2e_test")


def _active_steps_query(goal_id: str) -> dict:
    return {"goal_id": goal_id, **_ACTIVE}


# ==================== 依赖就绪 + 推进 ====================

def advance(goal_id: str, watchdog_lock_id: str = "") -> dict:
    """推进 Goal：找出依赖就绪的 pending step，提交执行。
    被事件驱动唤醒（step 完成 / 审批通过 / Goal 进入 running）。

    循环到不动点：可执行 step 提交后进 running（异步，等 worker 回调），
    不可执行 step 在 _submit_step 内同步降级为终态——这会即时解锁其后继，
    因此需要在同一次 advance 内再扫一遍，否则依赖降级 step 的后继会永久卡在 pending。
    """
    goal = get_collection("ai_goals").find_one({"goal_id": goal_id}, {"_id": 0})
    if not goal:
        return {"ok": False, "error": "Goal 不存在"}

    watchdog_lock = goal.get("watchdog_lock") or {}
    if watchdog_lock.get("expires_at", 0) > int(time.time()) \
            and watchdog_lock.get("lock_id") != watchdog_lock_id:
        return {"ok": True, "waiting_watchdog_lock": {
            "lock_id": watchdog_lock.get("lock_id", ""),
            "owner": watchdog_lock.get("owner", "watchdog"),
            "expires_at": watchdog_lock.get("expires_at"),
        }}

    if goal["status"] not in ("running", "verifying"):
        return {"ok": False, "reason": f"Goal 状态 {goal['status']} 不可推进"}

    submitted = []
    attempted = set()  # 防重入：一个 step 在单次 advance 内只提交一次
    while True:
        steps = list(get_collection("ai_goal_steps").find(_active_steps_query(goal_id), {"_id": 0}))
        done_ids = {s["step_id"] for s in steps if s["status"] in ("completed", "degraded", "skipped")}

        pass_submitted = []
        for step in steps:
            sid = step["step_id"]
            if step["status"] != "pending" or sid in attempted:
                continue
            deps = step.get("depends_on", [])
            if all(d in done_ids for d in deps):
                attempted.add(sid)
                _submit_step(goal, step)
                pass_submitted.append(sid)

        submitted.extend(pass_submitted)
        if not pass_submitted:
            break  # 不动点：本轮没有新就绪 step

    # 死锁防护：若某 pending step 依赖了已 failed/skipped 的前序，它永远就绪不了，
    # 而 failed 是终态 → all_terminal 永远 False → goal 卡死。级联标 skipped 解开死锁。
    while True:
        steps = list(get_collection("ai_goal_steps").find(_active_steps_query(goal_id), {"_id": 0}))
        blocked_ids = {s["step_id"] for s in steps if s["status"] in ("failed", "skipped")}
        cascaded = False
        for step in steps:
            if step["status"] != "pending":
                continue
            if any(d in blocked_ids for d in step.get("depends_on", [])):
                _mark_step(goal_id, step["step_id"], "skipped",
                           extra={"blocked_reason": f"前序 {step.get('depends_on')} 失败/跳过，依赖未满足"})
                cascaded = True
        if not cascaded:
            break

    # 全部终态 → 按 plan_kind 分流：
    # - discovery：这轮只是找目标，交还主管/记忆体综合目标，再生成 objective plan
    # - objective：目标执行轮，才进入验收
    fresh = list(get_collection("ai_goal_steps").find(_active_steps_query(goal_id), {"_id": 0, "status": 1}))
    all_terminal = all(s["status"] in _STEP_TERMINAL_STATES for s in fresh) if fresh else False
    if all_terminal:
        fresh_goal = get_collection("ai_goals").find_one(
            {"goal_id": goal_id}, {"_id": 0, "pending_plan_transition": 1, "current_plan_kind": 1}
        ) or {}
        pending_transition = fresh_goal.get("pending_plan_transition") or {}
        if pending_transition.get("status") == "scheduled":
            return {"ok": True, "waiting_transition": pending_transition}
        current_kind = goal.get("current_plan_kind") or _current_plan_kind(goal_id)
        if current_kind == "resource":
            # 资源准备轮：clone 完回写 local_path → 重新画像 → 进入目标发现
            from engine.goal_runtime import complete_resource_plan
            return complete_resource_plan(goal_id)
        if current_kind == "discovery":
            from engine.goal_runtime import complete_discovery_plan
            return complete_discovery_plan(goal_id)
        return _verify_goal(goal_id)

    return {"ok": True, "submitted": submitted}


def recover_stuck_goals(stale_seconds: int = 60, limit: int = 100) -> dict:
    """看门狗自愈：running/verifying 但当前轮 step 已全终态时，自动补一次 advance。

    典型场景：worker 完成回调打到 gateway 重启窗口，step 已写 completed/failed，
    但 on_step_done 后半段的 advance/验收没跑完，Goal 会永久停在 running。
    这里不接管正常执行，只处理"没有任何在途 step，且一段时间无变化"的安全场景。
    """
    now = int(time.time())
    stale_seconds = max(int(stale_seconds or 0), 0)
    goals = list(get_collection("ai_goals").find(
        {"status": {"$in": ["running", "verifying"]}},
        {"_id": 0, "goal_id": 1, "status": 1, "updated_at": 1, "created_at": 1,
         "current_plan_kind": 1, "plan_version": 1},
    ).limit(limit))

    recovered = []
    skipped = []
    goals_col = get_collection("ai_goals")
    db = goals_col.database
    for goal in goals:
        gid = goal.get("goal_id")
        steps = list(get_collection("ai_goal_steps").find(
            _active_steps_query(gid),
            {"_id": 0, "step_id": 1, "status": 1, "updated_at": 1, "created_at": 1,
             "plan_kind": 1, "plan_version": 1},
        ))
        if not steps:
            skipped.append({"goal_id": gid, "reason": "no_active_steps"})
            continue
        non_terminal = [s for s in steps if s.get("status") not in _STEP_TERMINAL_STATES]
        if non_terminal:
            skipped.append({"goal_id": gid, "reason": "has_inflight_steps",
                            "steps": [s.get("step_id") for s in non_terminal]})
            continue
        last_activity = max(
            [int(goal.get("updated_at") or goal.get("created_at") or 0)]
            + [int(s.get("updated_at") or s.get("created_at") or 0) for s in steps]
        )
        age = now - last_activity
        if age < stale_seconds:
            skipped.append({"goal_id": gid, "reason": "not_stale", "age": age})
            continue

        lock_id = f"wd_{uuid.uuid4().hex[:8]}"
        lock_ttl = max(stale_seconds, 30)
        lock_result = goals_col.update_one(
            {
                "goal_id": gid,
                "status": {"$in": ["running", "verifying"]},
                "$or": [
                    {"watchdog_lock": {"$exists": False}},
                    {"watchdog_lock.expires_at": {"$lte": now}},
                ],
            },
            {"$set": {"watchdog_lock": {
                "lock_id": lock_id,
                "owner": "goal-watchdog",
                "acquired_at": now,
                "expires_at": now + lock_ttl,
            }}},
        )
        if getattr(lock_result, "modified_count", 0) != 1:
            skipped.append({"goal_id": gid, "reason": "watchdog_lock_busy"})
            continue

        skip_after_lock = None
        try:
            fresh_goal = goals_col.find_one(
                {"goal_id": gid}, {"_id": 0, "status": 1, "updated_at": 1, "created_at": 1,
                                   "current_plan_kind": 1, "plan_version": 1}
            ) or {}
            fresh_steps = list(get_collection("ai_goal_steps").find(
                _active_steps_query(gid),
                {"_id": 0, "step_id": 1, "status": 1, "updated_at": 1, "created_at": 1,
                 "plan_kind": 1, "plan_version": 1},
            ))
            if fresh_goal.get("status") not in ("running", "verifying"):
                skip_after_lock = {"goal_id": gid, "reason": f"status_changed:{fresh_goal.get('status')}"}
            elif not fresh_steps or any(s.get("status") not in _STEP_TERMINAL_STATES for s in fresh_steps):
                skip_after_lock = {"goal_id": gid, "reason": "steps_changed"}
            else:
                fresh_last = max(
                    [int(fresh_goal.get("updated_at") or fresh_goal.get("created_at") or 0)]
                    + [int(s.get("updated_at") or s.get("created_at") or 0) for s in fresh_steps]
                )
                fresh_age = now - fresh_last
                if fresh_age < stale_seconds:
                    skip_after_lock = {"goal_id": gid, "reason": "not_stale_after_lock", "age": fresh_age}
                else:
                    goal = fresh_goal
                    steps = fresh_steps
                    age = fresh_age
                    state.emit_event(db, gid, "watchdog_advance", {
                        "reason": "goal_running_but_all_active_steps_terminal",
                        "status": goal.get("status"),
                        "plan_kind": goal.get("current_plan_kind", ""),
                        "plan_version": goal.get("plan_version", 1),
                        "step_count": len(steps),
                        "age": age,
                        "stale_seconds": stale_seconds,
                        "lock_id": lock_id,
                    }, actor="watchdog")
                    result = advance(gid, watchdog_lock_id=lock_id)
                    recovered.append({"goal_id": gid, "result": result})
        finally:
            goals_col.update_one({"goal_id": gid, "watchdog_lock.lock_id": lock_id},
                                 {"$unset": {"watchdog_lock": ""}})
        if skip_after_lock:
            skipped.append(skip_after_lock)

    return {"ok": True, "scanned": len(goals), "recovered": recovered, "skipped": skipped}


def _acceptance_refs(goal: dict, acceptance_ids: list) -> list:
    """把 step.serves_acceptance 展开成前端可直接展示的验收点摘要。"""
    acc_map = {a.get("id"): a for a in goal.get("acceptance", []) or []}
    refs = []
    for aid in acceptance_ids or []:
        acc = acc_map.get(aid, {})
        refs.append({
            "id": aid,
            "desc": acc.get("desc", ""),
            "side": acc.get("side", ""),
            "evidence_type": acc.get("evidence_type", ""),
            "verdict": acc.get("verdict", ""),
        })
    return refs


def _activation_reason(step: dict, agent: dict, acceptance_refs: list) -> str:
    """给用户看的激活理由：优先使用 Planner rationale，回退到能力契约。"""
    if step.get("rationale"):
        return step["rationale"]
    contract = agent.get("capability_contract", {}) or {}
    purpose = contract.get("purpose") or agent.get("description") or ""
    acc_text = "、".join(a.get("id", "") for a in acceptance_refs if a.get("id"))
    if acc_text and purpose:
        return f"为服务验收点 {acc_text}，调度能产出 {step.get('evidence_type') or step.get('capability_key')} 证据的{agent.get('agent_name', '智能体')}：{purpose}"
    return purpose or f"调度 {agent.get('agent_name', '智能体')} 执行 {step.get('capability_key')}"


def _submit_step(goal: dict, step: dict):
    """提交单个 step 执行。

    can_execute=false 的高要求步骤 → 走 fallback 降级。
    可执行 → 写 ai_task_queue（带 goal_id/step_id/idempotency_key）。
    """
    goal_id = goal["goal_id"]
    step_id = step["step_id"]
    db = get_collection("ai_goals").database

    # pending → ready（依赖就绪）
    _mark_step(goal_id, step_id, "ready")

    # 不可执行 → 降级
    if not step.get("can_execute", False):
        fallback = step.get("fallback")
        _mark_step(goal_id, step_id, "running")  # ready→running
        _mark_step(goal_id, step_id, "degraded", extra={
            "blocked_reason": f"缺少前置条件: {step.get('required_sources')}",
            "fallback_applied": fallback,
        })
        state.emit_event(db, goal_id, "step_degraded", {
            "step_id": step_id,
            "reason": f"前置条件不满足，降级为 {fallback}",
            "required_sources": step.get("required_sources"),
        }, actor="scheduler")
        return

    # 可执行 → 复用共用原语：install_agent + resolve 入参 + enqueue_agent_task
    # （plan 执行阶段也装 workspace 实例，前端能看到智能体上场；入参经 resolver 喂饱真实 handler）
    from engine import agent_runtime, step_input_resolver
    cap = step["capability_key"]

    # 取该 step 绑定的平台真实智能体（优先按 agent_id，回退按能力）。
    agent = None
    if step.get("agent_id"):
        agent = get_collection("ai_agents").find_one(
            {"agent_id": step["agent_id"], "status": "active"}, {"_id": 0})
    if not agent:
        agent = agent_runtime.agent_by_capability(cap)
    if not agent:
        # 硬约束：安装必须绑定平台真实智能体（携带 handler_class → worker skill 的真实绑定）。
        # 找不到真实 agent 时，绝不合成最小假 agent 派活——那会把任务派给没绑 skill 的空壳，
        # 制造"装了智能体在干活"的假象。诚实降级为终态 + 告警，让 Goal 走到验收如实停 partial。
        _mark_step(goal_id, step_id, "running")  # ready→running
        _mark_step(goal_id, step_id, "degraded", extra={
            "blocked_reason": f"无平台真实智能体绑定能力 '{cap}'（未安装/未激活）",
            "fallback_applied": None,
        })
        state.emit_event(db, goal_id, "step_no_real_agent", {
            "step_id": step_id, "capability": cap, "agent_id": step.get("agent_id"),
            "reason": "安装需绑定平台真实智能体（handler_class→worker skill），拒绝合成假 agent",
        }, actor="scheduler")
        return

    _mark_step(goal_id, step_id, "running")

    idem = _idempotency_key(goal_id, step_id, step.get("plan_version", 1), step.get("depends_on", []))

    # 装 workspace 实例（goal_id + step_id 维度）
    agent_runtime.install_agent(agent, goal_id=goal_id, step_id=step_id, phase="step")
    # resolver 据 goal.sources + 前序产物组装真实 handler 入参
    prior = _prior_artifacts(goal_id, step)
    inputs = step_input_resolver.resolve(cap, goal, step, prior)
    # 组装完整 payload + 入队（executor 路由）+ 实例置 running
    task_id = agent_runtime.enqueue_agent_task(
        agent, inputs, goal_id=goal_id, step_id=step_id,
        req_id=goal.get("req_id", ""), phase="step", idempotency_key=idem,
    )

    from engine.contracts import get_executor
    executor = step.get("executor") or get_executor(cap)
    acceptance_refs = _acceptance_refs(goal, step.get("serves_acceptance", []))
    state.emit_event(db, goal_id, "step_submitted", {
        "step_id": step_id,
        "step_name": step.get("name", ""),
        "task_id": task_id,
        "capability": cap,
        "agent_id": agent["agent_id"],
        "agent_name": agent.get("agent_name", ""),
        "executor": executor,
        "plan_kind": step.get("plan_kind", "objective"),
        "plan_version": step.get("plan_version", 1),
        "depends_on": step.get("depends_on", []) or [],
        "source_ref": step.get("source_ref", ""),
        "evidence_type": step.get("evidence_type"),
        "required_sources": step.get("required_sources", []) or [],
        "produces_evidence": step.get("produces_evidence", []) or [],
        "serves_acceptance": acceptance_refs,
        "acceptance_ids": step.get("serves_acceptance", []) or [],
        "rationale": step.get("rationale", ""),
        "activation_reason": _activation_reason(step, agent, acceptance_refs),
        "input_keys": sorted(list(inputs.keys())),
    }, actor="scheduler")


def _idempotency_key(goal_id, step_id, plan_version, depends_on) -> str:
    raw = f"{goal_id}|{step_id}|{plan_version}|{sorted(depends_on)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _prior_artifacts(goal_id: str, step: dict) -> list:
    """取前序依赖 step 产出的 artifacts（供 step_input_resolver 组装本步入参）。"""
    deps = step.get("depends_on", [])
    if not deps:
        return []
    return list(get_collection("ai_goal_artifacts").find(
        {"goal_id": goal_id, "step_id": {"$in": deps}}, {"_id": 0}
    ))


def _current_plan_kind(goal_id: str) -> str:
    """根据当前未 supersede steps 推断 plan_kind，兼容旧 goal 没写 current_plan_kind。"""
    steps = list(get_collection("ai_goal_steps").find(
        _active_steps_query(goal_id), {"_id": 0, "plan_kind": 1, "plan_version": 1}
    ).sort("plan_version", -1).limit(1))
    return (steps[0].get("plan_kind") if steps else "") or "objective"


def _refresh_runtime_feasibility(goal_id: str) -> dict:
    """刷新本轮实际执行画像。

    feasibility.executable 是输入画像：创建/重画像时判断"资源是否足够支持执行级测试"。
    runtime_* 是结果画像：验收时根据实际落库 evidence 判断"本轮是否真的跑到了执行级证据"。
    两者分开，避免 repo_only/static-only goal 跑完后被误解为还应该变成可执行。
    """
    evidence = list(get_collection("ai_goal_evidence").find(
        {"goal_id": goal_id},
        {"_id": 0, "type": 1, "verdict": 1},
    ))
    evidence_types = sorted({e.get("type") for e in evidence if e.get("type")})
    execution_types = sorted(t for t in evidence_types if t in _EXECUTION_EVIDENCE_TYPES)

    counts = {}
    for ev in evidence:
        verdict = ev.get("verdict") or "unknown"
        counts[verdict] = counts.get(verdict, 0) + 1

    snapshot = {
        "runtime_executable": bool(execution_types),
        "runtime_evidence_types": evidence_types,
        "runtime_execution_evidence_types": execution_types,
        "runtime_evidence_counts": counts,
        "runtime_evaluated_at": int(time.time()),
    }
    get_collection("ai_goals").update_one(
        {"goal_id": goal_id},
        {"$set": {f"feasibility.{k}": v for k, v in snapshot.items()}},
    )
    return snapshot


def _mark_step(goal_id: str, step_id: str, to_status: str, extra: dict = None):
    """通过状态机更新 step 状态（仅作用于当前活跃 step）"""
    db = get_collection("ai_goals").database
    step = get_collection("ai_goal_steps").find_one(
        {"goal_id": goal_id, "step_id": step_id, **_ACTIVE}, {"_id": 0, "status": 1})
    if not step:
        return
    from_status = step["status"]
    try:
        state.assert_transition("step", from_status, to_status)
    except state.IllegalTransition:
        return
    update = {"status": to_status, "updated_at": int(time.time())}
    if extra:
        update.update(extra)
    get_collection("ai_goal_steps").update_one(
        {"goal_id": goal_id, "step_id": step_id, **_ACTIVE}, {"$set": update}
    )


# ==================== Step 完成回调 ====================

def on_step_done(goal_id: str, step_id: str, output: dict, success: bool = None):
    """step 执行完成回调（worker 调用）。
    1. 校验产出（契约 success 判定）
    2. 存 artifact + 绑定 evidence
    3. Steward 评估沉淀记忆
    4. 推进 DAG
    """
    db = get_collection("ai_goals").database
    step = get_collection("ai_goal_steps").find_one(
        {"goal_id": goal_id, "step_id": step_id, **_ACTIVE}, {"_id": 0})
    if not step:
        return {"ok": False, "error": "step 不存在"}

    cap = step["capability_key"]

    # 1. 成功判定：进程成功(worker 传 success) ≠ 业务成功(契约校验)。
    #    worker 不抛异常只代表进程跑完；产物是否有效由契约 check_success 判定。两者都满足才算成功。
    process_ok = True if success is None else bool(success)
    success = process_ok and check_success(cap, output)

    # 2. 记录 attempt
    attempt = {
        "attempt_no": len(step.get("attempts", [])) + 1,
        "status": "completed" if success else "failed",
        "output_summary": str(output)[:300],
        "at": int(time.time()),
    }
    get_collection("ai_goal_steps").update_one(
        {"goal_id": goal_id, "step_id": step_id, **_ACTIVE},
        {"$push": {"attempts": attempt}}
    )

    if success:
        # 存 artifact（自包含留痕）
        _save_artifact(goal_id, step, output)
        # 绑定 evidence 到 acceptance
        _bind_evidence(goal_id, step, output)
        _mark_step(goal_id, step_id, "completed")
        # 同步 workspace 实例状态（探查阶段 on_probe_done 已做，执行阶段之前漏 → UI 卡 running）
        from engine import agent_runtime
        agent_runtime.mark_agent("completed", agent_id=step.get("agent_id"),
                                 goal_id=goal_id, step_id=step_id, phase="step")
        state.emit_event(db, goal_id, "step_completed", {"step_id": step_id, "capability": cap}, actor="scheduler")

        # 3. Steward 评估沉淀（LLM）
        goal = get_collection("ai_goals").find_one({"goal_id": goal_id}, {"_id": 0, "goal_statement": 1})
        eval_result = steward.evaluate_and_remember(
            goal_id, step_id, step.get("agent_id", ""),
            output_summary=str(output)[:1500],
            goal_statement=goal.get("goal_statement", "") if goal else "",
        )
        state.emit_event(db, goal_id, "steward_evaluated", {
            "step_id": step_id, "conclusion": eval_result.get("conclusion", "")
        }, actor="steward")
    else:
        # 失败 → 重试策略
        _handle_failure(goal_id, step, output)

    # 4. 推进 DAG
    return advance(goal_id)


def _save_artifact(goal_id: str, step: dict, output: dict):
    """落库 step 产物，留痕自包含——这是"幻觉可查"的地基。

    自包含 = 谁产出(agent_id/agent_name) + 针对哪个源(source_ref) + 喂了什么(inputs_snapshot)
            + 怎么分析(reasoning/报告) + 产出什么(output/summary)。
    有"输入快照 + 推理 + 输出"三件套，才能对照核查（如分析是否引用了输入里不存在的东西）。
    """
    cap = step["capability_key"]
    step_id = step["step_id"]
    # 注册表实例（按 goal_id+agent_id 持久）取 agent_name；输入快照按 step 取（per-step，不被同 agent 并行覆盖）
    inst = get_collection("ai_workspace_agents").find_one(
        {"goal_id": goal_id, "agent_id": step.get("agent_id")}, {"_id": 0}) or {}
    # 思考流程/完整报告：handler 优先给 report/reasoning，回退 change_summary
    reasoning = output.get("report") or output.get("reasoning") or output.get("change_summary", "")
    get_collection("ai_goal_artifacts").insert_one({
        "artifact_id": f"art_{uuid.uuid4().hex[:8]}",
        "goal_id": goal_id,
        "step_id": step_id,
        "phase": "step",
        "plan_kind": step.get("plan_kind", "objective"),
        "plan_version": step.get("plan_version", 1),
        "type": cap,
        "capability_key": cap,
        # —— 谁产出 ——
        "agent_id": step.get("agent_id") or inst.get("agent_id", ""),
        "agent_name": inst.get("agent_name", ""),
        # —— 针对哪个源（多 repo 扇出后绑定）——
        "source_ref": step.get("source_ref", ""),
        # —— 喂了什么（per-step 输入快照，存在 step 文档上）——
        "inputs_snapshot": step.get("inputs_snapshot", {}),
        # —— 怎么分析（思考流程/报告）——
        "reasoning": (reasoning or "")[:8000],
        # —— 产出什么 ——
        "summary": output.get("summary", "") or str(output)[:200],
        "ref": output.get("ref", "") or output.get("report_url", ""),
        "data": output,
        "created_at": int(time.time()),
    })


def _bind_evidence(goal_id: str, step: dict, output: dict):
    """把 step 产出绑定为 acceptance 的 evidence。

    证据等级决定 verdict（第一性原理：生成用例 ≠ 业务通过）：
    - 验证级证据(static_analysis/api_test/device_test...) → pass/fail（真业务结论）
    - 准备级证据(testcase_generated/doc_review) → prepared（资产已就绪，未业务验证）
    """
    from engine.contracts import is_verification_grade
    evidence_type = step.get("evidence_type")
    if not evidence_type:
        return
    # 判定 verdict
    if is_verification_grade(evidence_type):
        # 诚实铁律：验证级证据必须真正"验证过"才算 pass。
        # branch_review 报 no_change（无变更可分析）= 没对验收点做任何实证 → not_applicable，绝不假 pass。
        if output.get("no_change") is True:
            verdict = "not_applicable"
        else:
            verdict = output.get("test_result", "pass")
            if verdict not in ("pass", "fail", "partial", "blocked"):
                verdict = "pass" if output else "partial"
    else:
        verdict = "prepared"   # 准备级：用例已生成/需求已拆解，待真实验证才算 pass

    for acc_id in step.get("serves_acceptance", []):
        ev_id = f"ev_{uuid.uuid4().hex[:8]}"
        get_collection("ai_goal_evidence").insert_one({
            "evidence_id": ev_id,
            "goal_id": goal_id,
            "step_id": step["step_id"],
            "acceptance_id": acc_id,
            "type": evidence_type,
            "verdict": verdict,
            "summary": output.get("summary", "")[:200],
            "ref": output.get("ref", "") or output.get("report_url", ""),
            "confidence": output.get("confidence", 0.8),
            "plan_version": step.get("plan_version", 1),
            "created_at": int(time.time()),
        })
        # 回填 acceptance.bound_to
        get_collection("ai_goals").update_one(
            {"goal_id": goal_id, "acceptance.id": acc_id},
            {"$set": {"acceptance.$.bound_to": ev_id, "acceptance.$.verdict": verdict}}
        )


def _handle_failure(goal_id: str, step: dict, output: dict):
    """失败处理：按重试策略。超过上限 → blocked。"""
    db = get_collection("ai_goals").database
    step_id = step["step_id"]
    attempts = len(step.get("attempts", []))
    max_attempts = 2

    if step.get("retryable", True) and attempts < max_attempts:
        _mark_step(goal_id, step_id, "retrying")
        state.emit_event(db, goal_id, "step_retrying", {
            "step_id": step_id, "attempt": attempts, "error": output.get("error", "")[:200]
        }, actor="scheduler")
        # 重新提交
        goal = get_collection("ai_goals").find_one({"goal_id": goal_id}, {"_id": 0})
        fresh_step = get_collection("ai_goal_steps").find_one(
            {"goal_id": goal_id, "step_id": step_id, **_ACTIVE}, {"_id": 0})
        _mark_step(goal_id, step_id, "running")
        _submit_step(goal, fresh_step)
    else:
        _mark_step(goal_id, step_id, "failed", extra={"error": output.get("error", "")[:300]})
        from engine import agent_runtime
        agent_runtime.mark_agent("failed", agent_id=step.get("agent_id"),
                                 goal_id=goal_id, step_id=step_id, phase="step")
        state.emit_event(db, goal_id, "step_failed", {
            "step_id": step_id, "error": output.get("error", "")[:200]
        }, actor="scheduler")


# ==================== 验收 ====================

def _verify_goal(goal_id: str) -> dict:
    """所有 step 终态 → 验收：检查 acceptance 是否都绑定了 pass 证据。"""
    db = get_collection("ai_goals").database
    goal = get_collection("ai_goals").find_one({"goal_id": goal_id}, {"_id": 0})

    # 转 verifying
    if goal["status"] == "running":
        state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "verifying", "所有步骤完成，开始验收")

    acceptance = goal.get("acceptance", [])
    bound = [a for a in acceptance if a.get("bound_to")]
    passed = [a for a in acceptance if a.get("verdict") == "pass"]

    completion_policy = goal.get("completion_policy", "auto_complete")
    runtime_feasibility = _refresh_runtime_feasibility(goal_id)
    goal.setdefault("feasibility", {}).update(runtime_feasibility)

    state.emit_event(db, goal_id, "verification", {
        "total_acceptance": len(acceptance),
        "bound": len(bound),
        "passed": len(passed),
        "runtime_executable": runtime_feasibility["runtime_executable"],
        "runtime_execution_evidence_types": runtime_feasibility["runtime_execution_evidence_types"],
        "runtime_evidence_types": runtime_feasibility["runtime_evidence_types"],
    }, actor="scheduler")

    # Critic 确定性判定：complete / replan / partial
    from engine import critic
    decision = critic.decide_after_verify(goal)
    state.emit_event(db, goal_id, "critic_decision", {
        "decision": decision["decision"],
        "reason": decision["reason"],
        "unmet": decision.get("unmet", []),
        "achievable_unmet": decision.get("achievable_unmet", []),
        "unmet_signature": decision.get("unmet_signature", ""),
    }, actor="critic")

    if decision["decision"] == "complete":
        if completion_policy == "continuous":
            state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "guarding", "达标，转持续守护")
            _generate_summary(goal_id, "guarding")
            state.emit_event(db, goal_id, "goal_guarding", {
                "passed": len(passed), "total_acceptance": len(acceptance),
                "runtime_executable": runtime_feasibility["runtime_executable"],
            }, actor="scheduler")
            return {"ok": True, "status": "guarding"}
        state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "completed", "全部验收通过")
        _generate_summary(goal_id, "completed")
        state.emit_event(db, goal_id, "goal_completed", {
            "passed": len(passed), "total_acceptance": len(acceptance),
            "runtime_executable": runtime_feasibility["runtime_executable"],
        }, actor="scheduler")
        return {"ok": True, "status": "completed"}

    if decision["decision"] == "replan":
        if decision.get("unmet_signature"):
            get_collection("ai_goals").update_one(
                {"goal_id": goal_id},
                {"$set": {
                    "last_replan_unmet_signature": decision.get("unmet_signature"),
                    "last_replan_unmet_signature_rows": decision.get("unmet_signature_rows", []),
                    "last_replan_reason": decision.get("reason", ""),
                    "last_replan_signature_at": int(time.time()),
                }},
            )
        # 外部驱动模式(auto_replan=False，如演示)：不自动内部重规划，
        # 停在 partial_completed 等下一次代码更新触发，让"一次改动=一轮"完全可控。
        if not goal.get("auto_replan", True):
            state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "partial_completed",
                             f"本轮未达标(外部驱动，等下次触发): {decision['reason']}")
            _generate_summary(goal_id, "partial_completed")
            state.emit_event(db, goal_id, "goal_partial", {
                "passed": len(passed), "total_acceptance": len(acceptance),
                "reason": decision["reason"], "external_driven": True,
            }, actor="scheduler")
            return {"ok": True, "status": "partial_completed", "manual": True,
                    "passed": len(passed), "total": len(acceptance)}
        # 多轮执行：回规划态生成针对缺口的新 plan（每轮不同）
        from engine.goal_runtime import replan
        return replan(goal_id, reason=decision["reason"], trigger="critic")

    if decision["decision"] == "blocked":
        state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "blocked",
                         f"自动重跑中断: {decision['reason']}")
        _generate_summary(goal_id, "blocked")
        state.emit_event(db, goal_id, "goal_blocked", {
            "passed": len(passed),
            "total_acceptance": len(acceptance),
            "reason": decision["reason"],
            "unmet": decision.get("unmet", []),
            "unmet_signature": decision.get("unmet_signature", ""),
        }, actor="critic")
        return {"ok": True, "status": "blocked", "reason": decision["reason"],
                "passed": len(passed), "total": len(acceptance)}

    # 部分完成（诚实停止，不假装）
    state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "partial_completed",
                     f"部分验收: {len(passed)}/{len(acceptance)} — {decision['reason']}")
    _generate_summary(goal_id, "partial_completed")
    state.emit_event(db, goal_id, "goal_partial", {
        "passed": len(passed), "total_acceptance": len(acceptance),
        "reason": decision["reason"], "external_driven": False,
    }, actor="scheduler")
    return {"ok": True, "status": "partial_completed", "passed": len(passed), "total": len(acceptance)}


def _generate_summary(goal_id: str, final_status: str):
    """生成不可变最终总结快照"""
    goal = get_collection("ai_goals").find_one({"goal_id": goal_id}, {"_id": 0})
    steps = list(get_collection("ai_goal_steps").find({"goal_id": goal_id}, {"_id": 0}))
    evidence = list(get_collection("ai_goal_evidence").find({"goal_id": goal_id}, {"_id": 0}))
    acceptance = goal.get("acceptance", [])

    summary = {
        "goal_id": goal_id,
        "title": goal.get("title", ""),
        "final_status": final_status,
        "duration": {
            "started": goal.get("created_at", 0),
            "ended": int(time.time()),
            "total_sec": int(time.time()) - goal.get("created_at", 0),
        },
        "execution_stats": {
            "total_steps": len(steps),
            "completed": sum(1 for s in steps if s["status"] == "completed"),
            "degraded": sum(1 for s in steps if s["status"] == "degraded"),
            "failed": sum(1 for s in steps if s["status"] == "failed"),
            "skipped": sum(1 for s in steps if s["status"] == "skipped"),
            "total_attempts": sum(len(s.get("attempts", [])) for s in steps),
        },
        "acceptance_summary": [
            {"id": a["id"], "desc": a["desc"], "verdict": a.get("verdict", "pending"), "evidence_ref": a.get("bound_to")}
            for a in acceptance
        ],
        "evidence_collected": len(evidence),
        "created_at": int(time.time()),
    }
    get_collection("ai_goal_summary").replace_one({"goal_id": goal_id}, summary, upsert=True)

    # 完成通知（仅达标/守护/部分完成发；取消、失败不发"完成"通知，避免误导）
    if final_status in ("completed", "guarding", "partial_completed"):
        try:
            from common.notify import notify_goal_completed
            partial = final_status == "partial_completed"
            gaps = [a["desc"] for a in acceptance if a.get("verdict") != "pass"] if partial else None
            notify_goal_completed(goal_id, goal.get("title", ""), partial=partial, gaps=gaps)
        except Exception:
            pass

    return summary
