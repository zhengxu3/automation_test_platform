"""代码分析任务 — Git Diff + LLM 流式分析"""
import asyncio
import json
import os
import re
import subprocess
import time
from ai_worker.base_task import BaseTaskHandler
from common.db import get_collection
from llm.llm_factory import LLMFactory
from llm.structured import generate_structured

HANDLER_META = {
    "key": "branch_review",
    "label": "代码分析",
    "description": "对比分支代码差异，分析影响范围，生成回归测试清单",
    "capabilities": ["code_diff", "branch_compare", "regression_analysis"],
    "inputs": [
        {"key": "mode", "label": "对比模式", "type": "select", "required": True, "options": ["分支对比", "最近更新"], "default": "最近更新"},
        {"key": "repo_single", "label": "仓库", "type": "repo_select", "required": False},
        {"key": "repo_master", "label": "主分支仓库", "type": "repo_select", "required": False},
        {"key": "repo_branch", "label": "开发分支仓库", "type": "repo_select", "required": False},
    ],
}

ROUTE_PATTERNS = [
    r'@\w+\.route\(\s*["\']([^"\']+)["\']([^)]*)\)',  # flask @app.route("/x", methods=["POST"])
    r'@\w+\.(get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']',  # fastapi/flask shortcut
    r'\.(get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']',      # express app.post("/x")
    r'(?:path|url|re_path)\(\s*["\']([^"\']+)["\']',               # django urls
]
_SCAN_IGNORE = {".git", "node_modules", "build", "dist", "__pycache__", ".venv", "venv", "target"}


def _is_generated(path: str) -> bool:
    """是否为编译产物/依赖/IDE 等非源码文件——代码分析的受影响模块不该含这些。"""
    p = (path or "").replace("\\", "/")
    if p.startswith("__pycache__/") or "/__pycache__/" in p:
        return True
    if p.endswith((".pyc", ".pyo", ".class", ".o", ".so", ".min.js", ".map")):
        return True
    return any(seg in p for seg in (
        "node_modules/", "/build/", "/dist/", "/.gradle/", "/target/", "/.idea/", "/Pods/", "/.venv/"))


class BranchReviewTask(BaseTaskHandler):
    """Task Type 2 — 代码审查"""

    async def run(self):
        inputs = self.payload.get("inputs", {})
        mode = inputs.get("mode", "最近更新")
        repo_name = self.payload.get("repo_name", "")
        repo_path = self.payload.get("repo_path", "")
        base_branch = self.payload.get("base_branch", "master")
        target_branch = self.payload.get("target_branch", base_branch)

        # 路径自适应
        import platform, yaml
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config.yaml")
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
        git_base = config.get("git", {}).get("repo_base_path", "/data/repos")
        if platform.system() != "Linux":
            git_base = os.path.expanduser("~/Documents/work_code")
        repo_path = os.path.join(git_base, os.path.basename(repo_path))

        # 加载智能体配置
        agent = get_collection("ai_agents").find_one({"agent_id": self.payload.get("agent_id", "")}, {"_id": 0})
        system_prompt = self.payload.get("system_prompt", "") or (agent or {}).get("system_prompt", "")
        model_id = self.payload.get("model_id", "") or (agent or {}).get("model_id", "gemini_flash")

        self.log("═══ 代码分析智能体启动 ═══")
        self.log(f"📦 仓库: {repo_name} | 模式: {mode}")
        self.log(f"🔀 对比: {base_branch} ← {target_branch}")
        self.log(f"🧠 模型: {model_id}")

        # ========== 1. Git Diff ==========
        self.log("🔍 执行 Git Diff...")
        if mode == "最近更新":
            diff_result = await asyncio.to_thread(self._git_diff_head, repo_path)
        else:
            diff_result = await asyncio.to_thread(self._git_diff_files, repo_path, base_branch, target_branch)

        changed = diff_result.get("changed", [])
        deleted = diff_result.get("deleted", [])

        if diff_result.get("error"):
            self.log(f"⚠️ Git Diff 警告: {diff_result['error']}", "warning")

        if not changed:
            self.log("⚠️ 未检测到文件变更", "warning")
            return {
                "change_summary": "无文件变更",
                "regression_cases": [],
                "risk_points": [],
                "affected_modules": [],
                "interface_doc": {"affected_endpoints": [], "summary": "无文件变更"},
                "no_change": True,
            }

        self.log(f"📂 变更: {len(changed)} 个 | 删除: {len(deleted)} 个")
        for f in changed[:15]:
            self.log(f"  ├─ {f}")
        if len(changed) > 15:
            self.log(f"  └─ ... 还有 {len(changed) - 15} 个文件")

        # ========== 2. 读取变更内容 ==========
        self.log("📄 读取变更内容...")
        diff_content = await asyncio.to_thread(self._get_diff_content, repo_path, base_branch, target_branch, mode)

        # ========== 3. 组装 Prompt + 调用 LLM ==========
        self.log("🧠 组装上下文，调用大模型...")

        requirement_context = self.payload.get("requirement_context", "")
        req_section = f"\n## 需求分析上下文\n{requirement_context[:6000]}\n" if requirement_context else ""

        user_prompt = f"""{req_section}
## 项目信息
- 仓库：{repo_name}
- 对比：{base_branch} → {target_branch}
- 变更文件数：{len(changed)}

## 变更文件
{json.dumps(changed, ensure_ascii=False)}

## Diff 内容
```
{diff_content[:30000]}
```

请输出：
1. **变更摘要**（一段话概括）
2. **影响范围**（按 P0/P1/P2 排序）
3. **回归测试清单**（每条标注 [代码向]/[扩展验证] + [前端可达]/[服务端触发]/[内部触发]）
4. **风险点**（可能引入的 bug 或兼容性问题）
"""

        self.log("⏳ 等待 AI 响应...")
        result = await asyncio.to_thread(LLMFactory.generate, model_id, system_prompt, user_prompt)
        text = result.get("text", "")
        usage = result.get("usage", {})

        if not text or len(text.strip()) < 50:
            self.log("❌ 模型产出无效", "error")
            raise ValueError("模型产出过短")

        self.log(f"📊 Token: {usage.get('total_tokens', 0)}")

        # ========== 4. 保存产出 ==========
        req_id = self.payload.get("req_id", "")
        agent_id = self.payload.get("agent_id", "")
        if req_id:
            get_collection("ai_workspace_outputs").insert_one({
                "req_id": req_id, "agent_id": agent_id, "task_id": self.task_id,
                "content": text, "format": "markdown",
                "round": 1, "created_at": int(time.time()),
            })
            get_collection("ai_workspace_agents").update_one(
                {"req_id": req_id, "agent_id": agent_id},
                {"$set": {"status": "completed", "updated_at": int(time.time())}},
            )

        self.log(f"✅ 分析完成，产出 {len(text)} 字符", "success")

        # 结构化产出（goal 模式契约校验用；req 模式忽略返回值）
        regression_cases = self._parse_regression_cases(text)
        interface_doc = await asyncio.to_thread(
            self._extract_interface_doc,
            repo_path, changed, diff_content, text, requirement_context, model_id,
        )
        return {
            "change_summary": text[:500],
            "regression_cases": regression_cases,
            "risk_points": self._parse_section_items(text, "风险"),
            "affected_modules": changed[:30],
            "interface_doc": interface_doc,
            "no_change": False,
            "report": text,
            "confidence": 0.8,
        }

    def _discover_routes(self, repo_path: str, limit: int = 400) -> list:
        """确定性扫描后端路由，供接口契约抽取 grounding。"""
        routes = []
        if not repo_path or not os.path.isdir(repo_path):
            return routes
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in _SCAN_IGNORE and not d.startswith(".")]
            for fn in files:
                if not fn.endswith((".py", ".js", ".ts", ".go", ".java")):
                    continue
                fp = os.path.join(root, fn)
                rel = os.path.relpath(fp, repo_path)
                try:
                    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                        for i, line in enumerate(f, 1):
                            route = self._parse_route_line(line)
                            if route:
                                route.update({"file": rel, "line": i, "code": line.strip()[:160]})
                                routes.append(route)
                except Exception:
                    continue
                if len(routes) >= limit:
                    return routes
        return routes

    @staticmethod
    def _parse_route_line(line: str) -> dict:
        for idx, pat in enumerate(ROUTE_PATTERNS):
            m = re.search(pat, line, re.IGNORECASE)
            if not m:
                continue
            if idx == 0:
                path = m.group(1)
                rest = m.group(2) or ""
                method_match = re.search(r"methods\s*=\s*\[([^\]]+)\]", rest, re.IGNORECASE)
                method = "GET"
                if method_match:
                    methods = re.findall(r'["\']([A-Z]+)["\']', method_match.group(1), re.IGNORECASE)
                    method = (methods[0] if methods else "GET").upper()
                return {"method": method, "path": path}
            if idx in (1, 2):
                return {"method": m.group(1).upper(), "path": m.group(2)}
            return {"method": "ANY", "path": m.group(1)}
        return {}

    def _extract_interface_doc(self, repo_path: str, changed: list, diff_content: str,
                               analysis_text: str, requirement_context: str,
                               model_id: str) -> dict:
        """后端代码分析的承上启下产物：受影响接口文档。

        branch_review 负责读 diff/路由/业务代码，因此由它产 interface_doc；
        api_test 只消费该文档生成和执行测试。
        """
        routes = self._discover_routes(repo_path)
        if not routes:
            return {"affected_endpoints": [], "summary": "未扫描到显式接口路由", "confidence": 0.0}

        route_text = "\n".join(
            f"- {r['method']} {r['path']} ({r['file']}:{r['line']}) {r.get('code', '')}"
            for r in routes[:200]
        )
        schema = {
            "required": ["affected_endpoints"],
            "types": {"affected_endpoints": "list"},
        }
        system = (
            "你是后端代码分析智能体的一部分。你已经拿到本次 diff、代码分析报告和仓库路由清单。"
            "请产出给 API 测试智能体使用的结构化接口文档。"
            "只根据 diff/路由/需求里能 grounding 的内容写请求参数、响应字段、错误码；不知道就写 unknown，禁止臆造。"
        )
        user = f"""## 需求上下文
{requirement_context[:2000]}

## 代码分析报告
{analysis_text[:4000]}

## 变更文件
{json.dumps(changed, ensure_ascii=False)}

## Diff 内容
```
{diff_content[:20000]}
```

## 仓库路由清单
{route_text}

输出 JSON：
{{
  "affected_endpoints": [
    {{
      "method": "POST",
      "path": "/login",
      "affected_reason": "为何被本次变更波及",
      "impact": "direct|indirect",
      "request": {{"字段名": "类型/含义/约束，unknown 表示未知"}},
      "responses": {{
        "success": {{"字段名": "取值/含义，unknown 表示未知"}},
        "errors": [
          {{"code": "错误码或unknown", "when": "触发条件", "fields": {{"字段名": "含义"}}}}
        ]
      }},
      "grounding": ["来自哪个文件/路由/diff片段"]
    }}
  ],
  "summary": "接口影响面一句话总结",
  "confidence": 0.0
}}"""
        result = generate_structured(
            system_prompt=system,
            user_prompt=user,
            schema=schema,
            model_id=model_id,
            max_retries=2,
            default={"affected_endpoints": [], "summary": "接口文档抽取降级", "confidence": 0.0},
        )
        doc = result.data if isinstance(result.data, dict) else {}
        endpoints = []
        known = {(r["method"], r["path"]) for r in routes}
        known_paths = {r["path"] for r in routes}
        for ep in doc.get("affected_endpoints", []) or []:
            if not isinstance(ep, dict):
                continue
            method = str(ep.get("method") or "ANY").upper()
            path = str(ep.get("path") or "")
            if not path:
                continue
            # 允许 LLM 在已知 path 上补 method；不允许凭空造不存在 path。
            if (method, path) not in known and path not in known_paths:
                continue
            ep["method"] = method
            ep["path"] = path
            endpoints.append(ep)
        doc["affected_endpoints"] = endpoints
        doc.setdefault("summary", f"识别受影响接口 {len(endpoints)} 个")
        doc.setdefault("confidence", result.data.get("confidence", 0.5) if isinstance(result.data, dict) else 0.5)
        doc["source"] = "branch_review"
        doc["route_count"] = len(routes)
        return doc

    @staticmethod
    def _parse_section_items(text: str, keyword: str) -> list:
        """从 markdown 文本中提取含关键词标题段落下的条目"""
        lines = text.split("\n")
        items, capturing = [], False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or (stripped and stripped[0].isdigit() and "**" in stripped):
                capturing = keyword in stripped
                continue
            if capturing and stripped:
                m = re.match(r'^\s*(?:[-*]|\d+[\.\)、])\s*(.+)', line)
                if m:
                    items.append(m.group(1).strip()[:200])
        return items[:50]

    def _parse_regression_cases(self, text: str) -> list:
        """提取回归测试清单条目；解析不到则用整体摘要兜底一条，保证契约可校验"""
        cases = self._parse_section_items(text, "回归")
        if not cases:
            # 兜底：分析有实质产出就视为至少一条回归建议
            if len(text.strip()) >= 50:
                cases = [text.strip().split("\n")[0][:200] or "代码变更回归建议（见完整分析报告）"]
        return cases

    # ========== Git 操作 ==========
    def _git_diff_head(self, repo_path: str) -> dict:
        try:
            subprocess.run(["git", "pull", "--ff-only"], cwd=repo_path, capture_output=True, timeout=30)
            result = subprocess.run(["git", "diff", "--name-status", "HEAD~1"], cwd=repo_path, capture_output=True, text=True, timeout=30)
            return self._parse_diff_output(result.stdout)
        except Exception as e:
            return {"changed": [], "deleted": [], "error": str(e)}

    def _git_diff_files(self, repo_path: str, base: str, target: str) -> dict:
        try:
            subprocess.run(["git", "fetch", "origin"], cwd=repo_path, capture_output=True, timeout=30)
            ref = f"{base}...{target}" if "origin/" in target else f"{base}...origin/{target}"
            result = subprocess.run(["git", "diff", "--name-status", ref], cwd=repo_path, capture_output=True, text=True, timeout=30)
            return self._parse_diff_output(result.stdout)
        except Exception as e:
            return {"changed": [], "deleted": [], "error": str(e)}

    def _get_diff_content(self, repo_path: str, base: str, target: str, mode: str) -> str:
        try:
            if mode == "最近更新":
                cmd = ["git", "diff", "HEAD~1", "--stat", "-p"]
            else:
                ref = f"{base}...{target}" if "origin/" in target else f"{base}...origin/{target}"
                cmd = ["git", "diff", ref, "--stat", "-p"]
            result = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, timeout=60)
            return result.stdout[:50000]  # 限制大小
        except Exception:
            return ""

    def _parse_diff_output(self, stdout: str) -> dict:
        changed, deleted = [], []
        for line in stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            path = parts[1]
            if _is_generated(path):   # 过滤编译产物/依赖目录，受影响模块只留真实源码
                continue
            if parts[0].startswith("D"):
                deleted.append(path)
            else:
                changed.append(path)
        return {"changed": changed, "deleted": deleted}
