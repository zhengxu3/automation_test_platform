"""Device Worker 入口 — motor + Change Stream 模式（与 AI Worker 一致）
部署在设备服务器（10.40.18.40），消费 device_task_queue。
任务完成后直接写 MongoDB Atlas（产出/日志/记忆），因为共用同一集群。
"""
import asyncio
import os
import sys
import time
import threading
import uuid
import yaml
import certifi

WORKER_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(WORKER_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient

from device_worker.device_manager import DeviceManager
from device_worker.screen_server import ScreenServer
from device_worker.task_handlers import TASK_CLASS_ROUTER

# 全局
_running_task_instances = {}

STATUS_PENDING = 1
STATUS_RUNNING = 2
STATUS_COMPLETED = 3
STATUS_FAILED = 4

MAX_CONCURRENCY = 5
FALLBACK_INTERVAL = 10
COLLECTION = "device_task_queue"

GOAL_TASK_TYPE = 40                  # Goal 能力统一入口（与 engine.agent_runtime 一致）
NETWORK_ONLY_CAPS = {"api_test", "web_test", "script_gen", "device_test"}  # 只做网络/代码生成或 mock，不占设备的 Goal 能力


def resolve_task_suspend(task_id, choice):
    task_instance = _running_task_instances.get(task_id)
    if task_instance:
        task_instance.resolve_suspend(choice)


def load_config():
    config_path = os.path.join(WORKER_DIR, "config.yaml")
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def _load_root_config():
    local_path = os.path.join(PROJECT_ROOT, 'config.local.yaml')
    default_path = os.path.join(PROJECT_ROOT, 'config.yaml')
    path = local_path if os.path.exists(local_path) else default_path
    with open(path, "r") as f:
        return yaml.safe_load(f)


def _get_mongo_uri():
    cfg = _load_root_config()
    return cfg['mongodb']['uri']


def _get_db_name(uri):
    env = os.getenv("APP_ENV", "test")
    cfg = _load_root_config()
    if env == "production":
        return cfg['mongodb'].get('db_name', uri.split("/")[-1].split("?")[0])
    return cfg['mongodb'].get('db_name_test', uri.split("/")[-1].split("?")[0])


def connect_sync_db():
    """同步 DB 连接（给 DeviceManager / BI Server / 任务处理器用）"""
    uri = _get_mongo_uri()
    client = MongoClient(uri, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    db_name = _get_db_name(uri)
    print(f"✅ MongoDB 连接成功 (sync): {db_name}")
    return client[db_name]


def connect_async_db():
    """异步 DB 连接（给 Change Stream 用）"""
    uri = _get_mongo_uri()
    db_name = _get_db_name(uri)
    client = AsyncIOMotorClient(uri, tlsCAFile=certifi.where(), maxPoolSize=10)
    return client[db_name]


# ==================== 任务执行 ====================

async def process_task(task: dict, semaphore: asyncio.Semaphore, sync_db, config, device_mgr, screen, async_col):
    """执行单个设备任务"""
    task_id = task["task_id"]
    task_type = task.get("task_type")
    payload = task.get("payload", {})

    async with semaphore:
        await async_col.update_one({"task_id": task_id}, {"$set": {"status": STATUS_RUNNING, "started_at": int(time.time())}})
        print(f"🚀 [{task_id}] type={task_type} 开始执行")

        device_id = task.get("device_id", "")
        # 网络型 Goal 能力（如 api_test）：只需网络可达 base_url，不占设备 → 跳过设备获取/准备/释放。
        # （放在 device 服务器是因为生产 Linux 连不到测试服务器，只有设备机在可达网络内）
        capability = payload.get("capability_key", "")
        network_only = (task_type == GOAL_TASK_TYPE and capability in NETWORK_ONLY_CAPS)
        try:
            if not network_only:
                # 分配设备
                if not device_id:
                    requirements = payload.get("device_requirements")
                    device_id = device_mgr.acquire(task_id, requirements)
                    if not device_id:
                        await async_col.update_one({"task_id": task_id}, {"$set": {"status": STATUS_PENDING, "started_at": 0}})
                        print(f"⏳ [{task_id}] 无空闲设备，回退到待执行")
                        return
                    await async_col.update_one({"task_id": task_id}, {"$set": {"device_id": device_id}})

                # 设备准备
                package = payload.get("package", "")
                apk_path = payload.get("apk_path", "")
                ok, err = device_mgr.prepare(device_id, package=package, apk_path=apk_path)
                if not ok:
                    raise RuntimeError(f"设备准备失败: {err}")

            # 执行任务
            TaskClass = TASK_CLASS_ROUTER.get(task_type)
            if not TaskClass:
                raise ValueError(f"未知任务类型: {task_type}")

            devices = [] if network_only else [{"device_id": device_id, "role": "default"}]
            handler = TaskClass(task_id, payload, devices, device_mgr, screen, sync_db)
            _running_task_instances[task_id] = handler

            await handler.run()
            await async_col.update_one({"task_id": task_id}, {"$set": {"status": STATUS_COMPLETED, "finished_at": int(time.time())}})
            print(f"✅ [{task_id}] 完成")

            # 回流 AI 平台（直接写 Atlas，同集群）
            await _report_completion(sync_db, task_id, payload)

        except Exception as e:
            await async_col.update_one({"task_id": task_id}, {"$set": {"status": STATUS_FAILED, "error_msg": str(e)[:200], "finished_at": int(time.time())}})
            print(f"❌ [{task_id}] 失败: {e}")
            # 回流失败状态
            await _report_failure(sync_db, task_id, payload, str(e))

        finally:
            if device_id:
                device_mgr.release(device_id)
            _running_task_instances.pop(task_id, None)


async def _report_completion(db, task_id, payload):
    """任务完成后 HTTP 回调 AI Gateway，触发记忆体评估"""
    result = db["device_task_results"].find_one({"task_id": task_id}, {"_id": 0})
    summary = result.get("summary", "UI 验证完成") if result else "UI 验证完成"
    detail = result.get("detail", "") if result else ""
    report_url = result.get("report_url", "") if result else ""

    # Goal 模式：回调 step_callback 触发 scheduler 推进
    goal_id = payload.get("goal_id")
    step_id = payload.get("step_id")
    if goal_id and step_id:
        saved_output = (result or {}).get("output")
        output = dict(saved_output) if isinstance(saved_output, dict) else {}
        output = {
            **output,
            "summary": summary, "detail": detail, "report_url": report_url,
            "test_result": output.get("test_result") or (result or {}).get("status", "pass"),
            "ref": output.get("ref") or report_url,
        }
        await _callback_goal_step(goal_id, step_id, output, success=True)
        return

    # 半自动 req 模式
    req_id = payload.get("req_id")
    agent_id = payload.get("agent_id", "")
    if not req_id:
        return
    await _callback_gateway("completed", req_id, agent_id, task_id, summary, detail, report_url)


async def _callback_goal_step(goal_id, step_id, output, success):
    """Goal 模式：HTTP 回调 /ai/goal/step_callback 触发调度推进"""
    import aiohttp
    gateway_url = os.getenv("AI_GATEWAY_URL", "http://127.0.0.1:5010")
    url = f"{gateway_url}/ai/goal/step_callback"
    body = {"goal_id": goal_id, "step_id": step_id, "output": output, "success": success}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    print(f"📡 Goal step 回调成功: {goal_id}/{step_id}")
                else:
                    print(f"⚠️ Goal step 回调失败 [{resp.status}]")
    except Exception as e:
        print(f"⚠️ Goal step 回调异常: {e}")


async def _report_failure(db, task_id, payload, error):
    """任务失败 HTTP 回调"""
    goal_id = payload.get("goal_id")
    step_id = payload.get("step_id")
    if goal_id and step_id:
        await _callback_goal_step(goal_id, step_id, {"error": error[:300]}, success=False)
        return
    req_id = payload.get("req_id")
    agent_id = payload.get("agent_id", "")
    if not req_id:
        return
    await _callback_gateway("failed", req_id, agent_id, task_id, f"设备任务失败: {error[:200]}", "", "")


async def _callback_gateway(status, req_id, agent_id, task_id, summary, detail, report_url):
    """POST 到 AI Gateway 的 device_callback 接口"""
    import aiohttp
    gateway_url = os.getenv("AI_GATEWAY_URL", "http://127.0.0.1:5010")
    url = f"{gateway_url}/ai/req/device_callback"
    body = {
        "status": status,
        "req_id": req_id,
        "agent_id": agent_id,
        "task_id": task_id,
        "summary": summary,
        "detail": detail,
        "report_url": report_url,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    print(f"📡 回调成功: {req_id}/{agent_id}")
                else:
                    text = await resp.text()
                    print(f"⚠️ 回调失败 [{resp.status}]: {text[:100]}")
    except Exception as e:
        print(f"⚠️ 回调异常: {e}，降级直写 DB")
        # 降级：直接写 Atlas（保底）
        from pymongo import MongoClient
        uri = _get_mongo_uri()
        client = MongoClient(uri, tlsCAFile=certifi.where())
        fallback_db = client[_get_db_name(uri)]
        fallback_db["ai_workspace_logs"].insert_one({
            "req_id": req_id, "agent_id": agent_id,
            "chunk": f"{'✅' if status == 'completed' else '❌'} {summary}",
            "status": "completed" if status == "completed" else "error",
            "timestamp": int(time.time()), "source": "device_worker",
        })


# ==================== 监听循环（Change Stream + 兜底轮询）====================

async def claim_and_run(task, semaphore, active, async_col, sync_db, config, device_mgr, screen):
    result = await async_col.update_one(
        {"task_id": task["task_id"], "status": STATUS_PENDING},
        {"$set": {"status": STATUS_RUNNING}}
    )
    if result.modified_count == 1:
        t = asyncio.create_task(process_task(task, semaphore, sync_db, config, device_mgr, screen, async_col))
        active.add(t)
        t.add_done_callback(active.discard)


async def drain_pending(semaphore, active, async_col, sync_db, config, device_mgr, screen):
    cursor = async_col.find({"status": STATUS_PENDING}).sort("created_at", 1)
    async for task in cursor:
        if len(active) >= MAX_CONCURRENCY:
            break
        await claim_and_run(task, semaphore, active, async_col, sync_db, config, device_mgr, screen)


async def watch_loop(semaphore, active, async_col, sync_db, config, device_mgr, screen):
    pipeline = [{"$match": {"operationType": "insert"}}]
    while True:
        try:
            async with async_col.watch(pipeline, full_document="updateLookup") as stream:
                print("👁️  Change Stream 已连接，监听 device_task_queue...")
                async for change in stream:
                    doc = change.get("fullDocument")
                    if not doc or doc.get("status") != STATUS_PENDING:
                        continue
                    if len(active) < MAX_CONCURRENCY:
                        await claim_and_run(doc, semaphore, active, async_col, sync_db, config, device_mgr, screen)
        except Exception as e:
            print(f"⚠️ Change Stream 断开: {e}，3s 后重连...")
            await asyncio.sleep(3)


async def fallback_loop(semaphore, active, async_col, sync_db, config, device_mgr, screen):
    while True:
        await asyncio.sleep(FALLBACK_INTERVAL)
        try:
            await drain_pending(semaphore, active, async_col, sync_db, config, device_mgr, screen)
        except Exception as e:
            print(f"⚠️ 兜底轮询异常: {e}")


# ==================== 设备发现循环 ====================

async def device_discover_loop(device_mgr, interval):
    while True:
        try:
            device_mgr.discover()
            device_mgr.release_expired()
        except Exception as e:
            print(f"⚠️ 设备发现异常: {e}")
        await asyncio.sleep(interval)


# ==================== BI Server ====================

def start_bi_server(sync_db, device_mgr, config):
    from device_worker.bi_server import app, init_app
    init_app(sync_db, device_mgr, config)
    port = config["server"]["bi_port"]
    print(f"🌐 BI 接口启动: 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ==================== 主入口 ====================

async def main():
    config = load_config()
    sync_db = connect_sync_db()
    async_db = connect_async_db()
    async_col = async_db[COLLECTION]

    device_mgr = DeviceManager(sync_db, config)
    screen = ScreenServer(config)

    import device_worker.screen_server as ss_module
    ss_module.screen_server_instance = screen

    # BI Server（独立线程）
    bi_thread = threading.Thread(target=start_bi_server, args=(sync_db, device_mgr, config), daemon=True)
    bi_thread.start()

    # 实时画面 WS
    await screen.start()

    # 重置遗留 running 任务
    reset = await async_col.update_many({"status": STATUS_RUNNING}, {"$set": {"status": STATUS_FAILED, "error_msg": "worker 重启中断"}})
    print(f"🧹 重置 {reset.modified_count} 个遗留任务")

    # 先消费积压
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    active: set = set()
    await drain_pending(semaphore, active, async_col, sync_db, config, device_mgr, screen)

    print(f"🤖 Device Worker 启动 | 并发={MAX_CONCURRENCY} | 兜底={FALLBACK_INTERVAL}s")

    # 并行：Change Stream + 设备发现 + 兜底轮询
    await asyncio.gather(
        watch_loop(semaphore, active, async_col, sync_db, config, device_mgr, screen),
        fallback_loop(semaphore, active, async_col, sync_db, config, device_mgr, screen),
        device_discover_loop(device_mgr, config["device"]["poll_interval"]),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("服务已停止")
