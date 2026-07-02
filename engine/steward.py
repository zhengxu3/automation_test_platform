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
    "types": {"goal_statement": "str", "objectives": "list", "acceptance": "list", "confidence": "float"},
}


def _slug(value: str, fallback: str = "main") -> str:
    raw = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or ""))
    raw = "_".join(part for part in raw.split("_") if part)
    return (raw or fallback)[:32]


def _acceptance_group(a: dict) -> str:
    et = a.get("evidence_type", "")
    side = a.get("side", "")
    if et == "api_test" or side == "api":
        return "api"
    if et == "web_test" or side == "web":
        return "web"
    if et == "device_test" or side in ("client", "device"):
        return "client"
    if et == "e2e_test":
        return "e2e"
    if et == "static_analysis" or side == "code":
        ref = a.get("source_ref", "")
        return f"code_{_slug(ref)}" if ref else "code"
    if et in ("doc_review", "testcase_generated") or side == "doc":
        return "requirement"
    return "general"


def _objective_template(group: str, *, title: str, source_ref: str = "",
                        input_mode: str = "", confidence: float = 0.0) -> dict:
    if group.startswith("code_") or group == "code":
        scope = [source_ref] if source_ref else []
        name = f"仓库 {source_ref} 代码变更影响范围清晰且无明显回归风险" if source_ref else "代码变更影响范围清晰且无明显回归风险"
        return {
            "objective_id": f"obj_{group}",
            "title": name,
            "desc": "通过代码画像/变更分析确认本次代码改动的影响范围、风险点和回归面。",
            "source": "code_inferred",
            "scope": scope,
            "priority": "P0" if input_mode in ("repo_only", "full") else "P1",
            "confidence": confidence,
            "status": "pending",
        }
    templates = {
        "requirement": ("需求意图被正确理解并形成可验证范围", "doc", "P0"),
        "api": ("受影响后端接口行为符合目标且保持兼容", "code_inferred", "P0"),
        "web": ("Web 前端关键展示与交互符合目标", "code_inferred", "P1"),
        "client": ("客户端关键交互符合目标", "code_inferred", "P1"),
        "e2e": ("跨端链路围绕本次变更保持一致", "cross_repo_inferred", "P0"),
        "general": (title or "目标可被证据覆盖", "inferred", "P1"),
    }
    obj_title, source, priority = templates.get(group, templates["general"])
    return {
        "objective_id": f"obj_{group}",
        "title": obj_title,
        "desc": "该目标是需求任务下的业务/技术结果；验收点和测试用例只是证明路径。",
        "source": source,
        "scope": [source_ref] if source_ref else [],
        "priority": priority,
        "confidence": confidence,
        "status": "pending",
    }


def _normalize_objectives(raw: list, *, title: str, confidence: float) -> list:
    objectives = []
    seen = set()
    for i, obj in enumerate(raw or [], 1):
        if not isinstance(obj, dict):
            continue
        oid = obj.get("objective_id") or obj.get("id") or f"obj_{i}"
        oid = _slug(oid, f"obj_{i}")
        if not oid.startswith("obj_"):
            oid = f"obj_{oid}"
        if oid in seen:
            oid = f"{oid}_{i}"
        seen.add(oid)
        objectives.append({
            "objective_id": oid,
            "title": obj.get("title") or obj.get("name") or title or f"目标 {i}",
            "desc": obj.get("desc", ""),
            "source": obj.get("source", "inferred"),
            "scope": obj.get("scope", []) or ([] if not obj.get("source_ref") else [obj.get("source_ref")]),
            "priority": obj.get("priority", "P1"),
            "confidence": obj.get("confidence", confidence),
            "status": obj.get("status", "pending"),
            "needs_confirmation": bool(obj.get("needs_confirmation", False)),
        })
    return objectives


def _ensure_goal_contract_layers(data: dict, *, title: str, input_mode: str,
                                 allowed_evidence: list, probe_outputs: list = None) -> dict:
    """兼容式目标分层：objectives 是目标，acceptance 是证明条件，不把 test cases 当目标。"""
    data = data or {}
    confidence = data.get("confidence", 0.0)
    acceptance = data.get("acceptance", []) or []
    if not isinstance(acceptance, list):
        acceptance = []

    # 先补验收点基础字段；后面再按验收点分组补 objectives。
    for i, a in enumerate(acceptance):
        if not isinstance(a, dict):
            acceptance[i] = {"id": f"a{i+1}", "desc": str(a), "evidence_type": (allowed_evidence or ["doc_review"])[0]}
            a = acceptance[i]
        if not a.get("id"):
            a["id"] = f"a{i+1}"
        if not a.get("evidence_type"):
            a["evidence_type"] = (allowed_evidence or ["doc_review"])[0]
        a.setdefault("bound_to", None)
        a.setdefault("verdict", "pending")
        a.setdefault("coverage_role", "required")

    objectives = _normalize_objectives(data.get("objectives", []), title=title, confidence=confidence)
    by_id = {o["objective_id"]: o for o in objectives}

    def ensure_group_objective(group: str, source_ref: str = "") -> str:
        oid = f"obj_{group}"
        if oid not in by_id:
            obj = _objective_template(group, title=title, source_ref=source_ref,
                                      input_mode=input_mode, confidence=confidence)
            by_id[obj["objective_id"]] = obj
            objectives.append(obj)
        return oid

    for a in acceptance:
        if a.get("objective_id"):
            oid = a["objective_id"]
            if oid not in by_id:
                if objectives:
                    a["objective_id"] = objectives[0]["objective_id"]
                else:
                    group = _acceptance_group(a)
                    a["objective_id"] = ensure_group_objective(group, a.get("source_ref", ""))
            continue
        group = _acceptance_group(a)
        group_oid = f"obj_{group}"
        if group_oid in by_id:
            a["objective_id"] = group_oid
        elif len(objectives) == 1:
            a["objective_id"] = objectives[0]["objective_id"]
        else:
            a["objective_id"] = ensure_group_objective(group, a.get("source_ref", ""))

    # 极端降级：有目标但无验收点，目标可展示但不能假装已可验证。
    if not objectives:
        group = "requirement" if input_mode == "doc_only" else "code" if input_mode == "repo_only" else "general"
        objectives.append(_objective_template(group, title=title, input_mode=input_mode, confidence=confidence))

    data["objectives"] = objectives
    data["acceptance"] = acceptance
    return data


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
                  testcase_content: str = "",
                  input_mode: str = "doc_only", memory_context: str = "",
                  allowed_evidence: list = None, model_id: str = "gemini_flash") -> dict:
    """从输入生成目标契约（objectives + acceptance points）。

    第一性原理：Goal = 用户真正想改变的世界状态；Acceptance = 什么证据能证明完成。
    allowed_evidence: EvidencePolicy 给出的当前可产出证据类型，验收点的 evidence_type 只能从中选。
    """
    allowed_evidence = allowed_evidence or ["doc_review"]

    testcase_guidance = ""
    if testcase_content:
        testcase_guidance = (
            "注意：用户提供了测试用例文件。你的职责是从用例**反推业务目标**——这些用例整体在验证什么业务结果？\n"
            "归纳成 1-5 个 objectives。原始用例全部归入候选池，不要逐条变成 acceptance 或 objective。\n"
        )

    system = (
        "你是测试平台的记忆体主管(Steward)。你的职责是把用户输入提炼成【少量目标】和【可验证验收标准】。\n"
        "目标 objective 必须是'要改变/验证的业务或技术结果'，不是测试步骤，也不是单条测试用例。\n"
        "验收点 acceptance 是证明目标成立的条件；测试用例 test_cases 只是候选执行手段，不要把用例逐条升级成目标。\n"
        "无文档时只能生成 code_inferred 推断目标，语气要保守，低置信目标必须 needs_confirmation=true。\n"
        + testcase_guidance
        + f"严格约束：验收点的 evidence_type 只能从这些当前能产出的类型里选：{allowed_evidence}。\n"
        "不要要求当前无法产出的证据类型（诚实判断证据边界）。"
    )

    parts = [f"需求标题：{title}"]
    if doc_content:
        parts.append(f"需求文档：\n{doc_content[:3000]}")
    if testcase_content:
        parts.append(f"测试用例文件（仅作候选资产，从中反推业务目标，不要逐条升级）：\n{testcase_content[:4000]}")
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
  "objectives": [
    {"objective_id": "obj_policy", "title": "目标标题，不是用例", "desc": "目标说明", "source": "doc|code_inferred|doc+code|cross_repo_inferred", "scope": ["repo_id可选"], "priority": "P0|P1|P2", "confidence": 0.0到1.0, "needs_confirmation": false}
  ],
  "acceptance": [
    {"id": "a1", "objective_id": "obj_policy", "desc": "证明某个目标成立的验收标准", "evidence_type": "doc_review|static_analysis|api_test|web_test|device_test", "coverage_role": "required|optional"}
  ],
  "rationale": "为什么这样拆解目标",
  "confidence": 0.0到1.0
}
规则：
- objectives 通常 1-5 条，不要按测试用例数量膨胀。
- acceptance 是覆盖目标的证明条件，不要求 100% 测试用例都执行。
- acceptance 的 evidence_type 要符合输入模式能力：doc_only 模式只能用 doc_review/testcase_generated。"""

    result = generate_structured(
        system, user, schema=GOAL_GEN_SCHEMA, model_id=model_id,
        max_retries=2,
        default={"goal_statement": title, "objectives": [], "acceptance": [], "confidence": 0.0, "rationale": "生成失败降级"},
    )

    data = result.data
    data = _ensure_goal_contract_layers(
        data, title=title, input_mode=input_mode,
        allowed_evidence=allowed_evidence, probe_outputs=[],
    )

    return {
        "goal_statement": data.get("goal_statement", title),
        "objectives": data.get("objectives", []),
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

    方案 A：不重新发明目标，把探查智能体已经理解出的业务目标/可测面【综合规整】成
    objectives + 结构化 acceptance。测试用例只作为候选资产，不直接等同目标。
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
            parts.append(f"  用例候选（仅作执行候选池，不要逐条升级成目标）：{data.get('test_cases', [])}")
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
        "职责：把探查产物【综合规整】成少量 objectives 和可验证 acceptance——不要重新发明、不要脱离探查产物。\n"
        "objective 是'要验证/改变的业务或技术结果'，不是测试动作，也不是单条测试用例。\n"
        "test_cases 只是候选用例池；用例未必 100% 执行，只要 acceptance 覆盖目标即可。\n"
        "若同时有需求和代码，要生成文档业务目标 + 代码/联动风险目标，不能只偏一侧。\n"
        "若没有文档只有代码，只能生成 code_inferred 推断目标，必须保守表达，置信度低时 needs_confirmation=true。\n"
        "代码/API/Web/真机类验收点必须带 source_ref，值必须来自对应代码画像探查产物的 source_ref。\n"
        + (side_text + "\n" if side_text else "")
        + f"严格约束：验收点 evidence_type 只能从当前可产出类型里选：{allowed_evidence}。"
    )
    user = "\n".join(str(p) for p in parts) + """

基于探查产物，输出 JSON：
{
  "goal_statement": "一句话描述要验证/改变的世界状态（综合自探查产物）",
  "objectives": [
    {"objective_id": "obj_policy", "title": "目标标题，不是测试用例", "desc": "目标说明", "source": "doc|code_inferred|doc+code|cross_repo_inferred", "scope": ["repo_id可选"], "priority": "P0|P1|P2", "confidence": 0.0到1.0, "needs_confirmation": false}
  ],
  "acceptance": [
    {"id": "a1", "objective_id": "obj_policy", "desc": "证明某个目标成立的验收标准", "side": "doc|code|api|web", "evidence_type": "doc_review|static_analysis|testcase_generated|api_test|web_test|device_test", "source_ref": "代码仓库 source_ref；文档侧可空", "coverage_role": "required|optional"}
  ],
  "rationale": "如何从探查产物综合出该目标",
  "confidence": 0.0到1.0
}
只用当前可产出的 evidence_type，诚实判断证据边界。
目标通常 1-5 条，不要按测试用例数量膨胀。
有代码探查产物时，acceptance 必须包含若干 side=code 的代码侧验收点（evidence_type=static_analysis）。"""

    result = generate_structured(
        system, user, schema=GOAL_GEN_SCHEMA, model_id=model_id, max_retries=2,
        default={"goal_statement": title, "objectives": [], "acceptance": [], "confidence": 0.0, "rationale": "综合失败降级"},
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
    # LLM 降级/漏产时做保守兜底：代码-only 至少有代码影响范围目标的验收点；
    # 文档-only 至少从验收点候选中抽少量证明条件。注意不把 test_cases 逐条升级。
    if has_code and "static_analysis" in (allowed_evidence or []) \
            and not any(a.get("evidence_type") == "static_analysis" for a in acceptance):
        refs = _code_probe_refs(probe_outputs)
        for ref in refs or [""]:
            acceptance.append({
                "id": f"code{len(acceptance) + 1}",
                "desc": (f"仓库 {ref} 的代码变更影响范围、风险点和回归面已被识别"
                         if ref else "代码变更影响范围、风险点和回归面已被识别"),
                "evidence_type": "static_analysis", "side": "code",
                "source_ref": ref,
                "bound_to": None, "verdict": "pending",
                "coverage_role": "required",
            })
    if has_doc and not acceptance and ("testcase_generated" in (allowed_evidence or []) or "doc_review" in (allowed_evidence or [])):
        et = "testcase_generated" if "testcase_generated" in (allowed_evidence or []) else "doc_review"
        points = []
        for o in probe_outputs:
            if o.get("type") == "requirement_analysis":
                points.extend((o.get("data") or {}).get("acceptance_points", []) or [])
        for point in points[:6] or ["需求意图已被拆解成可验证范围"]:
            acceptance.append({
                "id": f"doc{len(acceptance) + 1}",
                "desc": str(point),
                "evidence_type": et, "side": "doc",
                "source_ref": "",
                "bound_to": None, "verdict": "pending",
                "coverage_role": "required",
            })
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
            "coverage_role": "required",
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
            "coverage_role": "required",
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
            "coverage_role": "required",
        })
    data["acceptance"] = acceptance
    data = _ensure_goal_contract_layers(
        data, title=title, input_mode=input_mode,
        allowed_evidence=allowed_evidence, probe_outputs=probe_outputs,
    )
    return {
        "goal_statement": data.get("goal_statement", title),
        "objectives": data.get("objectives", []),
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
