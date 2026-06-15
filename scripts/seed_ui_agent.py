"""向 ai_agents 集合插入 UI 自动化智能体"""
import time, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.db import get_collection

agent_doc = {
    "agent_id": "agent_ui_automation",
    "agent_name": "UI 自动化验证",
    "description": "根据需求描述自动生成 UI 测试脚本，在真机执行并收集结果",
    "category": "device",
    "handler_class": "script_gen",
    "model_id": "gemini_flash",
    "system_prompt": "你是 Android UI 自动化测试专家。根据需求描述和 App 知识生成 pytest + uiautomator2 测试脚本。脚本必须包含：设备连接、App 启动、操作步骤（点击/输入/滑动/断言）、截图、结果输出到 result.json。",
    "inputs": [
        {"key": "package", "label": "App 包名", "required": False, "default": ""},
        {"key": "scenario", "label": "测试场景补充", "required": False, "default": ""}
    ],
    "source": "system",
    "status": "active",
    "version": 1,
    "created_at": int(time.time()),
}

col = get_collection("ai_agents")
col.update_one({"agent_id": agent_doc["agent_id"]}, {"$set": agent_doc}, upsert=True)
print(f"✅ 已写入智能体: {agent_doc['agent_id']}")
