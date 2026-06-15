"""Steward 记忆体元智能体 — Goal Runtime 的"脑"

四个作用点：
1. 目标生成（goal_discovery）：从输入推断"用户想改变的世界状态" + 验收点
2. 目标偏移检测（alignment）：新信息 vs 当前目标的对齐度评估
3. 记忆评估/沉淀（每步完成后）
4. 复盘萃取（Goal 完成后）

所有 LLM 输出走 StructuredLLM，保证稳定。记忆分层：raw→inference→verified_fact→project_rule。
"""
import time
import uuid

from llm.structured import generate_structured
from common.db import get_collection


# ==================== 1. 目标生成 ====================

GOAL_GEN_SCHEMA = {
    "required": ["goal_statement", "acceptance", "confidence"],
    "types": {"goal_statement": "str", "acceptance": "list", "confidence": "float"},
}


def _probe_source_ref(o: dict) -> str:
    return o.get("source_ref") or o.get("repo_id") or (o.get("data") or {}).get("source_ref", "")


def _probe_project_type(o: dict) -> str:
    return str((o.get("data") or {}).get("project_type") or "").lower()


def _code_probe_refs(probe_outputs: list, evidence_type: str = "") -> list:
    """按证据类型筛出适合的 code_scan 源；用于给 acceptance 确定性补 source_ref。"""
    candidates = [o for o in probe_outputs if o.get("type") == "code_scan" and _probe_source_ref(o)]
    if not evidence_type:
        return [_probe_source_ref(o) for o in candidates]
    if evidence_type == "api_test":
        wanted = ("backend", "api")
    elif evidence_type == "web_test":
        wanted = ("frontend", "web")
    elif evidence_type == "device_test":
        wanted = ("android", "ios", "mobile")
    else:
        wanted = ()
    matched = [_probe_source_ref(o) for o in candidates if any(w in _probe_project_type(o) for w in wanted)]
    return matched or [_probe_source_ref(o) for o in candidates]


def _fill_acceptance_source_ref(acceptance: list, probe_outputs: list) -> None:
    """LLM 可给 source_ref，但不能完全信它；这里按 evidence_type 做确定性兜底。"""
    for a in acceptance:
        et = a.get("evidence_type", "")
        if a.get("source_ref") or et in ("doc_review", "testcase_generated"):
            continue
        refs = _code_probe_refs(probe_outputs, et)
        if len(refs) == 1:
            a["source_ref"] = refs[0]


def generate_goal(title: str, doc_content: str = "", code_summary: str = "",
                  input_mode: str = "doc_only", memory_context: str = "",
                  allowed_evidence: list = None, model_id: str = "gemini_flash") -> dict:
    """从输入生成目标契约（goal_statement + acceptance points）。

    第一性原理：Goal = 用户真正想改变的世界状态；Acceptance = 什么证据能证明完成。
    allowed_evidence: EvidencePolicy 给出的当前可产出证据类型，验收点的 evidence_type 只能从中选。
    """
    allowed_evidence = allowed_evidence or ["doc_review"]
    system = (
        "你是测试平台的记忆体主管(Steward)。你的职责是把用户输入提炼成清晰的测试目标和可验证的验收点。\n"
        "目标必须是'要改变/验证的世界状态'，不是'做什么动作'。\n"
        "每个验收点必须是可以用证据证明的，不能是模糊描述。\n"
        f"严格约束：验收点的 evidence_type 只能从这些当前能产出的类型里选：{allowed_evidence}。\n"
        "不要要求当前无法产出的证据类型（诚实判断证据边界）。"
    )

    parts = [f"需求标题：{title}"]
    if doc_content:
        parts.append(f"需求文档：\n{doc_content[:3000]}")
    if code_summary:
        parts.append(f"代码变更摘要：\n{code_summary[:1500]}")
    if memory_context:
        parts.append(f"历史经验（参考，避免重复踩坑）：\n{memory_context}")
    parts.append(f"输入模式：{input_mode}")
    parts.append(f"当前可产出的证据类型（验收点只能用这些）：{allowed_evidence}")

    user = "\n\n".join(parts) + """

请生成测试目标，输出 JSON：
{
  "goal_statement": "一句话描述要验证/改变的世界状态",
  "acceptance": [
    {"id": "a1", "desc": "可验证的验收点", "evidence_type": "doc_review|static_analysis|api_test|web_test|device_test"}
  ],
  "rationale": "为什么这样拆解目标",
  "confidence": 0.0到1.0
}
acceptance 的 evidence_type 要符合输入模式能力：doc_only 模式只能用 doc_review/testcase_generated。"""

    result = generate_structured(
        system, user, schema=GOAL_GEN_SCHEMA, model_id=model_id,
        max_retries=2,
        default={"goal_statement": title, "acceptance": [], "confidence": 0.0, "rationale": "生成失败降级"},
    )

    # 给 acceptance 补 id 和 bound_to
    data = result.data
    for i, a in enumerate(data.get("acceptance", [])):
        if "id" not in a:
            a["id"] = f"a{i+1}"
        a["bound_to"] = None       # 证据初始未绑定
        a["verdict"] = "pending"

    return {
        "goal_statement": data.get("goal_statement", title),
        "acceptance": data.get("acceptance", []),
        "rationale": data.get("rationale", ""),
        "confidence": data.get("confidence", 0.0),
        "_meta": {"ok": result.ok, "attempts": result.attempts, "degraded": result.degraded, "usage": result.usage},
    }


# ==================== 1b. 据探查产物综合目标（Discovery Probe Round）====================

def synthesize_goal_from_probe(title: str, probe_outputs: list, input_mode: str = "doc_only",
                               allowed_evidence: list = None, memory_context: str = "",
                               model_id: str = "gemini_flash") -> dict:
    """据探查产物（需求拆解 / 代码画像）综合规整成目标契约。

    方案 A：不重新发明目标，把探查智能体已经理解出的验收点候选/可测面【综合规整】成
    goal_statement + 结构化 acceptance（补 id / evidence_type / bound_to）。
    probe_outputs: [{type, summary, data}]，type 为 capability_key。
    """
    allowed_evidence = allowed_evidence or ["doc_review"]
    parts = [f"目标标题：{title}", f"输入模式：{input_mode}"]
    for o in probe_outputs:
        data = o.get("data", {}) or {}
        cap = o.get("type", "")
        if cap == "requirement_analysis":
            parts.append("【需求拆解探查产物】")
            parts.append(f"  验收点候选：{data.get('acceptance_points', [])}")
            parts.append(f"  用例候选：{data.get('test_cases', [])}")
        elif cap == "code_scan":
            parts.append("【代码画像探查产物】")
            parts.append(f"  仓库标识 source_ref：{o.get('source_ref') or data.get('source_ref', '')}")
            parts.append(f"  仓库名称：{o.get('source_name') or data.get('repo_name', '')}")
            parts.append(f"  项目类型：{data.get('project_type')}")
            parts.append(f"  可测面：{data.get('testable_surfaces', [])}")
            parts.append(f"  建议验收：{data.get('suggested_acceptance', [])}")
            parts.append(f"  推断风险：{data.get('inferred_risks', [])}")
        else:
            parts.append(f"【探查产物 {cap}】{o.get('summary', '')}")
    if memory_context:
        parts.append(f"历史经验（参考）：{memory_context}")

    # 区分输入侧：有代码探查→必须产代码侧验收点(static_analysis)；有需求探查→产文档侧
    probe_caps = {o.get("type") for o in probe_outputs}
    has_code = bool(probe_caps & {"code_scan", "branch_review"})
    has_doc = "requirement_analysis" in probe_caps
    side_rule = []
    if has_doc:
        side_rule.append("- 文档侧(side=doc)：从需求拆出的验收点，evidence_type=testcase_generated/doc_review（只证明用例已生成/需求已拆解，不代表业务通过）")
    if has_code:
        side_rule.append("- 代码侧(side=code)：针对代码变更/可测面的验收点，evidence_type=static_analysis（证明变更影响面合规、无夹带改动、关键模块正确）")
    side_text = "\n".join(side_rule)

    system = (
        "你是记忆体主管(Steward)。探查智能体已对输入做了理解，下面是探查产物。\n"
        "职责：把探查产物【综合规整】成清晰的测试目标和可验证验收点——不要重新发明、不要脱离探查产物。\n"
        "目标是'要验证/改变的世界状态'，不是'做什么动作'。\n"
        "若同时有需求和代码，【两侧都必须出验收点】：文档侧 + 代码侧，不能只偏一侧。\n"
        "代码/API/Web/真机类验收点必须带 source_ref，值必须来自对应代码画像探查产物的 source_ref。\n"
        + (side_text + "\n" if side_text else "")
        + f"严格约束：验收点 evidence_type 只能从当前可产出类型里选：{allowed_evidence}。"
    )
    user = "\n".join(str(p) for p in parts) + """

基于探查产物，输出 JSON：
{
  "goal_statement": "一句话描述要验证/改变的世界状态（综合自探查产物）",
  "acceptance": [
    {"id": "a1", "desc": "可验证的验收点", "side": "doc|code|api|web", "evidence_type": "doc_review|static_analysis|testcase_generated|api_test|web_test|device_test", "source_ref": "代码仓库 source_ref；文档侧可空"}
  ],
  "rationale": "如何从探查产物综合出该目标",
  "confidence": 0.0到1.0
}
只用当前可产出的 evidence_type，诚实判断证据边界。
有代码探查产物时，acceptance 必须包含若干 side=code 的代码侧验收点（evidence_type=static_analysis）。"""

    result = generate_structured(
        system, user, schema=GOAL_GEN_SCHEMA, model_id=model_id, max_retries=2,
        default={"goal_statement": title, "acceptance": [], "confidence": 0.0, "rationale": "综合失败降级"},
    )
    data = result.data
    acceptance = data.get("acceptance", [])
    for i, a in enumerate(acceptance):
        if "id" not in a:
            a["id"] = f"a{i+1}"
        a["bound_to"] = None
        a["verdict"] = "pending"
        if not a.get("side"):
            a["side"] = "code" if a.get("evidence_type") in (
                "static_analysis", "api_test", "web_test", "device_test", "e2e_test") else "doc"
    _fill_acceptance_source_ref(acceptance, probe_outputs)
    # 接口测试可达时（环境有 base_url），确保至少有一个 api_test 验收点——
    # 否则规划器不会排 api_test，接口行为永远停在"静态分析/用例生成"，无法真验。
    if "api_test" in (allowed_evidence or []) and not any(a.get("evidence_type") == "api_test" for a in acceptance):
        refs = _code_probe_refs(probe_outputs, "api_test")
        acceptance.append({
            "id": f"api{len(acceptance) + 1}",
            "desc": "接口实际行为符合需求（对受影响接口发请求验证：入参校验/错误码/成功语义）",
            "evidence_type": "api_test", "side": "api",
            "source_ref": refs[0] if len(refs) == 1 else "",
            "bound_to": None, "verdict": "pending",
        })
    # Web 测试可达时，确保 Web 仓库也能落到 objective 阶段的 web_test 步骤。
    if "web_test" in (allowed_evidence or []) and not any(a.get("evidence_type") == "web_test" for a in acceptance):
        refs = _code_probe_refs(probe_outputs, "web_test")
        acceptance.append({
            "id": f"web{len(acceptance) + 1}",
            "desc": "Web 前端页面实际行为符合需求（访问测试环境并验证关键交互/展示）",
            "evidence_type": "web_test", "side": "web",
            "source_ref": refs[0] if len(refs) == 1 else "",
            "bound_to": None, "verdict": "pending",
        })
    # 客户端(Android/iOS)测试可达时，确保客户端仓库也能落到 objective 阶段的 device_test 步骤。
    if "device_test" in (allowed_evidence or []) and not any(a.get("evidence_type") == "device_test" for a in acceptance):
        refs = _code_probe_refs(probe_outputs, "device_test")
        acceptance.append({
            "id": f"dev{len(acceptance) + 1}",
            "desc": "客户端(App)关键交互实际行为符合需求（生成 UI 脚本并验证登录/核心流程）",
            "evidence_type": "device_test", "side": "client",
            "source_ref": refs[0] if len(refs) == 1 else "",
            "bound_to": None, "verdict": "pending",
        })
    data["acceptance"] = acceptance
    return {
        "goal_statement": data.get("goal_statement", title),
        "acceptance": data.get("acceptance", []),
        "rationale": data.get("rationale", ""),
        "confidence": data.get("confidence", 0.0),
        "_meta": {"ok": result.ok, "attempts": result.attempts, "degraded": result.degraded},
    }


# ==================== 2. 目标偏移检测 ====================

ALIGNMENT_SCHEMA = {
    "required": ["alignment", "action", "confidence"],
    "types": {"alignment": "int", "action": "str", "confidence": "float"},
}


def assess_alignment(goal_statement: str, acceptance: list, new_info: str,
                     model_id: str = "gemini_flash") -> dict:
    """评估新信息与当前目标的对齐度。

    返回 alignment(0-100) + action(none/expand/switch) + 偏移原因。
    用于：动态新增源、代码提交、新文档时判断是否偏离目标。
    """
    system = (
        "你是记忆体主管(Steward)的目标守卫模块。评估新信息是否与当前测试目标一致。\n"
        "对齐度高=新信息服务于当前目标；对齐度低=新信息引入了目标外的方向。"
    )

    acc_text = "\n".join(f"- {a.get('desc', '')}" for a in acceptance)
    user = f"""当前目标：{goal_statement}

当前验收点：
{acc_text}

新信息：
{new_info[:2000]}

评估新信息与当前目标的对齐度，输出 JSON：
{{
  "alignment": 0到100的整数,
  "reason": "一句话说明对齐或偏离的原因",
  "action": "none|expand|switch",
  "suggested_acceptance": ["若需扩展/切换，建议新增的验收点描述"],
  "confidence": 0.0到1.0
}}
action 含义：none=完全对齐无需动作；expand=部分相关建议扩展目标；switch=严重偏离建议切换/分支。"""

    result = generate_structured(
        system, user, schema=ALIGNMENT_SCHEMA, model_id=model_id,
        max_retries=2,
        default={"alignment": 50, "action": "none", "reason": "评估失败降级", "confidence": 0.0},
    )

    data = result.data
    return {
        "alignment": data.get("alignment", 50),
        "reason": data.get("reason", ""),
        "action": data.get("action", "none"),
        "suggested_acceptance": data.get("suggested_acceptance", []),
        "confidence": data.get("confidence", 0.0),
        "_meta": {"ok": result.ok, "attempts": result.attempts, "degraded": result.degraded},
    }


# ==================== 3. 记忆评估/沉淀 ====================

def evaluate_and_remember(goal_id: str, step_id: str, agent_id: str,
                          output_summary: str, goal_statement: str = "",
                          model_id: str = "gemini_flash") -> dict:
    """每步完成后：记忆体评估产出 + 沉淀记忆点。

    记忆分层：本次评估产出 inference 层记忆（需后续证据背书才升 verified_fact）。
    """
    system = "你是记忆体主管(Steward)。评估智能体产出，提炼值得影响后续决策的记忆。"
    user = f"""当前目标：{goal_statement or '(未设定)'}

智能体产出摘要：
{output_summary[:2000]}

评估并提炼记忆，输出 JSON：
{{
  "conclusion": "一句话总结这步做了什么、结果如何",
  "quality": {{"confidence": 0.0到1.0, "hallucination_risk": "low|medium|high"}},
  "memory_point": "值得影响后续决策的一条精简结论（不记流水账）",
  "memory_layer": "raw_observation|inference",
  "confidence": 0.0到1.0
}}"""

    result = generate_structured(
        system, user, model_id=model_id, max_retries=2,
        default={"conclusion": output_summary[:100], "memory_point": "", "memory_layer": "raw_observation", "confidence": 0.0},
    )
    data = result.data

    # 写记忆点
    point_id = f"mp_{uuid.uuid4().hex[:8]}"
    if data.get("memory_point"):
        get_collection("ai_memory_points").insert_one({
            "point_id": point_id,
            "goal_id": goal_id,
            "step_id": step_id,
            "agent_id": agent_id,
            "summary": data.get("memory_point", ""),
            "layer": data.get("memory_layer", "inference"),  # 默认 inference
            "quality": data.get("quality", {}),
            "verified": False,           # 需证据背书才 verified
            "source": "steward_evaluation",
            "created_at": int(time.time()),
        })

    return {
        "conclusion": data.get("conclusion", ""),
        "memory_point_id": point_id if data.get("memory_point") else None,
        "quality": data.get("quality", {}),
        "_meta": {"ok": result.ok, "degraded": result.degraded},
    }


def retrieve_memory(goal_id: str = "", req_id: str = "", limit: int = 5,
                    verified_only: bool = False) -> str:
    """检索历史记忆（喂给 Planner）。
    verified_only=True 时只返回 verified_fact/project_rule（强影响规划）。
    """
    query = {}
    if goal_id:
        query["goal_id"] = goal_id
    if req_id:
        query["req_id"] = req_id
    if verified_only:
        query["$or"] = [{"verified": True}, {"layer": "project_rule"}]

    points = list(get_collection("ai_memory_points").find(
        query, {"_id": 0, "summary": 1, "layer": 1}
    ).sort("created_at", -1).limit(limit))

    return "\n".join(f"[{p.get('layer', '?')}] {p['summary']}" for p in points)


# ==================== 4. 记忆晋升（凭证据背书）====================

def promote_memory(point_id: str, evidence_verdict: str):
    """证据背书晋升：inference 被 evidence(verdict=pass) 支撑 → 升 verified_fact"""
    if evidence_verdict == "pass":
        get_collection("ai_memory_points").update_one(
            {"point_id": point_id},
            {"$set": {"verified": True, "layer": "verified_fact", "promoted_at": int(time.time())}}
        )
        return True
    return False
