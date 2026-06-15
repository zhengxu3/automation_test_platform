"""AI 异步任务基类"""


class BaseTaskHandler:
    """所有 task 继承此类，实现 run()"""

    HANDLER_META = None  # 子类覆盖，用于自动扫描注册

    def __init__(self, task_id: str, payload: dict):
        self.task_id = task_id
        self.payload = payload

    async def run(self):
        raise NotImplementedError("子类必须实现 run()")

    def log(self, msg: str, level: str = "info"):
        """写日志到 ai_workspace_logs"""
        from common.db import get_collection
        import time
        req_id = self.payload.get("req_id", "")
        agent_id = self.payload.get("agent_id", "")
        if not req_id:
            return
        get_collection("ai_workspace_logs").insert_one({
            "req_id": req_id,
            "agent_id": agent_id,
            "task_id": self.task_id,
            "chunk": msg,
            "status": level,
            "timestamp": int(time.time()),
            "source": "ai_worker",
        })
