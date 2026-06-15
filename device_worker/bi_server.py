"""设备服务 BI 接口 (Flask :5007)"""
import os
import time
import uuid
from flask import Flask, request, jsonify

app = Flask(__name__)

_db = None
_device_mgr = None
_config = None


def init_app(db, device_mgr, config):
    global _db, _device_mgr, _config
    _db, _device_mgr, _config = db, device_mgr, config


def _ok(data=None, msg="success"):
    return jsonify({"code": 0, "message": msg, "data": data or {}})


def _err(msg, code=400):
    return jsonify({"code": code, "message": msg}), code


# ==================== 设备 ====================

@app.route("/device/list", methods=["GET"])
def device_list():
    return _ok(_device_mgr.get_list())


@app.route("/device/capacity", methods=["GET"])
def device_capacity():
    return _ok(_device_mgr.get_capacity())


@app.route("/device/acquire", methods=["POST"])
def device_acquire():
    data = request.get_json() or {}
    task_id = data.get("task_id", str(uuid.uuid4())[:12])
    requirements = data.get("requirements")
    device_id = _device_mgr.acquire(task_id, requirements)
    if not device_id:
        return _err("无空闲设备")
    return _ok({"device_id": device_id, "task_id": task_id})


@app.route("/device/release", methods=["POST"])
def device_release():
    data = request.get_json() or {}
    device_id = data.get("device_id", "")
    if not device_id:
        return _err("缺少 device_id")
    _device_mgr.release(device_id)
    return _ok(msg="释放成功")


# ==================== 任务 ====================

@app.route("/device/task/running", methods=["GET"])
def task_running():
    col = _db[_config["collections"]["task_queue"]]
    tasks = list(col.find({"status": 2}, {"_id": 0}))
    return _ok(tasks)


@app.route("/device/task/create", methods=["POST"])
def task_create():
    data = request.get_json() or {}
    task_type = data.get("task_type")
    payload = data.get("payload", {})
    device_id = data.get("device_id", "")
    if not task_type:
        return _err("缺少 task_type")

    task_id = str(uuid.uuid4()).replace("-", "")[:16]

    if not device_id:
        requirements = payload.get("device_requirements")
        device_id = _device_mgr.acquire(task_id, requirements)
        if not device_id:
            return _err("无空闲设备，请稍后重试")
    else:
        result = _device_mgr.col.find_one_and_update(
            {"device_id": device_id, "status": "idle"},
            {"$set": {"status": "busy", "locked_by": task_id, "locked_at": int(time.time())}}
        )
        if not result:
            return _err(f"设备 {device_id} 不可用")

    col = _db[_config["collections"]["task_queue"]]
    col.insert_one({
        "task_id": task_id, "task_type": task_type, "payload": payload,
        "status": 1, "device_id": device_id, "error_msg": "",
        "created_at": int(time.time()),
    })
    return _ok({"task_id": task_id, "device_id": device_id}, "任务已创建")


@app.route("/device/task/<task_id>", methods=["GET"])
def task_status(task_id):
    col = _db[_config["collections"]["task_queue"]]
    task = col.find_one({"task_id": task_id}, {"_id": 0})
    if not task:
        return _err("任务不存在")
    results_col = _db[_config["collections"]["task_results"]]
    result = results_col.find_one({"task_id": task_id}, {"_id": 0})
    return _ok({"task": task, "result": result})


@app.route("/device/task/<task_id>/cancel", methods=["POST"])
def task_cancel(task_id):
    col = _db[_config["collections"]["task_queue"]]
    task = col.find_one({"task_id": task_id})
    if not task:
        return _err("任务不存在")
    if task.get("status") not in (1, 2):
        return _err("任务已完成，无法取消")
    col.update_one({"task_id": task_id}, {"$set": {"status": 5, "error_msg": "手动取消"}})
    device_id = task.get("device_id", "")
    if device_id:
        _device_mgr.release(device_id)
    return _ok(msg="已取消，设备已释放")


# ==================== App 管理 ====================

@app.route("/device/app/list", methods=["GET"])
def app_list():
    col = _db[_config["collections"]["app_registry"]]
    apps = list(col.find({}, {"_id": 0}))
    return _ok(apps)


@app.route("/device/app/create", methods=["POST"])
def app_create():
    app_name = request.form.get("app_name", "").strip()
    package = request.form.get("package", "").strip()
    if not app_name or not package:
        return _err("缺少 app_name 或 package")

    app_id = str(uuid.uuid4()).replace("-", "")[:12]
    apk_path = ""
    if "apk" in request.files:
        f = request.files["apk"]
        apk_dir = os.path.expanduser(_config["device"]["apk_dir"])
        os.makedirs(apk_dir, exist_ok=True)
        apk_path = os.path.join(apk_dir, f"{app_id}_{f.filename}")
        f.save(apk_path)

    col = _db[_config["collections"]["app_registry"]]
    col.insert_one({
        "app_id": app_id, "app_name": app_name, "package": package,
        "apk_path": apk_path, "status": "not_learned", "pages_count": 0,
        "created_at": int(time.time()),
    })
    return _ok({"app_id": app_id}, "App 创建成功")


@app.route("/device/app/<app_id>/pages", methods=["GET"])
def app_pages(app_id):
    col = _db[_config["collections"]["app_pages"]]
    pages = list(col.find({"app_id": app_id}, {"_id": 0, "screenshot_b64": 0}))
    return _ok(pages)


# ==================== 内部接口 ====================

@app.route("/internal/tap_event", methods=["POST"])
def tap_event():
    from device_worker.screen_server import screen_server_instance
    data = request.get_json() or {}
    if screen_server_instance:
        screen_server_instance.report_tap(data.get("device_id", ""), data.get("x", 0), data.get("y", 0))
    return _ok()


# ==================== 挂起确认 ====================

@app.route("/device/suspend/list", methods=["GET"])
def suspend_list():
    col = _db["device_suspend_queue"]
    status = request.args.get("status", "pending")
    items = list(col.find({"status": status}, {"_id": 0}).sort("created_at", -1).limit(50))
    return _ok(items)


@app.route("/device/task/<task_id>/resume", methods=["POST"])
def task_resume(task_id):
    data = request.get_json() or {}
    choice = data.get("choice", "")
    suspend_id = data.get("suspend_id", "")
    if not choice:
        return _err("缺少 choice")
    col = _db["device_suspend_queue"]
    col.update_one(
        {"task_id": task_id, "status": "pending"} if not suspend_id else {"suspend_id": suspend_id},
        {"$set": {"status": "resolved", "choice": choice, "resolved_at": int(time.time())}}
    )
    from device_worker.main import resolve_task_suspend
    resolve_task_suspend(task_id, choice)
    return _ok(msg=f"已选择: {choice}")


# ==================== 外部脚本管理 ====================

@app.route("/device/script/list", methods=["GET"])
def script_list():
    scripts = list(_db["device_scripts"].find({}, {"_id": 0}))
    return _ok(scripts)


@app.route("/device/script/<script_id>/run", methods=["POST"])
def script_run(script_id):
    data = request.get_json() or {}
    params = data.get("params", {})
    task_id = str(uuid.uuid4()).replace("-", "")[:16]
    col = _db[_config["collections"]["task_queue"]]
    col.insert_one({
        "task_id": task_id, "task_type": 3,
        "payload": {"script_id": script_id, "params": params},
        "status": 1, "device_id": "", "error_msg": "",
        "created_at": int(time.time()),
    })
    return _ok({"task_id": task_id}, "任务已创建")
