"""StepInputResolver — 把 goal sources / 前序 probe artifacts 翻译成各能力真实 handler 需要的 payload

主线关键连接件：Planner 选了能力，但真实 handler 需要具体参数：
- requirement_analysis 要 doc_content
- branch_review / code_scan 要 repo_path / branch
没有这层，Goal step 提交了但 handler 拿不到入参 → 跑空。

确定性，零 LLM。直接用 goal.sources 里已带的 local_path/branch，不绕 ai_git_repos。
"""


def _first_doc(sources: list) -> str:
    for s in sources:
        if s.get("type") in ("doc", "user_desc"):
            content = s.get("content") or s.get("doc_content")
            if content:
                return content
    return ""


def _first_repo(sources: list) -> dict:
    for s in sources:
        if s.get("type") == "repo":
            return s
    return {}


def _repo_for_step(sources: list, step: dict) -> dict:
    """多 repo 扇出：优先按 step.source_ref 精确取该步绑定的 repo；无 source_ref 才回退第一个。

    干掉"永远取第一个 repo"的塌缩——多 repo 时每个分析步骤绑定自己的 source_ref。
    """
    step = step or {}
    ref = step.get("source_ref")
    if ref:
        for s in sources:
            if s.get("type") == "repo" and (s.get("source_id") == ref or s.get("repo_id") == ref):
                return s
    return _first_repo(sources)


def _prior_summary(prior_artifacts: list, artifact_type: str, source_ref: str = "") -> str:
    if source_ref:
        for a in (prior_artifacts or []):
            if a.get("type") == artifact_type and a.get("source_ref") == source_ref:
                return a.get("summary", "")
        for a in (prior_artifacts or []):
            if a.get("type") == artifact_type and not a.get("source_ref"):
                return a.get("summary", "")
    for a in (prior_artifacts or []):
        if a.get("type") == artifact_type:
            return a.get("summary", "")
    return ""


def _first_env(sources: list) -> dict:
    for s in sources:
        if s.get("type") == "environment":
            return s
    return {}


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _int_value(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _prior_data(prior_artifacts: list, artifact_type: str, source_ref: str = "") -> dict:
    """取前序某能力 artifact 的完整产出 data（如 branch_review 的 affected_modules/change_summary）。"""
    if source_ref:
        for a in (prior_artifacts or []):
            if a.get("type") == artifact_type and a.get("source_ref") == source_ref:
                return a.get("data", {}) or {}
        for a in (prior_artifacts or []):
            if a.get("type") == artifact_type and not a.get("source_ref"):
                return a.get("data", {}) or {}
    for a in (prior_artifacts or []):
        if a.get("type") == artifact_type:
            return a.get("data", {}) or {}
    return {}


def resolve(capability_key: str, goal: dict, step: dict = None, prior_artifacts: list = None) -> dict:
    """为某能力的真实 handler 组装 inputs（顶层字段，handler 直接可用）。"""
    sources = goal.get("sources", [])
    prior_artifacts = prior_artifacts or []

    if capability_key == "requirement_analysis":
        doc = _first_doc(sources) or goal.get("goal_statement", "") or goal.get("title", "")
        return {"doc_content": doc}

    if capability_key == "git_prepare":
        repo = _repo_for_step(sources, step)
        return {
            "git_url": repo.get("git_url", "") or repo.get("repo_url", "") or repo.get("git", ""),
            "branch": repo.get("branch", "") or "master",
            "repo_id": repo.get("repo_id", "") or repo.get("source_id", ""),
        }

    if capability_key == "code_scan":
        repo = _repo_for_step(sources, step)
        return {
            "repo_path": repo.get("local_path", ""),
            "repo_name": repo.get("repo_id", ""),
            "branch": repo.get("branch", ""),
        }

    if capability_key == "branch_review":
        repo = _repo_for_step(sources, step)
        return {
            "repo_name": repo.get("repo_id", ""),
            "repo_path": repo.get("local_path", ""),
            "base_branch": repo.get("base_branch") or repo.get("branch", "master"),
            "target_branch": repo.get("target_branch") or repo.get("branch", "master"),
            "inputs": {"mode": "最近更新"},
        }

    if capability_key == "alignment_analysis":
        return {
            "acceptance": goal.get("acceptance", []),
            "change_summary": _prior_summary(prior_artifacts, "branch_review"),
            "goal_statement": goal.get("goal_statement", ""),
        }

    if capability_key == "api_test":
        # 爆炸半径来自上游 branch_review 产物；base_url/账号来自 environment 源
        repo = _repo_for_step(sources, step)
        env = _first_env(sources)
        source_ref = (step or {}).get("source_ref", "")
        br = _prior_data(prior_artifacts, "branch_review", source_ref)
        return {
            "repo_path": repo.get("local_path", ""),
            "repo_name": repo.get("repo_id", ""),
            "base_url": env.get("base_url", ""),
            "test_accounts": env.get("test_accounts", []),
            "interface_doc": br.get("interface_doc", {}) or {},
            "affected_modules": br.get("affected_modules", []) or [],
            "change_summary": br.get("change_summary", "") or _prior_summary(prior_artifacts, "branch_review", source_ref),
            "requirement_context": _first_doc(sources) or goal.get("goal_statement", "") or goal.get("title", ""),
            "mock_mode": _truthy(env.get("api_test_mock") or env.get("mock_api_test")),
            "mock_fail_rounds": _int_value(env.get("api_test_mock_fail_rounds") or env.get("mock_fail_rounds")),
            "mock_regenerate_each_round": _truthy(
                env.get("api_test_mock_regenerate_each_round") or env.get("mock_regenerate_each_round")
            ),
            "max_replans": goal.get("budget", {}).get("max_replans", 3),
        }

    if capability_key == "web_test":
        repo = _repo_for_step(sources, step)
        env = _first_env(sources)
        source_ref = (step or {}).get("source_ref", "")
        prior = _prior_data(prior_artifacts, "branch_review", source_ref)
        return {
            "repo_path": repo.get("local_path", ""),
            "repo_name": repo.get("repo_id", ""),
            "base_url": env.get("web_url", "") or env.get("base_url", ""),
            "test_accounts": env.get("test_accounts", []),
            "acceptance": [
                a for a in goal.get("acceptance", [])
                if a.get("id") in (step or {}).get("serves_acceptance", [])
            ],
            "change_summary": prior.get("change_summary", "") or _prior_summary(prior_artifacts, "branch_review", source_ref),
            "requirement_context": _first_doc(sources) or goal.get("goal_statement", "") or goal.get("title", ""),
            "mock_mode": _truthy(env.get("web_test_mock") or env.get("mock_web_test")),
            "mock_fail_rounds": _int_value(env.get("web_test_mock_fail_rounds") or env.get("mock_fail_rounds")),
            "max_replans": goal.get("budget", {}).get("max_replans", 3),
        }

    if capability_key == "device_test":
        repo = _repo_for_step(sources, step)
        env = _first_env(sources)
        source_ref = (step or {}).get("source_ref", "")
        serves = (step or {}).get("serves_acceptance", [])
        return {
            "repo_path": repo.get("local_path", ""),
            "repo_name": repo.get("repo_id", ""),
            "package": env.get("package", "") or env.get("app_package", ""),
            "apk_path": env.get("apk_path", ""),
            "acceptance": [a for a in goal.get("acceptance", []) if a.get("id") in serves],
            "regression_cases": [a.get("desc", "") for a in goal.get("acceptance", []) if a.get("id") in serves],
            "scenario": goal.get("goal_statement", "") or goal.get("title", ""),
            "requirement_context": _first_doc(sources) or goal.get("goal_statement", "") or goal.get("title", ""),
            "change_summary": _prior_summary(prior_artifacts, "branch_review", source_ref),
            "mock_mode": _truthy(env.get("device_test_mock") or env.get("mock_device_test")),
            "mock_fail_rounds": _int_value(env.get("device_test_mock_fail_rounds") or env.get("mock_fail_rounds")),
            "max_replans": goal.get("budget", {}).get("max_replans", 3),
        }

    if capability_key == "script_gen":
        repo = _repo_for_step(sources, step)
        env = _first_env(sources)
        return {
            "repo_path": repo.get("local_path", ""),
            "repo_name": repo.get("repo_id", ""),
            "package": env.get("package", "") or env.get("app_package", ""),
            "apk_path": env.get("apk_path", ""),
            "device_caps": env.get("device_caps", {}) or env.get("device_profile", {}),
            "regression_cases": [a.get("desc", "") for a in goal.get("acceptance", [])
                                 if a.get("id") in (step or {}).get("serves_acceptance", [])],
            "scenario": goal.get("goal_statement", "") or goal.get("title", ""),
            "requirement_context": _first_doc(sources) or goal.get("goal_statement", "") or goal.get("title", ""),
        }

    # 默认兜底：透传目标陈述当 doc
    return {"doc_content": goal.get("goal_statement", "") or goal.get("title", "")}
