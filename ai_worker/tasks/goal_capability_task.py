"""Goal 能力执行器（task_type=40）

Goal 的所有 step / 探查任务走这里，按 capability_key 分发到具体执行逻辑，
返回符合 NODE_CONTRACTS 契约的结构化产出（供 scheduler 契约校验 + 证据绑定）。

执行层智能体的"手"。每个 capability 一个执行函数，产出对应 output schema。

入参来源（统一约定）：
- payload["inputs"] = step_input_resolver.resolve(...) 的结构化入参（首选）
- 缺失时从 goal.sources 兜底（兼容旧路径 / 探查无 resolver 的场景）

复用真实 handler：branch_review 直接委派 BranchReviewTask（复用 git diff），
不再用简化 LLM 空壳。code_scan 走确定性扫描 + LLM 画像。
"""
import asyncio
import os
import time

from ai_worker.base_task import BaseTaskHandler
from common.db import get_collection
from llm.structured import generate_structured

HANDLER_META = {
    "key": "goal_capability",
    "label": "Goal 能力执行器",
    "description": "Goal step 统一执行入口，按 capability_key 分发",
    "task_type": 40,
}

# 扫描仓库时跳过的目录
_SCAN_IGNORE = {".git", "node_modules", "build", "dist", "out", ".idea", ".gradle",
                "__pycache__", ".venv", "venv", "target", ".cache", "Pods"}


class GoalCapabilityTask(BaseTaskHandler):
    """task_type=40 — Goal step / 探查任务执行。run() 返回结构化 output。"""

    async def run(self) -> dict:
        capability = self.payload.get("capability_key", "")
        goal_id = self.payload.get("goal_id", "")

        self.log(f"🤖 执行能力 [{capability}]")

        goal = get_collection("ai_goals").find_one({"goal_id": goal_id}, {"_id": 0}) or {}

        executor = CAPABILITY_EXECUTORS.get(capability)
        if not executor:
            return {"error": f"未知能力: {capability}", "summary": f"能力 {capability} 无执行器"}

        # 同步执行器放线程；异步执行器（委派真实 async handler）直接 await
        if asyncio.iscoroutinefunction(executor):
            output = await executor(self, goal, self.payload)
        else:
            output = await asyncio.to_thread(executor, self, goal, self.payload)

        self.log(f"✅ [{capability}] 执行完成: {str(output.get('summary', ''))[:60]}")
        return output


# ==================== 入参兜底 ====================

def _inputs(payload: dict) -> dict:
    """取 resolver 组装的入参（payload["inputs"]）。"""
    return payload.get("inputs") or {}


def _doc_from_goal(goal: dict) -> str:
    for src in goal.get("sources", []):
        if src.get("type") == "doc":
            return src.get("content", "") or src.get("doc_content", "")
    return ""


def _repo_from_goal(goal: dict) -> dict:
    for src in goal.get("sources", []):
        if src.get("type") == "repo":
            return src
    return {}


def _resolve_repo_path(repo_path: str) -> str:
    """路径自适应：优先用传入路径；不存在则按 config.git.repo_base_path / 本机 work_code 取 basename 拼接。"""
    if repo_path and os.path.isdir(repo_path):
        return repo_path
    try:
        import platform
        import yaml
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config.yaml")
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
        git_base = config.get("git", {}).get("repo_base_path", "/data/repos")
        if platform.system() != "Linux":
            git_base = os.path.expanduser("~/Documents/work_code")
        candidate = os.path.join(git_base, os.path.basename(repo_path or ""))
        return candidate
    except Exception:
        return repo_path


def _collect_repo_tree(repo_path: str, limit: int = 200) -> list:
    """浅采集仓库文件路径（相对路径，跳过常见忽略目录），供 LLM 画像。"""
    files = []
    if not repo_path or not os.path.isdir(repo_path):
        return files
    for root, dirs, filenames in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in _SCAN_IGNORE and not d.startswith(".")]
        for fn in filenames:
            rel = os.path.relpath(os.path.join(root, fn), repo_path)
            files.append(rel)
            if len(files) >= limit:
                return files
    return files


# ==================== 各能力执行函数 ====================

def _exec_requirement_analysis(task, goal, payload) -> dict:
    """需求分析：拆验收点 + 测试用例。产出符合 requirement_analysis 契约。"""
    doc = _inputs(payload).get("doc_content", "")
    doc = doc or _doc_from_goal(goal) or goal.get("goal_statement", "") or goal.get("title", "")

    schema = {"required": ["acceptance_points", "test_cases"],
              "types": {"acceptance_points": "list", "test_cases": "list"}}
    result = generate_structured(
        system_prompt="你是需求分析专家。拆解需求为验收点和测试用例。",
        user_prompt=f"需求：{doc[:3000]}\n\n输出JSON：{{\"acceptance_points\":[\"验收点\"],\"test_cases\":[\"用例\"],\"summary\":\"一句话总结\",\"confidence\":0.0到1.0}}",
        schema=schema, model_id=payload.get("model_id", "gemini_flash"),
        default={"acceptance_points": [], "test_cases": [], "summary": "分析降级"},
    )
    d = result.data
    return {
        "acceptance_points": d.get("acceptance_points", []),
        "test_cases": d.get("test_cases", []),
        "summary": d.get("summary", f"拆出{len(d.get('acceptance_points', []))}个验收点"),
        "confidence": d.get("confidence", 0.5),
        "verdict": "pass" if d.get("acceptance_points") else "partial",
    }


def _exec_code_scan(task, goal, payload) -> dict:
    """代码扫描：扫单仓库产项目画像（探查能力，produces_evidence=[]）。

    确定性采集项目类型 + 文件树 → LLM 综合出 project_type/可测面/建议验收/风险/摘要。
    产出符合 code_scan 契约：success = 有 summary 且 (有 project_type 或 testable_surfaces)。
    """
    inp = _inputs(payload)
    repo = _repo_from_goal(goal)
    repo_name = inp.get("repo_name") or repo.get("repo_id", "")
    repo_path = _resolve_repo_path(inp.get("repo_path") or repo.get("local_path", ""))

    if not repo_path or not os.path.isdir(repo_path):
        return {
            "summary": "", "project_type": "", "testable_surfaces": [],
            "error": "repo_unavailable", "verdict": "blocked",
            "_note": f"仓库路径不可用: {repo_path}",
        }

    from engine.source_profiler import inspect_project
    project_types = inspect_project(repo_path).get("types", [])
    file_tree = _collect_repo_tree(repo_path)
    task.log(f"📂 扫描仓库 {repo_name}：{len(file_tree)} 文件，类型 {project_types}")

    schema = {
        "required": ["summary"],
        "types": {"testable_surfaces": "list", "suggested_acceptance": "list",
                  "main_modules": "list", "entry_points": "list", "inferred_risks": "list"},
    }
    result = generate_structured(
        system_prompt="你是代码架构分析专家。基于文件结构推断项目画像：类型、核心模块、入口、可测面、风险、建议验收点。",
        user_prompt=(
            f"仓库：{repo_name}\n确定性识别类型：{project_types}\n\n"
            f"文件清单（部分）：\n{chr(10).join(file_tree[:200])}\n\n"
            "输出JSON：{\"project_type\":\"android|ios|frontend|backend|...\","
            "\"main_modules\":[\"模块\"],\"entry_points\":[\"入口文件\"],"
            "\"testable_surfaces\":[\"可测面/可验证点\"],\"inferred_risks\":[\"风险\"],"
            "\"suggested_acceptance\":[\"建议验收点\"],\"summary\":\"项目画像一句话\",\"confidence\":0.0到1.0}"
        ),
        schema=schema, model_id=payload.get("model_id", "gemini_flash"),
        default={"summary": "", "project_type": "", "testable_surfaces": [], "suggested_acceptance": []},
    )
    d = result.data
    project_type = d.get("project_type") or (project_types[0] if project_types else "")
    summary = d.get("summary") or (f"{repo_name} 项目画像：{project_type}" if project_type else "")
    return {
        "project_type": project_type,
        "main_modules": d.get("main_modules", []),
        "entry_points": d.get("entry_points", []),
        "testable_surfaces": d.get("testable_surfaces", []),
        "inferred_risks": d.get("inferred_risks", []),
        "suggested_acceptance": d.get("suggested_acceptance", []),
        "summary": summary,
        "confidence": d.get("confidence", 0.5),
    }


async def _exec_branch_review(task, goal, payload) -> dict:
    """代码变更分析：委派真实 BranchReviewTask（复用 git diff），不再用 LLM 空壳。

    入参由 step_input_resolver 组装（repo_name/repo_path/base_branch/target_branch/inputs.mode）。
    """
    from ai_worker.tasks.branch_review_task import BranchReviewTask
    from engine import step_input_resolver

    inp = _inputs(payload) or step_input_resolver.resolve("branch_review", goal)
    sub_payload = {
        "inputs": inp.get("inputs", {"mode": "最近更新"}),
        "repo_name": inp.get("repo_name", ""),
        "repo_path": inp.get("repo_path", ""),
        "base_branch": inp.get("base_branch", "master"),
        "target_branch": inp.get("target_branch", inp.get("base_branch", "master")),
        "before_ref": inp.get("before_ref"),
        "after_ref": inp.get("after_ref"),
        "agent_id": payload.get("agent_id", ""),
        "system_prompt": payload.get("system_prompt", ""),
        "model_id": payload.get("model_id", "gemini_pro"),
        "req_id": payload.get("req_id", ""),
        "requirement_context": payload.get("requirement_context", ""),
    }
    sub = BranchReviewTask(task_id=task.task_id, payload=sub_payload)
    return await sub.run()


def _exec_alignment_analysis(task, goal, payload) -> dict:
    """需求-代码对齐。产出符合 alignment_analysis 契约。"""
    inp = _inputs(payload)
    acc = "\n".join(a.get("desc", "") for a in (inp.get("acceptance") or goal.get("acceptance", [])))
    change_summary = inp.get("change_summary", "")
    schema = {"required": ["alignment_score"], "types": {"alignment_score": "int"}}
    result = generate_structured(
        system_prompt="你是质量分析专家。对比需求和代码变更，发现夹带改动/漏实现。",
        user_prompt=f"验收点：{acc}\n变更摘要：{change_summary}\n目标：{goal.get('goal_statement', '')}\n\n输出JSON：{{\"aligned\":true,\"extra_changes\":[\"夹带\"],\"missing_impl\":[\"漏实现\"],\"alignment_score\":0到100,\"summary\":\"一句话\",\"confidence\":0.0}}",
        schema=schema, model_id=payload.get("model_id", "gemini_pro"),
        default={"alignment_score": 50, "summary": "对齐分析降级"},
    )
    d = result.data
    return {
        "aligned": d.get("aligned", True),
        "extra_changes": d.get("extra_changes", []),
        "missing_impl": d.get("missing_impl", []),
        "alignment_score": d.get("alignment_score", 50),
        "summary": d.get("summary", f"对齐度{d.get('alignment_score', 50)}%"),
        "confidence": d.get("confidence", 0.5),
        "verdict": "pass",
    }


def _exec_git_prepare(task, goal, payload) -> dict:
    """资源准备：按 git_url+branch 真实 clone/fetch/checkout，产出本地仓库路径。

    确定性，零 LLM。clone 到独立工作区 ~/.../_goal_repos/<goal_id>/<repo_id>，不碰被测代码。
    支持 file:// 本地仓库（演示/内网镜像）与远程 http(s)/git。
    """
    import subprocess
    inp = _inputs(payload)
    git_url = inp.get("git_url", "")
    branch = inp.get("branch", "") or "master"
    repo_id = inp.get("repo_id", "") or "repo"
    goal_id = goal.get("goal_id", "g")

    if not git_url:
        return {"local_path": "", "repo_id": repo_id, "branch": branch,
                "summary": "缺少 git_url，无法准备资源", "error": "no_git_url", "verdict": "blocked"}

    base = os.path.expanduser("~/Documents/work_code/_goal_repos")
    if platform_is_linux():
        base = "/data/repos/_goal_repos"
    dest = os.path.join(base, goal_id, repo_id)
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    def _run(args, cwd=None):
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=240)

    log = []
    if os.path.isdir(os.path.join(dest, ".git")):
        task.log(f"♻️ 已存在工作区，fetch+checkout {branch}")
        _run(["git", "fetch", "--all", "--prune"], cwd=dest)
        co = _run(["git", "checkout", branch], cwd=dest)
        log.append(co.stderr.strip()[:120])
        _run(["git", "pull", "--ff-only"], cwd=dest)  # 离线/无 remote 时失败无妨
    else:
        task.log(f"📥 clone {git_url} @ {branch}")
        r = _run(["git", "clone", "--branch", branch, git_url, dest])
        if r.returncode != 0:
            # 分支不存在或不支持 --branch（如裸 commit）→ 退化为默认 clone 再 checkout
            log.append(r.stderr.strip()[:200])
            r2 = _run(["git", "clone", git_url, dest])
            if r2.returncode == 0:
                _run(["git", "checkout", branch], cwd=dest)
            else:
                log.append(r2.stderr.strip()[:200])

    ok = os.path.isdir(os.path.join(dest, ".git"))
    head = ""
    if ok:
        h = _run(["git", "rev-parse", "--short", "HEAD"], cwd=dest)
        head = h.stdout.strip()
    return {
        "local_path": dest if ok else "",
        "repo_id": repo_id,
        "branch": branch,
        "commit": head,
        "summary": (f"已准备 {repo_id}@{branch} (HEAD {head}) → {dest}" if ok
                    else f"资源准备失败 {repo_id}@{branch}: {'; '.join(log)[:160]}"),
        "verdict": "pass" if ok else "fail",
        "no_change": False,
    }


def platform_is_linux() -> bool:
    import platform
    return platform.system() == "Linux"


CAPABILITY_EXECUTORS = {
    "git_prepare": _exec_git_prepare,
    "requirement_analysis": _exec_requirement_analysis,
    "code_scan": _exec_code_scan,
    "branch_review": _exec_branch_review,
    "alignment_analysis": _exec_alignment_analysis,
    # script_gen / api_test / device_execution 需设备/环境，最小跑通先不实现，走降级
}
