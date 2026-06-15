"""Git 仓库管理路由"""
import time
import uuid
import os
import platform
from flask import Blueprint, request
from common.auth import require_auth, get_current_user
from common.response import ok, err
from common.db import get_collection

bp = Blueprint('repo', __name__)
COL = "ai_git_repos"


def _get_repo_base():
    """根据平台返回仓库存储根目录"""
    if platform.system() == 'Linux':
        return '/data/repos'
    return os.path.expanduser('~/Documents/work_code')


@bp.route('/list', methods=['GET'])
@require_auth
def repo_list():
    """仓库列表（支持按父级过滤）"""
    parent_id = request.args.get('parent_id')
    query = {"parent_repo_id": parent_id} if parent_id else {}
    repos = list(get_collection(COL).find(query, {"_id": 0}).sort("created_at", -1))
    return ok({"repos": repos, "total": len(repos)})


@bp.route('/detail', methods=['GET'])
@require_auth
def repo_detail():
    repo_id = request.args.get('repo_id', '')
    if not repo_id:
        return err("缺少 repo_id")
    repo = get_collection(COL).find_one({"repo_id": repo_id}, {"_id": 0})
    if not repo:
        return err("仓库不存在", 404)
    # 附带分支列表
    branches = list(get_collection(COL).find({"parent_repo_id": repo_id}, {"_id": 0, "repo_id": 1, "branch": 1, "status": 1}))
    repo["branches"] = branches
    return ok(repo)


@bp.route('/create', methods=['POST'])
@require_auth
def repo_create():
    """创建仓库"""
    data = request.get_json() or {}
    repo_name = data.get('repo_name', '').strip()
    git_url = data.get('git_url', '').strip()
    branch = data.get('branch', 'master').strip()
    language = data.get('language', 'android')

    if not repo_name or not git_url:
        return err("缺少 repo_name 或 git_url")

    base_path = _get_repo_base()
    local_path = os.path.join(base_path, repo_name)
    repo_id = uuid.uuid4().hex[:12]

    doc = {
        "repo_id": repo_id,
        "repo_name": repo_name,
        "git_url": git_url,
        "branch": branch,
        "local_path": local_path,
        "parent_repo_id": None,
        "language": language,
        "status": "pending",  # pending → cloning → ready → vectorizing → vectorized
        "lock": {"locked": False, "locked_by": "", "locked_at": 0},
        "last_commit": "",
        "last_vectorized_commit": "",
        "error_msg": "",
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    get_collection(COL).insert_one(doc)

    # TODO: 触发 git clone 任务（写入 ai_task_queue）

    return ok({"repo_id": repo_id})


@bp.route('/add_branch', methods=['POST'])
@require_auth
def repo_add_branch():
    """为已有仓库添加分支"""
    data = request.get_json() or {}
    parent_id = data.get('parent_repo_id', '')
    branch = data.get('branch', '').strip()

    if not parent_id or not branch:
        return err("缺少 parent_repo_id 或 branch")

    parent = get_collection(COL).find_one({"repo_id": parent_id}, {"_id": 0})
    if not parent:
        return err("父仓库不存在")

    # 检查分支是否已存在
    existing = get_collection(COL).find_one({"parent_repo_id": parent_id, "branch": branch})
    if existing:
        return err(f"分支 {branch} 已存在")

    base_path = _get_repo_base()
    dir_name = f"{parent['repo_name']}__{branch.replace('/', '_')}"
    local_path = os.path.join(base_path, dir_name)
    repo_id = uuid.uuid4().hex[:12]

    doc = {
        "repo_id": repo_id,
        "repo_name": parent["repo_name"],
        "git_url": parent["git_url"],
        "branch": branch,
        "local_path": local_path,
        "parent_repo_id": parent_id,
        "language": parent.get("language", ""),
        "status": "pending",
        "lock": {"locked": False, "locked_by": "", "locked_at": 0},
        "last_commit": "",
        "last_vectorized_commit": "",
        "error_msg": "",
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    get_collection(COL).insert_one(doc)

    # TODO: 触发 git clone 任务

    return ok({"repo_id": repo_id, "branch": branch})


@bp.route('/update', methods=['POST'])
@require_auth
def repo_update():
    data = request.get_json() or {}
    repo_id = data.get('repo_id', '')
    if not repo_id:
        return err("缺少 repo_id")
    allowed = {
        "language", "status", "error_msg", "last_commit", "role", "environment",
        "base_url", "web_url", "apk_source", "apk_path", "test_accounts", "test_data",
        "device_profile", "device_id", "api_test_mock", "mock_api_test",
        "web_test_mock", "mock_web_test", "device_test_mock", "mock_device_test",
        "mock_fail_rounds", "api_test_mock_fail_rounds", "web_test_mock_fail_rounds",
        "device_test_mock_fail_rounds", "mock_regenerate_each_round",
    }
    update = {k: v for k, v in data.items() if k in allowed}
    update["updated_at"] = int(time.time())
    get_collection(COL).update_one({"repo_id": repo_id}, {"$set": update})
    return ok({"repo_id": repo_id})


@bp.route('/delete', methods=['POST'])
@require_auth
def repo_delete():
    data = request.get_json() or {}
    repo_id = data.get('repo_id', '')
    if not repo_id:
        return err("缺少 repo_id")
    # 删除本体 + 所有子分支
    get_collection(COL).delete_many({"$or": [{"repo_id": repo_id}, {"parent_repo_id": repo_id}]})
    return ok({"deleted": True})


@bp.route('/vectorize', methods=['POST'])
@require_auth
def repo_vectorize():
    """触发向量化"""
    data = request.get_json() or {}
    repo_id = data.get('repo_id', '')
    if not repo_id:
        return err("缺少 repo_id")

    repo = get_collection(COL).find_one({"repo_id": repo_id}, {"_id": 0})
    if not repo:
        return err("仓库不存在")
    if repo.get("status") in ("cloning", "vectorizing"):
        return err(f"仓库正在{repo['status']}中，请等待")

    get_collection(COL).update_one({"repo_id": repo_id}, {"$set": {"status": "vectorizing", "updated_at": int(time.time())}})

    # TODO: 写入 ai_task_queue 触发 repo_vectorize_task

    return ok({"repo_id": repo_id, "status": "vectorizing"})


@bp.route('/lock', methods=['POST'])
@require_auth
def repo_lock():
    data = request.get_json() or {}
    repo_id = data.get('repo_id', '')
    if not repo_id:
        return err("缺少 repo_id")
    get_collection(COL).update_one({"repo_id": repo_id}, {"$set": {
        "lock.locked": True, "lock.locked_by": get_current_user(), "lock.locked_at": int(time.time())
    }})
    return ok({"locked": True})


@bp.route('/unlock', methods=['POST'])
@require_auth
def repo_unlock():
    data = request.get_json() or {}
    repo_id = data.get('repo_id', '')
    if not repo_id:
        return err("缺少 repo_id")
    get_collection(COL).update_one({"repo_id": repo_id}, {"$set": {
        "lock.locked": False, "lock.locked_by": "", "lock.locked_at": 0
    }})
    return ok({"unlocked": True})
