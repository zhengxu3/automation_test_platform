"""AST 爆炸范围分析 — 复用 engine/parsers 套件 + 方法级 diff → 精确受影响接口。

能力：
1. 用现有 parsers（TreeSitterParser / JvmJarParser / SwiftASTParser）提取方法/函数
2. 给定 diff 行号 → 定位被修改的方法
3. 方法↔endpoint 映射（装饰器/注解/Laravel routes）
4. 组合：diff files → changed methods → affected endpoints（个位数精度）

确定性引擎，零 token。
"""
import asyncio
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional


# ─── Data Models ────────────────────────────────────────────────────────────

@dataclass
class MethodDef:
    name: str
    start_line: int
    end_line: int
    class_name: str = ""
    file_path: str = ""
    route_method: str = ""
    route_path: str = ""
    calls_out: list = field(default_factory=list)


@dataclass
class AffectedEndpoint:
    method: str
    path: str
    handler: str
    file: str
    reason: str = "method_changed"


@dataclass
class ASTDiffResult:
    changed_methods: list = field(default_factory=list)
    affected_endpoints: list = field(default_factory=list)
    ripple_methods: list = field(default_factory=list)  # 上游调用方（波及范围）
    files_analyzed: int = 0
    language: str = ""


# ─── Diff → Changed Lines ───────────────────────────────────────────────────

def _parse_unified_diff_lines(diff_text: str) -> set:
    """从 unified diff 中解析变更的目标行号（新文件侧）。"""
    lines = set()
    for m in re.finditer(r'^@@\s*-\d+(?:,\d+)?\s*\+(\d+)(?:,(\d+))?\s*@@', diff_text, re.M):
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) else 1
        for i in range(start, start + count):
            lines.add(i)
    return lines


def _git_diff_line_numbers(repo_path: str, file_path: str,
                           base_ref: str = "HEAD~1", head_ref: str = "HEAD") -> set:
    """获取文件在 base..head 之间变更的行号集合。"""
    try:
        r = subprocess.run(
            ["git", "diff", f"{base_ref}..{head_ref}", "-U0", "--", file_path],
            cwd=repo_path, capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return set()
        return _parse_unified_diff_lines(r.stdout)
    except Exception:
        return set()


# ─── Language / Parser resolution ───────────────────────────────────────────

_LANG_MAP = {
    ".py": "python", ".php": "php", ".go": "go",
    ".java": "java", ".kt": "kotlin", ".swift": "swift",
}

# 通用框架方法名黑名单 — 几乎所有类都有，反查会大量误中
_GENERIC_METHOD_NAMES = {
    # Android/Java 生命周期
    "onCreate", "onStart", "onResume", "onPause", "onStop", "onDestroy",
    "onCreateView", "onViewCreated", "onDestroyView", "onActivityCreated",
    "initView", "initData", "init", "setup", "bind", "unbind",
    "getLayoutResId", "getLayout", "getContentView",
    # 通用模式
    "getInstance", "newInstance", "create", "build", "apply", "run",
    "toString", "hashCode", "equals", "clone", "close", "dispose",
    # iOS 生命周期
    "viewDidLoad", "viewWillAppear", "viewDidAppear", "viewWillDisappear",
    "layoutSubviews", "awakeFromNib", "dealloc",
    # PHP
    "__construct", "__destruct", "__get", "__set", "__call",
    # Python
    "__init__", "__str__", "__repr__", "setup", "teardown",
}


def _detect_language(path: str) -> Optional[str]:
    ext = os.path.splitext(path)[1].lower()
    return _LANG_MAP.get(ext)


def _detect_repo_name(repo_path: str) -> str:
    """从仓库路径推断 repo_name（用于向量库查询）。"""
    if not repo_path:
        return ""
    basename = os.path.basename(repo_path.rstrip("/"))
    try:
        from common.db import get_collection
        # 先查 ai_git_repos
        repo = get_collection("ai_git_repos").find_one(
            {"local_path": repo_path}, {"repo_name": 1, "repo_id": 1, "git_url": 1})
        if repo:
            # 向量库的 repo_name 可能是 git_url，优先用它
            git_url = repo.get("git_url", "")
            if git_url:
                # 检查向量库里是否用 git_url 作为 repo_name
                if get_collection("code_ast_nodes").find_one({"repo_name": git_url}, {"_id": 1}):
                    return git_url
            return repo.get("repo_name") or repo.get("repo_id") or basename
        # 回退：查向量库里是否有 basename 或包含 basename 的 repo_name
        node = get_collection("code_ast_nodes").find_one(
            {"repo_name": {"$regex": basename}}, {"repo_name": 1, "_id": 0})
        if node:
            return node["repo_name"]
    except Exception:
        pass
    return basename


def _nodes_to_methods(nodes: list, file_path: str = "") -> list:
    """把 parsers 产出的 node dicts 转为 MethodDef 列表。兼容各解析器不同字段名。"""
    methods = []
    for n in nodes:
        if n.get("node_type") not in ("method", "function"):
            continue
        name = n.get("name") or n.get("method_name") or ""
        start = n.get("start_line") or n.get("line_number") or 0
        # end_line: 有的解析器没有，用 start + code 行数估算
        end = n.get("end_line") or 0
        if not end and start and n.get("code_content"):
            end = start + n["code_content"].count("\n")
        methods.append(MethodDef(
            name=name,
            start_line=start,
            end_line=end or start,
            class_name=n.get("class_name", ""),
            file_path=file_path or n.get("file_path", ""),
            calls_out=n.get("calls_out", []),
        ))
    return methods


async def _parse_with_existing_parsers(repo_path: str, lang: str, file_path: str) -> list:
    """用现有 engine/parsers 解析单个文件。"""
    try:
        if lang == "php":
            from engine.parsers.tree_sitter_parser import TreeSitterParser
            parser = TreeSitterParser()
            return await parser.parse(repo_path, "php", target_file=file_path)
        elif lang == "go":
            from engine.parsers.tree_sitter_parser import TreeSitterParser
            parser = TreeSitterParser()
            return await parser.parse(repo_path, "go", target_file=file_path)
        elif lang in ("java", "kotlin"):
            from engine.parsers.jar_parser import JvmJarParser
            parser = JvmJarParser()
            jar_lang = "kotlin" if lang == "kotlin" else "java"
            return await parser.parse(repo_path, jar_lang, target_file=file_path)
        elif lang == "swift":
            from engine.parsers.swift_parser import SwiftASTParser
            parser = SwiftASTParser()
            return await parser.parse(repo_path, "swift", target_file=file_path)
    except Exception:
        pass
    return []


def _run_async_parser(repo_path: str, lang: str, file_path: str) -> list:
    """安全运行 async parser，兼容已有事件循环和无事件循环两种场景。"""
    try:
        loop = asyncio.get_running_loop()
        # 已在事件循环中（如 worker async 上下文），用 nest_asyncio 或新线程
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(asyncio.run, _parse_with_existing_parsers(repo_path, lang, file_path))
            return future.result(timeout=60)
    except RuntimeError:
        # 没有事件循环，直接 asyncio.run
        return asyncio.run(_parse_with_existing_parsers(repo_path, lang, file_path))


# ─── Python/Flask 内置解析（parsers 套件未覆盖 Python）────────────────────

def _extract_python_methods(source: bytes, file_path: str) -> list:
    """Python: tree-sitter 提取方法 + Flask 路由装饰器。"""
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_python as _tspython
        lang = Language(_tspython.language())
        parser = Parser(lang)
    except ImportError:
        return []

    tree = parser.parse(source)
    methods = []
    _walk_python(tree.root_node, source, methods, class_name="", file_path=file_path)
    return methods


def _walk_python(node, source: bytes, methods: list, class_name: str, file_path: str):
    for child in node.children:
        if child.type == "class_definition":
            cname = ""
            for n in child.children:
                if n.type == "identifier":
                    cname = n.text.decode()
                    break
            _walk_python(child, source, methods, class_name=cname, file_path=file_path)
        elif child.type == "function_definition":
            fname = ""
            for n in child.children:
                if n.type == "identifier":
                    fname = n.text.decode()
                    break
            methods.append(MethodDef(
                name=fname, start_line=child.start_point[0] + 1,
                end_line=child.end_point[0] + 1, class_name=class_name,
                file_path=file_path,
            ))
        elif child.type == "decorated_definition":
            route_method, route_path = "", ""
            func_node = None
            for n in child.children:
                if n.type == "decorator":
                    rm, rp = _parse_python_route_decorator(n)
                    if rp:
                        route_method, route_path = rm, rp
                elif n.type == "function_definition":
                    func_node = n
            if func_node:
                fname = ""
                for n in func_node.children:
                    if n.type == "identifier":
                        fname = n.text.decode()
                        break
                methods.append(MethodDef(
                    name=fname, start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1, class_name=class_name,
                    file_path=file_path, route_method=route_method, route_path=route_path,
                ))
        else:
            _walk_python(child, source, methods, class_name=class_name, file_path=file_path)


def _parse_python_route_decorator(decorator_node) -> tuple:
    text = decorator_node.text.decode()
    m = re.search(r'\.\s*(get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)', text, re.I)
    if m:
        return m.group(1).upper(), m.group(2)
    m = re.search(r'\.route\s*\(\s*["\']([^"\']+)["\']([^)]*)\)', text, re.I)
    if m:
        path = m.group(1)
        rest = m.group(2)
        method_m = re.search(r'methods\s*=\s*\[([^\]]+)\]', rest, re.I)
        method = "GET"
        if method_m:
            ms = re.findall(r'["\']([A-Z]+)["\']', method_m.group(1), re.I)
            method = ms[0].upper() if ms else "GET"
        return method, path
    return "", ""


# ─── Route mapping helpers ──────────────────────────────────────────────────

def _parse_laravel_routes(repo_path: str) -> dict:
    """解析 Laravel routes/*.php → {ControllerName@method: (HTTP_METHOD, path)}"""
    route_map = {}
    routes_dir = os.path.join(repo_path, "routes")
    if not os.path.isdir(routes_dir):
        return route_map
    for fn in os.listdir(routes_dir):
        if not fn.endswith(".php"):
            continue
        try:
            with open(os.path.join(routes_dir, fn), "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception:
            continue
        pat = re.compile(
            r"Route::(get|post|put|delete|patch|any)\s*\(\s*['\"]([^'\"]+)['\"]"
            r"\s*,\s*(?:\[([^]]+)\]|['\"]([^'\"]+)['\"])", re.I)
        for m in pat.finditer(content):
            http_method = m.group(1).upper()
            path = m.group(2)
            target = m.group(3) or m.group(4) or ""
            ctrl_m = re.search(r"(\w+Controller)::class\s*,\s*['\"](\w+)['\"]", target, re.I)
            if ctrl_m:
                route_map[f"{ctrl_m.group(1)}@{ctrl_m.group(2)}"] = (http_method, path)
                continue
            ctrl_m = re.match(r"(\w+Controller)@(\w+)", target.strip(), re.I)
            if ctrl_m:
                route_map[f"{ctrl_m.group(1)}@{ctrl_m.group(2)}"] = (http_method, path)
    return route_map


def _parse_jvm_annotations(source: str, start_line_0based: int) -> tuple:
    """从 Java/Kotlin 方法紧上方提取 Spring 路由注解。start_line_0based 是方法的 0-based 行号。"""
    lines = source.split("\n")
    # 从方法行往上连续找注解行（遇到非注解非空白就停）
    annotation_text = ""
    for i in range(start_line_0based - 1, max(-1, start_line_0based - 6), -1):
        if i < 0:
            break
        line = lines[i].strip()
        if line.startswith("@"):
            annotation_text = line + "\n" + annotation_text
        elif not line:
            continue  # 跳过空行
        else:
            break  # 遇到非注解非空行就停

    if not annotation_text:
        return "", ""
    # 优先匹配方法级注解 @GetMapping/@PostMapping 等（不含 @RequestMapping）
    m = re.search(
        r'@(Get|Post|Put|Delete|Patch)Mapping\s*\(\s*(?:value\s*=\s*)?["\'](/[^"\']*)["\']',
        annotation_text, re.I)
    if m:
        kind = m.group(1).lower()
        method_map = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH"}
        return method_map.get(kind, "ANY"), m.group(2)
    # 无路径的 @GetMapping 等
    m = re.search(r'@(Get|Post|Put|Delete|Patch)Mapping\b', annotation_text, re.I)
    if m:
        kind = m.group(1).lower()
        method_map = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH"}
        return method_map.get(kind, "ANY"), ""
    # 最后才看 @RequestMapping
    m = re.search(
        r'@RequestMapping\s*\(\s*(?:value\s*=\s*)?["\'](/[^"\']*)["\']',
        annotation_text, re.I)
    if m:
        return "ANY", m.group(1)
    return "", ""


# ─── Public API ─────────────────────────────────────────────────────────────

def extract_methods(source: bytes, lang: str) -> list:
    """直接从源码提取方法列表（兼容旧接口，用于测试）。"""
    if lang == "python":
        return _extract_python_methods(source, "")
    if lang == "php":
        return _extract_php_methods(source)
    if lang == "swift":
        return _extract_swift_methods(source)
    # Java/Kotlin 用简单 tree-sitter 兜底
    try:
        from tree_sitter import Language, Parser
        if lang == "java":
            import tree_sitter_java as _ts
            ts_lang = Language(_ts.language())
        elif lang == "kotlin":
            import tree_sitter_kotlin as _ts
            ts_lang = Language(_ts.language())
        else:
            return []
        parser = Parser(ts_lang)
        tree = parser.parse(source)
        methods = []
        _walk_jvm_simple(tree.root_node, source, methods, lang)
        return methods
    except ImportError:
        return []


def _extract_php_methods(source: bytes) -> list:
    """PHP tree-sitter 直接解析（兜底，不走 parsers 套件的 async 路径）。"""
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_php as _ts
        ts_lang = Language(_ts.language_php())
        parser = Parser(ts_lang)
    except ImportError:
        return []
    tree = parser.parse(source)
    methods = []
    _walk_php(tree.root_node, methods, class_name="")
    return methods


def _extract_swift_methods(source: bytes, file_path: str = "") -> list:
    """Swift tree-sitter 直接解析。"""
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_swift as _ts
        ts_lang = Language(_ts.language())
        parser = Parser(ts_lang)
    except ImportError:
        return []
    tree = parser.parse(source)
    methods = []
    _walk_swift(tree.root_node, methods, class_name="", file_path=file_path)
    return methods


def _walk_swift(node, methods: list, class_name: str, file_path: str = ""):
    for child in node.children:
        if child.type in ("class_declaration", "protocol_declaration", "struct_declaration"):
            cname = ""
            for n in child.children:
                if n.type == "type_identifier":
                    cname = n.text.decode()
                    break
            _walk_swift(child, methods, class_name=cname, file_path=file_path)
        elif child.type == "function_declaration":
            fname = ""
            for n in child.children:
                if n.type == "simple_identifier":
                    fname = n.text.decode()
                    break
            if fname:
                methods.append(MethodDef(
                    name=fname, start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1, class_name=class_name,
                    file_path=file_path,
                ))
        elif child.type in ("class_body", "protocol_body", "struct_body",
                            "extension_declaration"):
            # extension 可能有类型名
            ext_name = class_name
            if child.type == "extension_declaration":
                for n in child.children:
                    if n.type == "type_identifier":
                        ext_name = n.text.decode()
                        break
            _walk_swift(child, methods, class_name=ext_name, file_path=file_path)
        else:
            _walk_swift(child, methods, class_name=class_name, file_path=file_path)


def _walk_php(node, methods: list, class_name: str):
    for child in node.children:
        if child.type == "class_declaration":
            cname = ""
            for n in child.children:
                if n.type == "name":
                    cname = n.text.decode()
                    break
            for n in child.children:
                if n.type == "declaration_list":
                    _walk_php(n, methods, class_name=cname)
        elif child.type == "method_declaration":
            fname = ""
            for n in child.children:
                if n.type == "name":
                    fname = n.text.decode()
                    break
            methods.append(MethodDef(
                name=fname, start_line=child.start_point[0] + 1,
                end_line=child.end_point[0] + 1, class_name=class_name,
            ))
        elif child.type == "function_definition":
            fname = ""
            for n in child.children:
                if n.type == "name":
                    fname = n.text.decode()
                    break
            methods.append(MethodDef(
                name=fname, start_line=child.start_point[0] + 1,
                end_line=child.end_point[0] + 1, class_name=class_name,
            ))
        else:
            _walk_php(child, methods, class_name=class_name)


def _walk_jvm_simple(node, source: bytes, methods: list, lang: str, class_name: str = ""):
    """简单 JVM AST 遍历（测试/兜底用，生产优先走 JvmJarParser）。"""
    for child in node.children:
        if child.type in ("class_declaration", "class_definition"):
            cname = ""
            for n in child.children:
                if n.type == "identifier":
                    cname = n.text.decode()
                    break
            _walk_jvm_simple(child, source, methods, lang, class_name=cname)
        elif child.type in ("method_declaration", "function_declaration"):
            fname = ""
            route_method, route_path = "", ""
            for n in child.children:
                if n.type == "identifier":
                    fname = n.text.decode()
                elif n.type == "modifiers":
                    # 注解在 modifiers 子节点中
                    mod_text = n.text.decode(errors="replace")
                    route_method, route_path = _parse_jvm_annotation_text(mod_text)
            methods.append(MethodDef(
                name=fname, start_line=child.start_point[0] + 1,
                end_line=child.end_point[0] + 1, class_name=class_name,
                route_method=route_method, route_path=route_path,
            ))
        elif child.type in ("class_body", "class_member_declarations"):
            _walk_jvm_simple(child, source, methods, lang, class_name=class_name)
        else:
            _walk_jvm_simple(child, source, methods, lang, class_name=class_name)


def _parse_jvm_annotation_text(text: str) -> tuple:
    """从 modifiers 文本中提取 Spring 路由注解。"""
    # 优先方法级
    m = re.search(
        r'@(Get|Post|Put|Delete|Patch)Mapping\s*\(\s*(?:value\s*=\s*)?["\'](/?[^"\']*)["\']',
        text, re.I)
    if m:
        kind = m.group(1).lower()
        method_map = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH"}
        return method_map.get(kind, "ANY"), m.group(2)
    m = re.search(r'@(Get|Post|Put|Delete|Patch)Mapping\b', text, re.I)
    if m:
        kind = m.group(1).lower()
        method_map = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH"}
        return method_map.get(kind, "ANY"), ""
    return "", ""


def methods_affected_by_diff(source: bytes, lang: str, changed_lines: set) -> list:
    """源码 + 变更行号 → 被修改的方法列表。"""
    if not changed_lines:
        return []
    all_methods = extract_methods(source, lang)
    return [m for m in all_methods if any(m.start_line <= ln <= m.end_line for ln in changed_lines)]


def analyze_repo_diff(repo_path: str, changed_files: list,
                      base_ref: str = "HEAD~1", head_ref: str = "HEAD") -> ASTDiffResult:
    """核心入口：repo diff → 方法级变更 → 受影响接口。

    优先用 engine/parsers 套件（PHP tree-sitter / Java jar / Swift），
    Python 用内置 tree-sitter，找不到的语言包退回简单 tree-sitter 兜底。
    """
    result = ASTDiffResult()
    laravel_routes = None

    for rel_path in changed_files:
        lang = _detect_language(rel_path)
        if not lang:
            continue

        abs_path = os.path.join(repo_path, rel_path)
        if not os.path.isfile(abs_path):
            continue

        changed_lines = _git_diff_line_numbers(repo_path, rel_path, base_ref, head_ref)
        if not changed_lines:
            continue

        # ── 获取方法列表 ──
        methods = []

        # 优先用现有 parsers 套件（PHP/Go/Java/Kotlin/Swift）
        if lang in ("php", "go", "java", "kotlin", "swift"):
            try:
                nodes = _run_async_parser(repo_path, lang, abs_path)
                methods = _nodes_to_methods(nodes, rel_path)
            except Exception:
                pass

        # parsers 没产出 → 退回内置解析
        if not methods:
            try:
                with open(abs_path, "rb") as f:
                    source = f.read()
                methods = extract_methods(source, lang)
            except Exception:
                continue

        # 如果是 JVM 且 parsers 产出的 node 没有路由信息，补充注解解析
        if lang in ("java", "kotlin") and methods:
            try:
                with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                    src_text = f.read()
                for m in methods:
                    if not m.route_path:
                        rm, rp = _parse_jvm_annotations(src_text, m.start_line - 1)
                        m.route_method, m.route_path = rm, rp
            except Exception:
                pass

        # ── 定位被修改的方法 ──
        # 加 2 行 padding 容差（解析器行号可能不含结尾 } 行）
        affected = [m for m in methods if any(m.start_line <= ln <= m.end_line + 2 for ln in changed_lines)]
        result.files_analyzed += 1
        result.language = result.language or lang

        for m in affected:
            result.changed_methods.append(m)
            if m.route_path:
                result.affected_endpoints.append(AffectedEndpoint(
                    method=m.route_method or "ANY", path=m.route_path,
                    handler=f"{m.class_name}.{m.name}" if m.class_name else m.name,
                    file=rel_path,
                ))
            elif lang == "php" and m.class_name:
                if laravel_routes is None:
                    laravel_routes = _parse_laravel_routes(repo_path)
                key = f"{m.class_name}@{m.name}"
                if key in laravel_routes:
                    http_method, path = laravel_routes[key]
                    result.affected_endpoints.append(AffectedEndpoint(
                        method=http_method, path=path, handler=key, file=rel_path,
                    ))

    # ── 调用链传导：从向量库查谁调用了被修改的方法（波及范围）──
    if result.changed_methods:
        repo_name = _detect_repo_name(repo_path)
        if repo_name:
            try:
                from engine.vector_search import find_reverse_dependencies
                method_names = list(set(m.name for m in result.changed_methods if m.name))
                # 过滤通用框架方法名（这些几乎所有类都有，查了只会噪音）
                method_names = [n for n in method_names if n not in _GENERIC_METHOD_NAMES]
                if not method_names:
                    return result
                callers = find_reverse_dependencies(repo_name, "", method_names)
                callers = find_reverse_dependencies(repo_name, "", method_names)
                # 去掉自身（被修改的方法本身不算"上游"）
                changed_ids = set(
                    f"{m.class_name}::{m.name}" for m in result.changed_methods
                )
                for caller in callers:
                    caller_class = caller.get("class_name", "")
                    caller_name = caller.get("name") or caller.get("method_name") or ""
                    # 从 node_id 解析（格式 /file::Class::method#L123）
                    if not caller_name and caller.get("node_id"):
                        parts = caller["node_id"].split("::")
                        if len(parts) >= 3:
                            caller_class = parts[-2]
                            caller_name = parts[-1].split("#")[0]
                        elif len(parts) == 2:
                            caller_name = parts[-1].split("#")[0]
                    caller_id = f"{caller_class}::{caller_name}"
                    if caller_id in changed_ids:
                        continue
                    result.ripple_methods.append({
                        "name": caller_name,
                        "class_name": caller_class,
                        "file": caller.get("file_path", ""),
                        "node_id": caller.get("node_id", ""),
                        "reason": f"调用了 {', '.join(method_names[:3])}",
                    })
            except Exception:
                pass

    return result
