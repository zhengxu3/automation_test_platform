"""脚本生成任务 — AI 生成 UI 测试脚本 → 写入文件 → 创建设备任务"""
import time, os, uuid, json
from ai_worker.base_task import BaseTaskHandler
from common.db import get_collection
from llm.llm_factory import LLMFactory

HANDLER_META = {
    "key": "script_gen",
    "label": "脚本生成",
    "description": "AI 生成 UI 自动化测试脚本并提交设备执行",
    "inputs": [{"key": "package", "label": "App 包名"}, {"key": "scenario", "label": "测试场景"}],
    "task_type": 30,
}

SCRIPT_OUTPUT_DIR = os.path.expanduser("~/device_scripts/generated")


def _emit_codegen_event(goal_id: str, payload: dict, stage: str, summary: str, **extra):
    """兼容旧 ai_worker UI 脚本生成路径：把 codegen 进度写回 Goal 事件流。"""
    if not goal_id:
        return
    try:
        get_collection("ai_goal_events").insert_one({
            "goal_id": goal_id,
            "entity_type": "event",
            "event": "codegen_progress",
            "payload": {
                "step_id": payload.get("step_id", ""),
                "task_id": payload.get("task_id", ""),
                "stage": stage,
                "summary": summary,
                **extra,
            },
            "actor": "ai_worker",
            "timestamp": int(time.time()),
        })
    except Exception:
        return


class ScriptGenTask(BaseTaskHandler):
    async def run(self):
        req_id = self.payload["req_id"]
        agent_id = self.payload["agent_id"]
        inputs = self.payload.get("inputs", {})
        goal_id = self.payload.get("goal_id", "")
        self.payload.setdefault("task_id", self.task_id)

        self.log("🤖 开始生成 UI 测试脚本")
        _emit_codegen_event(goal_id, self.payload, "ui_script_generating",
                            "开始生成 UI 自动化测试脚本",
                            package=inputs.get("package", ""),
                            scenario=inputs.get("scenario", ""))

        # 获取需求信息
        req = get_collection("ai_requirements").find_one({"req_id": req_id}, {"_id": 0})
        if not req:
            raise ValueError("需求不存在")

        # 获取需求文档（如有）
        doc_content = req.get("doc_content", "") or req.get("description", "")

        # 获取已有记忆作为上下文
        memories = list(get_collection("ai_memory_points").find(
            {"req_id": req_id}, {"_id": 0, "summary": 1}
        ).sort("created_at", -1).limit(5))
        memory_text = "\n".join(m["summary"] for m in memories) if memories else "（首次执行）"

        # 构建 prompt
        package = inputs.get("package", "com.example.app")
        scenario = inputs.get("scenario", "")

        system_prompt = """你是 Android UI 自动化测试专家。生成 pytest + uiautomator2 测试脚本。
要求：
1. 使用 uiautomator2 库操作设备
2. 从环境变量读取 AVAILABLE_DEVICES 获取设备序列号
3. 包含截图步骤（d.screenshot() 保存到 DEVICE_TASK_OUTPUT_DIR）
4. 最后写入 result.json 到 DEVICE_TASK_OUTPUT_DIR
5. 只输出 Python 代码，不要解释"""

        user_prompt = f"""需求：{req.get('title', '')}
描述：{doc_content[:2000]}
场景：{scenario or '根据需求自行设计测试场景'}
包名：{package}
已有记忆：{memory_text}

请生成完整的 pytest 测试脚本。"""

        # 调用 LLM 生成脚本
        result = LLMFactory.generate("gemini_flash", system_prompt, user_prompt)
        script_code = result["text"]

        # 清理 markdown 代码块
        if "```python" in script_code:
            script_code = script_code.split("```python")[1].split("```")[0]
        elif "```" in script_code:
            script_code = script_code.split("```")[1].split("```")[0]

        self.log(f"📝 脚本生成完成 ({len(script_code)} 字符)")
        self.log(f"📊 Token: {result.get('usage', {}).get('total_tokens', 0)}")
        _emit_codegen_event(goal_id, self.payload, "ui_script_generated",
                            f"UI 自动化脚本生成完成，约 {len(script_code)} 字符",
                            script_chars=len(script_code),
                            tokens=result.get('usage', {}).get('total_tokens', 0))

        # 保存脚本文件
        script_id = uuid.uuid4().hex[:12]
        script_dir = os.path.join(SCRIPT_OUTPUT_DIR, script_id)
        os.makedirs(script_dir, exist_ok=True)
        script_path = os.path.join(script_dir, "test_main.py")
        with open(script_path, "w") as f:
            f.write(script_code)
        _emit_codegen_event(goal_id, self.payload, "finished",
                            f"UI 自动化脚本已保存：{script_path}",
                            status="pass", script_path=script_path, script_chars=len(script_code))

        # 写入产出（脚本代码）
        get_collection("ai_workspace_outputs").insert_one({
            "req_id": req_id,
            "agent_id": agent_id,
            "task_id": self.task_id,
            "content": f"## AI 生成的测试脚本\n\n```python\n{script_code}\n```",
            "format": "markdown",
            "round": 1,
            "source": "script_gen",
            "created_at": int(time.time()),
        })

        # 创建设备任务（写入 device_task_queue）
        device_task_id = f"dtask_{uuid.uuid4().hex[:8]}"
        get_collection("device_task_queue").insert_one({
            "task_id": device_task_id,
            "task_type": 3,  # external_script
            "payload": {
                "script_id": script_id,
                "script": script_path,
                "package": package,
                "req_id": req_id,
                "agent_id": agent_id,
                "params": {},
                "timeout": 300,
            },
            "status": 1,
            "device_id": "",
            "created_at": int(time.time()),
        })

        self.log(f"📱 设备任务已创建: {device_task_id}")
        self.log(f"✅ 脚本生成完成，等待设备执行")

        # 更新智能体状态
        get_collection("ai_workspace_agents").update_one(
            {"req_id": req_id, "agent_id": agent_id},
            {"$set": {"status": "completed", "tokens": result.get('usage', {}).get('total_tokens', 0)}}
        )

        # 结构化产出（goal 模式契约校验用；req 模式忽略返回值）
        return {
            "script_path": script_path,
            "covered_cases": [scenario] if scenario else [],
            "device_task_id": device_task_id,
            "summary": f"已生成 UI 测试脚本并提交设备任务 {device_task_id}",
            "ref": script_path,
            "confidence": 0.8,
        }
