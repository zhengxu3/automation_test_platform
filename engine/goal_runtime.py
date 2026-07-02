"""Goal Runtime — 编排入口（状态机驱动）

把 SourceProfiler → Steward(目标生成) → Planner(规划) → Validator → 落库 串起来。
discovering → planning → awaiting_approval/running

职责分离：routes 只收请求，runtime 编排，engine 各模块单一职责。
状态变更只走 engine.state.transition。
"""
import os
import threading
import time
import uuid

from common.db import get_collection
from engine import state
from engine import steward
from engine import planner
from engine.source_profiler import profile_sources, evidence_policy
from engine import contracts


_PLAN_TRANSITION_TERMINAL = {"completed", "failed", "cancelled"}


def _effective_evidence(profile: dict) -> list:
    """目标生成/验收用的证据天花板：优先用真实可达(producible)，回退源理论上限(allowed)。"""
    return profile.get("producible_evidence_types") or profile.get("allowed_evidence_types", [])


def _evidence_reason(evidence_type: str) -> str:
    """把证据类型翻译成用户能看懂的编排理由。"""
    return {
        "doc_review": "只有文档侧信息，先让需求分析智能体拆解并形成可评审结论",
        "testcase_generated": "需要先把需求变成可执行/可评审的测试资产",
        "static_analysis": "涉及代码变更，先用代码分析智能体确认影响范围和风险",
        "api_test": "验收点落在后端接口行为，需要在可达测试网络里用 API 测试实证",
        "web_test": "验收点落在 Web 前端页面行为，需要访问测试环境做 Web UI 测试实证",
        "device_test": "验收点落在客户端真实交互，需要用 UI/真机测试实证",
        "e2e_test": "验收点跨前后端链路，需要端到端串联验证",
    }.get(evidence_type, "根据当前输入与可用智能体选择该证据类型")


def _acceptance_decisions(acceptance: list) -> list:
    """目标生成后的验收点分配表：验收点 → 证据类型 → 为什么。"""
    rows = []
    for a in acceptance or []:
        et = a.get("evidence_type", "")
        rows.append({
            "acceptance_id": a.get("id", ""),
            "objective_id": a.get("objective_id", ""),
            "desc": a.get("desc", ""),
            "side": a.get("side", ""),
            "evidence_type": et,
            "coverage_role": a.get("coverage_role", "required"),
            "why": _evidence_reason(et),
        })
    return rows


def _objective_decisions(objectives: list, acceptance: list) -> list:
    """目标分配表：objective → 覆盖它的验收点数量。"""
    counts = {}
    for a in acceptance or []:
        oid = a.get("objective_id", "")
        if oid:
            counts[oid] = counts.get(oid, 0) + 1
    rows = []
    for obj in objectives or []:
        oid = obj.get("objective_id", "")
        rows.append({
            "objective_id": oid,
            "title": obj.get("title", ""),
            "source": obj.get("source", ""),
            "scope": obj.get("scope", []),
            "priority": obj.get("priority", ""),
            "confidence": obj.get("confidence", 0.0),
            "needs_confirmation": obj.get("needs_confirmation", False),
            "acceptance_count": counts.get(oid, 0),
        })
    return rows


def _emit_steward_thinking(db, goal_id: str, goal_result: dict, *,
                           source: str, profile: dict = None):
    """让主管/记忆体把目标拆解的思考过程显式告诉前端。"""
    objectives = goal_result.get("objectives", []) or []
    acceptance = goal_result.get("acceptance", []) or []
    state.emit_event(db, goal_id, "steward_thinking", {
        "source": source,
        "thought": goal_result.get("rationale", "") or "根据输入资源、探查产物和历史记忆生成目标与验收点",
        "goal_statement": goal_result.get("goal_statement", ""),
        "confidence": goal_result.get("confidence", 0.0),
        "input_mode": (profile or {}).get("input_mode", ""),
        "target_evidence": (profile or {}).get("max_evidence_strength"),
        "objective_count": len(objectives),
        "objective_decisions": _objective_decisions(objectives, acceptance),
        "objectives": objectives,
        "acceptance_decisions": _acceptance_decisions(acceptance),
    }, actor="steward")


def _step_thinking_rows(steps: list, capabilities: list, acceptance: list = None) -> list:
    """Planner 输出后的 step 分配表：step → 智能体/能力 → 服务验收点 → 依赖。"""
    cap_map = {c.get("capability_key"): c for c in capabilities or []}
    acc_map = {a.get("id"): a for a in acceptance or []}
    rows = []
    for s in steps or []:
        c = cap_map.get(s.get("capability_key"), {})
        serves = []
        for aid in s.get("serves_acceptance", []) or []:
            a = acc_map.get(aid, {})
            serves.append({
                "acceptance_id": aid,
                "objective_id": a.get("objective_id", ""),
                "desc": a.get("desc", ""),
                "evidence_type": a.get("evidence_type", ""),
            })
        rows.append({
            "step_id": s.get("step_id"),
            "step_name": s.get("name", ""),
            "capability": s.get("capability_key", ""),
            "agent_id": s.get("agent_id") or c.get("agent_id", ""),
            "agent_name": c.get("agent_name", ""),
            "depends_on": s.get("depends_on", []) or [],
            "serves_acceptance": serves,
            "evidence_type": s.get("evidence_type"),
            "source_ref": s.get("source_ref", ""),
            "why": s.get("rationale", "") or c.get("purpose", ""),
        })
    return rows


def _emit_planner_thinking(db, goal_id: str, *, plan_kind: str, plan_version: int,
                           plan_summary: str, steps: list, capabilities: list,
                           acceptance: list = None, prior_context: str = ""):
    """把 Planner 如何选能力、排依赖、激活智能体的理由显式落事件。"""
    rows = _step_thinking_rows(steps, capabilities, acceptance)
    state.emit_event(db, goal_id, "planner_thinking", {
        "plan_kind": plan_kind,
        "plan_version": plan_version,
        "thought": plan_summary or "根据验收点、可用能力和依赖关系生成最小必要 DAG",
        "capability_count": len(capabilities or []),
        "step_count": len(steps or []),
        "prior_context": prior_context[:1200] if prior_context else "",
        "step_decisions": rows,
    }, actor="planner")


def _latest_events(goal_id: str, limit: int = 300) -> list:
    """取最新事件窗口并按时间正序返回，避免升序 limit 永远卡在最早 N 条。"""
    events = list(get_collection("ai_goal_events").find(
        {"goal_id": goal_id}, {"_id": 0}
    ).sort([("timestamp", -1), ("_id", -1)]).limit(limit))
    events.reverse()
    return events


def _git_prepare_capability() -> dict:
    """resource plan 专用能力。discover_capabilities 会排除它，因此这里显式补给可视化。"""
    agent = get_collection("ai_agents").find_one(
        {"agent_id": "agent_git_prepare", "status": "active"}, {"_id": 0}
    ) or {}
    contract = agent.get("capability_contract", {}) or contracts.NODE_CONTRACTS.get("git_prepare", {})
    return {
        "capability_key": "git_prepare",
        "agent_id": agent.get("agent_id", "agent_git_prepare"),
        "agent_name": agent.get("agent_name", "资源准备"),
        "purpose": contract.get("purpose", "按 git 地址+分支 clone/fetch/checkout，产出本地仓库路径"),
        "required_sources": contract.get("required_sources", []),
        "produces_evidence": contract.get("produces_evidence", []),
        "risk_level": contract.get("risk_level", "low"),
        "requires_approval": contract.get("requires_approval", False),
        "fallback": contract.get("fallback"),
        "can_execute_now": True,
    }


def _plan_transition_delay_sec() -> int:
    raw = os.getenv("GOAL_PLAN_TRANSITION_DELAY_SEC")
    if raw is None and os.getenv("APP_ENV") == "test":
        return 0
    try:
        return max(int(raw if raw is not None else "3"), 0)
    except Exception:
        return 3


def _plan_kind_label(kind: str) -> str:
    return {
        "resource": "资源准备",
        "discovery": "目标发现",
        "objective": "目标执行",
    }.get(kind or "", kind or "下一阶段")


def _start_plan_transition_timer(goal_id: str, transition_id: str, delay_sec: int):
    if delay_sec <= 0:
        return

    def _run():
        try:
            execute_due_plan_transition(goal_id, transition_id=transition_id)
        except Exception as exc:
            db = get_collection("ai_goals").database
            try:
                state.emit_event(db, goal_id, "runtime_error", {
                    "error": f"plan transition timer failed: {str(exc)[:240]}",
                    "transition_id": transition_id,
                }, actor="system")
            except Exception:
                pass

    timer = threading.Timer(delay_sec, _run)
    timer.daemon = True
    timer.start()


def _schedule_plan_transition(goal_id: str, *, from_kind: str, to_kind: str,
                              plan_version: int, trigger: str, context: dict = None,
                              delay_sec: int = None) -> dict:
    """真实 plan 间隔：先持久化待切换标记，再到点启动下一轮 plan。

    Timer 只是加速器；pending_plan_transition 落库后，watchdog 可在进程重启后兜底执行。
    """
    goals = get_collection("ai_goals")
    goal = goals.find_one({"goal_id": goal_id}, {"_id": 0, "pending_plan_transition": 1})
    if goal is None:
        return {"ok": False, "error": "Goal 不存在"}

    existing = goal.get("pending_plan_transition") or {}
    if existing.get("status") == "scheduled":
        return {"ok": True, "waiting_transition": existing, "idempotent": True}

    delay = _plan_transition_delay_sec() if delay_sec is None else max(int(delay_sec), 0)
    if delay <= 0:
        transition = {
            "transition_id": f"ptr_{uuid.uuid4().hex[:8]}",
            "from_kind": from_kind,
            "to_kind": to_kind,
            "plan_version": plan_version,
            "trigger": trigger,
            "delay_sec": 0,
            "next_plan_at": int(time.time()),
            "status": "scheduled",
            "context": context or {},
        }
        return _execute_plan_transition(goal_id, transition)

    now = int(time.time())
    transition = {
        "transition_id": f"ptr_{uuid.uuid4().hex[:8]}",
        "from_kind": from_kind,
        "to_kind": to_kind,
        "plan_version": plan_version,
        "trigger": trigger,
        "delay_sec": delay,
        "next_plan_at": now + delay,
        "status": "scheduled",
        "context": context or {},
    }
    goals.update_one({"goal_id": goal_id}, {"$set": {"pending_plan_transition": transition}})
    state.emit_event(goals.database, goal_id, "plan_transition", {
        "transition_id": transition["transition_id"],
        "from_kind": from_kind,
        "from_label": _plan_kind_label(from_kind),
        "to_kind": to_kind,
        "to_label": _plan_kind_label(to_kind),
        "plan_version": plan_version,
        "delay_sec": delay,
        "next_plan_at": transition["next_plan_at"],
        "trigger": trigger,
    }, actor="scheduler")
    _start_plan_transition_timer(goal_id, transition["transition_id"], delay)
    return {"ok": True, "waiting_transition": transition}


def execute_due_plan_transition(goal_id: str, transition_id: str = "") -> dict:
    goal = get_collection("ai_goals").find_one({"goal_id": goal_id}, {"_id": 0})
    if not goal:
        return {"ok": False, "error": "Goal 不存在"}
    pending = goal.get("pending_plan_transition") or {}
    if pending.get("status") != "scheduled":
        return {"ok": True, "skipped": True, "reason": "no_pending_transition"}
    if transition_id and pending.get("transition_id") != transition_id:
        return {"ok": True, "skipped": True, "reason": "transition_replaced"}
    if goal.get("status") in _PLAN_TRANSITION_TERMINAL:
        return {"ok": True, "skipped": True, "reason": f"goal_terminal:{goal.get('status')}"}
    now = int(time.time())
    if int(pending.get("next_plan_at") or 0) > now:
        return {"ok": True, "waiting_transition": pending}
    return _execute_plan_transition(goal_id, pending)


def process_due_plan_transitions(limit: int = 100) -> dict:
    now = int(time.time())
    goals = list(get_collection("ai_goals").find(
        {"pending_plan_transition.status": "scheduled",
         "pending_plan_transition.next_plan_at": {"$lte": now},
         "status": {"$nin": list(_PLAN_TRANSITION_TERMINAL)}},
        {"_id": 0, "goal_id": 1, "pending_plan_transition": 1},
    ).limit(limit))
    processed = []
    for goal in goals:
        processed.append({
            "goal_id": goal.get("goal_id"),
            "result": execute_due_plan_transition(goal.get("goal_id")),
        })
    return {"ok": True, "processed": processed, "scanned": len(goals)}


def _execute_plan_transition(goal_id: str, transition: dict) -> dict:
    goals = get_collection("ai_goals")
    goal = goals.find_one({"goal_id": goal_id}, {"_id": 0})
    if not goal:
        return {"ok": False, "error": "Goal 不存在"}
    if goal.get("status") in _PLAN_TRANSITION_TERMINAL:
        return {"ok": True, "skipped": True, "reason": f"goal_terminal:{goal.get('status')}"}

    goals.update_one({"goal_id": goal_id}, {"$unset": {"pending_plan_transition": ""}})
    state.emit_event(goals.database, goal_id, "plan_transition_started", {
        "transition_id": transition.get("transition_id", ""),
        "from_kind": transition.get("from_kind", ""),
        "to_kind": transition.get("to_kind", ""),
        "to_label": _plan_kind_label(transition.get("to_kind", "")),
        "plan_version": transition.get("plan_version"),
        "trigger": transition.get("trigger", ""),
    }, actor="scheduler")

    profile = goal.get("feasibility", {}) or profile_sources(goal.get("sources", []))
    policy = goal.get("evidence_policy", {}) or evidence_policy(profile)
    to_kind = transition.get("to_kind")
    plan_version = int(transition.get("plan_version") or goal.get("plan_version", 1) or 1)
    context = transition.get("context") or {}

    if to_kind == "discovery":
        from engine import probe_planner
        probe_caps = probe_planner.select_probe_capabilities(profile)
        if not probe_caps:
            return _fallback_generate_goal(goal_id, profile, policy)
        return _plan_discovery_and_start(goal_id, profile, policy, probe_caps, plan_version=plan_version)

    if to_kind == "objective":
        memory_ctx = steward.retrieve_memory(goal_id=goal_id, verified_only=False)
        return _plan_and_start(
            goal_id, profile, policy,
            goal.get("goal_statement", ""),
            context.get("acceptance") or goal.get("acceptance", []),
            goal.get("goal_confidence", 0.0),
            memory_ctx,
            plan_kind="objective",
            plan_version=plan_version,
            prior_context=context.get("prior_context", ""),
        )

    return {"ok": False, "error": f"未知 plan transition to_kind: {to_kind}"}


def discover_and_plan(goal_id: str) -> dict:
    """Goal 创建后的核心流程：可行性画像 → 目标生成 → 规划 → 校验。

    这是 feasibility-first 的落地：先画像，再规划。
    """
    goals = get_collection("ai_goals")
    goal = goals.find_one({"goal_id": goal_id}, {"_id": 0})
    if not goal:
        raise ValueError(f"Goal 不存在: {goal_id}")

    db = get_collection("ai_goals").database
    sources = goal.get("sources", [])

    # ===== Phase 1: 可行性画像（确定性，零 token）=====
    state.emit_event(db, goal_id, "profiling_started", {"sources_count": len(sources)})
    profile = profile_sources(sources)
    policy = evidence_policy(profile)
    # 真实可达天花板：源理论上限 ∩ active 智能体实际能产的证据类型
    # 目标生成/验收判定都用它，避免拆出"没有手能干"的验收点 → 空转 replan
    profile["producible_evidence_types"] = planner.producible_evidence_types(profile)

    goals.update_one({"goal_id": goal_id}, {"$set": {
        "feasibility": profile,
        "evidence_policy": policy,
        "target_state": goal.get("target_state", "unknown"),
    }})
    state.emit_event(db, goal_id, "feasibility_profiled", {
        "input_mode": profile["input_mode"],
        "allowed_evidence": profile["allowed_evidence_types"],
        "producible_evidence": profile["producible_evidence_types"],
        "executable": profile["executable"],
        "target_evidence": policy["target_evidence"],
    }, actor="profiler")

    # ===== Phase 1.45: 智能体注册阶段 =====
    # 把本 goal 可用的真实智能体快照进注册表（持久, append-only），
    # 管家/规划器据此思考"有什么智能体、能用什么"；前端可见"装了谁、能不能执行"。
    from engine import agent_runtime
    # 只登记当前输入下【真正能执行】的智能体进工作集合（没文档就不显示需求分析、没URL就不显示api/web测试）。
    # 规划器另行用 discover_capabilities 看全量(含 needs_upgrade)，不受影响。
    _caps = [c for c in planner.discover_capabilities(profile) if c.get("can_execute_now")]
    agent_runtime.register_candidate_agents(goal_id, _caps)
    state.emit_event(db, goal_id, "agents_registered", {
        "count": len(_caps),
        "agents": [{"agent_id": c.get("agent_id"), "name": c.get("agent_name", ""),
                    "capability": c.get("capability_key"), "can_execute": c.get("can_execute_now")}
                   for c in _caps],
    }, actor="steward")

    # ===== Phase 1.4: 资源准备计划（Plan1 resource）=====
    # 产品入口是"传 git 地址+分支"：repo 还没 clone 到本地 → 先出资源准备 plan，
    # 真实 clone/fetch/checkout 产出 local_path，再进目标发现。
    repos_to_prep = _repos_needing_prep(sources)
    if repos_to_prep:
        return _plan_resource_and_start(goal_id, profile, policy, repos_to_prep)

    # ===== Phase 1.5: 目标探查轮（Discovery Probe Round）=====
    # 先让探查智能体跑一轮，据探查产物生成目标（而非 Steward 直接看 raw doc）
    from engine import probe_planner
    probe_caps = probe_planner.select_probe_capabilities(profile)
    if probe_caps:
        return _plan_discovery_and_start(goal_id, profile, policy, probe_caps)

    # ===== Phase 2: 目标生成（无探查能力时的兼容旧路径）=====
    # 检索历史记忆喂给目标生成
    memory_ctx = steward.retrieve_memory(req_id=goal.get("req_id", ""), verified_only=True)

    doc_content = ""
    testcase_content = ""
    code_summary = ""
    for src in sources:
        if src.get("type") == "doc":
            doc_content += (src.get("content", "") or src.get("doc_content", "")) + "\n"
        elif src.get("type") == "testcase":
            testcase_content += (src.get("content", "") or "") + "\n"
        if src.get("type") == "repo":
            code_summary += f"仓库 {src.get('repo_id')}/{src.get('branch', '')} "

    goal_result = steward.generate_goal(
        title=goal.get("title", ""),
        doc_content=doc_content.strip(),
        testcase_content=testcase_content.strip(),
        code_summary=code_summary,
        input_mode=profile["input_mode"],
        memory_context=memory_ctx,
        allowed_evidence=_effective_evidence(profile),
    )

    goals.update_one({"goal_id": goal_id}, {"$set": {
        "goal_statement": goal_result["goal_statement"],
        "objectives": goal_result.get("objectives", []),
        "acceptance": goal_result["acceptance"],
        "goal_confidence": goal_result["confidence"],
        "goal_rationale": goal_result.get("rationale", ""),
        "target_state": "confirmed",
    }})
    _emit_steward_thinking(db, goal_id, goal_result, source="fallback_raw_sources", profile=profile)
    state.emit_event(db, goal_id, "goal_generated", {
        "goal_statement": goal_result["goal_statement"],
        "objective_count": len(goal_result.get("objectives", [])),
        "acceptance_count": len(goal_result["acceptance"]),
        "confidence": goal_result["confidence"],
    }, actor="steward")

    # 转入 planning → 复用 _plan_and_start（探查轮完成后 on_probe_done 也走这里）
    return _plan_and_start(goal_id, profile, policy,
                           goal_result["goal_statement"], goal_result["acceptance"],
                           goal_result["confidence"], memory_ctx)


def _plan_and_start(goal_id: str, profile: dict, policy: dict, goal_statement: str,
                    acceptance: list, goal_confidence: float = 0.0, memory_ctx: str = "",
                    plan_kind: str = "objective", plan_version: int = None,
                    prior_context: str = "") -> dict:
    """Phase 3-5：规划 → 校验 → 落库 → 启动。discover_and_plan 与 on_probe_done 共用。"""
    goals = get_collection("ai_goals")
    goal = goals.find_one({"goal_id": goal_id}, {"_id": 0}) or {}
    db = goals.database
    plan_version = plan_version or goal.get("plan_version", 1)
    if goal.get("status") != "replanning":
        state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "planning", "目标已生成，开始规划")

    # ===== Phase 3: 规划（Planner，LLM）=====
    capabilities = planner.discover_capabilities(profile)
    state.emit_event(db, goal_id, "capabilities_discovered",
                     {"count": len(capabilities), "keys": [c["capability_key"] for c in capabilities]},
                     actor="planner")
    plan_result = planner.generate_plan(
        goal_statement=goal_statement, acceptance=acceptance,
        profile=profile, capabilities=capabilities, memory_context=memory_ctx,
        prior_context=prior_context,
    )

    # ===== Phase 4: Plan 校验（确定性）=====
    validation = planner.validate_plan(plan_result["steps"], capabilities)
    if not validation["valid"]:
        state.emit_event(db, goal_id, "plan_validation_failed",
                         {"problems": validation["problems"]}, actor="planner")
        state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "blocked",
                         f"计划校验失败: {validation['problems']}")
        return {"ok": False, "stage": "validation", "problems": validation["problems"]}

    enriched = planner.enrich_steps(
        plan_result["steps"], capabilities, plan_version=plan_version, acceptance=acceptance
    )
    for step in enriched:
        step["plan_kind"] = plan_kind
    _save_steps(goal_id, enriched)
    goals.update_one({"goal_id": goal_id}, {"$set": {
        "plan_version": plan_version,
        "current_plan_kind": plan_kind,
    }})
    state.emit_event(db, goal_id, "plan_generated", {
        "step_count": len(enriched), "plan_summary": plan_result["plan_summary"],
        "plan_kind": plan_kind, "plan_version": plan_version,
        "needs_approval": any(s["requires_approval"] for s in enriched),
    }, actor="planner")
    _emit_planner_thinking(
        db, goal_id, plan_kind=plan_kind, plan_version=plan_version,
        plan_summary=plan_result.get("plan_summary", ""),
        steps=enriched, capabilities=capabilities, acceptance=acceptance,
        prior_context=prior_context,
    )

    # ===== Phase 5: 决定下一状态 =====
    needs_approval = any(s["requires_approval"] for s in enriched)
    if needs_approval:
        state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "awaiting_approval",
                         "计划含高风险步骤，等待批准")
        next_state = "awaiting_approval"
    else:
        state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "running", "计划已就绪，开始执行")
        next_state = "running"
        from engine import goal_scheduler
        goal_scheduler.advance(goal_id)

    return {
        "ok": True, "profile": profile, "policy": policy,
        "goal_statement": goal_statement, "acceptance": acceptance,
        "steps": enriched, "plan_summary": plan_result["plan_summary"],
        "next_state": next_state, "goal_confidence": goal_confidence,
        "plan_confidence": plan_result["confidence"],
        "plan_kind": plan_kind,
        "plan_version": plan_version,
    }


def _repos_needing_prep(sources: list) -> list:
    """需要资源准备的 repo：给了 git 地址但本地还没 clone（无 local_path 或路径不存在）。"""
    import os
    out = []
    for s in sources:
        if s.get("type") != "repo":
            continue
        git_url = s.get("git_url") or s.get("repo_url") or s.get("git")
        lp = s.get("local_path")
        if git_url and (not lp or not os.path.isdir(lp)):
            out.append(s)
    return out


def _plan_resource_and_start(goal_id: str, profile: dict, policy: dict, repos_to_prep: list) -> dict:
    """Plan1 资源准备：按 git 地址+分支 clone/fetch/checkout，产出 local_path 回写 source。

    多 repo 扇出：每个待准备 repo 一个 git_prepare step（带 source_ref）。
    """
    from engine import goal_scheduler
    goals = get_collection("ai_goals")
    db = goals.database

    state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "planning", "开始生成资源准备计划")
    goals.update_one({"goal_id": goal_id}, {"$set": {
        "target_state": "unknown",
        "plan_version": 1,
        "current_plan_kind": "resource",
    }})

    capabilities = [_git_prepare_capability()]
    raw_steps = []
    for r in repos_to_prep:
        sid = r.get("repo_id") or r.get("source_id") or f"repo{len(raw_steps)}"
        raw_steps.append({
            "step_id": f"r{len(raw_steps) + 1}",
            "name": f"准备仓库 {r.get('repo_id') or sid}@{r.get('branch', 'master')}",
            "capability_key": "git_prepare",
            "agent_id": "agent_git_prepare",
            "source_ref": sid,
            "depends_on": [],
            "serves_acceptance": [],
            "required_sources": [],
            "produces_evidence": [],
            "evidence_type": None,
            "phase": "execution",
            "executor": "ai_worker",
            "risk_level": "low",
            "requires_approval": False,
            "can_execute": True,
            "needs_upgrade": False,
            "fallback": None,
            "rationale": "传入的是 git 地址+分支，先 clone/fetch/checkout 到本地工作区",
            "status": "pending",
            "attempts": [],
            "plan_version": 1,
            "plan_kind": "resource",
        })

    if not raw_steps:
        # 没有可准备的 repo（理论不该到这）→ 直接进探查
        from engine import probe_planner
        return _plan_discovery_and_start(goal_id, profile, policy,
                                         probe_planner.select_probe_capabilities(profile))

    _save_steps(goal_id, raw_steps)
    state.emit_event(db, goal_id, "plan_generated", {
        "plan_kind": "resource", "plan_version": 1,
        "step_count": len(raw_steps), "plan_summary": "克隆/拉取代码仓库，准备本地资源",
        "needs_approval": False,
    }, actor="planner")
    _emit_planner_thinking(
        db, goal_id, plan_kind="resource", plan_version=1,
        plan_summary="克隆/拉取代码仓库，准备本地资源",
        steps=raw_steps, capabilities=capabilities, acceptance=[],
    )
    state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "running", "资源准备计划已就绪，开始执行")
    goal_scheduler.advance(goal_id)
    return {"ok": True, "stage": "resource", "plan_kind": "resource",
            "plan_version": 1, "steps": raw_steps}


def complete_resource_plan(goal_id: str) -> dict:
    """resource plan 全部终态后由 scheduler 唤醒：把 clone 出的 local_path 回写 sources，
    重新画像（此时 repo 已在本地，能力升级），再进入目标发现 plan（plan_version=2）。"""
    goals = get_collection("ai_goals")
    goal = goals.find_one({"goal_id": goal_id}, {"_id": 0})
    if not goal:
        return {"ok": False, "error": "Goal 不存在"}
    db = goals.database

    # 取 resource 产物（git_prepare 输出的 local_path），按 repo_id/source_ref 回写 source
    arts = list(get_collection("ai_goal_artifacts").find(
        {"goal_id": goal_id, "plan_kind": "resource"}, {"_id": 0}))
    prepared = {}   # source_ref / repo_id → local_path
    for a in arts:
        data = a.get("data", {}) or {}
        lp = data.get("local_path")
        if lp:
            prepared[a.get("source_ref") or data.get("repo_id", "")] = lp
            if data.get("repo_id"):
                prepared[data["repo_id"]] = lp

    sources = goal.get("sources", [])
    for s in sources:
        if s.get("type") != "repo":
            continue
        key = s.get("source_id") or s.get("repo_id", "")
        lp = prepared.get(key) or prepared.get(s.get("repo_id", ""))
        if lp:
            s["local_path"] = lp
    goals.update_one({"goal_id": goal_id}, {"$set": {"sources": sources}})
    state.emit_event(db, goal_id, "resource_ready", {
        "prepared": [{"ref": k, "local_path": v} for k, v in prepared.items()],
    }, actor="scheduler")

    # 重新画像（local_path 已就绪）
    profile = profile_sources(sources)
    policy = evidence_policy(profile)
    profile["producible_evidence_types"] = planner.producible_evidence_types(profile)
    goals.update_one({"goal_id": goal_id}, {"$set": {
        "feasibility": profile, "evidence_policy": policy,
    }})

    # 资源就绪后能力升级 → 刷新注册表（append-only）
    from engine import agent_runtime
    # 只登记当前输入下【真正能执行】的智能体进工作集合（没文档就不显示需求分析、没URL就不显示api/web测试）。
    # 规划器另行用 discover_capabilities 看全量(含 needs_upgrade)，不受影响。
    _caps = [c for c in planner.discover_capabilities(profile) if c.get("can_execute_now")]
    agent_runtime.register_candidate_agents(goal_id, _caps)
    state.emit_event(db, goal_id, "agents_registered", {
        "count": len(_caps),
        "agents": [{"agent_id": c.get("agent_id"), "name": c.get("agent_name", ""),
                    "capability": c.get("capability_key"), "can_execute": c.get("can_execute_now")}
                   for c in _caps],
    }, actor="steward")

    from engine import probe_planner
    probe_caps = probe_planner.select_probe_capabilities(profile)
    if not probe_caps:
        return _fallback_generate_goal(goal_id, profile, policy)
    return _schedule_plan_transition(
        goal_id, from_kind="resource", to_kind="discovery", plan_version=2,
        trigger="resource_ready",
    )


def _discovery_step_name(capability_key: str) -> str:
    names = {
        "requirement_analysis": "需求分析",
        "code_scan": "代码画像扫描",
        "branch_review": "代码变更分析",
    }
    return names.get(capability_key, capability_key)


def _plan_discovery_and_start(goal_id: str, profile: dict, policy: dict, probe_caps: list,
                              plan_version: int = 1) -> dict:
    """把原隐藏 probe 升级为可见 discovery plan。

    第一版 plan 不再假装目标已存在；它的职责是运行分析智能体，找到目标。
    多 repo 时按 repo 扇出（code_scan 每个 repo 一个 step，带 source_ref）。
    """
    from engine import goal_scheduler
    goals = get_collection("ai_goals")
    goal = goals.find_one({"goal_id": goal_id}, {"_id": 0})
    db = goals.database

    state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "planning", "开始生成目标发现计划")
    goals.update_one({"goal_id": goal_id}, {"$set": {
        "target_state": "discovering",
        "plan_version": plan_version,
        "current_plan_kind": "discovery",
    }})

    capabilities = planner.discover_capabilities(profile)
    cap_map = {c["capability_key"]: c for c in capabilities}
    repos = profile.get("repos", []) or []
    raw_steps = []

    def _add(cap, source_ref="", suffix=""):
        c = cap_map.get(cap)
        if not c:
            return
        raw_steps.append({
            "step_id": f"d{len(raw_steps) + 1}",
            "name": _discovery_step_name(cap) + (f"·{suffix}" if suffix else ""),
            "capability_key": cap,
            "source_ref": source_ref,
            "depends_on": [],
            "serves_acceptance": [],
            "needs_upgrade": not c.get("can_execute_now", False),
            "rationale": "目标发现阶段需要先理解输入资源",
        })

    for cap in probe_caps:
        if cap == "code_scan" and len(repos) > 1:
            # 多 repo 扇出：每个 repo 一个代码画像 step（带 source_ref，不再塌缩成一个）
            for r in repos:
                _add(cap, source_ref=r.get("repo_id") or r.get("source_id", ""),
                     suffix=r.get("repo_id") or r.get("source_id", ""))
        else:
            _add(cap)

    if not raw_steps:
        # 没有可用探查智能体 → 回退兼容路径（Steward 直接据 sources 生成目标）
        return _fallback_generate_goal(goal_id, profile, policy)

    validation = planner.validate_plan(raw_steps, capabilities)
    if not validation["valid"]:
        state.emit_event(db, goal_id, "plan_validation_failed",
                         {"problems": validation["problems"], "plan_kind": "discovery"}, actor="planner")
        state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "blocked",
                         f"目标发现计划校验失败: {validation['problems']}")
        return {"ok": False, "stage": "discovery_plan", "problems": validation["problems"]}

    enriched = planner.enrich_steps(raw_steps, capabilities, plan_version=plan_version, acceptance=[])
    for step in enriched:
        step["plan_kind"] = "discovery"
    _save_steps(goal_id, enriched)
    state.emit_event(db, goal_id, "plan_generated", {
        "plan_kind": "discovery", "plan_version": plan_version,
        "step_count": len(enriched), "plan_summary": "分析输入资源，发现并确认目标",
        "needs_approval": any(s["requires_approval"] for s in enriched),
    }, actor="planner")
    _emit_planner_thinking(
        db, goal_id, plan_kind="discovery", plan_version=plan_version,
        plan_summary="分析输入资源，发现并确认目标",
        steps=enriched, capabilities=capabilities, acceptance=[],
    )

    state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "running", "目标发现计划已就绪，开始执行")
    goal_scheduler.advance(goal_id)
    return {"ok": True, "stage": "discovery", "plan_kind": "discovery",
            "plan_version": plan_version, "steps": enriched}


def _fallback_generate_goal(goal_id: str, profile: dict, policy: dict) -> dict:
    """无探查智能体时的兼容路径：Steward 直接据 sources 生成目标。"""
    goals = get_collection("ai_goals")
    goal = goals.find_one({"goal_id": goal_id}, {"_id": 0})
    db = goals.database
    memory_ctx = steward.retrieve_memory(req_id=goal.get("req_id", ""), verified_only=True)
    doc_content, testcase_content, code_summary = "", "", ""
    for src in goal.get("sources", []):
        if src.get("type") == "doc":
            doc_content += (src.get("content", "") or src.get("doc_content", "")) + "\n"
        elif src.get("type") == "testcase":
            testcase_content += (src.get("content", "") or "") + "\n"
        if src.get("type") == "repo":
            code_summary += f"仓库 {src.get('repo_id')}/{src.get('branch', '')} "
    gr = steward.generate_goal(
        title=goal.get("title", ""), doc_content=doc_content.strip(),
        testcase_content=testcase_content.strip(), code_summary=code_summary,
        input_mode=profile["input_mode"], memory_context=memory_ctx,
        allowed_evidence=_effective_evidence(profile),
    )
    goals.update_one({"goal_id": goal_id}, {"$set": {
        "goal_statement": gr["goal_statement"],
        "objectives": gr.get("objectives", []),
        "acceptance": gr["acceptance"],
        "goal_confidence": gr["confidence"], "goal_rationale": gr.get("rationale", ""),
        "target_state": "confirmed",
    }})
    _emit_steward_thinking(db, goal_id, gr, source="fallback_raw_sources", profile=profile)
    state.emit_event(db, goal_id, "goal_generated", {
        "goal_statement": gr["goal_statement"],
        "objective_count": len(gr.get("objectives", [])),
        "acceptance_count": len(gr["acceptance"]),
        "confidence": gr["confidence"],
    }, actor="steward")
    current_kind = goal.get("current_plan_kind") or "discovery"
    next_version = max(int(goal.get("plan_version", 1) or 1) + 1, 1)
    return _schedule_plan_transition(
        goal_id, from_kind=current_kind, to_kind="objective", plan_version=next_version,
        trigger="fallback_goal_generated",
        context={"acceptance": gr["acceptance"]},
    )


def complete_discovery_plan(goal_id: str) -> dict:
    """discovery plan 全部终态后由 scheduler 唤醒：综合目标并生成 objective plan。"""
    goals = get_collection("ai_goals")
    goal = goals.find_one({"goal_id": goal_id}, {"_id": 0})
    if not goal:
        return {"ok": False, "error": "Goal 不存在"}
    db = goals.database

    profile = goal.get("feasibility", {})
    policy = goal.get("evidence_policy", {})
    memory_ctx = steward.retrieve_memory(goal_id=goal_id, verified_only=False)
    artifacts = list(get_collection("ai_goal_artifacts").find(
        {"goal_id": goal_id, "plan_kind": "discovery"}, {"_id": 0}
    ).sort("created_at", 1))
    if not artifacts:
        # 兼容旧数据或所有 discovery step 降级未产物的情况。
        artifacts = list(get_collection("ai_goal_artifacts").find(
            {"goal_id": goal_id, "phase": "probe"}, {"_id": 0}
        ).sort("created_at", 1))

    probe_outputs = [{
        "type": a.get("type"),
        "summary": a.get("summary", ""),
        "data": a.get("data", {}),
        "source_ref": a.get("source_ref", ""),
        "source_name": (a.get("inputs_snapshot") or {}).get("repo_name", ""),
    } for a in artifacts]
    gr = steward.synthesize_goal_from_probe(
        title=goal.get("title", ""), probe_outputs=probe_outputs,
        input_mode=profile.get("input_mode", "doc_only"),
        allowed_evidence=_effective_evidence(profile), memory_context=memory_ctx,
    )
    goals.update_one({"goal_id": goal_id}, {"$set": {
        "goal_statement": gr["goal_statement"],
        "objectives": gr.get("objectives", []),
        "acceptance": gr["acceptance"],
        "goal_confidence": gr["confidence"], "goal_rationale": gr.get("rationale", ""),
        "target_state": "confirmed",
    }})
    _emit_steward_thinking(db, goal_id, gr, source="discovery_plan", profile=profile)
    state.emit_event(db, goal_id, "goal_generated", {
        "goal_statement": gr["goal_statement"],
        "objective_count": len(gr.get("objectives", [])),
        "acceptance_count": len(gr["acceptance"]),
        "confidence": gr["confidence"], "from": "discovery_plan",
    }, actor="steward")

    next_version = max(int(goal.get("plan_version", 1) or 1) + 1, 2)
    return _schedule_plan_transition(
        goal_id, from_kind="discovery", to_kind="objective", plan_version=next_version,
        trigger="goal_generated",
        context={"acceptance": gr["acceptance"]},
    )


def trigger_code_update_round(goal_id: str, reason: str = "代码更新", sides=None,
                              changed_repo_id: str = "", before_ref: str = "",
                              after_ref: str = "", changed_files=None) -> dict:
    """外部代码更新（新提交）→ 同一 Goal 推进新一轮 objective plan，对新代码重新验证。

    这是"持续守护/多轮"的最小落地：把上一轮 objective step append-only 留痕，
    验证级验收点重置 pending（要对新代码重验），重规划 objective 跑新一轮。

    sides: 显式指定本轮触及的 side（backend/web/client）；不传则按 git diff 自动探测。
    changed_repo_id/before_ref/after_ref/changed_files: webhook 触发时携带的远端变更上下文。
           有 changed_repo_id 时只计算该仓，不误扫同 goal 下其他仓的本地 HEAD。
           只重置/只重跑【被触及 side】的验证级验收点 —— 改后端只亮 api、改前端只亮 web。
           探测不出 side（无法 diff）→ 退回"全部重置"的兼容行为。
    """
    from engine.contracts import is_verification_grade
    from engine import blast_radius as br
    goals = get_collection("ai_goals")
    goal = goals.find_one({"goal_id": goal_id}, {"_id": 0})
    if not goal:
        return {"ok": False, "error": "Goal 不存在"}
    db = goals.database
    status = goal.get("status")

    if status == "completed":
        return {"ok": False, "skipped": True, "reason": "goal 已完成(终态)，无法再起轮；建议 continuous 策略"}
    if status in ("discovering", "planning", "running", "verifying", "replanning"):
        return {"ok": False, "skipped": True, "reason": f"上一轮仍在进行({status})，跳过本次触发"}

    # ===== 爆炸范围：本轮代码改动触及哪些 side =====
    touched = set(sides) if sides else set()
    diff_known = bool(sides)
    changed_files_by_repo = {}
    diff_failures = []
    if not touched:
        for r in goal.get("sources", []):
            if r.get("type") != "repo":
                continue
            rid = r.get("repo_id", "")
            if changed_repo_id and rid != changed_repo_id:
                continue
            files = []
            if changed_files is not None and (not changed_repo_id or rid == changed_repo_id):
                files = list(changed_files or [])
                diff_known = True
            elif r.get("local_path"):
                base = before_ref if changed_repo_id and rid == changed_repo_id and before_ref else "HEAD~1"
                head = after_ref if changed_repo_id and rid == changed_repo_id and after_ref else "HEAD"
                result = br.git_changed_files_result(r["local_path"], base_ref=base, head_ref=head)
                files = result.get("files", [])
                if result.get("ok"):
                    diff_known = True
                else:
                    diff_failures.append({"repo_id": rid, "reason": result.get("reason", "diff_failed")})
            changed_files_by_repo[rid or r.get("local_path", "")] = files
            touched |= br.changed_sides_from_files(files)

    if diff_known and not touched:
        state.emit_event(db, goal_id, "code_update_ignored", {
            "reason": "changed_files_no_test_side",
            "changed_repo_id": changed_repo_id,
            "before": before_ref,
            "after": after_ref,
            "changed_files_by_repo": changed_files_by_repo,
            "diff_failures": diff_failures,
        }, actor="code_update")
        return {"ok": True, "skipped": True, "reason": "本次改动未命中可测试 side",
                "changed_repo_id": changed_repo_id, "touched_sides": []}

    # partial_completed / guarding / paused / blocked → 回 running
    state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "running", reason, actor="code_update")

    new_pv = int(goal.get("plan_version", 1)) + 1
    new_round = int(goal.get("round", 1)) + 1

    # 上一轮 step append-only 留痕
    get_collection("ai_goal_steps").update_many(
        {"goal_id": goal_id, "superseded_by": {"$exists": False}},
        {"$set": {"superseded_by": new_pv}})

    # 只重置被触及 side 的验证级验收点。
    # - diff_known=False：探测不可用 → 退回全部重置兼容；
    # - diff_known=True 且 touched 为空：本次改动没命中 backend/web/client → 不重置验证点。
    acc = goal.get("acceptance", [])
    reset_ids = br.acceptance_to_reset(acc, touched) if touched else (set() if diff_known else None)
    for a in acc:
        if not is_verification_grade(a.get("evidence_type", "")):
            continue
        if reset_ids is None or a.get("id") in reset_ids:
            # 如果指定了 changed_repo_id，只重置该 repo 的验收点
            if changed_repo_id and a.get("source_ref") and a.get("source_ref") != changed_repo_id:
                continue
            a["verdict"] = "pending"
            a["bound_to"] = None
    goals.update_one({"goal_id": goal_id}, {"$set": {
        "acceptance": acc, "plan_version": new_pv, "round": new_round,
    }})
    state.emit_event(db, goal_id, "code_update_round", {
        "round": new_round, "plan_version": new_pv, "reason": reason,
        "touched_sides": sorted(touched),
        "reset_acceptance": (sorted(reset_ids) if reset_ids is not None else "all"),
        "changed_repo_id": changed_repo_id,
        "before": before_ref,
        "after": after_ref,
        "changed_files_by_repo": changed_files_by_repo,
        "diff_known": diff_known,
        "diff_failures": diff_failures,
    }, actor="code_update")

    profile = goal.get("feasibility", {}) or profile_sources(goal.get("sources", []))
    policy = goal.get("evidence_policy", {})
    memory_ctx = steward.retrieve_memory(goal_id=goal_id, verified_only=False)
    # side gating：只规划"未通过"的验收点（被重置的触及 side + 历史未达成），
    # 已通过/已准备的不再重跑 —— 改后端这轮就只排 api、不重排 web。
    active_acc = [a for a in acc if a.get("verdict") not in ("pass", "prepared", "not_applicable")]
    # 只规划被改动 repo 的验收点
    if changed_repo_id:
        active_acc = [a for a in active_acc if not a.get("source_ref") or a.get("source_ref") == changed_repo_id]
    plan_acc = active_acc or acc
    return _schedule_plan_transition(
        goal_id, from_kind=goal.get("current_plan_kind", "objective"), to_kind="objective",
        plan_version=new_pv, trigger="code_update",
        context={"acceptance": plan_acc},
    )


def pause_goal(goal_id: str, reason: str = "人工暂停", actor: str = "human") -> dict:
    """暂停 Goal：阻止后续调度推进；已派出的远端任务不强杀，回调会因 paused 不再 advance。"""
    goals = get_collection("ai_goals")
    goal = goals.find_one({"goal_id": goal_id}, {"_id": 0})
    if not goal:
        return {"ok": False, "error": "Goal 不存在"}
    if goal.get("status") == "paused":
        return {"ok": True, "status": "paused", "idempotent": True}

    db = goals.database
    try:
        state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "paused", reason, actor=actor)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    state.emit_event(db, goal_id, "goal_paused", {"reason": reason}, actor=actor)
    return {"ok": True, "status": "paused"}


def resume_goal(goal_id: str, reason: str = "人工恢复", actor: str = "human") -> dict:
    """恢复 Goal：paused → running，并立刻推进一次 DAG。"""
    goals = get_collection("ai_goals")
    goal = goals.find_one({"goal_id": goal_id}, {"_id": 0})
    if not goal:
        return {"ok": False, "error": "Goal 不存在"}
    if goal.get("status") == "running":
        return {"ok": True, "status": "running", "idempotent": True}

    db = goals.database
    try:
        state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "running", reason, actor=actor)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    state.emit_event(db, goal_id, "goal_resumed", {"reason": reason}, actor=actor)
    from engine import goal_scheduler
    advanced = goal_scheduler.advance(goal_id)
    return {"ok": True, "status": "running", "advanced": advanced}


def cancel_goal(goal_id: str, reason: str = "人工取消", actor: str = "human") -> dict:
    """取消 Goal：转终态 cancelled，并把当前活跃未终态 step 标记为取消/跳过。

    这是 goal 级取消，不等同于 device task 级强杀。已经在 worker 中运行的任务即使之后回调，
    scheduler 也不会推进 cancelled Goal。
    """
    goals = get_collection("ai_goals")
    goal = goals.find_one({"goal_id": goal_id}, {"_id": 0})
    if not goal:
        return {"ok": False, "error": "Goal 不存在"}
    if goal.get("status") == "cancelled":
        return {"ok": True, "status": "cancelled", "idempotent": True}

    db = goals.database
    try:
        state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "cancelled", reason, actor=actor)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    terminal = {"completed", "degraded", "skipped", "failed", "cancelled"}
    updated = 0
    steps_col = get_collection("ai_goal_steps")
    for step in steps_col.find({"goal_id": goal_id, "superseded_by": {"$exists": False}}, {"_id": 0}):
        if step.get("status") in terminal:
            continue
        to_status = "cancelled" if step.get("status") in ("pending", "waiting") else "skipped"
        steps_col.update_one(
            {"goal_id": goal_id, "step_id": step.get("step_id"), "superseded_by": {"$exists": False}},
            {"$set": {"status": to_status, "cancel_reason": reason, "updated_at": int(time.time())}},
        )
        updated += 1

    get_collection("ai_workspace_agents").update_many(
        {"goal_id": goal_id, "status": {"$in": ["registered", "idle", "running"]}},
        {"$set": {"status": "cancelled", "updated_at": int(time.time())}},
    )
    state.emit_event(db, goal_id, "goal_cancelled", {
        "reason": reason,
        "steps_marked": updated,
    }, actor=actor)
    # 终态快照（前端终态结果卡统一读 summary；取消也要有，否则取消后无结果可展示）
    try:
        from engine.goal_scheduler import _generate_summary
        _generate_summary(goal_id, "cancelled")
    except Exception:
        pass
    return {"ok": True, "status": "cancelled", "steps_marked": updated}


def on_probe_done(goal_id: str, agent_id: str, output: dict) -> dict:
    """探查智能体完成回调（worker phase=probe 分流到这里）。
    存探查产物 → 全完成则 synthesize_goal_from_probe → 规划启动。
    """
    from engine import agent_runtime
    import uuid
    goals = get_collection("ai_goals")
    goal = goals.find_one({"goal_id": goal_id}, {"_id": 0})
    if not goal:
        return {"ok": False, "error": "Goal 不存在"}
    db = goals.database
    output = output if isinstance(output, dict) else {}

    inst = get_collection("ai_workspace_agents").find_one(
        {"goal_id": goal_id, "agent_id": agent_id, "phase": "probe"}, {"_id": 0})
    capability = output.get("capability_key") or (inst or {}).get("capability_key", "")

    # 存探查产物（phase=probe，自包含留痕，与 step 产物同格式，供 synthesize 与幻觉核查）
    get_collection("ai_goal_artifacts").insert_one({
        "artifact_id": f"art_{uuid.uuid4().hex[:8]}", "goal_id": goal_id, "phase": "probe",
        "type": capability, "capability_key": capability, "agent_id": agent_id,
        "agent_name": (inst or {}).get("agent_name", ""),
        "source_ref": (inst or {}).get("source_ref", ""),
        "inputs_snapshot": (inst or {}).get("inputs_snapshot", {}),
        "reasoning": (output.get("report") or output.get("reasoning") or "")[:8000],
        "summary": (output.get("summary", "") or "")[:200], "data": output,
        "created_at": int(time.time()),
    })
    agent_runtime.mark_agent("completed", agent_id=agent_id, goal_id=goal_id, phase="probe")
    state.emit_event(db, goal_id, "probe_done", {"agent_id": agent_id, "capability": capability}, actor="steward")

    collected = agent_runtime.collect_probe_outputs(goal_id)
    if not collected["all_done"]:
        return {"ok": True, "stage": "probe", "waiting": True}

    # 全部探查完成 → 据探查产物综合目标
    profile = goal.get("feasibility", {})
    policy = goal.get("evidence_policy", {})
    memory_ctx = steward.retrieve_memory(goal_id=goal_id, verified_only=False)
    probe_outputs = [{
        "type": a.get("type"),
        "summary": a.get("summary", ""),
        "data": a.get("data", {}),
        "source_ref": a.get("source_ref", ""),
        "source_name": (a.get("inputs_snapshot") or {}).get("repo_name", ""),
    } for a in collected["outputs"]]
    gr = steward.synthesize_goal_from_probe(
        title=goal.get("title", ""), probe_outputs=probe_outputs,
        input_mode=profile.get("input_mode", "doc_only"),
        allowed_evidence=_effective_evidence(profile), memory_context=memory_ctx,
    )
    goals.update_one({"goal_id": goal_id}, {"$set": {
        "goal_statement": gr["goal_statement"],
        "objectives": gr.get("objectives", []),
        "acceptance": gr["acceptance"],
        "goal_confidence": gr["confidence"], "goal_rationale": gr.get("rationale", ""),
        "target_state": "confirmed",
    }})
    _emit_steward_thinking(db, goal_id, gr, source="legacy_probe", profile=profile)
    state.emit_event(db, goal_id, "goal_generated", {
        "goal_statement": gr["goal_statement"],
        "objective_count": len(gr.get("objectives", [])),
        "acceptance_count": len(gr["acceptance"]),
        "confidence": gr["confidence"], "from": "probe",
    }, actor="steward")
    return _plan_and_start(goal_id, profile, policy, gr["goal_statement"],
                           gr["acceptance"], gr["confidence"], memory_ctx)


def replan(goal_id: str, reason: str = "", trigger: str = "critic") -> dict:
    """重规划：基于上一轮执行情况生成【不同的】新 plan（append-only 保留旧计划）。

    只应在当前轮所有 step 已终态时调用（由 _verify_goal/critic 触发，故无 in-flight step 冲突）。
    流程：转 replanning → 收集上轮上下文 → 旧 step 标 superseded → 版本+1 → Planner(带上轮上下文)
          → 校验 → 落库新版本 step → 回 running/awaiting_approval → advance。
    """
    goals = get_collection("ai_goals")
    goal = goals.find_one({"goal_id": goal_id}, {"_id": 0})
    if not goal:
        raise ValueError(f"Goal 不存在: {goal_id}")

    db = goals.database
    cur_pv = goal.get("plan_version", 1)
    new_pv = cur_pv + 1
    new_round = goal.get("round", 1) + 1
    new_replan_count = goal.get("replan_count", 0) + 1

    # 转 replanning（verifying/running/blocked → replanning）
    state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "replanning",
                     reason or "验收未达标，重新规划", actor=trigger)

    profile = goal.get("feasibility", {})
    acceptance = goal.get("acceptance", [])
    passed = [a for a in acceptance if a.get("verdict") == "pass"]
    unmet = [a for a in acceptance if a.get("verdict") != "pass"]

    # ===== 收集上一轮上下文（在 supersede 之前读取活跃 step）=====
    last_steps = list(get_collection("ai_goal_steps").find(
        {"goal_id": goal_id, "superseded_by": {"$exists": False}}, {"_id": 0}
    ))
    prior_context = _build_prior_context(passed, unmet, last_steps)

    state.emit_event(db, goal_id, "replan_triggered", {
        "round": new_round, "plan_version": new_pv, "reason": reason,
        "passed": [a["id"] for a in passed], "unmet": [a["id"] for a in unmet],
    }, actor=trigger)

    # ===== 旧计划 append-only：标 superseded =====
    get_collection("ai_goal_steps").update_many(
        {"goal_id": goal_id, "superseded_by": {"$exists": False}},
        {"$set": {"superseded_by": new_pv}}
    )
    goals.update_one({"goal_id": goal_id}, {"$set": {
        "plan_version": new_pv, "round": new_round, "replan_count": new_replan_count,
    }})

    # ===== 下一轮 objective 真实延迟启动（带上轮上下文，保证 plan 不同）=====
    return _schedule_plan_transition(
        goal_id, from_kind="objective", to_kind="objective", plan_version=new_pv,
        trigger="replan",
        context={"acceptance": acceptance, "prior_context": prior_context},
    )


def _build_prior_context(passed: list, unmet: list, last_steps: list) -> str:
    """把上一轮的执行情况整理成给 Planner 的文本（让它针对缺口重规划，不重复已通过）。"""
    lines = []
    if passed:
        lines.append("已通过验收（不要重复这些工作）：")
        lines += [f"  ✅ {a.get('id')}: {a.get('desc', '')}" for a in passed]
    if unmet:
        lines.append("未达成验收（本轮要重点攻克）：")
        lines += [f"  ❌ {a.get('id')}: {a.get('desc', '')} [证据类型: {a.get('evidence_type', '?')}, 当前判定: {a.get('verdict', 'pending')}]"
                  for a in unmet]
    if last_steps:
        lines.append("上一轮步骤执行结果：")
        for s in last_steps:
            attempts = s.get("attempts", [])
            last_attempt = attempts[-1] if attempts else {}
            detail = last_attempt.get("output_summary", "") or s.get("blocked_reason", "")
            lines.append(f"  - {s.get('name')}({s.get('capability_key')}): {s.get('status')}"
                         + (f" — {detail[:100]}" if detail else ""))
    return "\n".join(lines) or "（无上一轮信息）"


def _save_steps(goal_id: str, steps: list):
    """落库 ai_goal_steps（先清旧的同 plan_version）"""
    col = get_collection("ai_goal_steps")
    for step in steps:
        step["goal_id"] = goal_id
        step["created_at"] = int(time.time())
        col.update_one(
            {"goal_id": goal_id, "step_id": step["step_id"], "plan_version": step.get("plan_version", 1)},
            {"$set": step}, upsert=True
        )


def get_goal_full(goal_id: str) -> dict:
    """获取 Goal 完整信息（含 steps + 事件流 + 证据 + 产物 + 总结）供前端展示"""
    goal = get_collection("ai_goals").find_one({"goal_id": goal_id}, {"_id": 0})
    if not goal:
        return None
    # 活跃 step（当前轮次，未被 replan 取代）供实时 DAG 展示
    steps = list(get_collection("ai_goal_steps").find(
        {"goal_id": goal_id, "superseded_by": {"$exists": False}}, {"_id": 0}
    ).sort("step_id", 1))
    # 历史 step（被 replan 取代的旧计划）— 只返回轻量字段供回放标题
    superseded = list(get_collection("ai_goal_steps").find(
        {"goal_id": goal_id, "superseded_by": {"$exists": True}},
        {"_id": 0, "step_id": 1, "capability_key": 1, "status": 1, "plan_version": 1,
         "superseded_by": 1, "agent_name": 1, "source_ref": 1, "evidence_type": 1}
    ).sort([("plan_version", 1), ("step_id", 1)]))
    # 事件流：最近 100 条（前端按需加载更多）
    events = _latest_events(goal_id, limit=100)
    evidence = list(get_collection("ai_goal_evidence").find(
        {"goal_id": goal_id}, {"_id": 0}
    ).sort("created_at", 1))
    # 产物：排除 data 大字段 + 限制最近 50 条
    artifacts = list(get_collection("ai_goal_artifacts").find(
        {"goal_id": goal_id}, {"_id": 0, "data": 0}
    ).sort("created_at", -1).limit(50))
    artifacts.reverse()
    summary = get_collection("ai_goal_summary").find_one({"goal_id": goal_id}, {"_id": 0})
    agents = list(get_collection("ai_workspace_agents").find(
        {"goal_id": goal_id}, {"_id": 0}
    ).sort([("phase", 1), ("installed_at", 1)]))
    memories = list(get_collection("ai_memory_points").find(
        {"goal_id": goal_id}, {"_id": 0}
    ).sort("created_at", -1).limit(30))

    return {
        "goal": goal,
        "steps": steps,
        "superseded_steps": superseded,
        "events": events,
        "evidence": evidence,
        "artifacts": artifacts,
        "agents": agents,
        "summary": summary,
        "memories": memories,
    }
