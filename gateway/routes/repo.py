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


def _detect_default_branch(git_url: str, local_path: str) -> str:
    """自动检测仓库主分支（不要求用户填）。"""
    import subprocess
    # 本地已有仓库，读 HEAD
    if os.path.isdir(os.path.join(local_path, '.git')):
        try:
            r = subprocess.run(['git', 'symbolic-ref', 'refs/remotes/origin/HEAD'],
                              cwd=local_path, capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                return r.stdout.strip().split('/')[-1]
        except Exception:
            pass
    # 远程探测
    try:
        r = subprocess.run(['git', 'ls-remote', '--symref', git_url, 'HEAD'],
                          capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if 'ref:' in line and 'HEAD' in line:
                    return line.split('refs/heads/')[-1].split()[0]
    except Exception:
        pass
    return 'main'


def _detect_project_type(local_path: str) -> str:
    """根据特征文件判断项目类型。"""
    if not os.path.isdir(local_path):
        return ''
    checks = [
        ('build.gradle', 'android'), ('AndroidManifest.xml', 'android'),
        ('Podfile', 'ios'), ('Info.plist', 'ios'),
        ('package.json', 'web'), ('pom.xml', 'java'),
        ('go.mod', 'go'), ('requirements.txt', 'python'), ('app.py', 'python'),
    ]
    for fname, lang in checks:
        for root, dirs, files in os.walk(local_path):
            if fname in files:
                return lang
            # 只扫前两层
            depth = root.replace(local_path, '').count(os.sep)
            if depth >= 2:
                dirs.clear()
    return ''


def _ensure_repo_ready(git_url: str, local_path: str, branch: str) -> str:
    """确保仓库已 clone + checkout 到指定分支，返回状态。"""
    import subprocess
    if not os.path.isdir(os.path.join(local_path, '.git')):
        # clone
        try:
            subprocess.run(['git', 'clone', git_url, local_path],
                          capture_output=True, timeout=300)
        except Exception:
            return 'pending'
    # checkout
    try:
        subprocess.run(['git', 'fetch', 'origin'], cwd=local_path, capture_output=True, timeout=60)
        subprocess.run(['git', 'checkout', branch], cwd=local_path, capture_output=True, timeout=30)
        subprocess.run(['git', 'pull', 'origin', branch], cwd=local_path, capture_output=True, timeout=60)
        return 'ready'
    except Exception:
        return 'pending'


def _get_head_commit(local_path: str) -> dict | None:
    """读取本地仓库 HEAD 的 commit hash + message + time。"""
    import subprocess
    if not os.path.isdir(local_path):
        return None
    try:
        r = subprocess.run(
            ['git', 'log', '-1', '--format=%H|%s|%ct'],
            cwd=local_path, capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and '|' in r.stdout:
            parts = r.stdout.strip().split('|', 2)
            return {"hash": parts[0][:8], "msg": parts[1][:80], "time": int(parts[2]) if parts[2].isdigit() else 0}
    except Exception:
        pass
    return None


@bp.route('/list', methods=['GET'])
@require_auth
def repo_list():
    """仓库列表（支持按父级过滤）"""
    parent_id = request.args.get('parent_id')
    query = {"parent_repo_id": parent_id} if parent_id else {}
    repos = list(get_collection(COL).find(query, {"_id": 0}).sort("created_at", -1))
    # 补充最新 commit 信息（从本地 git 读）
    for r in repos:
        if not r.get("last_commit") and r.get("local_path") and r.get("status") in ("ready", "vector_ready", "vectorized"):
            commit_info = _get_head_commit(r["local_path"])
            if commit_info:
                r["last_commit"] = commit_info["hash"]
                r["last_commit_msg"] = commit_info["msg"]
                r["last_commit_time"] = commit_info["time"]
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
    branch = data.get('branch', '').strip()  # 空=自动检测主分支
    language = data.get('language', '')

    if not repo_name or not git_url:
        return err("缺少 repo_name 或 git_url")

    base_path = _get_repo_base()
    local_path = os.path.join(base_path, repo_name)
    repo_id = uuid.uuid4().hex[:12]

    # 自动检测主分支（不要求用户填）
    if not branch:
        branch = _detect_default_branch(git_url, local_path)

    # 自动检测项目语言/类型
    if not language:
        language = _detect_project_type(local_path)

    # 自动 clone（如果本地还没有）+ checkout
    status = _ensure_repo_ready(git_url, local_path, branch)

    doc = {
        "repo_id": repo_id,
        "repo_name": repo_name,
        "git_url": git_url,
        "branch": branch,
        "local_path": local_path,
        "parent_repo_id": None,
        "language": language,
        "status": status,
        "lock": {"locked": False, "locked_by": "", "locked_at": 0},
        "last_commit": "",
        "last_vectorized_commit": "",
        "error_msg": "",
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    get_collection(COL).insert_one(doc)

    return ok({"repo_id": repo_id, "branch": branch, "status": status})


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

    # 复用父仓库的 local_path（同一个 git 目录，只 fetch 分支）
    local_path = parent.get("local_path", "")
    repo_id = uuid.uuid4().hex[:12]

    # fetch 分支确保可用
    import subprocess
    status = "ready"
    if local_path and os.path.isdir(local_path):
        try:
            subprocess.run(['git', 'fetch', 'origin', branch], cwd=local_path, capture_output=True, timeout=60)
        except Exception:
            status = "pending"
    else:
        status = "pending"

    doc = {
        "repo_id": repo_id,
        "repo_name": parent["repo_name"],
        "git_url": parent["git_url"],
        "branch": branch,
        "local_path": local_path,
        "parent_repo_id": parent_id,
        "language": parent.get("language", ""),
        "status": status,
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

    # 写入任务队列触发 worker 向量化
    task_id = uuid.uuid4().hex[:16]
    embed_model = data.get('embed_model', 'gemini')
    get_collection("ai_task_queue").insert_one({
        "task_id": task_id,
        "task_type": 10,  # repo_vectorize
        "status": 1,  # pending
        "payload": {"repo_id": repo_id, "repo_name": repo.get("repo_name", ""), "local_path": repo.get("local_path", ""), "language": repo.get("language", ""), "branch": repo.get("branch", ""), "embed_model": embed_model},
        "created_at": int(time.time()),
    })

    return ok({"repo_id": repo_id, "status": "vectorizing"})


@bp.route('/logs', methods=['GET'])
@require_auth
def repo_logs():
    """获取仓库操作日志（向量化进度等）"""
    repo_id = request.args.get('repo_id', '')
    offset = int(request.args.get('offset', 0))
    if not repo_id:
        return err("缺少 repo_id")
    repo = get_collection(COL).find_one({"repo_id": repo_id}, {"_id": 0, "vectorize_logs": 1})
    logs = (repo or {}).get("vectorize_logs", [])
    return ok({"logs": logs[offset:], "total": len(logs)})


@bp.route('/lock', methods=['POST'])
@require_auth
def repo_lock():
    data = request.get_json() or {}
    repo_id = data.get('repo_id', '')
    if not repo_id:
        return err("缺少 repo_id")
    repo = get_collection(COL).find_one({"repo_id": repo_id}, {"_id": 0, "lock": 1})
    if repo and repo.get("lock", {}).get("locked"):
        locked_by = repo["lock"].get("locked_by", "未知")
        return err(f"仓库已被 {locked_by} 锁定，请等待释放")
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
