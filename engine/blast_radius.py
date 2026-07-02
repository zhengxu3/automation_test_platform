"""BlastRadius — 代码改动 → 受影响 side 分类（确定性，零 LLM）。

多轮持续验证用：识别本轮代码改动触及哪些 side（backend / web / client），
据此只重置/只重跑【对应 side】的验收点 —— 改后端只亮 api 测试、改前端只亮 web 测试。

side 词表统一为 backend / web / client，与 evidence_type 一一映射：
  api_test→backend，web_test→web，device_test/e2e_test→client。
static_analysis（代码变更分析）是"算爆炸范围"的步骤本身，任意代码改动都重跑。
"""
import os
import subprocess


# 路径片段优先（演示仓库按 backend/ web/ android/ 分目录，命中最准）
_DIR_HINTS = (
    ("client", ("/android/", "/ios/", "/app/src/main/", "androidmanifest")),
    ("web", ("/web/", "/frontend/", "/webapp/", "/src/views/", "/src/pages/", "/src/components/")),
    ("backend", ("/backend/", "/server/", "/api/", "/routes/", "/app/api/")),
)

# 扩展名兜底
_CLIENT_EXT = (".kt", ".java", ".swift", ".m", ".gradle", ".xcodeproj")
_WEB_EXT = (".vue", ".jsx", ".tsx", ".ts", ".js", ".css", ".scss", ".less", ".html")
_BACKEND_EXT = (".py", ".go", ".rb", ".php")

# 特征文件名兜底
_BACKEND_FILES = ("requirements.txt", "manage.py", "app.py", "go.mod", "pom.xml")
_WEB_FILES = ("package.json", "vite.config.ts", "vue.config.js", "angular.json")


def classify_file_side(path: str) -> str:
    """单个文件路径 → side（backend/web/client/""）。确定性启发式。"""
    if not path:
        return ""
    p = path.replace("\\", "/").lower()
    base = p.rsplit("/", 1)[-1]

    # 1. 目录片段优先
    for side, hints in _DIR_HINTS:
        if any(h in p for h in hints):
            return side

    # 2. 特征文件名
    if base in _BACKEND_FILES:
        return "backend"
    if base in _WEB_FILES:
        return "web"
    if "androidmanifest" in base or base == "build.gradle":
        return "client"

    # 3. 扩展名兜底
    if p.endswith(_CLIENT_EXT):
        return "client"
    if p.endswith(_WEB_EXT):
        return "web"
    if p.endswith(_BACKEND_EXT):
        return "backend"
    return ""


def changed_sides_from_files(files) -> set:
    """文件列表 → 触及的 side 集合。"""
    return {s for s in (classify_file_side(f) for f in (files or [])) if s}


def _is_zero_ref(ref: str) -> bool:
    r = (ref or "").strip()
    return bool(r) and set(r) == {"0"}


def git_changed_files_result(local_path: str, base_ref: str = "HEAD~1", head_ref: str = "HEAD") -> dict:
    """取本地仓指定 ref 区间改动文件，并返回是否可信。

    ok=True/files=[] 表示"成功计算，确实没有可分类文件"；
    ok=False 表示"无法计算 diff"，调用方可按保守策略退回全量。
    对 root commit / 单提交仓，不能把首提交所有文件当作本轮改动，否则会误报爆炸范围。
    """
    if not local_path or not os.path.isdir(os.path.join(local_path, ".git")):
        return {"ok": False, "files": [], "reason": "not_git_repo"}

    def _run(args):
        try:
            r = subprocess.run(args, cwd=local_path, capture_output=True, text=True, timeout=30)
            return r.returncode, [ln.strip() for ln in r.stdout.splitlines() if ln.strip()], (r.stderr or "").strip()
        except Exception:
            return 1, [], "subprocess_error"

    if _is_zero_ref(base_ref):
        return {"ok": True, "files": [], "reason": "zero_before_no_baseline"}

    if base_ref == "HEAD~1":
        code, count_lines, _ = _run(["git", "rev-list", "--count", head_ref])
        if code == 0 and count_lines:
            try:
                if int(count_lines[0]) <= 1:
                    return {"ok": True, "files": [], "reason": "single_commit_no_parent"}
            except ValueError:
                pass

    code, files, stderr = _run(["git", "diff", "--name-only", f"{base_ref}..{head_ref}"])
    if code == 0:
        return {"ok": True, "files": files, "reason": ""}
    return {"ok": False, "files": [], "reason": stderr[:200] or "git_diff_failed"}


def git_changed_files(local_path: str, base_ref: str = "HEAD~1", head_ref: str = "HEAD") -> list:
    """兼容旧调用：只返回文件列表。"""
    return git_changed_files_result(local_path, base_ref, head_ref).get("files", [])


# evidence_type → side 桶
_EVIDENCE_SIDE = {
    "api_test": "backend",
    "web_test": "web",
    "device_test": "client",
    "e2e_test": "client",
}


def evidence_side(evidence_type: str) -> str:
    return _EVIDENCE_SIDE.get(evidence_type, "")


def acceptance_to_reset(acceptance: list, touched_sides: set):
    """本轮应重置(重验)的验收点 id 集合。
    - 验证级验收点：所属 side ∈ touched_sides 才重置；
    - static_analysis（代码变更分析）：任意代码改动都重置（它就是算爆炸范围的）；
    - touched_sides 为空 → 返回 None（调用方退回"全部重置"的兼容行为）。
    """
    if not touched_sides:
        return None
    ids = set()
    for a in acceptance:
        et = a.get("evidence_type", "")
        if et == "static_analysis":
            ids.add(a.get("id"))
            continue
        bucket = evidence_side(et)
        if bucket and bucket in touched_sides:
            ids.add(a.get("id"))
    return ids


def ast_changed_endpoints(repo_path: str, changed_files: list,
                          base_ref: str = "HEAD~1", head_ref: str = "HEAD") -> list:
    """用 AST 精确定位受影响接口（方法级 diff）。

    返回 list[dict]，每个 dict 含 method/path/handler/file。
    tree-sitter 不可用或无结果时返回空列表。
    """
    try:
        from engine.ast_blast_radius import analyze_repo_diff
        result = analyze_repo_diff(repo_path, changed_files, base_ref, head_ref)
        return [
            {"method": ep.method, "path": ep.path, "handler": ep.handler, "file": ep.file}
            for ep in result.affected_endpoints
        ]
    except Exception:
        return []
