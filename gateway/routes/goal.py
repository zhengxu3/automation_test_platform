"""Goal（Goal Runtime 驱动模式）路由

routes 只收请求 + 调 engine，不做编排逻辑。
统一入口：手动创建 / webhook 都走 /create，传同一套 sources。
"""
import time
import uuid
import threading

from flask import Blueprint, request
from common.auth import require_auth, get_current_user
from common.response import ok, err
from common.db import get_collection

bp = Blueprint('goal', __name__)


def _resolve_file_sources(sources):
    """解析文件引用型 source：带 file_id 的 doc/testcase → 读文件内容注入。"""
    from gateway.routes.upload import read_file_content
    resolved = []
    for src in sources:
        stype = src.get("type", "")
        file_id = src.get("file_id")
        if file_id and stype in ("doc", "testcase"):
            info = read_file_content(file_id)
            if info:
                resolved.append({
                    **src,
                    "type": info["category"],  # 以文件 category 为准
                    "content": info["content"],
                    "filename": info["filename"],
                })
            else:
                # 文件找不到：保留原始 source 但标记
                resolved.append({**src, "content": f"(文件 {file_id} 不存在或无法读取)"})
        else:
            resolved.append(src)
    return resolved


def _resolve_local_paths(sources):
    """repo source 缺 local_path 时，用 repo_id / git_url 去 ai_git_repos 反查已克隆的本地仓。

    产品入口"传库地址"只是为了定位本地仓和分支：本地已有就直接复用、不再触发 clone。
    （webhook 已这么做，这里给手动建任务/页面创建补齐同样的反查。）
    """
    repos = get_collection("ai_git_repos")
    for s in sources:
        if s.get("type") != "repo" or s.get("local_path"):
            continue
        repo = None
        if s.get("repo_id"):
            repo = repos.find_one({"repo_id": s["repo_id"]}, {"_id": 0})
        if not repo:
            gu = s.get("git_url") or s.get("repo_url") or s.get("git")
            if gu:
                repo = repos.find_one(
                    {"$or": [{"git_url": gu}, {"repo_url": gu}, {"url": gu}]}, {"_id": 0})
        if repo and repo.get("local_path"):
            s["local_path"] = repo["local_path"]
            if not s.get("repo_id") and repo.get("repo_id"):
                s["repo_id"] = repo["repo_id"]
    return sources


_ENV_SOURCE_KEYS = (
    "base_url", "web_url", "apk_source", "apk_path", "test_accounts", "test_data",
    "device_profile", "device_id", "api_test_mock", "mock_api_test",
    "web_test_mock", "mock_web_test", "device_test_mock", "mock_device_test",
    "mock_fail_rounds", "api_test_mock_fail_rounds", "web_test_mock_fail_rounds",
    "device_test_mock_fail_rounds", "mock_regenerate_each_round",
)


def _webhook_changed_files(data):
    """从通用/GitLab 风格 webhook payload 提取改动文件清单。"""
    files = []
    for key in ("changed_files", "files"):
        val = data.get(key)
        if isinstance(val, list):
            files.extend(str(x) for x in val if x)
    for c in data.get("commits", []) or []:
        if not isinstance(c, dict):
            continue
        for key in ("added", "modified", "removed"):
            files.extend(str(x) for x in (c.get(key) or []) if x)
    return sorted(set(files))


def _environment_source_from_webhook(data, repo=None):
    """webhook 新建守护 goal 时继承默认环境。

    优先级：repo 注册信息中的 environment/顶层环境字段 < webhook payload 顶层/env/environment。
    没有真实配置就不合成环境，避免把 static-only 伪装成可执行。
    """
    env = {}
    repo = repo or {}
    if isinstance(repo.get("environment"), dict):
        env.update({k: v for k, v in repo["environment"].items() if k in _ENV_SOURCE_KEYS and v not in (None, "", [])})
    env.update({k: repo[k] for k in _ENV_SOURCE_KEYS if repo.get(k) not in (None, "", [])})

    raw = {}
    if isinstance(data.get("env"), dict):
        raw.update(data["env"])
    if isinstance(data.get("environment"), dict):
        raw.update(data["environment"])
    raw.update({k: data[k] for k in _ENV_SOURCE_KEYS if data.get(k) not in (None, "", [])})
    env.update({k: v for k, v in raw.items() if k in _ENV_SOURCE_KEYS and v not in (None, "", [])})

    if not env:
        return None
    return {"type": "environment", **env}


def _run_async(goal_id):
    """后台跑 discover_and_plan（LLM 较慢，不阻塞请求）"""
    def task():
        try:
            from engine.goal_runtime import discover_and_plan
            discover_and_plan(goal_id)
        except Exception as e:
            from engine import state
            db = get_collection("ai_goals").database
            try:
                state.emit_event(db, goal_id, "runtime_error", {"error": str(e)[:300]}, actor="system")
                get_collection("ai_goals").update_one(
                    {"goal_id": goal_id}, {"$set": {"status": "blocked", "error": str(e)[:300]}}
                )
            except Exception:
                pass
    threading.Thread(target=task, daemon=True).start()


def _trigger_round_async(goal_id, reason, **change_ctx):
    """后台异步起新一轮（webhook 激活用）——钩子只告知，不阻塞在 LLM 规划上。"""
    def task():
        try:
            from engine import goal_runtime
            goal_runtime.trigger_code_update_round(goal_id, reason=reason, **change_ctx)
        except Exception as e:
            from engine import state
            db = get_collection("ai_goals").database
            try:
                state.emit_event(db, goal_id, "runtime_error", {"error": str(e)[:300]}, actor="system")
            except Exception:
                pass
    threading.Thread(target=task, daemon=True).start()


@bp.route('/create', methods=['POST'])
@require_auth
def goal_create():
    """创建 Goal — 统一入口（手动/webhook）。

    sources 例:
      [{"type": "doc", "content": "..."},
       {"type": "repo", "repo_id": "x", "branch": "dev", "local_path": "...", "role": "android_client"},
       {"type": "environment", "base_url": "...", "apk_source": "...", "test_accounts": [...]}]
    """
    data = request.get_json() or {}

    # 防连点去重：同 title 5 秒内不重复创建
    title = (data.get("title") or "").strip()
    if title:
        recent = get_collection("ai_goals").find_one(
            {"title": title, "created_at": {"$gte": int(time.time()) - 5}},
            {"_id": 0, "goal_id": 1}
        )
        if recent:
            return ok({"goal_id": recent["goal_id"], "deduplicated": True})

    # 兼容旧字段 + 新 sources 模型
    sources = data.get("sources", [])
    if not sources:
        # 从旧字段组装 sources
        if data.get("doc_content"):
            sources.append({"type": "doc", "content": data["doc_content"]})
        if data.get("raw_input"):
            sources.append({"type": "user_desc", "content": data["raw_input"]})
        for repo in data.get("repos", []):
            if isinstance(repo, dict):
                sources.append({"type": "repo", **repo})

    # 解析文件引用：file_id → 读取内容注入 source
    sources = _resolve_file_sources(sources)

    # 给地址型 repo 源补本地路径（本地已有则复用、不重 clone）
    sources = _resolve_local_paths(sources)

    # 守护模式自动给 repo 加 watch
    if data.get("completion_policy") == "continuous":
        for s in sources:
            if s.get("type") == "repo" and "watch" not in s:
                s["watch"] = True

    goal_id = f"goal_{uuid.uuid4().hex[:8]}"
    doc = {
        "goal_id": goal_id,
        "title": data.get("title", ""),
        "trigger": data.get("trigger", "manual"),         # manual | webhook | cron
        "completion_policy": data.get("completion_policy", "auto_complete"),  # auto_complete | continuous
        "auto_replan": data.get("auto_replan", True),   # False=外部驱动(失败停 partial 等下次触发)
        "sources": sources,
        "req_id": data.get("req_id", ""),
        "permissions": data.get("permissions", {}),       # 批量授权清单
        "budget": data.get("budget", {
            "max_tokens": 200000, "max_steps": 20, "max_replans": 3,
            "max_runtime_sec": 3600, "max_device_minutes": 30,
        }),
        "callback_urls": data.get("callback_urls", []),   # 出站钩子
        "notifications": data.get("notifications", []),  # 钉钉/飞书通知配置
        "status": "discovering",
        "goal_statement": "",
        "acceptance": [],
        "feasibility": {},
        "plan_version": 1,
        "round": 1,
        "replan_count": 0,
        "created_by": get_current_user(),
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    get_collection("ai_goals").insert_one(doc)

    # 异步触发可行性画像 + 目标生成 + 规划
    _run_async(goal_id)

    return ok({"goal_id": goal_id, "status": "discovering"})


@bp.route('/webhook', methods=['POST'])
def goal_webhook():
    """入站钩子 — Git push / CI 触发（无需登录，内部调用）。

    create-or-reactivate（和手动创建同源，只多一层判断）：
      查找监控该 repo@branch 的活跃守护 goal →
        - 有且提交码不同 → 逐个激活(trigger_code_update_round) 重新按目标检测；
        - 没有能处理的 → 创建一个新的 continuous 守护目标任务。
    钩子只做"查找 + 分发"，重活在 goal 运行时异步做。
    """
    import os
    data = request.get_json() or {}
    repo_id = data.get("repo_id", "")
    branch = data.get("branch", "")
    before = data.get("before", "")
    commit = data.get("commit", "") or data.get("after", "")
    changed_files = _webhook_changed_files(data)

    if not repo_id:
        return err("缺少 repo_id")

    # 鉴权（此端点将外放，生产必须设钩子 token）。优先环境变量 GOAL_HOOK_TOKEN，
    # 回退设置页保存的 DB token（ai_settings.key=hook，免改 env 重启）。兼容 GitLab 原生 secret(X-Gitlab-Token)。
    hook_token = os.getenv("GOAL_HOOK_TOKEN", "")
    if not hook_token:
        cfg = get_collection("ai_settings").find_one({"key": "hook"}, {"_id": 0, "token": 1})
        hook_token = (cfg or {}).get("token", "") or ""
    if hook_token:
        provided = request.headers.get("X-Hook-Token", "") or request.headers.get("X-Gitlab-Token", "")
        if provided != hook_token:
            return err("hook token 无效", 401)

    # ===== 1. 查找：监控该 repo 的"活监控"（非终态都算，避免在途时误建重复）=====
    reactivatable = {"guarding", "partial_completed", "paused", "blocked"}
    non_terminal = ["discovering", "planning", "running", "verifying", "replanning",
                    "awaiting_approval", "guarding", "partial_completed", "paused", "blocked"]
    live = list(get_collection("ai_goals").find(
        {"status": {"$in": non_terminal},
         "sources": {"$elemMatch": {"type": "repo", "repo_id": repo_id}}},
        {"_id": 0, "goal_id": 1, "status": 1, "sources": 1}
    ))

    # ===== 2. 有活监控 → 绝不新建；异步激活其中"可重触发 + 提交码不同"的（多个就 fan-out）=====
    if live:
        activated, busy = [], []
        for g in live:
            src = next((s for s in g.get("sources", [])
                        if s.get("type") == "repo" and s.get("repo_id") == repo_id), None)
            if not src:
                continue
            if branch and src.get("branch") and src.get("branch") != branch:
                continue
            if g["status"] not in reactivatable:
                busy.append(g["goal_id"])           # 在途，本次不动（下次 push 或跑完再追）
                continue
            if commit and src.get("commit") and src.get("commit") == commit:
                continue                            # 该 goal 已验过此提交码 → 跳过
            # 回写已验证到的提交码（去重）+ 后台异步触发（钩子只告知，不阻塞在 LLM 规划上）
            get_collection("ai_goals").update_one(
                {"goal_id": g["goal_id"], "sources.repo_id": repo_id},
                {"$set": {"sources.$.commit": commit, "sources.$.last_before": before}})
            _trigger_round_async(
                g["goal_id"],
                f"代码提交激活{(' ' + commit[:8]) if commit else ''}",
                changed_repo_id=repo_id,
                before_ref=before,
                after_ref=commit,
                changed_files=changed_files if changed_files else None,
            )
            # 发钉钉：代码提交检测通知（仅无 watcher 时发，避免重复）
            _src = next((s for s in g.get("sources", []) if s.get("repo_id") == repo_id), {})
            if not _src.get("watch"):
                try:
                    from common.notify import notify_code_detected
                    _git_url = _src.get("git_url", "")
                    _repo_label = _git_url.split("/")[-1].replace(".git", "") if "/" in _git_url else repo_id
                    _role = _src.get("role", "")
                    _display = f"{_role + ' · ' if _role else ''}{_repo_label}"
                    notify_code_detected(g, repo_name=_display, branch=branch or _src.get("branch", ""), commit=commit or "", message=(data.get("commits", [{}])[0].get("message", "") if data.get("commits") else ""))
                except Exception:
                    pass
            activated.append(g["goal_id"])
        return ok({"action": "activated" if activated else "no_change",
                   "activated": activated, "busy": busy})

    # ===== 3. 无活监控 → 建一个新的 continuous 守护目标任务 =====
    repo = get_collection("ai_git_repos").find_one({"repo_id": repo_id}, {"_id": 0})
    local_path = repo.get("local_path", "") if repo else ""
    sources = [{"type": "repo", "repo_id": repo_id, "branch": branch,
                "commit": commit, "last_before": before, "local_path": local_path,
                "role": data.get("role", "") or (repo or {}).get("role", "")}]
    env_source = _environment_source_from_webhook(data, repo)
    if env_source:
        sources.append(env_source)

    goal_id = f"goal_{uuid.uuid4().hex[:8]}"
    get_collection("ai_goals").insert_one({
        "goal_id": goal_id,
        "title": data.get("title", f"代码提交守护 {repo_id}/{branch}"),
        "trigger": "webhook",
        "completion_policy": "continuous",   # 守护：长期监听后续 push
        "auto_replan": data.get("auto_replan", True),
        "sources": sources,
        "status": "discovering",
        "plan_version": 1,
        "round": 1,
        "replan_count": 0,
        "created_by": "webhook",
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    })
    _run_async(goal_id)
    return ok({"action": "created", "goal_id": goal_id, "status": "discovering"})


@bp.route('/list', methods=['GET'])
@require_auth
def goal_list():
    """Goal 列表（分页 + 瘦投影）。

    列表只取展示必要字段，避免把 sources/acceptance/feasibility/events 等大字段全拉回来
    （这是 goal 多了之后列表变慢的主因）。
    """
    try:
        page = max(int(request.args.get('page', 1)), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(max(int(request.args.get('page_size', 20)), 1), 100)
    except (TypeError, ValueError):
        page_size = 20

    col = get_collection("ai_goals")
    total = col.count_documents({})
    projection = {"_id": 0, "goal_id": 1, "title": 1, "goal_statement": 1,
                  "status": 1, "trigger": 1, "completion_policy": 1, "created_at": 1}
    goals = list(col.find({}, projection)
                 .sort("created_at", -1)
                 .skip((page - 1) * page_size)
                 .limit(page_size))
    return ok({"goals": goals, "total": total, "page": page, "page_size": page_size})


@bp.route('/detail', methods=['GET'])
@require_auth
def goal_detail():
    """返回 Goal 完整信息：goal + steps + events（供前端 DAG/决策流展示）"""
    goal_id = request.args.get('goal_id', '')
    if not goal_id:
        return err("缺少 goal_id")
    from engine.goal_runtime import get_goal_full
    full = get_goal_full(goal_id)
    if not full:
        return err("Goal 不存在", 404)
    return ok(full)


@bp.route('/approve', methods=['POST'])
@require_auth
def goal_approve():
    """审批通过 — awaiting_approval → running"""
    data = request.get_json() or {}
    goal_id = data.get('goal_id', '')
    if not goal_id:
        return err("缺少 goal_id")

    from engine import state
    db = get_collection("ai_goals").database
    goal = get_collection("ai_goals").find_one({"goal_id": goal_id}, {"_id": 0})
    if not goal:
        return err("Goal 不存在", 404)

    try:
        state.transition(db, "ai_goals", "goal_id", goal_id, "goal", "running",
                         "人工批准计划", actor="human")
        # 审批留痕
        get_collection("ai_goal_approvals").insert_one({
            "approval_id": f"appr_{uuid.uuid4().hex[:8]}",
            "goal_id": goal_id,
            "scope": "goal",
            "approved_by": get_current_user(),
            "approved_at": int(time.time()),
            "reason": data.get("reason", "批准计划执行"),
        })
        # 关键：审批通过即启动 DAG，提交首批就绪 step
        from engine import goal_scheduler
        goal_scheduler.advance(goal_id)
    except Exception as e:
        return err(f"审批失败: {str(e)[:100]}")

    return ok({"status": "running"})


@bp.route('/pause', methods=['POST'])
@require_auth
def goal_pause():
    """暂停 Goal — 不强杀已派任务，只阻止后续推进。"""
    data = request.get_json() or {}
    goal_id = data.get('goal_id', '')
    if not goal_id:
        return err("缺少 goal_id")
    from engine import goal_runtime
    result = goal_runtime.pause_goal(
        goal_id,
        reason=data.get("reason", "人工暂停"),
        actor=get_current_user() or "human",
    )
    if not result.get("ok"):
        return err(f"暂停失败: {result.get('error', '')[:120]}")
    return ok(result)


@bp.route('/resume', methods=['POST'])
@require_auth
def goal_resume():
    """恢复 Goal — paused → running，并补一次 DAG 推进。"""
    data = request.get_json() or {}
    goal_id = data.get('goal_id', '')
    if not goal_id:
        return err("缺少 goal_id")
    from engine import goal_runtime
    result = goal_runtime.resume_goal(
        goal_id,
        reason=data.get("reason", "人工恢复"),
        actor=get_current_user() or "human",
    )
    if not result.get("ok"):
        return err(f"恢复失败: {result.get('error', '')[:120]}")
    return ok(result)


@bp.route('/cancel', methods=['POST'])
@require_auth
def goal_cancel():
    """取消 Goal — 转终态 cancelled，并停止后续调度。"""
    data = request.get_json() or {}
    goal_id = data.get('goal_id', '')
    if not goal_id:
        return err("缺少 goal_id")
    from engine import goal_runtime
    result = goal_runtime.cancel_goal(
        goal_id,
        reason=data.get("reason", "人工取消"),
        actor=get_current_user() or "human",
    )
    if not result.get("ok"):
        return err(f"取消失败: {result.get('error', '')[:120]}")
    return ok(result)


@bp.route('/decisions', methods=['GET'])
@require_auth
def goal_decisions():
    """获取 Goal 的事件流/决策历史（可回放）"""
    goal_id = request.args.get('goal_id', '')
    if not goal_id:
        return err("缺少 goal_id")
    # 取最新窗口再正序返回。升序+limit 会在事件超过上限后永远卡在最早 N 条。
    events = list(get_collection("ai_goal_events").find(
        {"goal_id": goal_id}, {"_id": 0}
    ).sort([("timestamp", -1), ("_id", -1)]).limit(300))
    events.reverse()
    return ok({"events": events})


@bp.route('/step_callback', methods=['POST'])
def goal_step_callback():
    """Goal step 完成回调 — 供远程 device_worker HTTP 调用。

    ai_worker 同进程直接调 scheduler.on_step_done；
    device_worker 远程，通过此接口触发同一套推进逻辑（绑证据+Steward评估+推进DAG）。
    无需登录（设备机内部调用，生产可加 IP 白名单/内部 token）。
    """
    data = request.get_json() or {}
    goal_id = data.get('goal_id', '')
    step_id = data.get('step_id', '')
    output = data.get('output', {})
    success = data.get('success', None)
    if not goal_id or not step_id:
        return err("缺少 goal_id 或 step_id")

    try:
        from engine import goal_scheduler
        result = goal_scheduler.on_step_done(goal_id, step_id, output, success)
        return ok({"advanced": result})
    except Exception as e:
        return err(f"回调处理失败: {str(e)[:200]}")


@bp.route('/chat', methods=['POST'])
@require_auth
def goal_chat():
    """与记忆体(Steward)交谈 — 基于 Goal 的目标/记忆/证据回答"""
    data = request.get_json() or {}
    goal_id = data.get('goal_id', '')
    question = data.get('question', '')
    if not goal_id or not question:
        return err("缺少 goal_id 或 question")

    goal = get_collection("ai_goals").find_one({"goal_id": goal_id}, {"_id": 0})
    if not goal:
        return err("Goal 不存在", 404)

    # 收集上下文：目标 + 验收 + 记忆 + 步骤状态
    acc_text = "\n".join(
        f"- {a['desc']} [{a.get('verdict', 'pending')}]" for a in goal.get("acceptance", [])
    ) or "（无验收点）"

    memories = list(get_collection("ai_memory_points").find(
        {"goal_id": goal_id}, {"_id": 0, "summary": 1, "layer": 1}
    ).sort("created_at", -1).limit(15))
    mem_text = "\n".join(f"[{m.get('layer', '?')}] {m['summary']}" for m in memories) or "（暂无记忆）"

    steps = list(get_collection("ai_goal_steps").find(
        {"goal_id": goal_id}, {"_id": 0, "name": 1, "status": 1}
    ))
    step_text = "\n".join(f"- {s['name']}: {s['status']}" for s in steps) or "（无步骤）"

    from llm.llm_factory import LLMFactory
    system = (
        "你是这个 Goal 的记忆体主管(Steward)。基于目标、验收点、记忆、步骤状态回答用户问题。"
        "回答简洁专业，找不到信息就说'当前记忆中没有相关信息'。"
    )
    user = f"""目标：{goal.get('goal_statement', goal.get('title', ''))}
状态：{goal.get('status')}

验收点：
{acc_text}

执行步骤：
{step_text}

记忆：
{mem_text}

用户问题：{question}"""

    try:
        result = LLMFactory.generate("gemini_flash", system, user)
        answer = result.get("text", "")
    except Exception as e:
        return err(f"对话失败: {str(e)[:100]}")

    # 留痕对话
    import time as _t
    get_collection("ai_goal_conversations").insert_one({
        "goal_id": goal_id, "role": "user", "content": question, "timestamp": int(_t.time())
    })
    get_collection("ai_goal_conversations").insert_one({
        "goal_id": goal_id, "role": "steward", "content": answer, "timestamp": int(_t.time())
    })

    return ok({"answer": answer})


@bp.route('/chat/history', methods=['GET'])
@require_auth
def goal_chat_history():
    """对话历史"""
    goal_id = request.args.get('goal_id', '')
    if not goal_id:
        return err("缺少 goal_id")
    msgs = list(get_collection("ai_goal_conversations").find(
        {"goal_id": goal_id}, {"_id": 0}
    ).sort("timestamp", 1).limit(50))
    return ok({"messages": msgs})


@bp.route('/update_watch', methods=['POST'])
@require_auth
def goal_update_watch():
    """更新 Goal 某个 repo source 的自监控配置。"""
    data = request.get_json() or {}
    goal_id = data.get('goal_id', '')
    repo_index = data.get('repo_index')
    if not goal_id or repo_index is None:
        return err("缺少 goal_id 或 repo_index")

    goal = get_collection("ai_goals").find_one({"goal_id": goal_id}, {"_id": 0, "sources": 1})
    if not goal:
        return err("Goal 不存在", 404)

    sources = goal.get("sources", [])
    idx = int(repo_index)
    # 找到第 idx 个 repo 类型 source
    repo_indices = [i for i, s in enumerate(sources) if s.get("type") == "repo"]
    if idx < 0 or idx >= len(repo_indices):
        return err("repo_index 越界")
    real_idx = repo_indices[idx]

    update = {}
    if "watch" in data:
        update[f"sources.{real_idx}.watch"] = bool(data["watch"])
    if "watch_interval" in data:
        update[f"sources.{real_idx}.watch_interval"] = max(int(data["watch_interval"]), 10)

    if update:
        get_collection("ai_goals").update_one({"goal_id": goal_id}, {"$set": update})

    return ok({"updated": list(update.keys())})


@bp.route('/cases', methods=['GET'])
@require_auth
def get_cases():
    """获取 goal 的 case 列表"""
    goal_id = request.args.get('goal_id', '')
    if not goal_id:
        return err("缺少 goal_id")
    if request.args.get('count_only'):
        count = get_collection("ai_goal_cases").count_documents({"goal_id": goal_id})
        return ok({"count": count})
    cases = list(get_collection("ai_goal_cases").find({"goal_id": goal_id}, {"_id": 0}))
    return ok({"cases": cases, "count": len(cases)})


@bp.route('/event/<event_id>', methods=['GET'])
def get_event_detail(event_id):
    """获取单个事件详情 + 关联 artifact（无需登录，只读）"""
    from bson import ObjectId
    try:
        event = get_collection("ai_goal_events").find_one({"_id": ObjectId(event_id)})
    except Exception:
        event = None
    # 兜底：也支持字符串 _id 查找
    if not event:
        event = get_collection("ai_goal_events").find_one({"_id": event_id})
    if not event:
        return err("事件不存在")
    event["_id"] = str(event["_id"])
    goal_id = event.get("goal_id", "")
    # 关联 artifact（同 step_id）
    step_id = (event.get("payload") or {}).get("step_id", "")
    artifact = None
    if step_id:
        artifact = get_collection("ai_goal_artifacts").find_one(
            {"goal_id": goal_id, "step_id": step_id},
            {"_id": 0}, sort=[("created_at", -1)])
    # 补充 commit 详情（提交人/message）
    commit_detail = None
    code_update_event = None
    payload = event.get("payload") or {}

    # 如果是 case_reminder，找同一轮的 code_update_round 事件合并
    if event.get("event") == "case_reminder":
        code_update_event = get_collection("ai_goal_events").find_one(
            {"goal_id": goal_id, "event": "code_update_round", "timestamp": {"$lte": event.get("timestamp", 0)}},
            {"_id": 0}, sort=[("timestamp", -1)])
        if code_update_event:
            cu_payload = code_update_event.get("payload") or {}
            # 取 commit 详情
            goal_doc = get_collection("ai_goals").find_one({"goal_id": goal_id}, {"_id": 0, "sources": 1})
            src = next((s for s in (goal_doc or {}).get("sources", []) if s.get("repo_id") == cu_payload.get("changed_repo_id")), {})
            local_path = src.get("local_path", "")
            after_ref = cu_payload.get("after", "")
            if local_path and after_ref:
                import subprocess
                try:
                    out = subprocess.check_output(["git", "log", "-1", "--format=%H|%an|%ae|%s", after_ref], cwd=local_path, stderr=subprocess.DEVNULL, timeout=5).decode().strip()
                    parts = out.split("|", 3)
                    if len(parts) == 4:
                        commit_detail = {"hash": parts[0][:8], "author": parts[1], "email": parts[2], "message": parts[3]}
                except Exception:
                    pass

    elif event.get("event") == "code_update_round" and payload.get("changed_repo_id"):
        goal_doc = get_collection("ai_goals").find_one({"goal_id": goal_id}, {"_id": 0, "sources": 1})
        src = next((s for s in (goal_doc or {}).get("sources", []) if s.get("repo_id") == payload["changed_repo_id"]), {})
        local_path = src.get("local_path", "")
        after_ref = payload.get("after", "")
        if local_path and after_ref:
            import subprocess
            try:
                out = subprocess.check_output(["git", "log", "-1", "--format=%H|%an|%ae|%s", after_ref], cwd=local_path, stderr=subprocess.DEVNULL, timeout=5).decode().strip()
                parts = out.split("|", 3)
                if len(parts) == 4:
                    commit_detail = {"hash": parts[0][:8], "author": parts[1], "email": parts[2], "message": parts[3]}
            except Exception:
                pass

    return ok({"event": event, "artifact": artifact, "commit_detail": commit_detail, "code_update_event": code_update_event})


@bp.route('/case/<case_id>', methods=['GET'])
@require_auth
def get_case_detail(case_id):
    """获取单条 case 详情"""
    goal_id = request.args.get('goal_id', '')
    query = {"case_id": case_id}
    if goal_id:
        query["goal_id"] = goal_id
    case = get_collection("ai_goal_cases").find_one(query, {"_id": 0})
    if not case:
        return err("Case 不存在")
    return ok({"case": case})


@bp.route('/upload_cases', methods=['POST'])
@require_auth
def upload_cases():
    """上传 PDF/HTML 文件，LLM 提取结构化 case 存入 ai_goal_cases"""
    import time as _t
    goal_id = request.form.get('goal_id', '')
    if not goal_id:
        return err("缺少 goal_id")

    files = request.files.getlist('files')
    if not files:
        return err("未上传文件")

    total_cases = 0
    for f in files:
        filename = f.filename or ""
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        raw = f.read()

        if ext == 'pdf':
            import fitz
            doc = fitz.open(stream=raw, filetype="pdf")
            text = "\n".join(page.get_text() for page in doc)
            doc.close()
        elif ext in ('html', 'htm'):
            from bs4 import BeautifulSoup
            text = BeautifulSoup(raw, 'html.parser').get_text(separator="\n")
        else:
            return err(f"不支持的文件类型: {ext}")

        cases = _extract_cases_from_text(text, goal_id, filename)
        if cases:
            get_collection("ai_goal_cases").insert_many(cases)
            total_cases += len(cases)

    return ok({"count": total_cases, "goal_id": goal_id})


def _extract_cases_from_text(text: str, goal_id: str, source_file: str) -> list:
    """用 LLM 从文本中提取结构化 case 列表。保留原始内容不修改。"""
    import time as _t
    from llm.structured import generate_structured

    # 分批处理大文本（每批 12000 字符，有重叠避免截断 case）
    BATCH = 12000
    OVERLAP = 500
    batches = []
    for start in range(0, len(text), BATCH - OVERLAP):
        batches.append(text[start:start + BATCH])

    all_cases = []
    for batch_text in batches:
        result = generate_structured(
            system_prompt=(
                "你是测试用例提取专家。从文本中识别并提取每一条测试用例。\n"
                "规则：\n"
                "1. 完整保留原始用例内容（步骤、预期结果），不要修改、不要缩写\n"
                "2. title 取原文的用例标题/场景名\n"
                "3. module 取这条用例所属的功能模块(如 组队流程、状态机、匹配 等业务模块名)\n"
                "4. steps 完整保留原始操作步骤\n"
                "5. expected 完整保留原始预期结果\n"
                "6. priority 从原文标注读取（P0/P1/P2），没有则默认 P1\n"
                "7. 不要遗漏任何用例，每个测试场景/测试点都算一条\n"
            ),
            user_prompt=(
                f'从以下文本中提取所有测试用例，返回 JSON：\n'
                f'{{"cases": [{{"case_id": "TC-001", "title": "原始标题", "module": "业务模块名", '
                f'"steps": "完整原始步骤", "expected": "完整原始预期结果", "priority": "P0"}}]}}\n\n'
                f"文本内容：\n{batch_text}"
            ),
            schema={"required": ["cases"], "types": {"cases": "list"}},
            default={"cases": []},
            require_confidence=False,
        )
        for c in (result.data.get("cases") or []):
            all_cases.append(c)

    # 去重（按 title）
    seen_titles = set()
    now = int(_t.time())
    cases = []
    for c in all_cases:
        title = c.get("title", "").strip()
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        cases.append({
            "case_id": f"TC-{len(cases)+1:03d}",
            "goal_id": goal_id,
            "title": title,
            "module": c.get("module", ""),
            "steps": c.get("steps", ""),
            "expected": c.get("expected", ""),
            "priority": c.get("priority", "P1"),
            "api_info": {},  # 不猜接口信息
            "source_file": source_file,
            "created_at": now,
        })
    return cases
