"""Case 匹配器：代码分析产物 × 用户上传 case → 提醒结构"""
import os

_NON_SOURCE_EXT = {
    ".xml", ".json", ".yaml", ".yml", ".properties", ".gradle", ".toml",
    ".md", ".txt", ".csv", ".png", ".jpg", ".svg", ".gif",
    ".lock", ".sum", ".mod", ".cfg", ".ini", ".conf",
}
_NON_SOURCE_NAMES = {
    "build.gradle", "settings.gradle", "gradle.properties",
    "proguard-rules.pro", "androidmanifest.xml",
    "package.json", "package-lock.json", "tsconfig.json",
    "requirements.txt", "poetry.lock", "pipfile.lock",
    "gemfile.lock", "podfile.lock", "cartfile.resolved",
}


def _is_source_file(path: str) -> bool:
    """只保留源码文件，过滤配置/资源/构建文件。"""
    p = path.replace("\\", "/").lower()
    basename = p.rsplit("/", 1)[-1]
    if basename in _NON_SOURCE_NAMES:
        return False
    ext = os.path.splitext(basename)[1]
    if ext in _NON_SOURCE_EXT:
        return False
    # res/values/ 下的资源文件（strings.xml 等）
    if "/res/" in p and ext == ".xml":
        return False
    return True


def _extract_names_from_path(path: str) -> set:
    """从文件路径提取可能的模块/类名（用于模糊匹配）"""
    names = set()
    basename = os.path.splitext(os.path.basename(path))[0]  # TeamController
    names.add(basename.lower())
    # 去掉常见后缀
    for suffix in ("Controller", "Service", "Manager", "Helper", "Repository", "Model", "Task"):
        if basename.endswith(suffix):
            names.add(basename[:-len(suffix)].lower())
    # 目录里的模块名也加
    parts = path.replace("\\", "/").split("/")
    for p in parts:
        if p and not p.startswith(".") and p not in ("src", "main", "java", "kotlin", "php", "app", "Http", "Services", "Controllers", "Models"):
            names.add(p.lower())
    return names


def match_cases(affected_modules, interface_doc, cases, change_summary="", risk_points=None, blast_radius=None):
    """
    确定性匹配 + 文件路径模糊匹配。

    Args:
        affected_modules: ["app/Http/Controllers/TeamController.php", ...] 来自 branch_review
        interface_doc: [{"endpoint": "/api/login", ...}] 或 {"affected_endpoints": [...]} 来自 branch_review
        cases: [{case_id, title, module, api_info, ...}] 来自 ai_goal_cases
        change_summary: branch_review 的变更摘要（描述改了什么功能）
        risk_points: branch_review 的风险点列表
        blast_radius: AST 方法级变更列表 [{"name","class_name","file",...}]

    Returns:
        {hit_cases, ripple_cases, uncovered_changes, summary}
    """
    if not cases:
        return {"hit_cases": [], "ripple_cases": [], "uncovered_changes": [], "summary": {}}

    # 过滤非源码文件（配置/资源/构建文件不参与 Case 匹配）
    affected_modules = [m for m in (affected_modules or []) if _is_source_file(m)]

    # 从 affected_modules 提取所有可能的名字
    all_names = set()
    for m in (affected_modules or []):
        all_names.update(_extract_names_from_path(m))
        all_names.add(m.lower())

    # AST blast_radius 的 class_name 是更精确的信号
    blast_class_names = set()
    for br in (blast_radius or []):
        cn = (br.get("class_name") or "").lower()
        if cn:
            blast_class_names.add(cn)
            all_names.add(cn)
            # 去后缀也加
            for suffix in ("presenter", "viewmodel", "adapter", "helper", "util", "controller", "service", "manager"):
                if cn.endswith(suffix):
                    all_names.add(cn[:-len(suffix)])

    # interface_doc 兼容两种格式
    endpoints = set()
    if isinstance(interface_doc, list):
        for doc in interface_doc:
            ep = doc.get("endpoint") or doc.get("path") or ""
            if ep:
                endpoints.add(ep.lower().rstrip("/"))
    elif isinstance(interface_doc, dict):
        for ep_obj in (interface_doc.get("affected_endpoints") or []):
            ep = ep_obj.get("endpoint") or ep_obj.get("path") or (ep_obj if isinstance(ep_obj, str) else "")
            if ep:
                endpoints.add(str(ep).lower().rstrip("/"))

    hit_cases = []
    matched_modules = set()

    for case in cases:
        case_module = (case.get("module") or "").lower()
        case_endpoint = ""
        api_info = case.get("api_info") or {}
        if api_info:
            case_endpoint = (api_info.get("endpoint") or "").lower().rstrip("/")

        hit_reason = None
        # 模块名匹配（模糊：case module 出现在任何 affected 文件名/路径中）
        if case_module and case_module in all_names:
            hit_reason = f"模块 {case.get('module')} 被改动"
            matched_modules.add(case_module)
        # endpoint 匹配
        elif case_endpoint and case_endpoint in endpoints:
            hit_reason = f"接口 {api_info.get('endpoint')} 被改动"
            matched_modules.add(case_module or case_endpoint)
            # endpoint 命中时，把 endpoint 路径段关键词加入 matched_modules
            # 这样 affected_module 中含相同词根的模块也被视为已覆盖
            for seg in case_endpoint.strip("/").split("/"):
                if seg and seg not in ("api", "v1", "v2", "v3"):
                    matched_modules.add(seg)
        # 反向：case module 出现在任何 affected_module 字符串或 blast class_name 里
        elif case_module:
            for m in (affected_modules or []):
                if case_module in m.lower():
                    hit_reason = f"文件 {os.path.basename(m)} 关联模块 {case.get('module')}"
                    matched_modules.add(case_module)
                    break
            if not hit_reason:
                for cn in blast_class_names:
                    if case_module in cn:
                        hit_reason = f"类 {cn} 关联模块 {case.get('module')}"
                        matched_modules.add(case_module)
                        break

        if hit_reason:
            hit_cases.append({
                "case_id": case.get("case_id"),
                "title": case.get("title"),
                "hit_reason": hit_reason,
                "confidence": "high",
                "action": "rerun",
            })

    # 未覆盖：用 blast_radius 方法级粒度（优先）或文件级（兜底）
    uncovered = []
    import re
    raw_summary = change_summary or ""
    for prefix in ("好的，", "好的,", "作为代码审查专家，", "作为代码审查专家,", "我对本次变更分析如下：", "我对本次变更分析如下:"):
        raw_summary = raw_summary.replace(prefix, "")
    summary_match = re.search(r'变更摘要[^\n]*\n(.*?)(?:\n#|\n\*|\n---|\Z)', raw_summary, re.S)
    summary_short = summary_match.group(1).strip()[:200] if summary_match else raw_summary.strip()[:200]

    # 优先用 blast_radius 方法级数据生成 uncovered（更精确）
    seen_modules = set()
    if blast_radius:
        for br in (blast_radius or []):
            cn = br.get("class_name") or ""
            method = br.get("name") or ""
            if not cn:
                continue
            # 检查该类是否已被 Case 命中覆盖
            if cn.lower() in matched_modules:
                continue
            module_key = cn  # 去重键
            if module_key in seen_modules:
                continue
            seen_modules.add(module_key)
            # 收集该类所有被改的方法
            changed_methods = [b.get("name") for b in blast_radius if (b.get("class_name") or "") == cn]
            # 从 blast_radius 里找该类方法的 description 作为具体说明
            desc_items = [b.get("description") for b in blast_radius if (b.get("class_name") or "") == cn and b.get("description")]
            if desc_items:
                # description 可能是变更分析全文，只取第一句作为建议
                raw = desc_items[0].split("。")[0].split("\n")[0][:80]
                suggestion = f"建议验证: {raw}"
            else:
                suggestion = f"建议验证 {cn} 的 {', '.join(changed_methods[:3])} 功能"
            uncovered.append({
                "module": cn,
                "change_type": "method_changed",
                "risk": "medium",
                "changed_methods": changed_methods[:10],
                "description": f"修改了 {cn} 的 {len(changed_methods)} 个方法: {', '.join(changed_methods[:3])}",
                "suggestion": suggestion,
            })
    else:
        # 兜底：按文件级（无 AST 数据时）
        risk_list = risk_points or []
        for m in (affected_modules or []):
            m_names = _extract_names_from_path(m)
            if not m_names.intersection(matched_modules):
                basename = os.path.basename(m)
                if basename in seen_modules:
                    continue
                seen_modules.add(basename)
                desc = ""
                for rp in risk_list:
                    rp_str = rp if isinstance(rp, str) else rp.get("desc", rp.get("description", ""))
                    if any(n in rp_str.lower() for n in m_names):
                        desc = rp_str[:60]
                        break
                uncovered.append({
                    "module": basename,
                    "change_type": "modified",
                    "risk": "medium",
                    "description": desc or "变更未被现有 Case 覆盖",
                    "suggestion": f"建议针对 {basename.replace('.php','').replace('.kt','').replace('.java','')} 补充 Case",
                })

    # LLM 语义关联：给 uncovered 变更找到所属玩法 + 最相关的 case
    if uncovered and (summary_short or cases):
        try:
            from llm.structured import generate_structured
            case_modules = sorted(set(c.get("module", "") for c in cases if c.get("module")))
            case_titles_sample = [f"{c.get('case_id')} {c.get('module')}: {c.get('title')}" for c in cases[:30]]
            # 用类名+方法名而不是文件名
            uncov_desc = [f"{u['module']}({', '.join(u.get('changed_methods', [])[:3])})" if u.get("changed_methods") else u["module"] for u in uncovered]

            r = generate_structured(
                system_prompt="你是 QA 测试专家。根据代码变更的类名和方法名，判断它属于哪个业务玩法，并找出最相关的已有测试用例。",
                user_prompt=(
                    f"代码变更摘要：\n{summary_short}\n\n"
                    f"未覆盖的变更（类名+方法）：{uncov_desc}\n\n"
                    f"已有 case 模块列表：{case_modules}\n\n"
                    f"已有 case 样例：\n" + "\n".join(case_titles_sample) + "\n\n"
                    f"对每个未覆盖文件返回：\n"
                    f'{{"suggestions": [{{"file": "文件名", "belongs_to": "所属玩法/模块", "related_cases": ["最相关的case_id"], "test_advice": "建议验证什么场景"}}]}}'
                ),
                schema={"required": ["suggestions"], "types": {"suggestions": "list"}},
                default={"suggestions": []},
                require_confidence=False,
            )
            for sug in (r.data.get("suggestions") or []):
                for u in uncovered:
                    if u["module"] == sug.get("file") or sug.get("file", "").lower() in u["module"].lower():
                        u["belongs_to"] = sug.get("belongs_to", "")
                        # 把 related_cases 从 ID 扩展为带标题
                        related_ids = sug.get("related_cases", [])
                        related_with_title = []
                        for rid in related_ids[:3]:
                            matched_case = next((c for c in cases if c.get("case_id") == rid), None)
                            if matched_case:
                                related_with_title.append(f"{rid}({matched_case.get('title','')[:25]})")
                            else:
                                related_with_title.append(rid)
                        u["related_cases"] = related_with_title
                        u["suggestion"] = sug.get("test_advice", u["suggestion"])
                        if u["belongs_to"]:
                            u["description"] = f"属于【{u['belongs_to']}】玩法。{u.get('description', '')}"
                        break
        except Exception:
            pass  # LLM 失败不影响主流程

    summary = {
        "should_rerun": [c["case_id"] for c in hit_cases],
        "should_watch": [],
        "need_new_case": [u["module"] for u in uncovered],
    }

    return {
        "hit_cases": hit_cases,
        "ripple_cases": [],
        "uncovered_changes": uncovered,
        "summary": summary,
    }
