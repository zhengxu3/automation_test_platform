"""节点契约层 — Goal Runtime 第一性原理

每个节点不是"调prompt生成东西"，而是一份可验证、可审计、可兜底的工作单元契约。
一份契约收敛五个问题：LLM输出校验/失败分类/审批边界/Plan可验证/能力发现。
"""

# ==================== 证据注册表 ====================
# precondition(requires) + strength 一张表。加新证据类型 = 加一行配置，不动引擎。

EVIDENCE_REGISTRY = {
    "doc_review": {
        "label": "文档评审",
        "requires": [["doc"], ["user_desc"]],   # 满足任一组即可（OR of ANDs）
        "strength": 1,
    },
    "static_analysis": {
        "label": "静态代码分析",
        "requires": [["repo"]],
        "strength": 2,
    },
    "testcase_generated": {
        "label": "测试用例生成",
        "requires": [["doc"], ["repo"]],
        "strength": 2,
    },
    "api_test": {
        "label": "API 测试",
        "requires": [["repo:backend", "env:base_url"]],
        "strength": 4,
    },
    "web_test": {
        "label": "Web UI 测试",
        "requires": [["repo:web", "env:web_url"], ["repo:web", "env:base_url"]],
        "strength": 4,
    },
    "device_test": {
        "label": "真机 UI 测试",
        "requires": [["repo:client", "env:apk", "device", "env:test_account"]],
        "strength": 5,
    },
    "e2e_test": {
        "label": "端到端测试",
        "requires": [["env:api", "env:client_pkg", "device", "env:test_data"]],
        "strength": 6,
    },
}


def evidence_satisfiable(evidence_type: str, available: set) -> bool:
    """给定可用能力集合，判断某证据类型是否可产出。
    available 例: {"doc", "repo", "repo:backend", "env:base_url", "device"}
    requires 是 OR-of-ANDs：任一组的所有项都满足即可。
    """
    spec = EVIDENCE_REGISTRY.get(evidence_type)
    if not spec:
        return False
    for group in spec["requires"]:
        if all(item in available for item in group):
            return True
    return False


def allowed_evidence_types(available: set) -> list:
    """返回当前可用能力下，所有可产出的证据类型（按强度排序）"""
    result = [et for et in EVIDENCE_REGISTRY if evidence_satisfiable(et, available)]
    return sorted(result, key=lambda et: EVIDENCE_REGISTRY[et]["strength"])


def missing_for_evidence(evidence_type: str, available: set) -> list:
    """返回产出某证据类型还缺什么（取缺口最小的那组）"""
    spec = EVIDENCE_REGISTRY.get(evidence_type)
    if not spec:
        return []
    best_gap = None
    for group in spec["requires"]:
        gap = [item for item in group if item not in available]
        if best_gap is None or len(gap) < len(best_gap):
            best_gap = gap
    return best_gap or []


# ==================== 证据等级：准备级 vs 验证级 ====================
# 第一性原理：生成用例/拆解需求 ≠ 业务已验证通过。
# 准备级只证明"测试资产已就绪"，业务 pass 必须来自验证级证据。
PREPARATION_EVIDENCE = {"doc_review", "testcase_generated"}        # 只证明"已生成/已拆解"
VERIFICATION_EVIDENCE = {"static_analysis", "api_test", "web_test",
                         "device_test", "e2e_test", "manual_review"}  # 才证明"业务通过"


def is_verification_grade(evidence_type: str) -> bool:
    """该证据类型是否构成业务"验证通过"（而非仅"准备就绪"）。"""
    return evidence_type in VERIFICATION_EVIDENCE


# ==================== 节点契约 ====================
# 每个节点至少 7 字段：purpose/input/output/success/failure/risk/mutates

NODE_CONTRACTS = {
    "git_prepare": {
        "purpose": "资源准备：按 git 地址+分支 clone/fetch/checkout，产出本地 repo 路径供后续分析",
        "executor": "ai_worker",
        "input": ["git_url", "branch", "repo_id"],
        "output": ["local_path", "repo_id", "branch"],
        "produces_evidence": [],   # 资源准备，不产验收证据
        "success": lambda o: bool(o.get("local_path")),
        "failure": ["clone_failed", "branch_not_found", "auth_failed"],
        "risk": "low",
        "mutates": False,          # 只写到独立工作区，不碰被测代码
        "timeout_sec": 300,
        "retryable": True,
        "fallback": None,
    },
    "requirement_analysis": {
        "purpose": "解析需求文档，拆出验收点和测试用例",
        "executor": "ai_worker",
        "input": ["doc_content"],
        "output": ["acceptance_points", "test_cases", "docs"],
        "produces_evidence": ["doc_review", "testcase_generated"],
        "success": lambda o: len(o.get("acceptance_points", [])) > 0,
        "failure": ["empty_doc", "invalid_output"],
        "risk": "low",
        "mutates": False,
        "timeout_sec": 300,
        "retryable": True,
        "fallback": None,
    },
    "branch_review": {
        "purpose": "识别代码变更对测试目标的影响",
        "executor": "ai_worker",
        "input": ["repo_id", "base_branch", "target_branch", "acceptance"],
        "output": ["change_summary", "risk_points", "regression_cases", "affected_modules", "interface_doc"],
        "produces_evidence": ["static_analysis"],
        "success": lambda o: len(o.get("regression_cases", [])) > 0 or o.get("no_change") is True,
        "failure": ["repo_unavailable", "empty_diff", "invalid_output"],
        "risk": "low",
        "mutates": False,
        "timeout_sec": 600,
        "retryable": True,
        "fallback": None,
    },
    "alignment_analysis": {
        "purpose": "需求-代码对齐：发现夹带改动/漏实现/影响面不一致",
        "executor": "ai_worker",
        "input": ["acceptance_points", "change_summary"],
        "output": ["aligned", "extra_changes", "missing_impl", "alignment_score"],
        "produces_evidence": ["static_analysis"],
        "success": lambda o: "alignment_score" in o,
        "failure": ["insufficient_input", "invalid_output"],
        "risk": "low",
        "mutates": False,
        "timeout_sec": 300,
        "retryable": True,
        "fallback": None,
    },
    "script_gen": {
        "purpose": "为回归清单生成可执行 UI 测试脚本",
        "executor": "device_worker",
        "input": ["regression_cases", "package", "device_caps"],
        "output": ["script_path", "covered_cases"],
        "produces_evidence": [],   # 生成不产证据，执行才产
        "success": lambda o: bool(o.get("script_path")),
        "failure": ["no_cases", "gen_invalid_code"],
        "risk": "medium",        # 生成代码，需评估
        "mutates": False,        # 写到独立脚本目录，不碰被测代码
        "timeout_sec": 300,
        "retryable": True,
        "fallback": "doc_review",
    },
    "device_execution": {
        "purpose": "在真机执行脚本并采集证据",
        "executor": "device_worker",
        "input": ["script_path", "device_id"],
        "output": ["test_result", "screenshots", "report_url"],
        "produces_evidence": ["device_test"],
        "success": lambda o: o.get("test_result") in ("pass", "fail"),  # 跑完即成功，pass/fail是结果
        "failure": ["device_offline", "script_crash", "timeout"],
        "risk": "high",          # 执行 AI 生成代码 + 占用设备，必须审批
        "mutates": True,         # 操作设备状态
        "timeout_sec": 900,
        "retryable": True,
        "fallback": "script_steps_doc",
    },
    "api_test": {
        "purpose": "对后端接口生成请求并验证响应",
        "executor": "device_worker",
        "input": ["repo_id", "base_url", "auth", "interface_doc"],
        "output": ["test_result", "cases_passed", "cases_failed"],
        "produces_evidence": ["api_test"],
        "success": lambda o: o.get("test_result") in ("pass", "fail"),
        "failure": ["env_unreachable", "auth_failed", "timeout"],
        "risk": "medium",
        "mutates": False,        # 只读接口测试（不含写操作）
        "timeout_sec": 600,
        "retryable": True,
        "fallback": "static_analysis",
    },
    "web_test": {
        "purpose": "对 Web 前端页面执行可达性/交互冒烟验证，产出 Web UI 测试证据",
        "executor": "device_worker",
        "input": ["repo_id", "base_url", "acceptance"],
        "output": ["test_result", "cases_passed", "cases_failed", "report"],
        "produces_evidence": ["web_test"],
        "success": lambda o: o.get("test_result") in ("pass", "fail"),
        "failure": ["env_unreachable", "page_error", "timeout"],
        "risk": "medium",
        "mutates": False,
        "timeout_sec": 600,
        "retryable": True,
        "fallback": "static_analysis",
    },
    "device_test": {
        "purpose": "对客户端(Android/iOS)生成 UI 自动化脚本并验证关键交互，产出真机 UI 测试证据",
        "executor": "device_worker",
        "input": ["repo_id", "repo_path", "acceptance"],
        "output": ["test_result", "cases_passed", "cases_failed", "report"],
        "produces_evidence": ["device_test"],
        "success": lambda o: o.get("test_result") in ("pass", "fail"),
        "failure": ["device_offline", "apk_missing", "script_crash", "timeout"],
        "risk": "medium",
        "mutates": False,
        "timeout_sec": 900,
        "retryable": True,
        "fallback": "static_analysis",
    },
    "code_scan": {
        "purpose": "扫描单个仓库，产出项目画像（类型/模块/入口/可测面/风险/建议验收），用于无需求时从代码反推目标",
        "input": ["repo_path"],
        "output": ["project_type", "main_modules", "entry_points", "testable_surfaces",
                   "inferred_risks", "suggested_acceptance", "summary"],
        "produces_evidence": [],   # 探查产物，不直接绑验收证据（用于生成目标，非证明目标）
        "success": lambda o: bool(o.get("summary")) and bool(o.get("project_type") or o.get("testable_surfaces")),
        "failure": ["repo_unavailable", "empty_repo", "invalid_output"],
        "risk": "low",
        "mutates": False,
        "timeout_sec": 600,
        "retryable": True,
        "fallback": None,
    },
}


def get_contract(capability_key: str) -> dict:
    return NODE_CONTRACTS.get(capability_key)


def get_executor(capability_key: str) -> str:
    """返回能力的执行位置：ai_worker（同进程）/ device_worker（远程设备机）"""
    c = NODE_CONTRACTS.get(capability_key, {})
    return c.get("executor", "ai_worker")


def check_success(capability_key: str, output: dict) -> bool:
    """用契约的 success 判定函数校验产出（确定性，非 LLM 自述）"""
    contract = NODE_CONTRACTS.get(capability_key)
    if not contract:
        return False
    try:
        return bool(contract["success"](output))
    except Exception:
        return False


def validate_step_io(steps: list) -> list:
    """Plan 校验：检查每个 step 的 input 能否被前序 step 的 output 或初始 source 满足。
    返回问题列表（空 = 校验通过）。
    """
    problems = []
    available_outputs = set()  # 累积可用产出键

    # 初始源提供的键
    for step in steps:
        cap = step.get("capability_key")
        contract = NODE_CONTRACTS.get(cap)
        if not contract:
            problems.append(f"step {step.get('step_id')}: 未知能力 '{cap}'")
            continue

    # 依赖拓扑：按 depends_on 顺序累积 output
    step_map = {s.get("step_id"): s for s in steps}
    for step in steps:
        cap = step.get("capability_key")
        contract = NODE_CONTRACTS.get(cap)
        if not contract:
            continue
        # 该步依赖的前序产出
        for dep_id in step.get("depends_on", []):
            dep = step_map.get(dep_id)
            if not dep:
                problems.append(f"step {step.get('step_id')}: 依赖不存在的 step '{dep_id}'")
                continue
            dep_contract = NODE_CONTRACTS.get(dep.get("capability_key"))
            if dep_contract:
                available_outputs.update(dep_contract["output"])
    return problems


def detect_cycle(steps: list) -> bool:
    """检测 DAG 是否成环"""
    graph = {s.get("step_id"): set(s.get("depends_on", [])) for s in steps}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in graph}

    def dfs(node):
        if color.get(node) == GRAY:
            return True
        if color.get(node) == BLACK:
            return False
        color[node] = GRAY
        for dep in graph.get(node, set()):
            if dep in graph and dfs(dep):
                return True
        color[node] = BLACK
        return False

    return any(dfs(sid) for sid in graph if color[sid] == WHITE)
