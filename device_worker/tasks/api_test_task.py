"""Goal 设备侧能力任务（device_worker）

API 接口测试：爆炸半径→受影响API→动态建脚本→真打后端/可控 mock→自愈→持久化套件
Web UI 测试：按 Web 仓库+测试环境 URL 做基础冒烟/可控 mock，后续可替换为 Playwright 级交互执行
UI 脚本生成：需求/回归点→动态生成 uiautomator2 脚本→落独立脚本目录

为什么在 device_worker：生产 Linux 机连不到测试服务器，只有设备服务器在能连到测试环境的网络里。
API 测试只需网络可达 base_url，不需要真机，故 device_worker 对该能力跳过设备获取。

闭环（仿 branch_review 代码对比范式，但落到接口）：
  1. 接口文档：优先读上游 branch_review 产出的 interface_doc（受影响接口契约）
  2. 动态建脚本：LLM 据接口文档 + base_url + 测试账号 + 需求上下文 生成 requests 测试脚本
  3. 真打后端：subprocess 跑脚本，写 result.json（每用例 pass/fail）
  4. 自愈：脚本崩溃(非断言失败)→读 traceback 改脚本重跑（有界）；区分"脚本bug"与"真实API失败"
  5. 持久化：每需求(goal/req)维护一套 ai_api_test_suites（脚本+运行历史），二次运行复用

核心逻辑 execute_api_test() 不依赖设备基建，可独立运行（真实验证用）。
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import uuid

from device_worker.tasks.base import BaseDeviceTask, NOTIFY_SILENT

HANDLER_META = {
    "key": "api_test",
    "label": "API 接口测试",
    "description": "Goal 模式设备侧能力统一入口：api_test / web_test / script_gen",
    "task_type": 40,   # Goal 能力统一入口 task_type
}

MAX_HEAL = 2                      # 脚本崩溃自愈上限
OUTPUT_ROOT = "/tmp/api_test"
WEB_TEST_OUTPUT_ROOT = "/tmp/web_test"
UI_SCRIPT_OUTPUT_ROOT = "/tmp/ui_script_gen"
ROUTE_PATTERNS = [
    r'@\w+\.route\(\s*["\']([^"\']+)["\']',          # flask @app.route("/x") / @bp.route
    r'@\w+\.(?:get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']',  # fastapi @app.get("/x")
    r'(?:path|url|re_path)\(\s*["\']([^"\']+)["\']',  # django urls
    r'\.(?:GET|POST|PUT|DELETE)\(\s*["\']([^"\']+)["\']',
]
_SCAN_IGNORE = {".git", "node_modules", "build", "dist", "__pycache__", ".venv", "venv", "target"}


def _emit_codegen_event(db, inputs: dict, stage: str, summary: str, **extra):
    """把 device_worker 内部的代码生成/自愈过程写回 Goal 事件流。"""
    goal_id = inputs.get("goal_id")
    if db is None or not goal_id:
        return
    payload = {
        "step_id": inputs.get("step_id", ""),
        "task_id": inputs.get("task_id", ""),
        "stage": stage,
        "summary": summary,
        **extra,
    }
    try:
        db["ai_goal_events"].insert_one({
            "goal_id": goal_id,
            "entity_type": "event",
            "event": "codegen_progress",
            "payload": payload,
            "actor": "device_worker",
            "timestamp": int(time.time()),
        })
    except Exception:
        # 进度事件是可观测性增强，不能影响真实测试执行。
        return


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


# ==================== 路由发现（确定性扫描）====================

def discover_routes(repo_path: str, limit: int = 400) -> list:
    """扫后端仓库源码，正则抽出路由定义行（供 LLM 推爆炸半径）。返回 [{file, line, endpoint}]。"""
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
                        for pat in ROUTE_PATTERNS:
                            m = re.search(pat, line)
                            if m:
                                routes.append({"file": rel, "line": i, "endpoint": m.group(1),
                                               "code": line.strip()[:160]})
                                break
            except Exception:
                continue
            if len(routes) >= limit:
                return routes
    return routes


# ==================== LLM：爆炸半径 → 受影响 endpoint ====================

def extract_affected_endpoints(inputs: dict, routes: list, model_id: str, log=print) -> list:
    """据代码变更（affected_modules + change_summary）+ 全量路由，LLM 推断受波及的接口。"""
    from llm.structured import generate_structured

    affected_modules = inputs.get("affected_modules") or []
    change_summary = inputs.get("change_summary") or ""
    requirement = inputs.get("requirement_context") or ""
    route_text = "\n".join(f"  {r['endpoint']}  ({r['file']}:{r['line']})  {r['code']}" for r in routes[:200]) or "（未扫到显式路由）"

    schema = {"required": ["endpoints"], "types": {"endpoints": "list"}}
    system = (
        "你是后端测试的影响面分析专家。给定代码变更和该服务的全部接口路由，"
        "推断哪些 API 接口被本次变更直接或间接波及（爆炸半径）。"
        "直接波及=变更文件就是路由处理；间接波及=变更的函数/模块被某接口调用链触达。"
    )
    user = f"""## 需求上下文
{requirement[:2000]}

## 本次代码变更摘要
{change_summary[:3000]}

## 变更文件
{json.dumps(affected_modules, ensure_ascii=False)}

## 该服务全部接口路由
{route_text}

请输出受本次变更波及的接口（含间接调用链触达的），JSON：
{{"endpoints": [
  {{"method": "POST", "path": "/login", "reason": "为何被波及", "impact": "direct|indirect"}}
]}}
只列真实存在于上面路由清单或变更明显涉及的接口，别臆造。"""

    result = generate_structured(system, user, schema=schema, model_id=model_id, max_retries=2,
                                 default={"endpoints": []})
    eps = result.data.get("endpoints", []) or []
    log(f"🎯 爆炸半径推断受影响接口 {len(eps)} 个: {[e.get('path') for e in eps]}")
    return eps


# ==================== LLM：动态生成测试脚本 ====================

def _strip_code_fence(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:python)?\s*(.+?)```", text, re.DOTALL)
    return m.group(1).strip() if m else text


def generate_test_script(inputs: dict, endpoints: list, model_id: str, log=print) -> str:
    """LLM 生成自包含 requests 测试脚本：跑各接口、按需求断言、把结果写 result.json。"""
    from llm.llm_factory import LLMFactory

    base_url = inputs.get("base_url", "")
    test_accounts = inputs.get("test_accounts") or []
    requirement = inputs.get("requirement_context") or ""
    change_summary = inputs.get("change_summary") or ""
    interface_doc = inputs.get("interface_doc") or {}

    system = (
        "你是资深 API 自动化测试工程师。生成一个**自包含、可直接运行**的 Python3 测试脚本。\n"
        "硬性要求：\n"
        "1. 只用标准库 + requests。\n"
        "2. base_url 从环境变量 BASE_URL 读取（os.environ['BASE_URL']）。\n"
        "3. 覆盖需求要点与受影响接口的正例/反例（如非法入参、边界、错误码）。\n"
        "4. 每条用例记录 {name, method, path, passed(bool), expected, actual, detail}。\n"
        "5. 最后把结果写到环境变量 RESULT_PATH 指定的文件，JSON 结构：\n"
        "   {\"all_passed\": bool, \"cases_passed\": int, \"cases_failed\": int, \"summary\": str, \"cases\": [...]}。\n"
        "6. 断言失败不要抛异常中断，要记成 passed=false 继续跑完所有用例。\n"
        "7. 不要 print 之外的副作用，不要无限等待，单请求 timeout=10。\n"
        "**断言 grounding（关键，避免臆造）**：\n"
        "- 预期响应码/字段优先来自【接口契约文档】；契约里是 unknown 的，不要硬断言具体值。\n"
        "- 预期响应码/字段只能用'接口契约/变更摘要/需求'里明确出现的值（如 code=INVALID_PHONE/ACCOUNT_LOCKED 等），"
        "禁止臆造未提及的值（如 SUCCESS/Login successful）。\n"
        "- 成功用例：以业务成功标志判定（如响应 JSON 的 ok==true），不要假设具体成功码字符串。\n"
        "- 响应字段名以实际返回 JSON 为准（如 code/msg/ok），不要假设 message/error 等未出现的字段。\n"
        "- 禁止在脚本中包含平台未下发的硬编码业务数据快照；尤其不要捏造 token、session_id、user_id、订单号、匹配结果、"
        "会员状态、性别画像等动态业务状态。\n"
        "- 所有动态依赖必须来自数据流：优先读取前序登录/准备 Step 的输出 Artifacts；没有 Artifact 时，"
        "只能通过接口契约里的登录/准备接口在脚本运行时实时获取，不能写死兜底值。\n"
        "- 测试账号只能使用【测试账号】输入中出现的账号；如果账号缺失，应把对应用例记为 passed=false，"
        "detail 写明缺少测试账号，而不是自行构造账号。\n"
        "- **锁定类用例严禁 time.sleep 等待解锁**（会超时）；只验证'连续失败到阈值后返回锁定码'即可，不验证自动解锁。\n"
        "只输出脚本代码本身，不要解释。"
    )
    user = f"""## 需求
{requirement[:2000]}

## 变更摘要
{change_summary[:2000]}

## 受影响接口（爆炸半径）
{json.dumps(endpoints, ensure_ascii=False, indent=2)}

## 接口契约文档（来自代码分析智能体，断言必须以它为最高优先级 grounding）
{json.dumps(interface_doc, ensure_ascii=False, indent=2)[:5000]}

## 测试账号（如需登录）
{json.dumps(test_accounts, ensure_ascii=False)}

## base_url（运行时从 BASE_URL 环境变量取，此处供参考）
{base_url}

生成完整可运行脚本。"""

    result = LLMFactory.generate(model_id, system, user)
    script = _strip_code_fence(result.get("text", ""))
    log(f"📝 生成测试脚本 {len(script)} 字符")
    return script


def _load_interface_doc(inputs: dict) -> dict:
    """读取上游代码分析智能体产出的接口文档，兼容字符串/对象两种形态。"""
    doc = inputs.get("interface_doc") or {}
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except Exception:
            doc = {}
    return doc if isinstance(doc, dict) else {}


def _endpoints_from_interface_doc(interface_doc: dict) -> list:
    """从 interface_doc 取受影响 endpoint，保留 request/responses 作为测试 grounding。"""
    endpoints = interface_doc.get("affected_endpoints") or interface_doc.get("endpoints") or []
    normalized = []
    for ep in endpoints:
        if not isinstance(ep, dict):
            continue
        path = ep.get("path") or ep.get("endpoint")
        if not path:
            continue
        item = dict(ep)
        item["method"] = str(item.get("method") or "ANY").upper()
        item["path"] = path
        normalized.append(item)
    return normalized


def heal_script(script: str, stderr: str, model_id: str, log=print) -> str:
    """脚本崩溃 → 据 traceback 修复脚本（只修脚本 bug，不掩盖真实接口失败）。"""
    from llm.llm_factory import LLMFactory
    system = (
        "你是 Python 调试专家。下面的测试脚本运行崩溃了（不是断言失败，是脚本本身报错）。"
        "据报错修复脚本，保持原测试意图与 result.json 输出契约不变。只输出修复后的完整脚本。"
    )
    user = f"## 报错\n{stderr[:3000]}\n\n## 原脚本\n```python\n{script}\n```"
    result = LLMFactory.generate(model_id, system, user)
    fixed = _strip_code_fence(result.get("text", ""))
    log(f"🔧 自愈修复脚本 {len(fixed)} 字符")
    return fixed


# ==================== 执行脚本（subprocess 真跑）====================

def _run_script(script: str, base_url: str, work_dir: str) -> dict:
    """跑脚本，返回 {script_error(bool), returncode, stderr, result(dict|None)}。"""
    os.makedirs(work_dir, exist_ok=True)
    script_path = os.path.join(work_dir, "api_test_script.py")
    result_path = os.path.join(work_dir, "result.json")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)
    if os.path.exists(result_path):
        os.remove(result_path)

    env = os.environ.copy()
    env["BASE_URL"] = base_url
    env["RESULT_PATH"] = result_path
    try:
        proc = subprocess.run([sys.executable, script_path], cwd=work_dir, env=env,
                              capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"script_error": True, "returncode": -1, "stderr": "脚本执行超时(120s)",
                "result": None, "script_path": script_path, "result_path": result_path}

    result = None
    if os.path.exists(result_path):
        try:
            with open(result_path, "r", encoding="utf-8") as f:
                result = json.load(f)
        except Exception:
            result = None
    # 脚本崩溃判定：非零退出 且 没产出合法 result.json
    script_error = (proc.returncode != 0 and result is None) or (result is None)
    return {"script_error": script_error, "returncode": proc.returncode,
            "stderr": (proc.stderr or "")[:4000], "stdout": (proc.stdout or "")[:2000],
            "result": result, "script_path": script_path, "result_path": result_path}


# ==================== 持久化套件（每需求一套）====================

def _suite_key(inputs: dict, endpoints: list) -> dict:
    sig = ",".join(sorted(f"{e.get('method','')}:{e.get('path','')}" for e in endpoints))
    key = {"endpoints_sig": sig}
    if inputs.get("goal_id"):
        key["goal_id"] = inputs["goal_id"]
    elif inputs.get("req_id"):
        key["req_id"] = inputs["req_id"]
    return key


def persist_suite(db, inputs: dict, endpoints: list, script: str, run_record: dict, log=print):
    """每需求维护一套接口测试套件：脚本 + 运行历史（append）。二次运行复用此脚本。"""
    if db is None:
        return None
    key = _suite_key(inputs, endpoints)
    existing = db["ai_api_test_suites"].find_one(key, {"_id": 0, "suite_id": 1})
    suite_id = (existing or {}).get("suite_id") or f"suite_{uuid.uuid4().hex[:8]}"
    db["ai_api_test_suites"].update_one(
        key,
        {"$set": {"suite_id": suite_id, "goal_id": inputs.get("goal_id", ""),
                  "req_id": inputs.get("req_id", ""), "base_url": inputs.get("base_url", ""),
                  "endpoints": endpoints, "script": script, "updated_at": int(time.time())},
         "$push": {"run_history": run_record},
         "$setOnInsert": {"created_at": int(time.time())}},
        upsert=True,
    )
    log(f"💾 持久化测试套件 {suite_id} (key={key})")
    return suite_id


def load_persisted_script(db, inputs: dict, endpoints: list):
    """二次运行复用：若已有同 endpoints 签名的套件，取其脚本。"""
    if db is None:
        return None
    suite = db["ai_api_test_suites"].find_one(_suite_key(inputs, endpoints), {"_id": 0, "script": 1})
    return (suite or {}).get("script")


def _mock_round_index(db, inputs: dict) -> int:
    """mock 多轮用：按 goal.replan_count 推导当前第几次目标验证。"""
    if db is None or not inputs.get("goal_id"):
        return 1
    goal = db["ai_goals"].find_one({"goal_id": inputs["goal_id"]}, {"_id": 0, "replan_count": 1}) or {}
    return int(goal.get("replan_count", 0) or 0) + 1


def _mock_replan_budget(db, inputs: dict) -> int:
    """读取 mock 演示可用的 replan 预算；输入优先，DB 兜底。"""
    explicit = inputs.get("max_replans")
    if explicit is not None:
        return _int_value(explicit, 3)
    if db is not None and inputs.get("goal_id"):
        goal = db["ai_goals"].find_one(
            {"goal_id": inputs["goal_id"]}, {"_id": 0, "budget": 1}
        ) or {}
        return _int_value((goal.get("budget") or {}).get("max_replans"), 3)
    return 3


# ==================== 提交代码模拟：pass/fail 由提交的代码本身决定 ====================
_SIDE_EXT = {
    "backend": (".py", ".go", ".rb", ".php"),
    "web": (".vue", ".jsx", ".tsx", ".ts", ".js"),
    "client": (".kt", ".java", ".swift"),
}
_BUG_TOKEN = "MOCK_BUG"
_SCAN_SKIP = {".git", "node_modules", "build", "dist", "__pycache__", ".venv", "venv", "target"}


def _side_source_files(repo_path: str, side: str) -> list:
    exts = _SIDE_EXT.get(side, ())
    out = []
    if not exts or not repo_path or not os.path.isdir(repo_path):
        return out
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in _SCAN_SKIP and not d.startswith(".")]
        for fn in files:
            if fn.endswith(exts):
                out.append(os.path.join(root, fn))
    return out


def _code_has_bug(repo_path: str, side: str, token: str = _BUG_TOKEN) -> bool:
    """提交代码模拟：某 side 源码含 MOCK_BUG 标记 → 视为有缺陷(测试失败)；提交移除即通过。"""
    for fp in _side_source_files(repo_path, side):
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                if token in f.read():
                    return True
        except Exception:
            continue
    return False


def _mock_should_fail(inputs: dict, code_side: str, round_index: int, fallback_fail_rounds: int) -> bool:
    """提交代码模拟：仓库该 side 源码含 MOCK_BUG → 失败、无则通过（成败由提交的代码决定）；
    仅当仓库无该 side 源码时才退回轮次兜底（边缘情况）。无任何外部开关，不污染 goal 流程。"""
    repo_path = inputs.get("repo_path")
    if repo_path and _side_source_files(repo_path, code_side):
        return _code_has_bug(repo_path, code_side)
    return round_index <= fallback_fail_rounds


def _mock_api_run(inputs: dict, endpoints: list, db=None) -> dict:
    """显式 mock 模式：不打 base_url，但保留生成脚本和多轮 pass/fail 行为。"""
    round_index = _mock_round_index(db, inputs)
    requested_fail_rounds = _int_value(inputs.get("mock_fail_rounds"), 0)
    max_replans = _mock_replan_budget(db, inputs)
    safe_fail_rounds = min(requested_fail_rounds, max(max_replans - 1, 0))
    if requested_fail_rounds > safe_fail_rounds:
        _emit_codegen_event(
            db, inputs, "mock_config_adjusted",
            f"mock_fail_rounds={requested_fail_rounds} 超过安全预算，已调整为 {safe_fail_rounds}",
            requested_fail_rounds=requested_fail_rounds,
            effective_fail_rounds=safe_fail_rounds,
            max_replans=max_replans,
        )
    fail_rounds = safe_fail_rounds
    should_fail = _mock_should_fail(inputs, "backend", round_index, fail_rounds)
    cases_failed = 1 if should_fail else 0
    cases_passed = max(len(endpoints), 1) - cases_failed
    cases = []
    for i, ep in enumerate(endpoints or [{"method": "ANY", "path": "/mock"}], 1):
        failed = should_fail and i == 1
        cases.append({
            "name": f"mock_{ep.get('method', 'ANY')}_{ep.get('path', '/mock')}",
            "method": ep.get("method", "ANY"),
            "path": ep.get("path", "/mock"),
            "passed": not failed,
            "expected": "接口行为符合目标",
            "actual": "mock 故意失败以触发下一轮 plan" if failed else "mock 通过",
            "detail": f"mock round={round_index}, fail_rounds={fail_rounds}, max_replans={max_replans}",
        })
    return {
        "all_passed": not should_fail,
        "cases_passed": cases_passed,
        "cases_failed": cases_failed,
        "summary": (
            f"mock 第 {round_index} 轮故意保留失败，驱动 critic 进入下一轮"
            if should_fail else f"mock 第 {round_index} 轮通过，目标可收敛"
        ),
        "cases": cases,
        "round_index": round_index,
        "requested_fail_rounds": requested_fail_rounds,
        "effective_fail_rounds": fail_rounds,
        "mock": True,
    }


def _mock_web_run(inputs: dict, db=None) -> dict:
    """Web UI mock 多轮：不访问 base_url，但保留 pass/fail 收敛行为。"""
    round_index = _mock_round_index(db, inputs)
    requested_fail_rounds = _int_value(inputs.get("mock_fail_rounds"), 0)
    max_replans = _mock_replan_budget(db, inputs)
    fail_rounds = min(requested_fail_rounds, max(max_replans - 1, 0))
    if requested_fail_rounds > fail_rounds:
        _emit_codegen_event(
            db, inputs, "mock_config_adjusted",
            f"web mock_fail_rounds={requested_fail_rounds} 超过安全预算，已调整为 {fail_rounds}",
            requested_fail_rounds=requested_fail_rounds,
            effective_fail_rounds=fail_rounds,
            max_replans=max_replans,
        )
    should_fail = round_index <= fail_rounds
    should_fail = _mock_should_fail(inputs, "web", round_index, fail_rounds)
    return {
        "all_passed": not should_fail,
        "cases_passed": 0 if should_fail else 1,
        "cases_failed": 1 if should_fail else 0,
        "cases": [{
            "name": "mock_web_smoke",
            "passed": not should_fail,
            "expected": "Web 关键页面和交互符合目标",
            "actual": "mock 故意失败以触发下一轮 plan" if should_fail else "mock 通过",
            "detail": f"mock round={round_index}, fail_rounds={fail_rounds}, max_replans={max_replans}",
        }],
        "summary": (
            f"web mock 第 {round_index} 轮故意保留失败，驱动 critic 进入下一轮"
            if should_fail else f"web mock 第 {round_index} 轮通过，目标可收敛"
        ),
        "round_index": round_index,
        "mock": True,
    }


def _mock_device_run(inputs: dict, db=None) -> dict:
    """客户端(Android/iOS) UI mock：不占真机，pass/fail 由客户端源码 MOCK_BUG 驱动。"""
    round_index = _mock_round_index(db, inputs)
    fail_rounds = _int_value(inputs.get("mock_fail_rounds"), 0)
    should_fail = _mock_should_fail(inputs, "client", round_index, fail_rounds)
    return {
        "all_passed": not should_fail,
        "cases_passed": 0 if should_fail else 1,
        "cases_failed": 1 if should_fail else 0,
        "cases": [{
            "name": "mock_device_smoke",
            "passed": not should_fail,
            "expected": "客户端关键交互符合目标",
            "actual": "mock 故意失败以触发下一轮 plan" if should_fail else "mock 通过",
            "detail": f"mock round={round_index}",
        }],
        "summary": (f"客户端 mock 第 {round_index} 轮故意保留失败，驱动 critic 进入下一轮"
                    if should_fail else f"客户端 mock 第 {round_index} 轮通过，目标可收敛"),
        "round_index": round_index,
        "mock": True,
    }


def generate_ui_test_script(inputs: dict, model_id: str, log=print) -> str:
    """LLM 生成自包含 UI 自动化脚本。这里只生成代码，不占设备执行。"""
    from llm.llm_factory import LLMFactory

    package = inputs.get("package") or "com.example.app"
    scenario = inputs.get("scenario") or ""
    requirement = inputs.get("requirement_context") or ""
    regression_cases = inputs.get("regression_cases") or []
    device_caps = inputs.get("device_caps") or {}

    system = (
        "你是 Android UI 自动化测试专家。生成一个可直接运行的 Python3 pytest + uiautomator2 脚本。\n"
        "要求：\n"
        "1. 从环境变量 AVAILABLE_DEVICES 读取设备序列号。\n"
        "2. 从环境变量 DEVICE_TASK_OUTPUT_DIR 读取输出目录，截图和 result.json 都写到那里。\n"
        "3. 每个用例记录 {name, passed, expected, actual, detail}。\n"
        "4. 不要无限等待，单个控件等待要有超时。\n"
        "5. 只输出 Python 代码，不要解释。"
    )
    user = f"""## 目标/场景
{scenario[:1500]}

## 需求上下文
{requirement[:2500]}

## 回归点
{json.dumps(regression_cases, ensure_ascii=False, indent=2)[:3000]}

## App 包名
{package}

## 设备要求
{json.dumps(device_caps, ensure_ascii=False)}

生成完整 UI 自动化脚本。"""

    result = LLMFactory.generate(model_id, system, user)
    script = _strip_code_fence(result.get("text", ""))
    log(f"📝 生成 UI 测试脚本 {len(script)} 字符")
    return script


def execute_ui_script_gen(inputs: dict, db=None, model_id: str = "gemini_flash", log=print) -> dict:
    """UI 脚本生成闭环：生成脚本 → 落文件 → 进度事件。"""
    _emit_codegen_event(db, inputs, "ui_script_generating", "开始生成 UI 自动化测试脚本",
                        case_count=len(inputs.get("regression_cases") or []),
                        package=inputs.get("package", ""))
    script = generate_ui_test_script(inputs, model_id, log)
    _emit_codegen_event(db, inputs, "ui_script_generated",
                        f"UI 自动化脚本生成完成，约 {len(script)} 字符",
                        script_chars=len(script))

    root = os.path.join(UI_SCRIPT_OUTPUT_ROOT, inputs.get("goal_id") or inputs.get("req_id") or uuid.uuid4().hex[:8])
    script_id = uuid.uuid4().hex[:12]
    script_dir = os.path.join(root, script_id)
    os.makedirs(script_dir, exist_ok=True)
    script_path = os.path.join(script_dir, "test_main.py")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)

    _emit_codegen_event(db, inputs, "finished",
                        f"UI 自动化脚本已保存：{script_path}",
                        status="pass", script_path=script_path, script_chars=len(script))
    return {
        "test_result": "pass",
        "script_path": script_path,
        "covered_cases": inputs.get("regression_cases") or [],
        "summary": f"已生成 UI 自动化脚本 {script_id}",
        "ref": script_path,
        "confidence": 0.8,
        "no_change": False,
    }


def execute_web_test(inputs: dict, db=None, model_id: str = "gemini_flash", log=print) -> dict:
    """Web UI 基础验证：mock 多轮或在 device 网络内访问 base_url 做冒烟检查。"""
    base_url = inputs.get("base_url", "")
    mock_mode = _truthy(inputs.get("mock_mode"))
    if not base_url and not mock_mode:
        _emit_codegen_event(db, inputs, "blocked", "缺少 Web 测试环境 URL，Web UI 测试无法执行")
        return {"test_result": "blocked", "summary": "缺少 Web 测试环境 URL",
                "cases_passed": 0, "cases_failed": 0, "error": "no_base_url"}

    _emit_codegen_event(
        db, inputs, "web_test_start",
        "开始执行 Web UI 基础验证" if not mock_mode else "开始执行 Web UI mock 验证",
        base_url=base_url or "mock://web-test",
    )

    if mock_mode:
        result = _mock_web_run(inputs, db)
        status = "pass" if result.get("all_passed") else "fail"
        _emit_codegen_event(
            db, inputs, "mock_result",
            result.get("summary", ""),
            status=status,
            round_index=result.get("round_index"),
            cases_passed=result.get("cases_passed", 0),
            cases_failed=result.get("cases_failed", 0),
        )
    else:
        try:
            req = urllib.request.Request(
                base_url,
                headers={"User-Agent": "ai-service-goal-web-test/1.0"},
                method="GET",
            )
            started = time.time()
            with urllib.request.urlopen(req, timeout=20) as resp:
                status_code = getattr(resp, "status", 0) or resp.getcode()
                body = resp.read(8192).decode("utf-8", errors="ignore")
            elapsed_ms = int((time.time() - started) * 1000)
            ok = 200 <= int(status_code) < 400 and bool(body.strip())
            result = {
                "all_passed": ok,
                "cases_passed": 1 if ok else 0,
                "cases_failed": 0 if ok else 1,
                "status_code": status_code,
                "elapsed_ms": elapsed_ms,
                "cases": [{
                    "name": "web_entry_smoke",
                    "passed": ok,
                    "expected": "Web 测试环境首页可访问且返回页面内容",
                    "actual": f"HTTP {status_code}, body_len={len(body)}",
                    "detail": f"elapsed_ms={elapsed_ms}",
                }],
                "summary": (
                    f"Web 入口冒烟通过：HTTP {status_code}, {elapsed_ms}ms"
                    if ok else f"Web 入口冒烟失败：HTTP {status_code}, body_len={len(body)}"
                ),
            }
            status = "pass" if ok else "fail"
        except Exception as exc:
            result = {
                "all_passed": False,
                "cases_passed": 0,
                "cases_failed": 1,
                "error": str(exc)[:500],
                "cases": [{
                    "name": "web_entry_smoke",
                    "passed": False,
                    "expected": "Web 测试环境可访问",
                    "actual": str(exc)[:300],
                    "detail": "network_or_page_error",
                }],
                "summary": f"Web 入口冒烟失败：{str(exc)[:160]}",
            }
            status = "fail"

    root = os.path.join(WEB_TEST_OUTPUT_ROOT, inputs.get("goal_id") or inputs.get("req_id") or uuid.uuid4().hex[:8])
    os.makedirs(root, exist_ok=True)
    report_path = os.path.join(root, f"web_test_{uuid.uuid4().hex[:8]}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    cases_passed = int(result.get("cases_passed", 0))
    cases_failed = int(result.get("cases_failed", 0))
    _emit_codegen_event(
        db, inputs, "finished",
        f"Web UI 测试完成：{cases_passed} 通过 / {cases_failed} 失败",
        status=status,
        cases_passed=cases_passed,
        cases_failed=cases_failed,
        report_path=report_path,
    )
    return {
        "test_result": status,
        "cases_passed": cases_passed,
        "cases_failed": cases_failed,
        "summary": result.get("summary", ""),
        "detail": json.dumps(result.get("cases", []), ensure_ascii=False)[:3000],
        "report": json.dumps(result, ensure_ascii=False)[:6000],
        "ref": report_path,
        "confidence": 0.75 if status == "pass" else 0.55,
        "no_change": False,
    }


def execute_device_test(inputs: dict, db=None, model_id: str = "gemini_flash", log=print) -> dict:
    """客户端(Android/iOS) UI 测试：生成 UI 脚本(展示自动生成代码) + mock 多轮(MOCK_BUG 客户端代码驱动)。
    非 mock 模式无真机/APK 环境 → 诚实 blocked（演示用 mock）。"""
    mock_mode = _truthy(inputs.get("mock_mode"))
    _emit_codegen_event(db, inputs, "device_test_start",
                        "开始客户端 UI 自动化验证" + ("(mock)" if mock_mode else ""))
    # 展示"自动生成代码"：生成客户端 UI 脚本
    _emit_codegen_event(db, inputs, "ui_script_generating", "开始生成客户端 UI 自动化脚本",
                        case_count=len(inputs.get("acceptance") or []))
    script = generate_ui_test_script(inputs, model_id, log)
    _emit_codegen_event(db, inputs, "ui_script_generated",
                        f"客户端 UI 脚本生成完成，约 {len(script)} 字符", script_chars=len(script))
    root = os.path.join("/tmp/device_ui_test",
                        inputs.get("goal_id") or inputs.get("req_id") or uuid.uuid4().hex[:8])
    os.makedirs(root, exist_ok=True)
    script_path = os.path.join(root, f"device_ui_{uuid.uuid4().hex[:8]}.py")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)

    if not mock_mode:
        _emit_codegen_event(db, inputs, "blocked", "客户端真机执行需设备+APK 环境（当前非 mock）")
        return {"test_result": "blocked", "summary": "客户端真机执行需设备/APK 环境",
                "cases_passed": 0, "cases_failed": 0, "ref": script_path, "no_change": False}

    result = _mock_device_run(inputs, db)
    status = "pass" if result.get("all_passed") else "fail"
    cases_passed = int(result.get("cases_passed", 0))
    cases_failed = int(result.get("cases_failed", 0))
    _emit_codegen_event(db, inputs, "mock_result", result.get("summary", ""), status=status,
                        round_index=result.get("round_index"),
                        cases_passed=cases_passed, cases_failed=cases_failed)
    _emit_codegen_event(db, inputs, "finished",
                        f"客户端 UI 测试完成：{cases_passed} 通过 / {cases_failed} 失败",
                        status=status, cases_passed=cases_passed, cases_failed=cases_failed,
                        script_path=script_path)
    return {
        "test_result": status, "cases_passed": cases_passed, "cases_failed": cases_failed,
        "summary": result.get("summary", ""),
        "detail": json.dumps(result.get("cases", []), ensure_ascii=False)[:3000],
        "report": json.dumps(result, ensure_ascii=False)[:6000],
        "ref": script_path, "confidence": 0.75 if status == "pass" else 0.55, "no_change": False,
    }


# ==================== 核心编排（可独立运行，真实验证用）====================

def execute_api_test(inputs: dict, db=None, model_id: str = "gemini_flash", log=print) -> dict:
    """api_test 全闭环。inputs 由 step_input_resolver 组装。返回符合 api_test 契约的 output。"""
    base_url = inputs.get("base_url", "")
    repo_path = inputs.get("repo_path", "")
    mock_mode = _truthy(inputs.get("mock_mode"))
    if not base_url and not mock_mode:
        _emit_codegen_event(db, inputs, "blocked", "缺少 base_url，API 测试无法连接测试环境")
        return {"test_result": "blocked", "summary": "缺少 base_url，无法连测试环境",
                "cases_passed": 0, "cases_failed": 0, "error": "no_base_url"}
    if mock_mode and not base_url:
        base_url = "mock://api-test"

    work_dir = os.path.join(OUTPUT_ROOT, inputs.get("goal_id") or inputs.get("req_id") or uuid.uuid4().hex[:8],
                            f"run_{uuid.uuid4().hex[:6]}")

    # 1. 接口文档 → 受影响接口。正常路径：branch_review 已经产出 interface_doc；
    #    旧数据/上游没产文档时，保留扫路由+LLM 推断作为兼容兜底。
    interface_doc = _load_interface_doc(inputs)
    endpoints = _endpoints_from_interface_doc(interface_doc)
    if endpoints:
        log(f"📄 使用代码分析产出的接口文档，受影响接口 {len(endpoints)} 个: {[e.get('path') for e in endpoints]}")
        _emit_codegen_event(
            db, inputs, "interface_doc",
            f"读取上游代码分析产出的接口契约，定位 {len(endpoints)} 个受影响接口",
            endpoints=[{"method": e.get("method"), "path": e.get("path"),
                        "impact": e.get("impact", "")} for e in endpoints[:20]],
        )
    else:
        routes = discover_routes(repo_path)
        log(f"🔍 未拿到接口文档，降级扫描后端路由 {len(routes)} 条")
        _emit_codegen_event(
            db, inputs, "route_scan",
            f"未拿到接口契约，降级扫描后端路由 {len(routes)} 条并推断影响面",
            route_count=len(routes),
        )
        endpoints = extract_affected_endpoints(inputs, routes, model_id, log)
        if endpoints:
            interface_doc = {"affected_endpoints": endpoints, "summary": "api_test fallback 生成的接口影响面"}
    if not endpoints and mock_mode:
        # mock 演示：爆炸范围已判定要跑 api，端点定位不到就用全量路由扫描兜底，保证 mock 能跑
        routes = discover_routes(repo_path)
        endpoints = [{"method": "ANY", "path": r.get("endpoint", "/")} for r in routes] \
            or [{"method": "ANY", "path": "/mock"}]
        interface_doc = {"affected_endpoints": endpoints, "summary": "mock 全量路由兜底"}
        _emit_codegen_event(db, inputs, "mock_endpoints_fallback",
                            f"mock：未定位到受影响接口，用全量路由兜底 {len(endpoints)} 个端点",
                            endpoint_count=len(endpoints))
    if not endpoints:
        # 无法定位受影响接口 → 诚实 blocked（非 fail：不是接口坏，是没东西可测）
        _emit_codegen_event(db, inputs, "blocked", "未能从变更定位受影响接口，API 测试停止")
        return {"test_result": "blocked", "summary": "未能从变更定位到受影响接口",
                "cases_passed": 0, "cases_failed": 0, "endpoints": []}

    # 2. 脚本：优先复用已持久化套件，否则动态生成
    force_regenerate = mock_mode and _truthy(inputs.get("mock_regenerate_each_round"))
    script = None if force_regenerate else load_persisted_script(db, inputs, endpoints)
    reused = bool(script)
    if not script:
        if force_regenerate:
            _emit_codegen_event(
                db, inputs, "script_regenerate_forced",
                "mock 演示模式开启每轮重生成，跳过已持久化脚本复用",
                endpoint_count=len(endpoints),
            )
        _emit_codegen_event(
            db, inputs, "script_generating",
            f"开始为 {len(endpoints)} 个接口动态生成 requests 测试脚本",
            endpoint_count=len(endpoints),
        )
        script = generate_test_script(inputs, endpoints, model_id, log)
        _emit_codegen_event(
            db, inputs, "script_generated",
            f"测试脚本生成完成，约 {len(script)} 字符",
            script_chars=len(script),
            endpoint_count=len(endpoints),
        )
    else:
        log("♻️ 复用已持久化的测试脚本")
        _emit_codegen_event(
            db, inputs, "script_reused",
            f"复用该需求已持久化的接口测试脚本，约 {len(script)} 字符",
            script_chars=len(script),
            endpoint_count=len(endpoints),
        )

    # 3. 跑 + 自愈
    attempts = []
    _emit_codegen_event(db, inputs, "script_running", "开始执行接口测试脚本", attempt=1)
    attempt_no = 1
    if mock_mode:
        result = _mock_api_run(inputs, endpoints, db)
        cases_passed = int(result.get("cases_passed", 0))
        cases_failed = int(result.get("cases_failed", 0))
        status = "pass" if result.get("all_passed") else "fail"
        summary = result.get("summary", "")
        attempts.append({"attempt": attempt_no, "outcome": status, "passed": cases_passed,
                         "failed": cases_failed, "mock": True})
        _emit_codegen_event(
            db, inputs, "mock_result",
            summary,
            status=status,
            attempt=attempt_no,
            round_index=result.get("round_index"),
            cases_passed=cases_passed,
            cases_failed=cases_failed,
        )
        run = {"script_error": False, "result": result}
    else:
        run = _run_script(script, base_url, work_dir)

    while run["script_error"] and attempt_no <= MAX_HEAL:
        attempts.append({"attempt": attempt_no, "outcome": "script_error", "stderr": run["stderr"][:500]})
        log(f"⚠️ 脚本崩溃(第{attempt_no}次)，自愈重跑：{run['stderr'][:120]}")
        _emit_codegen_event(
            db, inputs, "script_error",
            f"脚本第 {attempt_no} 次运行崩溃，进入自愈",
            attempt=attempt_no,
            error=run["stderr"][:800],
        )
        script = heal_script(script, run["stderr"], model_id, log)
        _emit_codegen_event(
            db, inputs, "script_healed",
            f"自愈生成修复脚本，约 {len(script)} 字符",
            attempt=attempt_no,
            script_chars=len(script),
        )
        _emit_codegen_event(db, inputs, "script_running", "重新执行自愈后的测试脚本", attempt=attempt_no + 1)
        run = _run_script(script, base_url, work_dir)
        attempt_no += 1

    # 4. 结论
    if mock_mode:
        result = run["result"] or {}
    elif run["script_error"]:
        status, summary = "fail", f"脚本自愈 {MAX_HEAL} 次后仍无法运行：{run['stderr'][:200]}"
        cases_passed = cases_failed = 0
        result = {}
    else:
        result = run["result"] or {}
        cases_passed = int(result.get("cases_passed", 0))
        cases_failed = int(result.get("cases_failed", 0))
        all_passed = bool(result.get("all_passed")) and cases_failed == 0
        status = "pass" if all_passed else "fail"
        summary = result.get("summary", "") or f"{cases_passed} 通过 / {cases_failed} 失败"
    if not mock_mode:
        attempts.append({"attempt": attempt_no, "outcome": status, "passed": cases_passed, "failed": cases_failed})

    # 5. 持久化套件 + 运行历史
    run_record = {"at": int(time.time()), "status": status, "attempts": attempts,
                  "reused_script": reused, "cases_passed": cases_passed, "cases_failed": cases_failed}
    suite_id = persist_suite(db, inputs, endpoints, script, run_record, log)
    _emit_codegen_event(
        db, inputs, "finished",
        f"API 测试完成：{cases_passed} 通过 / {cases_failed} 失败",
        status=status,
        cases_passed=cases_passed,
        cases_failed=cases_failed,
        heal_attempts=len(attempts) - 1,
        suite_id=suite_id,
    )

    return {
        "test_result": status,
        "cases_passed": cases_passed,
        "cases_failed": cases_failed,
        "endpoints": endpoints,
        "interface_doc": interface_doc,
        "summary": summary,
        "detail": json.dumps(result.get("cases", []), ensure_ascii=False)[:3000],
        "report": json.dumps(result, ensure_ascii=False)[:6000],
        "suite_id": suite_id,
        "heal_attempts": len(attempts) - 1,
        "ref": run.get("script_path", ""),
        "result_ref": run.get("result_path", ""),
        "no_change": False,
    }


# ==================== device_worker 任务包装 ====================

class ApiTestTask(BaseDeviceTask):
    """task_type=40：device 侧 Goal 能力分发（api_test / web_test / script_gen）。"""
    notify_level = NOTIFY_SILENT

    async def run(self):
        import asyncio
        capability = self.payload.get("capability_key", "")
        if capability not in ("api_test", "web_test", "device_test", "script_gen"):
            await self._save_result("fail", f"device 端暂不支持能力: {capability}")
            return {"test_result": "fail", "error": f"unsupported_capability:{capability}"}

        inputs = dict(self.payload.get("inputs") or {})
        inputs.setdefault("goal_id", self.payload.get("goal_id", ""))
        inputs.setdefault("req_id", self.payload.get("req_id", ""))
        inputs.setdefault("step_id", self.payload.get("step_id", ""))
        inputs.setdefault("task_id", self.task_id)
        model_id = self.payload.get("model_id", "gemini_flash")

        await self.log(f"═══ Goal 设备侧能力启动 | capability={capability} ═══", level="info")
        db = self.db
        if capability == "api_test":
            output = await asyncio.to_thread(execute_api_test, inputs, db, model_id,
                                             lambda m, **k: print(f"[api_test {self.task_id}] {m}"))
        elif capability == "web_test":
            output = await asyncio.to_thread(execute_web_test, inputs, db, model_id,
                                             lambda m, **k: print(f"[web_test {self.task_id}] {m}"))
        elif capability == "device_test":
            output = await asyncio.to_thread(execute_device_test, inputs, db, model_id,
                                             lambda m, **k: print(f"[device_test {self.task_id}] {m}"))
        else:
            output = await asyncio.to_thread(execute_ui_script_gen, inputs, db, model_id,
                                             lambda m, **k: print(f"[script_gen {self.task_id}] {m}"))
        await self._save_result(output.get("test_result", "fail"), output.get("summary", ""), output)
        await self.log(f"✅ {capability} 完成: {output.get('summary','')[:80]}", level="success")
        return output

    async def _save_result(self, status, error="", result=None):
        if self.db is None:
            return
        col = self.db[self.device_mgr.config["collections"]["task_results"]] if self.device_mgr is not None else self.db["device_task_results"]
        col.replace_one({"task_id": self.task_id}, {
            "task_id": self.task_id, "device_id": self.device_id or "",
            "status": status, "summary": (result or {}).get("summary", ""),
            "detail": (result or {}).get("detail", ""), "error": error,
            "report": (result or {}).get("report", ""),
            "output": result or {},
            "created_at": int(time.time()),
        }, upsert=True)
