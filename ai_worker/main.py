"""AI Worker 主循环 — Change Stream 实时监听 + 轮询兜底"""
import asyncio
import os
import sys
import time

WORKER_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(WORKER_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import certifi
import yaml
from motor.motor_asyncio import AsyncIOMotorClient

from ai_worker.task_handlers import TASK_CLASS_ROUTER

MAX_CONCURRENCY = 20
FALLBACK_INTERVAL = 30  # 兜底轮询间隔（秒）
COLLECTION = "ai_task_queue"

STATUS_PENDING = 1
STATUS_RUNNING = 2
STATUS_COMPLETED = 3
STATUS_FAILED = 4


def _load_config():
    base = PROJECT_ROOT
    local_path = os.path.join(base, "config.local.yaml")
    default_path = os.path.join(base, "config.yaml")
    path = local_path if os.path.exists(local_path) else default_path
    with open(path, "r") as f:
        return yaml.safe_load(f)


def _get_async_db():
    config = _load_config()
    mongo_cfg = config.get("mongodb", {})
    uri = mongo_cfg.get("uri", "")
    env = os.getenv("APP_ENV", config.get("app", {}).get("env", "test"))
    db_name = mongo_cfg.get("db_name_test") if env == "test" else mongo_cfg.get("db_name")
    client = AsyncIOMotorClient(uri, tlsCAFile=certifi.where(), maxPoolSize=10)
    return client[db_name]


db = _get_async_db()
col = db[COLLECTION]


async def process_task(task: dict, semaphore: asyncio.Semaphore):
    """执行单个任务"""
    task_id = task["task_id"]
    task_type = task.get("task_type")
    payload = task.get("payload", {})

    async with semaphore:
        await col.update_one({"task_id": task_id}, {"$set": {"status": STATUS_RUNNING, "started_at": int(time.time())}})
        print(f"🚀 [{task_id}] type={task_type} 开始执行")

        try:
            TaskClass = TASK_CLASS_ROUTER.get(task_type)
            if not TaskClass:
                raise ValueError(f"未知任务类型: {task_type}")

            handler = TaskClass(task_id=task_id, payload=payload)
            timeout = payload.get("timeout", 600) + 60
            output = await asyncio.wait_for(handler.run(), timeout=timeout)

            await col.update_one({"task_id": task_id}, {"$set": {"status": STATUS_COMPLETED, "completed_at": int(time.time())}})
            print(f"✅ [{task_id}] 完成")

            # Goal 模式：回调调度器推进 DAG
            await _notify_goal_scheduler(payload, output if isinstance(output, dict) else {}, success=True)

        except asyncio.TimeoutError:
            await col.update_one({"task_id": task_id}, {"$set": {"status": STATUS_FAILED, "error": "超时", "completed_at": int(time.time())}})
            print(f"⏰ [{task_id}] 超时")
            await _notify_goal_scheduler(payload, {"error": "执行超时"}, success=False)
        except Exception as e:
            await col.update_one({"task_id": task_id}, {"$set": {"status": STATUS_FAILED, "error": str(e)[:500], "completed_at": int(time.time())}})
            print(f"❌ [{task_id}] 失败: {e}")
            await _notify_goal_scheduler(payload, {"error": str(e)[:300]}, success=False)

        # 释放智能体锁
        try:
            agent_id = payload.get("agent_id")
            if agent_id:
                await db["ai_agents"].update_one(
                    {"agent_id": agent_id},
                    {"$set": {"runtime_state.in_use": False, "runtime_state.used_by_task_id": ""}}
                )
        except Exception:
            pass


async def _notify_goal_scheduler(payload: dict, output: dict, success: bool):
    """Goal 任务完成回调：按 phase 分流（均放线程执行器，避免阻塞 worker 事件循环）。
    phase=probe → goal_runtime.on_probe_done（探查轮汇聚 → 生成目标 → 规划）
    有 step_id   → goal_scheduler.on_step_done（plan 执行推进）
    """
    goal_id = payload.get("goal_id")
    step_id = payload.get("step_id")
    phase = payload.get("phase", "step")
    if not goal_id:
        return  # 非 goal 任务（req 模式），无需回调
    try:
        if phase == "probe":
            from engine import goal_runtime
            result = await asyncio.to_thread(
                goal_runtime.on_probe_done, goal_id, payload.get("agent_id"), output)
            print(f"🔎 [goal:{goal_id} probe agent:{payload.get('agent_id')}] 探查回调 → {result}")
        elif step_id:
            from engine import goal_scheduler
            result = await asyncio.to_thread(
                goal_scheduler.on_step_done, goal_id, step_id, output, success)
            print(f"🎯 [goal:{goal_id} step:{step_id}] 回调完成 success={success} → {result}")
    except Exception as e:
        print(f"⚠️ [goal:{goal_id} phase={phase}] 调度回调异常: {e}")


async def claim_and_run(task: dict, semaphore: asyncio.Semaphore, active: set):
    """原子抢占 + 执行"""
    result = await col.update_one(
        {"task_id": task["task_id"], "status": STATUS_PENDING},
        {"$set": {"status": STATUS_RUNNING}}
    )
    if result.modified_count == 1:
        t = asyncio.create_task(process_task(task, semaphore))
        active.add(t)
        t.add_done_callback(active.discard)


async def drain_pending(semaphore: asyncio.Semaphore, active: set):
    """消费所有积压的 pending 任务"""
    cursor = col.find({"status": STATUS_PENDING}).sort("created_at", 1)
    async for task in cursor:
        if len(active) >= MAX_CONCURRENCY:
            break
        await claim_and_run(task, semaphore, active)


async def watch_loop(semaphore: asyncio.Semaphore, active: set):
    """Change Stream 实时监听新任务"""
    pipeline = [{"$match": {"operationType": "insert"}}]
    while True:
        try:
            async with col.watch(pipeline, full_document="updateLookup") as stream:
                print("👁️  Change Stream 已连接，实时监听中...")
                async for change in stream:
                    doc = change.get("fullDocument")
                    if not doc or doc.get("status") != STATUS_PENDING:
                        continue
                    if len(active) < MAX_CONCURRENCY:
                        await claim_and_run(doc, semaphore, active)
        except Exception as e:
            print(f"⚠️ Change Stream 断开: {e}，3s 后重连...")
            await asyncio.sleep(3)


async def fallback_loop(semaphore: asyncio.Semaphore, active: set):
    """兜底轮询：防 Change Stream 丢事件"""
    while True:
        await asyncio.sleep(FALLBACK_INTERVAL)
        try:
            await drain_pending(semaphore, active)
        except Exception as e:
            print(f"⚠️ 兜底轮询异常: {e}")


async def main():
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    active: set = set()

    # 启动时重置遗留 running 任务
    reset = await col.update_many({"status": STATUS_RUNNING}, {"$set": {"status": STATUS_FAILED, "error": "worker 重启中断"}})
    print(f"🧹 重置 {reset.modified_count} 个遗留任务")

    # 先消费积压
    await drain_pending(semaphore, active)

    print(f"🤖 AI Worker 启动 | 并发={MAX_CONCURRENCY} | 兜底={FALLBACK_INTERVAL}s")

    # 并行：Change Stream + 兜底轮询
    await asyncio.gather(
        watch_loop(semaphore, active),
        fallback_loop(semaphore, active),
    )


if __name__ == "__main__":
    # 启动 code_watcher 后台线程（随 worker 进程存活）
    import threading
    def _run_watcher():
        try:
            from engine.code_watcher import run_loop
            run_loop(30)
        except Exception as e:
            print(f"⚠️ code_watcher 异常退出: {e}")
    threading.Thread(target=_run_watcher, daemon=True, name="code_watcher").start()

    asyncio.run(main())
