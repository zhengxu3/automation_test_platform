"""注册执行层智能体（带能力契约）到 ai_agents

执行层智能体 = "手"，半自动/全自动共用。每个带能力契约让 Planner 看到结构化能力清单：
required_sources / produces_evidence / risk_level / requires_approval / fallback

git 地址不是智能体，是被测源。这里注册的是"用什么测"的能力。
"""
import time
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.db import get_collection

# 执行层智能体定义（capability_key 对应 engine/contracts.py 的 NODE_CONTRACTS）
AGENTS = [
    {
        "agent_id": "agent_git_prepare",
        "agent_name": "资源准备",
        "description": "按 git 地址+分支 clone/fetch/checkout，产出本地仓库路径供后续分析",
        "category": "resource",
        "capability_key": "git_prepare",
        "handler_class": "git_prepare",
        "model_id": "gemini_flash",
        "system_prompt": "",
        "capability_contract": {
            "purpose": "资源准备：clone/fetch/checkout git 仓库，产出本地路径",
            "required_sources": [],     # 不依赖已有 local_path，它就是来产 local_path 的
            "produces_evidence": [],
            "risk_level": "low",
            "requires_approval": False,
            "mutates": False,
            "timeout_sec": 300,
            "retryable": True,
            "fallback": None,
        },
        "inputs": [],
        "source": "system", "status": "active", "version": 1,
    },
    {
        "agent_id": "agent_req_analysis",
        "agent_name": "需求分析",
        "description": "解析需求文档，拆出验收点和测试用例",
        "category": "analysis",
        "capability_key": "requirement_analysis",
        "handler_class": "requirement_analysis",
        "model_id": "gemini_flash",
        "system_prompt": "你是需求分析专家。解析需求文档，拆解出可验证的验收点和测试用例。",
        # 能力契约（给 Planner 看的结构化边界）
        "capability_contract": {
            "purpose": "解析需求文档，拆出验收点和测试用例",
            "required_sources": ["doc"],
            "produces_evidence": ["doc_review", "testcase_generated"],
            "risk_level": "low",
            "requires_approval": False,
            "mutates": False,
            "timeout_sec": 300,
            "retryable": True,
            "fallback": None,
        },
        "inputs": [],
        "source": "system", "status": "active", "version": 1,
    },
    {
        "agent_id": "agent_branch_review",
        "agent_name": "代码变更分析",
        "description": "对比分支差异，识别变更影响范围；后端仓库额外产出受影响接口文档",
        "category": "code",
        "capability_key": "branch_review",
        "handler_class": "branch_review",
        "model_id": "gemini_pro",
        "system_prompt": "你是代码审查专家。分析分支差异，输出变更摘要、风险点、影响模块、回归建议；若是后端/API仓库，额外识别受影响接口契约，供 API 测试智能体使用。",
        "capability_contract": {
            "purpose": "识别代码变更对测试目标的影响，并在后端场景产出受影响接口文档",
            "required_sources": ["repo"],
            "produces_evidence": ["static_analysis"],
            "risk_level": "low",
            "requires_approval": False,
            "mutates": False,
            "timeout_sec": 600,
            "retryable": True,
            "fallback": None,
        },
        "inputs": [
            {"key": "repo_single", "label": "仓库/分支", "type": "repo_select", "required": True},
        ],
        "source": "system", "status": "active", "version": 1,
    },
    {
        "agent_id": "agent_code_scan",
        "agent_name": "代码画像扫描",
        "description": "扫描单个仓库，产出项目画像（类型/模块/入口/可测面/风险/建议验收），用于无需求时从代码反推目标",
        "category": "code",
        "capability_key": "code_scan",
        "handler_class": "code_scan",
        "model_id": "gemini_flash",
        "system_prompt": "你是代码架构分析专家。基于仓库文件结构推断项目类型、核心模块、入口、可测面、风险与建议验收点。",
        "capability_contract": {
            "purpose": "扫描单个仓库，产出项目画像，用于无需求时从代码反推目标",
            "required_sources": ["repo"],
            "produces_evidence": [],        # 探查产物，不直接绑验收证据（用于生成目标，非证明目标）
            "risk_level": "low",
            "requires_approval": False,
            "mutates": False,
            "timeout_sec": 600,
            "retryable": True,
            "fallback": None,
        },
        "inputs": [
            {"key": "repo_single", "label": "仓库", "type": "repo_select", "required": True},
        ],
        "source": "system", "status": "active", "version": 1,
    },
    {
        "agent_id": "agent_alignment",
        "agent_name": "需求-代码对齐",
        "description": "对比需求与代码变更，发现夹带改动/漏实现/影响面不一致",
        "category": "analysis",
        "capability_key": "alignment_analysis",
        "handler_class": "alignment_analysis",
        "model_id": "gemini_pro",
        "system_prompt": "你是质量分析专家。对比需求验收点和代码变更，发现夹带改动、漏实现、影响面不一致。",
        "capability_contract": {
            "purpose": "需求-代码对齐：发现夹带改动/漏实现",
            "required_sources": ["doc", "repo"],
            "produces_evidence": ["static_analysis"],
            "risk_level": "low",
            "requires_approval": False,
            "mutates": False,
            "timeout_sec": 300,
            "retryable": True,
            "fallback": None,
        },
        "inputs": [],
        "source": "system", "status": "inactive", "version": 1,   # handler 未实现，暂不进 Planner
    },
    {
        "agent_id": "agent_ui_automation",
        "agent_name": "UI 自动化验证",
        "description": "根据需求生成 UI 测试脚本，真机执行收集结果",
        "category": "device",
        "capability_key": "script_gen",
        "handler_class": "script_gen",
        "model_id": "gemini_flash",
        "system_prompt": "你是 Android UI 自动化测试专家。根据需求和 App 知识生成 pytest + uiautomator2 脚本：设备连接、App启动、操作步骤、截图、结果输出 result.json。",
        "capability_contract": {
            "purpose": "为回归清单生成可执行 UI 测试脚本并真机执行",
            "required_sources": ["repo:client"],
            "produces_evidence": [],       # 脚本生成不产证据；真机验证(device_test)由独立 device_execution 能力产出
            "risk_level": "high",          # 执行 AI 生成代码 + 占用设备
            "requires_approval": True,
            "mutates": True,
            "timeout_sec": 900,
            "retryable": True,
            "fallback": "doc_review",      # 缺设备/APK 降级为生成步骤说明
        },
        "inputs": [
            {"key": "package", "label": "App 包名", "required": False, "default": ""},
            {"key": "scenario", "label": "测试场景补充", "required": False, "default": ""},
        ],
        "source": "system", "status": "active", "version": 1,
    },
    {
        "agent_id": "agent_api_test",
        "agent_name": "API 接口测试",
        "description": "对后端接口生成请求并验证响应",
        "category": "api",
        "capability_key": "api_test",
        "handler_class": "api_test",
        "model_id": "gemini_flash",
        "system_prompt": "你是 API 测试专家。根据后端路由代码生成接口测试用例，发送请求验证响应。",
        "capability_contract": {
            "purpose": "对后端接口生成请求并验证响应",
            "required_sources": ["repo:backend", "env:base_url"],
            "produces_evidence": ["api_test"],
            "risk_level": "medium",
            "requires_approval": False,     # 只读接口测试(mutates=False)，自动化连续验证不卡人工审批
            "mutates": False,
            "timeout_sec": 600,
            "retryable": True,
            "fallback": "static_analysis",
        },
        "inputs": [
            {"key": "repo_single", "label": "后端仓库", "type": "repo_select", "required": True},
            {"key": "base_url", "label": "测试环境地址", "required": False},
        ],
        "source": "system", "status": "active", "version": 2,   # handler 已落地（device_worker/tasks/api_test_task.py）
    },
    {
        "agent_id": "agent_web_test",
        "agent_name": "Web UI 自动化验证",
        "description": "对 Web 前端测试环境执行可达性/交互冒烟验证，收集 Web UI 测试证据",
        "category": "web",
        "capability_key": "web_test",
        "handler_class": "web_test",
        "model_id": "gemini_flash",
        "system_prompt": "你是 Web UI 测试专家。根据需求、前端仓库和测试环境 URL 验证关键页面与交互。",
        "capability_contract": {
            "purpose": "对 Web 前端页面执行基础冒烟/交互验证",
            "required_sources": ["repo:web", "env:web_url"],
            "produces_evidence": ["web_test"],
            "risk_level": "medium",
            "requires_approval": False,
            "mutates": False,
            "timeout_sec": 600,
            "retryable": True,
            "fallback": "static_analysis",
        },
        "inputs": [
            {"key": "repo_single", "label": "Web 仓库", "type": "repo_select", "required": True},
            {"key": "base_url", "label": "Web 测试环境地址", "required": False},
        ],
        "source": "system", "status": "active", "version": 1,
    },
    {
        "agent_id": "agent_device_test",
        "agent_name": "客户端 UI 自动化验证",
        "description": "对 Android/iOS 客户端生成 UI 自动化脚本并验证关键交互，收集真机 UI 测试证据",
        "category": "device",
        "capability_key": "device_test",
        "handler_class": "device_test",
        "model_id": "gemini_flash",
        "system_prompt": "你是客户端 UI 自动化测试专家。根据需求和客户端仓库生成 UI 脚本并验证关键交互。",
        "capability_contract": {
            "purpose": "对客户端(Android/iOS)执行 UI 自动化验证，产出真机 UI 测试证据",
            "required_sources": ["repo:client"],
            "produces_evidence": ["device_test"],
            "risk_level": "medium",
            "requires_approval": False,
            "mutates": False,
            "timeout_sec": 900,
            "retryable": True,
            "fallback": "static_analysis",
        },
        "inputs": [
            {"key": "repo_single", "label": "客户端仓库", "type": "repo_select", "required": True},
        ],
        "source": "system", "status": "active", "version": 1,
    },
]


def main():
    col = get_collection("ai_agents")
    for agent in AGENTS:
        agent["updated_at"] = int(time.time())
        if "created_at" not in agent:
            agent["created_at"] = int(time.time())
        col.update_one({"agent_id": agent["agent_id"]}, {"$set": agent}, upsert=True)
        contract = agent["capability_contract"]
        print(f"✅ {agent['agent_id']:24} [{agent['capability_key']:22}] "
              f"risk={contract['risk_level']:6} approval={contract['requires_approval']}")
    print(f"\n共注册 {len(AGENTS)} 个执行层智能体")


if __name__ == "__main__":
    main()
