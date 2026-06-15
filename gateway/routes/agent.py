"""智能体管理路由 — 含上下级联动"""
from flask import Blueprint, request
from common.auth import require_auth, get_current_user
from common.response import ok, err
from common.db import get_collection

bp = Blueprint('agent', __name__)


@bp.route('/list', methods=['GET'])
@require_auth
def agent_list():
    """列表 — 支持按分类/父级过滤"""
    col = get_collection("ai_agents")
    query = {}
    category = request.args.get('category')
    parent_id = request.args.get('parent_id')
    if category:
        query["category"] = category
    if parent_id:
        query["parent_id"] = parent_id
    agents = list(col.find(query, {"_id": 0}).sort("created_at", -1))

    # 组装子级数量
    for a in agents:
        a["children_count"] = col.count_documents({"parent_id": a["agent_id"]})

    return ok({"agents": agents, "total": len(agents)})


@bp.route('/tree', methods=['GET'])
@require_auth
def agent_tree():
    """树形结构 — 返回带 children 嵌套的智能体列表"""
    col = get_collection("ai_agents")
    all_agents = list(col.find({}, {"_id": 0}).sort("created_at", -1))

    # 构建树
    agent_map = {a["agent_id"]: {**a, "children": []} for a in all_agents}
    roots = []
    for a in all_agents:
        parent = a.get("parent_id")
        if parent and parent in agent_map:
            agent_map[parent]["children"].append(agent_map[a["agent_id"]])
        else:
            roots.append(agent_map[a["agent_id"]])

    return ok({"tree": roots})


@bp.route('/detail', methods=['GET'])
@require_auth
def agent_detail():
    agent_id = request.args.get('agent_id', '')
    if not agent_id:
        return err("缺少 agent_id")
    col = get_collection("ai_agents")
    doc = col.find_one({"agent_id": agent_id}, {"_id": 0})
    if not doc:
        return err("智能体不存在", 404)
    # 附带子级列表
    doc["children"] = list(col.find({"parent_id": agent_id}, {"_id": 0, "agent_name": 1, "agent_id": 1, "category": 1, "description": 1}))
    return ok(doc)


@bp.route('/create', methods=['POST'])
@require_auth
def agent_create():
    import time, uuid
    data = request.get_json() or {}
    if not data.get('agent_name'):
        return err("缺少 agent_name")

    agent_id = f"agent_{uuid.uuid4().hex[:8]}"
    doc = {
        "agent_id": agent_id,
        "agent_name": data["agent_name"],
        "description": data.get("description", ""),
        "category": data.get("category", "custom"),
        # 上下级
        "parent_id": data.get("parent_id", None),  # 父级智能体 ID
        # 能力定义
        "inputs": data.get("inputs", []),     # [{key, label, type, required}]
        "outputs": data.get("outputs", []),   # [{key, label}]
        "handler_class": data.get("handler_class", ""),
        # 运行配置
        "model_id": data.get("model_id", "gemini_flash"),
        "system_prompt": data.get("system_prompt", ""),
        "runtime": {
            "mode": data.get("runtime_mode", "stateless"),  # stateless / persistent
            "max_runtime_sec": data.get("max_runtime_sec", 600),
            "run_platform": data.get("run_platform", "linux"),  # linux / device
            "device_count": data.get("device_count", 0),
        },
        # 元信息
        "source": data.get("source", "internal"),
        "version": data.get("version", "1.0.0"),
        "installable": data.get("installable", True),
        "created_by": get_current_user(),
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    get_collection("ai_agents").insert_one(doc)

    # 如果有父级，通知父级（可选：更新父级的 children 缓存）
    if doc["parent_id"]:
        _notify_parent_changed(doc["parent_id"])

    return ok({"agent_id": agent_id})


@bp.route('/update', methods=['POST'])
@require_auth
def agent_update():
    import time
    data = request.get_json() or {}
    agent_id = data.get('agent_id', '')
    if not agent_id:
        return err("缺少 agent_id")

    # 只允许更新这些字段
    allowed = {"agent_name", "description", "category", "parent_id", "inputs", "outputs",
               "handler_class", "model_id", "system_prompt", "runtime", "version", "installable"}
    update = {k: v for k, v in data.items() if k in allowed}
    update["updated_at"] = int(time.time())

    old = get_collection("ai_agents").find_one({"agent_id": agent_id}, {"_id": 0, "parent_id": 1})
    get_collection("ai_agents").update_one({"agent_id": agent_id}, {"$set": update})

    # 上下级变更通知
    old_parent = old.get("parent_id") if old else None
    new_parent = data.get("parent_id")
    if old_parent != new_parent:
        if old_parent:
            _notify_parent_changed(old_parent)
        if new_parent:
            _notify_parent_changed(new_parent)

    return ok({"agent_id": agent_id})


@bp.route('/delete', methods=['POST'])
@require_auth
def agent_delete():
    data = request.get_json() or {}
    agent_id = data.get('agent_id', '')
    if not agent_id:
        return err("缺少 agent_id")

    col = get_collection("ai_agents")
    # 级联处理：子级的 parent_id 置空
    col.update_many({"parent_id": agent_id}, {"$set": {"parent_id": None}})
    col.delete_one({"agent_id": agent_id})
    return ok({"deleted": True})


@bp.route('/children', methods=['GET'])
@require_auth
def agent_children():
    """获取某智能体的所有子级"""
    agent_id = request.args.get('agent_id', '')
    if not agent_id:
        return err("缺少 agent_id")
    children = list(get_collection("ai_agents").find({"parent_id": agent_id}, {"_id": 0}))
    return ok({"children": children})


@bp.route('/handlers', methods=['GET'])
@require_auth
def agent_handlers():
    """返回可用的执行器映射表（从 worker 注册表动态读取）"""
    handlers = _load_handler_metas()
    return ok({"handlers": handlers})


def _load_handler_metas():
    """扫描 ai_worker/tasks/ 下所有 HANDLER_META"""
    import importlib
    import pkgutil
    import os

    # task_type 映射（和 task_handlers.py 保持一致）
    TASK_TYPE_MAP = {
        "repo_lifecycle": 1,
        "branch_review": 2,
        "git_clone": 10,
        "repo_vectorize": 11,
        "requirement_analysis": 20,
        "device_script": 21,
        "external_script": 22,
    }

    metas = []
    tasks_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'ai_worker', 'tasks')

    task_files = [f for f in os.listdir(tasks_dir) if f.endswith('_task.py') and not f.startswith('__')] if os.path.exists(tasks_dir) else []

    if not task_files:
        # 当前已实现的执行器
        return [
            {"key": "", "label": "通用（LLM 对话）", "task_type": 0, "platform": "linux", "inputs": []},
            {"key": "requirement_analysis", "label": "需求分析", "task_type": 20, "platform": "linux", "inputs": [{"key": "req_id", "label": "需求ID", "required": True}]},
            {"key": "branch_review", "label": "代码分析", "task_type": 2, "platform": "linux", "inputs": [{"key": "repo_id", "label": "仓库ID", "required": True}, {"key": "branch", "label": "分支", "required": True}]},
        ]

    for f in task_files:
        module_name = f'ai_worker.tasks.{f[:-3]}'
        try:
            mod = importlib.import_module(module_name)
            meta = getattr(mod, 'HANDLER_META', None)
            if meta:
                key = meta.get("key", "")
                metas.append({
                    "key": key,
                    "label": meta.get("label", key),
                    "description": meta.get("description", ""),
                    "task_type": TASK_TYPE_MAP.get(key, 20),
                    "platform": "device" if "device" in key else "linux",
                    "inputs": meta.get("inputs", []),
                    "capabilities": meta.get("capabilities", []),
                })
        except Exception:
            continue

    # 加一个通用 handler
    metas.insert(0, {"key": "", "label": "通用（LLM 对话）", "task_type": 20, "platform": "linux", "inputs": [], "capabilities": []})
    return metas


def _notify_parent_changed(parent_id):
    """父级的子级发生变化时的回调（目前只记录时间戳，后续可扩展）"""
    import time
    get_collection("ai_agents").update_one(
        {"agent_id": parent_id},
        {"$set": {"children_updated_at": int(time.time())}}
    )
