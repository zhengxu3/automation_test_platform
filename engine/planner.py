"""Planner — 规划器（能力发现 + DAG 生成 + 校验）

第一性原理：为了完成目标，当前最小必要动作序列是什么。
Planner 只从已注册的能力清单里选+编排，不创造新能力。
LLM 规划 → plan_validator 确定性校验（依赖成环/能力存在/IO衔接）→ 通过才执行。
"""
import time
import uuid

from llm.structured import generate_structured
from common.db import get_collection
from engine.contracts import NODE_CONTRACTS, detect_cycle, evidence_satisfiable, is_verification_grade


# ==================== 能力发现 ====================

def discover_capabilities(profile: dict) -> list:
    """从 ai_agents 读取可用能力清单，按可行性画像过滤。

    只返回 required_sources 能被当前 available_capabilities 满足的智能体。
    给 Planner 看的是"能力说明"，不是 handler_class 实现细节。
    """
    available = set(profile.get("available_capabilities", []))
    agents = list(get_collection("ai_agents").find(
        {"status": "active"}, {"_id": 0}
    ))

    capabilities = []
    for agent in agents:
        contract = agent.get("capability_contract", {})
        if not contract:
            continue
        # git_prepare 是资源准备能力，只在 Plan1 resource 直接编排，不进目标发现/执行的能力清单
        if agent.get("capability_key") == "git_prepare":
            continue
        required = contract.get("required_sources", [])
        # 检查前置源是否满足（允许部分满足→标记为可降级）
        satisfied = all(_source_available(req, available) for req in required)
        capabilities.append({
            "capability_key": agent.get("capability_key"),
            "agent_id": agent.get("agent_id"),
            "agent_name": agent.get("agent_name"),
            "purpose": contract.get("purpose", agent.get("description", "")),
            "required_sources": required,
            "produces_evidence": contract.get("produces_evidence", []),
            "risk_level": contract.get("risk_level", "low"),
            "requires_approval": contract.get("requires_approval", False),
            "fallback": contract.get("fallback"),
            "can_execute_now": satisfied,
        })
    return capabilities


def _source_available(required: str, available: set) -> bool:
    """检查单个 required source 是否满足。
    required 如 "repo" / "repo:client" / "env:base_url" / "device"
    """
    return required in available


def producible_evidence_types(profile: dict) -> list:
    """当前 active 且源可满足的智能体【真实能产出】的证据类型并集（按 registry 强度排序）。

    比 source_profiler 的 allowed_evidence_types（源理论上限）更收紧的"真实可达天花板"：
    源理论上能产某证据，但没有任何 active 智能体实现它 → 不算可达。
    用于喂给 Steward 目标生成（只拆"真有手能干"的验收点）和 Critic 判定（不可产的不空转 replan）。
    """
    from engine.contracts import EVIDENCE_REGISTRY

    capabilities = discover_capabilities(profile)
    producible = set()
    for c in capabilities:
        if c.get("can_execute_now"):
            producible.update(c.get("produces_evidence", []) or [])

    # 与源理论上限取交集：既要源支持，又要有手能产
    source_allowed = set(profile.get("allowed_evidence_types", []))
    effective = (producible & source_allowed) if source_allowed else producible
    return sorted(effective, key=lambda et: EVIDENCE_REGISTRY.get(et, {}).get("strength", 0))


# ==================== 规划 ====================

PLAN_SCHEMA = {
    "required": ["steps", "confidence"],
    "types": {"steps": "list", "confidence": "float"},
}


def generate_plan(goal_statement: str, acceptance: list, profile: dict,
                  capabilities: list, memory_context: str = "",
                  prior_context: str = "", model_id: str = "gemini_pro") -> dict:
    """LLM 规划 DAG。只能用 capabilities 里声明的能力。

    prior_context: 重规划时注入的上轮上下文（已通过/未达成验收 + 上轮步骤结果）。
    有它时新计划必须聚焦未达成验收点、不重复已验证通过的工作（保证每轮 plan 不同）。
    """

    cap_text = "\n".join(
        f"- {c['capability_key']}: {c['purpose']} "
        f"[需要源: {c['required_sources']}, 产出证据: {c['produces_evidence']}, "
        f"风险: {c['risk_level']}, 当前可执行: {c['can_execute_now']}]"
        for c in capabilities
    )
    acc_text = "\n".join(
        f"- {a.get('id')}: {a.get('desc')} "
        f"(证据类型: {a.get('evidence_type', '?')}, source_ref: {a.get('source_ref', '') or '无'})"
        for a in acceptance
    )

    system = (
        "你是测试编排规划器(Planner)。把目标拆解成最小必要的步骤序列(DAG)。\n"
        "规则：\n"
        "1. 只能使用下方提供的能力，不能臆造能力\n"
        "2. 每步必须服务于某个验收点\n"
        "3. 有依赖的步骤用 depends_on 表达（如先分析代码再生成测试）\n"
        "4. 当前不可执行的能力(can_execute_now=false)仍可规划，但要标记 needs_upgrade\n"
        "5. 步骤要最小必要，不要冗余\n"
        "6. 验收点带 source_ref 时，服务该验收点的 step 必须带同一个 source_ref，以绑定正确仓库\n"
        "7. 若提供了【上一轮执行情况】，这是重规划：新计划必须只针对【未达成的验收点】，"
        "不要重复已通过验收的步骤，要换思路或补步骤去攻克上轮没拿下的缺口"
    )

    prior_section = f"\n【上一轮执行情况（重规划依据）】\n{prior_context}\n" if prior_context else ""

    user = f"""目标：{goal_statement}

输入模式：{profile.get('input_mode')}
可产出证据等级：{profile.get('allowed_evidence_types')}

验收点：
{acc_text}
{prior_section}
可用能力：
{cap_text}

历史经验：
{memory_context or '(无)'}

请规划步骤序列，输出 JSON：
{{
  "steps": [
    {{
      "step_id": "s1",
      "name": "步骤名称",
      "capability_key": "必须是上方可用能力之一",
      "source_ref": "若服务的验收点有 source_ref，这里必须填写同一值",
      "depends_on": [],
      "serves_acceptance": ["a1"],
      "needs_upgrade": false,
      "rationale": "为什么需要这步"
    }}
  ],
  "plan_summary": "整体计划一句话",
  "confidence": 0.0到1.0
}}"""

    result = generate_structured(
        system, user, schema=PLAN_SCHEMA, model_id=model_id,
        max_retries=2,
        default={"steps": [], "confidence": 0.0, "plan_summary": "规划失败降级"},
    )

    return {
        "steps": result.data.get("steps", []),
        "plan_summary": result.data.get("plan_summary", ""),
        "confidence": result.data.get("confidence", 0.0),
        "_meta": {"ok": result.ok, "attempts": result.attempts, "degraded": result.degraded},
    }


# ==================== Plan 校验（确定性）====================

def validate_plan(steps: list, capabilities: list) -> dict:
    """确定性校验 LLM 产出的 plan。返回 {valid, problems}。"""
    problems = []
    valid_caps = {c["capability_key"] for c in capabilities}
    cap_map = {c["capability_key"]: c for c in capabilities}

    if not steps:
        return {"valid": False, "problems": ["计划为空"]}

    step_ids = set()
    for step in steps:
        sid = step.get("step_id")
        cap = step.get("capability_key")

        # 1. step_id 唯一
        if sid in step_ids:
            problems.append(f"重复 step_id: {sid}")
        step_ids.add(sid)

        # 2. 能力必须存在（防臆造）
        if cap not in valid_caps:
            problems.append(f"step {sid}: 臆造了不存在的能力 '{cap}'")

    # 3. depends_on 指向存在的 step
    for step in steps:
        for dep in step.get("depends_on", []):
            if dep not in step_ids:
                problems.append(f"step {step.get('step_id')}: 依赖不存在的 step '{dep}'")

    # 4. 无环
    if detect_cycle(steps):
        problems.append("DAG 存在循环依赖")

    return {"valid": len(problems) == 0, "problems": problems}


def enrich_steps(steps: list, capabilities: list, plan_version: int = 1,
                 acceptance: list = None) -> list:
    """给 plan 的 step 补充契约信息（agent_id/risk/approval/evidence/can_execute）。
    plan_version: 当前计划版本（replan 时递增），落到每个 step 上。
    """
    cap_map = {c["capability_key"]: c for c in capabilities}
    acc_map = {a.get("id"): a for a in (acceptance or [])}
    enriched = []
    for step in steps:
        cap = step.get("capability_key")
        c = cap_map.get(cap, {})
        contract = NODE_CONTRACTS.get(cap, {})
        evidence_type = (c.get("produces_evidence") or [None])[0]
        source_ref = step.get("source_ref", "")
        if not source_ref:
            refs = {
                (acc_map.get(aid) or {}).get("source_ref", "")
                for aid in (step.get("serves_acceptance") or [])
                if (acc_map.get(aid) or {}).get("source_ref", "")
            }
            if len(refs) == 1:
                source_ref = next(iter(refs))
        enriched.append({
            "step_id": step.get("step_id"),
            "name": step.get("name", ""),
            "capability_key": cap,
            "agent_id": c.get("agent_id"),
            "source_ref": source_ref,
            "depends_on": step.get("depends_on", []),
            "serves_acceptance": step.get("serves_acceptance", []),
            "required_sources": c.get("required_sources", []),
            "produces_evidence": c.get("produces_evidence", []),
            "evidence_type": evidence_type,
            # 阶段：产验证级证据的步骤=verification（真验证），否则=execution（分析/生成）
            "phase": "verification" if is_verification_grade(evidence_type) else "execution",
            "executor": NODE_CONTRACTS.get(cap, {}).get("executor", "ai_worker"),
            "risk_level": c.get("risk_level", "low"),
            "requires_approval": c.get("requires_approval", False),
            "can_execute": c.get("can_execute_now", False),
            "needs_upgrade": step.get("needs_upgrade", False) or not c.get("can_execute_now", False),
            "fallback": c.get("fallback"),
            "rationale": step.get("rationale", ""),
            "status": "pending",
            "attempts": [],
            "plan_version": plan_version,
        })
    return enriched
