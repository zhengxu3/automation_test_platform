"""SourceProfiler — 可行性画像（Feasibility-First 最高原则）

Goal 第一步永远是 s0 可行性画像：我有什么源、能推断什么、能执行什么、能证明到什么程度。
全是确定性逻辑（扫文件+查表），零 token。

不可违背前提（多重性不能被拍扁，违反即多 repo 塌成一个 step）：
  1. available_capabilities 只表达"能力是否存在"，不表达 source 数量。
  2. 多 repo 身份必须从 SourceProfiler 保留到 Probe / Plan / Task / Artifact / Evidence。
  3. 同一个 goal 内 repo 可去重，不同 goal 不跨界去重。
  4. 任意按 repo 执行的实例 key 必须包含 source_id / repo_id。
  5. Planner 看到的不是"有 repo"，而是"有 N 个 repo 画像"。
"""
import os

from engine.contracts import allowed_evidence_types, EVIDENCE_REGISTRY, missing_for_evidence


# ==================== ProjectInspector（确定性扫描）====================

PROJECT_SIGNATURES = {
    "android":  ["build.gradle", "AndroidManifest.xml", "settings.gradle"],
    "ios":      ["Podfile", ".xcodeproj", "Info.plist"],
    "frontend": ["package.json", "vite.config.ts", "vue.config.js", "angular.json"],
    "backend":  ["requirements.txt", "manage.py", "app.py", "pom.xml", "go.mod"],
}


def inspect_project(repo_path: str) -> dict:
    """扫描仓库特征文件，判断项目类型"""
    if not repo_path or not os.path.isdir(repo_path):
        return {"types": [], "exists": False}

    found_types = []
    # 浅层扫描（根目录 + 一层子目录）
    candidates = set()
    try:
        for entry in os.listdir(repo_path):
            candidates.add(entry)
        # 常见嵌套：app/, src/
        for sub in ("app", "src"):
            subpath = os.path.join(repo_path, sub)
            if os.path.isdir(subpath):
                for entry in os.listdir(subpath):
                    candidates.add(entry)
    except Exception:
        pass

    for ptype, sigs in PROJECT_SIGNATURES.items():
        for sig in sigs:
            if any(sig in c or c.endswith(sig) for c in candidates):
                found_types.append(ptype)
                break

    return {"types": found_types, "exists": True}


# ==================== SourceProfiler ====================

def _repo_capabilities(project_types: list, role: str) -> set:
    """单个 repo 的能力标记集合（per-repo，不并入全局再回推）。"""
    caps = {"repo"}
    role_l = (role or "").lower()
    for t in project_types:
        if t in ("android", "ios"):
            caps.add("repo:client")
        if t == "frontend":
            caps.add("repo:web")
        if t == "backend":
            caps.add("repo:backend")
    if "frontend" in role_l or "web" in role_l:
        caps.add("repo:web")
    if "android" in role_l or "ios" in role_l or "mobile" in role_l:
        caps.add("repo:client")
    elif "client" in role_l and "web" not in role_l and "frontend" not in role_l:
        caps.add("repo:client")
    if "backend" in role_l or "api" in role_l:
        caps.add("repo:backend")
    if "fullstack" in role_l or "mixed" in role_l or "monorepo" in role_l:
        caps.add("repo:backend")
        caps.add("repo:web")
    return caps


def _env_capabilities(src: dict) -> set:
    caps = set()
    if src.get("base_url"):
        caps.add("env:base_url")
        caps.add("env:api")
        caps.add("env:web_url")
    if src.get("web_url"):
        caps.add("env:web_url")
    if src.get("apk_source") or src.get("apk_path"):
        caps.add("env:apk")
        caps.add("env:client_pkg")
    if src.get("test_accounts"):
        caps.add("env:test_account")
    if src.get("test_data"):
        caps.add("env:test_data")
    if src.get("device_profile") or src.get("device_id"):
        caps.add("device")
    return caps


def profile_sources(sources: list) -> dict:
    """从 Goal 的 sources 列表生成能力画像。

    sources 例:
      [{"type": "doc", ...},
       {"type": "repo", "repo_id": "x", "local_path": "/...", "role": "android_client"},
       {"type": "environment", "base_url": "...", "apk_source": "...", "test_accounts": [...]}]

    输出两层（前提 1/2/5）：
      - available_capabilities: 扁平能力集合，仅表达"能力是否存在"（兼容旧逻辑/证据判定）
      - repos / docs / environments: 带身份的逐源画像，保留多重性供 Probe/Plan/Task 扇出
    """
    available = set()        # 可用能力标记集合（扁平，不表达数量）
    project_types = []       # 全局去重的项目类型（兼容旧字段）
    available_sources = []
    repos, docs, environments = [], [], []
    has_doc = False
    has_repo = False
    has_env = False
    repo_i = doc_i = env_i = 0

    for src in sources:
        stype = src.get("type")

        if stype == "doc":
            has_doc = True
            available.add("doc")
            available_sources.append("doc")
            docs.append({
                "source_id": src.get("source_id") or f"src_doc_{doc_i}",
                "kind": "doc",
                "capabilities": ["doc"],
                "content_len": len(src.get("content") or src.get("doc_content") or ""),
            })
            doc_i += 1

        elif stype == "user_desc":
            available.add("user_desc")
            available_sources.append("user_desc")
            docs.append({
                "source_id": src.get("source_id") or f"src_doc_{doc_i}",
                "kind": "user_desc",
                "capabilities": ["user_desc"],
                "content_len": len(src.get("content") or ""),
            })
            doc_i += 1

        elif stype == "testcase":
            has_doc = True  # testcase 也算文档输入（有文本内容可分析）
            available.add("doc")
            available.add("testcase")
            available_sources.append("testcase")
            docs.append({
                "source_id": src.get("source_id") or f"src_doc_{doc_i}",
                "kind": "testcase",
                "capabilities": ["doc", "testcase"],
                "content_len": len(src.get("content") or ""),
            })
            doc_i += 1

        elif stype == "repo":
            has_repo = True
            available_sources.append("repo")
            local_path = src.get("local_path", "")
            inspected = inspect_project(local_path)
            rtypes = list(inspected.get("types", []))
            role = src.get("role", "")
            caps = _repo_capabilities(rtypes, role)
            available |= caps               # 并入全局扁平集合（前提 1：只表达存在）
            for t in rtypes:                 # 全局去重项目类型（兼容旧字段）
                if t not in project_types:
                    project_types.append(t)
            repos.append({                   # 前提 2/5：保留 per-repo 身份
                "source_id": src.get("source_id") or f"src_repo_{repo_i}",
                "repo_id": src.get("repo_id", ""),
                "repo_name": src.get("repo_name", ""),
                "local_path": local_path,
                "branch": src.get("branch", ""),
                "base_branch": src.get("base_branch", ""),
                "target_branch": src.get("target_branch", ""),
                "commit": src.get("commit", ""),
                "role": role,
                "project_types": rtypes,
                "capabilities": sorted(caps),
                "exists": inspected.get("exists", False),
            })
            repo_i += 1

        elif stype == "environment":
            has_env = True
            available_sources.append("environment")
            caps = _env_capabilities(src)
            available |= caps
            environments.append({
                "source_id": src.get("source_id") or f"src_env_{env_i}",
                "base_url": src.get("base_url", ""),
                "web_url": src.get("web_url", ""),
                "capabilities": sorted(caps),
            })
            env_i += 1

    # 输入模式判定
    if has_doc and has_repo:
        input_mode = "full"
    elif has_repo and not has_doc:
        input_mode = "repo_only"
    elif has_doc and not has_repo:
        input_mode = "doc_only"
    else:
        input_mode = "mixed" if available else "empty"

    # 可产出的证据类型
    allowed = allowed_evidence_types(available)
    blocked = [et for et in EVIDENCE_REGISTRY if et not in allowed]

    # 缺什么能升级（针对被阻塞的高价值证据）
    upgrade_hints = {}
    for et in blocked:
        gap = missing_for_evidence(et, available)
        if gap:
            upgrade_hints[et] = gap

    return {
        "input_mode": input_mode,
        "project_types": project_types,
        "available_sources": available_sources,
        "available_capabilities": sorted(available),
        # 带身份的逐源画像（前提 2/5：多重性不被拍扁）
        "repos": repos,
        "docs": docs,
        "environments": environments,
        "repo_count": len(repos),
        "allowed_evidence_types": allowed,
        "blocked_evidence_types": blocked,
        "upgrade_hints": upgrade_hints,        # {证据类型: [缺的东西]}
        "executable": any(et in allowed for et in ("api_test", "web_test", "device_test", "e2e_test")),
        "max_evidence_strength": max((EVIDENCE_REGISTRY[et]["strength"] for et in allowed), default=0),
    }


# ==================== EvidencePolicy ====================

def evidence_policy(profile: dict) -> dict:
    """根据可行性画像决定验收证据标准。

    核心：输入模式决定 acceptance 能要求到什么证据等级。
    doc_only 不能要求 device_test，只能 doc_review/testcase_generated。
    """
    allowed = set(profile.get("allowed_evidence_types", []))
    input_mode = profile.get("input_mode", "empty")

    # 每种输入模式的"目标证据等级"（能达到的最高可信证据）
    if input_mode == "doc_only":
        target = "testcase_generated"
        note = "仅文档：可拆解需求、生成可评审用例，无法真机验证"
    elif input_mode == "repo_only":
        target = "static_analysis"
        note = "仅代码：可做静态影响分析，执行需补充环境/设备"
    elif input_mode == "full":
        target = (
            "device_test" if "device_test" in allowed
            else ("web_test" if "web_test" in allowed
                  else ("api_test" if "api_test" in allowed else "static_analysis"))
        )
        note = "文档+代码齐全：可做完整对齐分析，执行能力取决于环境"
    else:
        target = "doc_review" if "doc_review" in allowed else None
        note = "输入不足"

    return {
        "target_evidence": target,
        "allowed_evidence_types": sorted(allowed),
        "min_acceptance_evidence": "doc_review" if "doc_review" in allowed else (sorted(allowed)[0] if allowed else None),
        "note": note,
        "can_execute": profile.get("executable", False),
    }
