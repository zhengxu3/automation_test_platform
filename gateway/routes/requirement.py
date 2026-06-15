"""需求管理路由"""
import time
import uuid
import os
import re
import tempfile
from flask import Blueprint, request
from common.auth import require_auth, get_current_user
from common.response import ok, err
from common.db import get_collection

bp = Blueprint('requirement', __name__)


def _extract_text_from_file(file_storage):
    """从上传文件提取文本内容（PDF/DOCX/TXT/MD）"""
    filename = file_storage.filename.lower()
    data = file_storage.read()

    if filename.endswith('.pdf'):
        import pymupdf
        doc = pymupdf.open(stream=data, filetype="pdf")
        return "\n".join(page.get_text() for page in doc)

    if filename.endswith(('.txt', '.md')):
        return data.decode('utf-8', errors='replace')

    if filename.endswith(('.docx', '.doc')):
        # 简单 docx 解析（纯文本提取）
        import zipfile
        import io
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                xml = zf.read('word/document.xml').decode()
                # 提取 <w:t> 标签内文本
                texts = re.findall(r'<w:t[^>]*>([^<]+)</w:t>', xml)
                return ' '.join(texts)
        except Exception:
            return data.decode('utf-8', errors='replace')

    return data.decode('utf-8', errors='replace')


def _parse_docs_from_output(req_id, agent_id, output_text):
    """从 LLM 产出中解析 [DOC:xxx] 标记，拆成独立文档"""
    doc_pattern = r'##\s*\[DOC:(\w+)\]\s*(.+?)(?=##\s*\[DOC:|\Z)'
    matches = re.findall(doc_pattern, output_text, re.DOTALL)
    if not matches:
        return []

    label_map = {
        "requirement_breakdown": "需求拆解",
        "test_cases": "测试用例",
        "test_strategy": "测试策略",
    }
    col = get_collection("ai_requirement_docs")
    docs = []
    for doc_type, content in matches:
        doc_type = doc_type.strip()
        content = content.strip()
        doc = {
            "doc_id": f"doc_{uuid.uuid4().hex[:8]}",
            "req_id": req_id,
            "agent_id": agent_id,
            "doc_type": doc_type,
            "doc_type_label": label_map.get(doc_type, doc_type),
            "content": content,
            "version": 1,
            "source": "ai_generate",
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
        }
        col.replace_one({"req_id": req_id, "doc_type": doc_type}, doc, upsert=True)
        docs.append(doc)
    return docs


@bp.route('/list', methods=['GET'])
@require_auth
def req_list():
    page = int(request.args.get('page', 1))
    page_size = int(request.args.get('page_size', 20))
    col = get_collection("ai_requirements")
    total = col.count_documents({})
    items = list(col.find({}, {"_id": 0}).sort("created_at", -1).skip((page - 1) * page_size).limit(page_size))
    return ok({"requirements": items, "total": total})


@bp.route('/create', methods=['POST'])
@require_auth
def req_create():
    """创建需求 — 支持 JSON 或 FormData（含文件上传）"""
    # 兼容 JSON 和 FormData
    if request.content_type and 'multipart' in request.content_type:
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '')
        agent_id = request.form.get('agent_id', '')
        file = request.files.get('file')
    else:
        data = request.get_json() or {}
        title = data.get('title', '').strip()
        description = data.get('description', '')
        agent_id = data.get('agent_id', '')
        file = None

    if not title:
        return err("标题不能为空")

    req_id = f"req_{uuid.uuid4().hex[:8]}"
    doc_content = ""
    doc_filename = ""

    # 解析上传文件
    if file and file.filename:
        doc_filename = file.filename
        doc_content = _extract_text_from_file(file)

    doc = {
        "req_id": req_id,
        "title": title,
        "description": description,
        "doc_content": doc_content[:50000],  # 限制存储大小
        "doc_filename": doc_filename,
        "status": "analyzing" if (doc_content or description) else "ready",
        "created_by": get_current_user(),
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    get_collection("ai_requirements").insert_one(doc)

    # 如果有内容，自动触发需求分析任务
    if doc_content or description:
        _trigger_analysis(req_id, agent_id, doc_content or description, doc_filename)

    return ok({"req_id": req_id, "status": doc["status"]})


@bp.route('/upload_doc', methods=['POST'])
@require_auth
def req_upload_doc():
    """对已有需求补充上传文档并触发分析"""
    req_id = request.form.get('req_id', '')
    agent_id = request.form.get('agent_id', '')
    file = request.files.get('file')

    if not req_id:
        return err("缺少 req_id")
    if not file or not file.filename:
        return err("请选择文件")

    req = get_collection("ai_requirements").find_one({"req_id": req_id})
    if not req:
        return err("需求不存在")

    doc_content = _extract_text_from_file(file)
    doc_filename = file.filename

    get_collection("ai_requirements").update_one(
        {"req_id": req_id},
        {"$set": {"doc_content": doc_content[:50000], "doc_filename": doc_filename, "status": "analyzing", "updated_at": int(time.time())}}
    )
    _trigger_analysis(req_id, agent_id, doc_content, doc_filename)
    return ok({"status": "analyzing"})


def _trigger_analysis(req_id, agent_id, content, filename=""):
    """创建需求分析任务入队"""
    # 如果没指定智能体，用系统默认
    if not agent_id:
        default = get_collection("ai_agents").find_one({"category": "requirement", "source": "system"})
        agent_id = default["agent_id"] if default else "system_req_analysis"

    # 自动安装到工作空间
    wa_col = get_collection("ai_workspace_agents")
    if not wa_col.find_one({"req_id": req_id, "agent_id": agent_id}):
        agent = get_collection("ai_agents").find_one({"agent_id": agent_id}, {"_id": 0})
        wa_col.insert_one({
            "req_id": req_id,
            "agent_id": agent_id,
            "agent_name": (agent or {}).get("agent_name", "需求分析"),
            "category": "requirement",
            "status": "running",
            "installed_at": int(time.time()),
        })
    else:
        wa_col.update_one({"req_id": req_id, "agent_id": agent_id}, {"$set": {"status": "running"}})

    task_id = f"task_{uuid.uuid4().hex[:8]}"
    get_collection("ai_task_queue").insert_one({
        "task_id": task_id,
        "task_type": 20,
        "payload": {
            "req_id": req_id,
            "agent_id": agent_id,
            "doc_content": content[:30000],
            "doc_filename": filename,
        },
        "status": 1,
        "created_at": int(time.time()),
    })


@bp.route('/detail', methods=['GET'])
@require_auth
def req_detail():
    req_id = request.args.get('req_id', '')
    if not req_id:
        return err("缺少 req_id")
    req = get_collection("ai_requirements").find_one({"req_id": req_id}, {"_id": 0, "doc_content": 0})
    if not req:
        return err("需求不存在", 404)
    agents = list(get_collection("ai_workspace_agents").find({"req_id": req_id}, {"_id": 0}).sort("installed_at", 1))
    # 附带统计
    stats = {
        "memory_count": get_collection("ai_memory_points").count_documents({"req_id": req_id}),
        "case_count": get_collection("ai_test_cases").count_documents({"req_id": req_id, "status": {"$ne": "obsolete"}}),
        "issue_count": get_collection("ai_workspace_logs").count_documents({"req_id": req_id, "type": "issue", "status": {"$ne": "resolved"}}),
        "doc_count": get_collection("ai_requirement_docs").count_documents({"req_id": req_id}),
    }
    return ok({"requirement": req, "workspace_agents": agents, "stats": stats})


@bp.route('/outputs', methods=['GET'])
@require_auth
def req_outputs():
    """获取需求产出（可按 agent_id 过滤）"""
    req_id = request.args.get('req_id', '')
    agent_id = request.args.get('agent_id', '')
    if not req_id:
        return err("缺少 req_id")
    query = {"req_id": req_id}
    if agent_id:
        query["agent_id"] = agent_id
    outputs = list(get_collection("ai_workspace_outputs").find(query, {"_id": 0}).sort("created_at", -1).limit(10))
    return ok({"outputs": outputs})


@bp.route('/docs', methods=['GET'])
@require_auth
def req_docs():
    """获取需求生成的文档列表"""
    req_id = request.args.get('req_id', '')
    if not req_id:
        return err("缺少 req_id")
    docs = list(get_collection("ai_requirement_docs").find({"req_id": req_id}, {"_id": 0}).sort("created_at", 1))
    return ok({"docs": docs})


@bp.route('/memories', methods=['GET'])
@require_auth
def req_memories():
    """获取需求记忆点列表"""
    req_id = request.args.get('req_id', '')
    if not req_id:
        return err("缺少 req_id")
    points = list(get_collection("ai_memory_points").find(
        {"req_id": req_id}, {"_id": 0}
    ).sort("created_at", -1).limit(50))
    return ok({"memories": points})


@bp.route('/memories/pin', methods=['POST'])
@require_auth
def req_memory_pin():
    """钉选/取消钉选记忆点"""
    data = request.get_json() or {}
    point_id = data.get('point_id', '')
    pinned = data.get('pinned', True)
    if not point_id:
        return err("缺少 point_id")
    get_collection("ai_memory_points").update_one({"point_id": point_id}, {"$set": {"pinned": pinned}})
    return ok({"pinned": pinned})


@bp.route('/cases', methods=['GET'])
@require_auth
def req_cases():
    """获取需求的测试用例"""
    req_id = request.args.get('req_id', '')
    if not req_id:
        return err("缺少 req_id")
    status_filter = request.args.get('status', '')
    query = {"req_id": req_id}
    if status_filter:
        query["status"] = status_filter
    cases = list(get_collection("ai_test_cases").find(query, {"_id": 0}).sort("created_at", -1).limit(100))
    # 统计
    all_cases = list(get_collection("ai_test_cases").find({"req_id": req_id}, {"_id": 0, "status": 1, "priority": 1}))
    stats = {
        "total": len(all_cases),
        "active": sum(1 for c in all_cases if c.get("status") == "active"),
        "passed": sum(1 for c in all_cases if c.get("status") == "passed"),
        "failed": sum(1 for c in all_cases if c.get("status") == "failed"),
    }
    return ok({"cases": cases, "stats": stats})


@bp.route('/issues', methods=['GET'])
@require_auth
def req_issues():
    """获取需求的问题列表"""
    req_id = request.args.get('req_id', '')
    if not req_id:
        return err("缺少 req_id")
    issues = list(get_collection("ai_workspace_logs").find(
        {"req_id": req_id, "type": "issue"}, {"_id": 0}
    ).sort("timestamp", -1).limit(50))
    return ok({"issues": issues})


@bp.route('/resolve_issue', methods=['POST'])
@require_auth
def req_resolve_issue():
    """标记问题状态"""
    data = request.get_json() or {}
    req_id = data.get('req_id', '')
    title = data.get('title', '')
    action = data.get('action', 'resolved')
    if not req_id or not title:
        return err("缺少参数")
    get_collection("ai_workspace_logs").update_one(
        {"req_id": req_id, "type": "issue", "chunk": title},
        {"$set": {"status": action, "resolved_at": int(time.time()), "resolved_by": get_current_user()}}
    )
    return ok({"action": action})


@bp.route('/logs', methods=['GET'])
@require_auth
def req_logs():
    """获取需求日志（结论+进度）"""
    req_id = request.args.get('req_id', '')
    agent_id = request.args.get('agent_id', '')
    offset = int(request.args.get('offset', 0))
    if not req_id:
        return err("缺少 req_id")
    query = {"req_id": req_id}
    if agent_id:
        query["agent_id"] = agent_id
    logs = list(get_collection("ai_workspace_logs").find(
        query, {"_id": 0}
    ).sort("timestamp", 1).skip(offset).limit(50))
    return ok({"logs": logs, "count": len(logs)})


@bp.route('/device_callback', methods=['POST'])
def req_device_callback():
    """Device Worker 任务完成回调 — 写产出 + 触发记忆体主管评估"""
    data = request.get_json() or {}
    req_id = data.get('req_id', '')
    agent_id = data.get('agent_id', '')
    task_id = data.get('task_id', '')
    status = data.get('status', '')  # completed / failed
    summary = data.get('summary', '')
    detail = data.get('detail', '')
    report_url = data.get('report_url', '')

    if not req_id or not agent_id:
        return err("缺少 req_id 或 agent_id")

    if status == 'completed':
        # 写产出
        content = f"## 设备执行结果\n\n{summary}"
        if detail:
            content += f"\n\n{detail}"
        if report_url:
            content += f"\n\n[📄 测试报告]({report_url})"

        get_collection("ai_workspace_outputs").insert_one({
            "req_id": req_id, "agent_id": agent_id,
            "content": content, "round": 1, "task_id": task_id,
            "source": "device_worker", "created_at": int(time.time()),
        })
        get_collection("ai_workspace_logs").insert_one({
            "req_id": req_id, "agent_id": agent_id,
            "chunk": f"✅ 设备任务完成: {summary}",
            "status": "completed", "timestamp": int(time.time()), "source": "device_worker",
        })
        get_collection("ai_workspace_agents").update_one(
            {"req_id": req_id, "agent_id": agent_id},
            {"$set": {"status": "completed", "finished_at": int(time.time())}}
        )

        # 触发记忆体主管评估
        _trigger_memory_evaluation(req_id, agent_id, summary, detail)

    else:
        # 失败
        get_collection("ai_workspace_logs").insert_one({
            "req_id": req_id, "agent_id": agent_id,
            "chunk": f"❌ 设备任务失败: {summary}",
            "status": "error", "timestamp": int(time.time()), "source": "device_worker",
        })
        get_collection("ai_workspace_agents").update_one(
            {"req_id": req_id, "agent_id": agent_id},
            {"$set": {"status": "error"}}
        )

    return ok({"received": True})


def _trigger_memory_evaluation(req_id, agent_id, summary, detail):
    """触发记忆体主管评估设备执行结果"""
    import json as _json
    from llm.llm_factory import LLMFactory

    try:
        # 获取目标
        req = get_collection("ai_requirements").find_one({"req_id": req_id}, {"_id": 0})
        acceptance_text = ""
        if req and req.get("active_goal"):
            acceptance_text = "\n".join(f"- {a['desc']}" for a in req["active_goal"].get("acceptance", []))

        # 已有记忆
        existing = list(get_collection("ai_memory_points").find(
            {"req_id": req_id}, {"_id": 0, "summary": 1}
        ).sort("created_at", -1).limit(5))
        memory_ctx = "\n".join(p["summary"] for p in existing) or "（无）"

        eval_prompt = f"""设备任务执行完成，请评估结果：

[执行结果]
{summary}
{detail[:1500]}

[当前目标]
{acceptance_text or '（未设定）'}

[已有记忆]
{memory_ctx}

输出 JSON：
{{"conclusion": "一句话总结", "memory_point": "精简记忆（存入记忆库）", "quality": {{"status": "pass/fail/partial"}}, "suggestion": "下一步建议"}}
只输出 JSON。"""

        result = LLMFactory.generate("gemini_flash", "你是记忆体主管，只输出JSON。", eval_prompt)
        eval_text = result["text"].strip()
        if "```" in eval_text:
            eval_text = eval_text.split("```")[1]
            if eval_text.startswith("json"):
                eval_text = eval_text[4:]
        evaluation = _json.loads(eval_text.strip())

        # 写记忆点
        get_collection("ai_memory_points").insert_one({
            "point_id": f"mp_{uuid.uuid4().hex[:8]}",
            "req_id": req_id, "agent_id": agent_id,
            "run_context": {"source": "device_execution_eval"},
            "summary": evaluation.get("memory_point", evaluation.get("conclusion", summary)),
            "key_facts": [],
            "quality": evaluation.get("quality", {}),
            "source": "steward_evaluation",
            "created_at": int(time.time()),
        })

        # 写结论日志
        get_collection("ai_workspace_logs").insert_one({
            "req_id": req_id, "agent_id": agent_id,
            "type": "conclusion",
            "chunk": evaluation.get("conclusion", ""),
            "evaluation": evaluation,
            "status": "completed", "timestamp": int(time.time()), "source": "steward",
        })

    except Exception as e:
        # 评估失败不影响主流程
        get_collection("ai_memory_points").insert_one({
            "point_id": f"mp_{uuid.uuid4().hex[:8]}",
            "req_id": req_id, "agent_id": agent_id,
            "run_context": {"source": "device_execution"},
            "summary": f"设备执行完成: {summary}",
            "key_facts": [], "source": "fallback", "created_at": int(time.time()),
        })


@bp.route('/install_agent', methods=['POST'])
@require_auth
def req_install_agent():
    """安装智能体到需求工作空间（注册 timeline）"""
    data = request.get_json() or {}
    req_id = data.get('req_id', '')
    agent_id = data.get('agent_id', '')
    if not req_id or not agent_id:
        return err("缺少 req_id 或 agent_id")

    # 检查是否已安装
    col = get_collection("ai_workspace_agents")
    existing = col.find_one({"req_id": req_id, "agent_id": agent_id})
    if existing:
        return err("该智能体已安装")

    # 从 ai_agents 获取基本信息
    agent = get_collection("ai_agents").find_one({"agent_id": agent_id}, {"_id": 0})
    if not agent:
        return err("智能体不存在")

    # 注册到工作空间
    doc = {
        "req_id": req_id,
        "agent_id": agent_id,
        "agent_name": agent.get("agent_name", ""),
        "category": agent.get("category", ""),
        "handler_class": agent.get("handler_class", ""),
        "model_id": agent.get("model_id", ""),
        "inputs": agent.get("inputs", []),
        "status": "idle",
        "tokens": 0,
        "installed_at": int(time.time()),
        "installed_by": get_current_user(),
    }
    col.insert_one(doc)
    return ok({"installed": True, "agent_name": doc["agent_name"]})


@bp.route('/uninstall_agent', methods=['POST'])
@require_auth
def req_uninstall_agent():
    """从工作空间卸载智能体"""
    data = request.get_json() or {}
    req_id = data.get('req_id', '')
    agent_id = data.get('agent_id', '')
    if not req_id or not agent_id:
        return err("缺少 req_id 或 agent_id")
    get_collection("ai_workspace_agents").delete_one({"req_id": req_id, "agent_id": agent_id})
    return ok({"uninstalled": True})


@bp.route('/delete', methods=['POST'])
@require_auth
def req_delete():
    data = request.get_json() or {}
    req_id = data.get('req_id', '')
    if not req_id:
        return err("缺少 req_id")
    get_collection("ai_requirements").delete_one({"req_id": req_id})
    return ok({"deleted": True})


@bp.route('/run_agent', methods=['POST'])
@require_auth
def req_run_agent():
    """运行智能体（同步执行 LLM，结果写入产出 + 记忆）"""
    data = request.get_json() or {}
    req_id = data.get('req_id', '')
    agent_id = data.get('agent_id', '')
    inputs = data.get('inputs', {})

    if not req_id or not agent_id:
        return err("缺少 req_id 或 agent_id")

    # 获取智能体配置
    agent = get_collection("ai_agents").find_one({"agent_id": agent_id}, {"_id": 0})
    if not agent:
        return err("智能体不存在")

    # 获取需求信息
    req = get_collection("ai_requirements").find_one({"req_id": req_id}, {"_id": 0})
    if not req:
        return err("需求不存在")

    # 检查是否需要异步执行（设备类智能体）
    if agent.get('category') == 'device' or agent.get('handler_class'):
        # 异步入队
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        get_collection("ai_task_queue").insert_one({
            "task_id": task_id,
            "task_type": 30 if agent.get('handler_class') == 'script_gen' else 20,
            "payload": {
                "req_id": req_id,
                "agent_id": agent_id,
                "inputs": inputs,
                "handler_class": agent.get('handler_class', ''),
                "system_prompt": agent.get('system_prompt', ''),
                "model_id": agent.get('model_id', 'gemini_flash'),
                "doc_content": req.get('doc_content', '') or req.get('description', ''),
            },
            "status": 1,
            "created_at": int(time.time()),
        })
        # 更新状态
        get_collection("ai_workspace_agents").update_one(
            {"req_id": req_id, "agent_id": agent_id},
            {"$set": {"status": "running", "started_at": int(time.time())}}
        )
        return ok({"status": "queued", "task_id": task_id})

    # 构建 prompt
    system_prompt = agent.get("system_prompt", "你是一个专业的测试分析助手。")
    model_id = agent.get("model_id", "gemini_flash")

    # 构建 user prompt（从需求 + inputs 组合）
    user_prompt = f"需求标题：{req.get('title', '')}\n需求描述：{req.get('description', '')}\n"
    if inputs:
        user_prompt += f"\n附加信息：{str(inputs)}"

    # 更新状态为运行中
    get_collection("ai_workspace_agents").update_one(
        {"req_id": req_id, "agent_id": agent_id},
        {"$set": {"status": "running", "started_at": int(time.time())}},
        upsert=True
    )

    try:
        # 调用 LLM
        from llm.llm_factory import LLMFactory
        result = LLMFactory.generate(model_id, system_prompt, user_prompt)

        output_text = result["text"]
        usage = result["usage"]
        actual_model = result.get("actual_model", model_id)

        # 写入产出
        get_collection("ai_workspace_outputs").insert_one({
            "req_id": req_id,
            "agent_id": agent_id,
            "content": output_text,
            "round": 1,
            "model_id": actual_model,
            "tokens": usage,
            "created_at": int(time.time()),
        })

        # 写入记忆体（结论）
        get_collection("ai_workspace_logs").insert_one({
            "req_id": req_id,
            "agent_id": agent_id,
            "chunk": f"✅ {agent.get('agent_name', '智能体')} 分析完成，产出 {len(output_text)} 字符",
            "status": "completed",
            "timestamp": int(time.time()),
            "source": "ai_worker",
        })

        # 更新状态为完成
        get_collection("ai_workspace_agents").update_one(
            {"req_id": req_id, "agent_id": agent_id},
            {"$set": {"status": "completed", "tokens": usage.get("total_tokens", 0), "finished_at": int(time.time())}}
        )

        # 记录 token 用量
        LLMFactory.record_usage(actual_model, usage, caller=agent.get("agent_name", agent_id), req_id=req_id)

        # === 记忆体主管评估 ===
        # 记忆体以主管视角审视智能体产出，输出完整评估 + 提取记忆
        try:
            # 获取当前目标
            acceptance_text = ""
            if req.get("active_goal"):
                acceptance_text = "\n".join(f"- {a['desc']}" for a in req["active_goal"].get("acceptance", []))

            # 获取已有记忆（上下文）
            existing_memory = list(get_collection("ai_memory_points").find(
                {"req_id": req_id}, {"_id": 0, "summary": 1}
            ).sort("created_at", -1).limit(5))
            memory_context = "\n".join(p["summary"] for p in existing_memory) or "（首次分析）"

            eval_prompt = f"""你是这个需求的记忆体主管。以下是一个智能体刚完成的工作报告，请进行完整评估。

[智能体信息]
名称：{agent.get('agent_name', '')}
运行参数：{str(inputs) if inputs else '默认'}
模型：{actual_model}

[当前测试目标]
{acceptance_text or '（未设定目标）'}

[已有记忆]
{memory_context}

[智能体产出（截取）]
{output_text[:2500]}

请以主管视角评估，输出严格 JSON：
{{
  "conclusion": "一句话总结这个智能体做了什么、结果如何",
  "quality": {{
    "hallucination_risk": "low/medium/high",
    "confidence": 0.0到1.0,
    "issues": ["发现的问题（如有）"]
  }},
  "goal_alignment": {{
    "affected_acceptance": ["受影响的验收条件描述"],
    "progress_note": "对目标进度的影响说明"
  }},
  "actions": [
    {{"type": "case_generated/doc_updated/knowledge_candidate/none", "detail": "具体说明"}}
  ],
  "suggestion": "对用户的下一步建议（一句话）",
  "memory_point": "从评估结论中提炼的一条精简记忆（存入记忆库）"
}}
只输出 JSON。"""

            import json as _json
            eval_result = LLMFactory.generate("gemini_flash", "你是记忆体主管，只输出JSON。", eval_prompt)
            eval_text = eval_result["text"].strip()
            if "```" in eval_text:
                eval_text = eval_text.split("```")[1]
                if eval_text.startswith("json"):
                    eval_text = eval_text[4:]
            evaluation = _json.loads(eval_text.strip())

            # 从评估结论中提取记忆点
            get_collection("ai_memory_points").insert_one({
                "point_id": f"mp_{uuid.uuid4().hex[:8]}",
                "req_id": req_id,
                "agent_id": agent_id,
                "run_context": {
                    "agent_name": agent.get("agent_name", ""),
                    "inputs": inputs,
                    "model_id": actual_model,
                },
                "summary": evaluation.get("memory_point", evaluation.get("conclusion", "")),
                "key_facts": evaluation.get("goal_alignment", {}).get("affected_acceptance", []),
                "quality": evaluation.get("quality", {}),
                "source": "steward_evaluation",
                "created_at": int(time.time()),
            })

            # 写入结论日志（前端主面板展示这个，不是原文）
            get_collection("ai_workspace_logs").insert_one({
                "req_id": req_id,
                "agent_id": agent_id,
                "type": "conclusion",
                "chunk": evaluation.get("conclusion", ""),
                "evaluation": evaluation,
                "status": "completed",
                "timestamp": int(time.time()),
                "source": "steward",
            })

        except Exception as e:
            # 评估失败不影响主流程，降级为简单记忆
            get_collection("ai_memory_points").insert_one({
                "point_id": f"mp_{uuid.uuid4().hex[:8]}",
                "req_id": req_id,
                "agent_id": agent_id,
                "run_context": {"agent_name": agent.get("agent_name", "")},
                "summary": output_text[:150],
                "key_facts": [],
                "source": "fallback",
                "created_at": int(time.time()),
            })
            evaluation = {"conclusion": output_text[:100], "quality": {}, "suggestion": ""}

        return ok({
            "output": output_text[:500],
            "evaluation": evaluation,
            "tokens": usage,
            "model": actual_model,
        })

    except Exception as e:
        # 失败状态
        get_collection("ai_workspace_agents").update_one(
            {"req_id": req_id, "agent_id": agent_id},
            {"$set": {"status": "error", "error_msg": str(e)[:200]}}
        )
        return err(f"运行失败: {str(e)[:100]}")


@bp.route('/goal/set', methods=['POST'])
@require_auth
def req_goal_set():
    """设定/初始化需求的测试目标"""
    data = request.get_json() or {}
    req_id = data.get('req_id', '')
    acceptance = data.get('acceptance', [])  # [{"desc": "xxx"}]
    source_doc = data.get('source_doc', '')

    if not req_id or not acceptance:
        return err("缺少 req_id 或 acceptance")

    col = get_collection("ai_requirements")
    req = col.find_one({"req_id": req_id}, {"_id": 0})
    if not req:
        return err("需求不存在")

    # 如果已有 active_goal，归档到历史
    old_goal = req.get("active_goal")
    history = req.get("goal_history", [])
    if old_goal:
        old_goal["status"] = "archived"
        history.append(old_goal)

    new_version = (old_goal["version"] + 1) if old_goal else 1
    new_goal = {
        "version": new_version,
        "acceptance": [{"id": f"ac_{i}", "desc": a["desc"], "status": "pending"} for i, a in enumerate(acceptance)],
        "source_doc": source_doc[:500],
        "status": "active",
        "created_at": int(time.time()),
    }

    col.update_one({"req_id": req_id}, {"$set": {
        "active_goal": new_goal,
        "goal_history": history,
        "pending_switch": None,
    }})
    return ok({"version": new_version, "acceptance_count": len(acceptance)})


@bp.route('/goal/assess', methods=['POST'])
@require_auth
def req_goal_assess():
    """评估新信息与当前目标的匹配度（每次新产出/新文档时调用）"""
    data = request.get_json() or {}
    req_id = data.get('req_id', '')
    new_info = data.get('new_info', '')

    if not req_id or not new_info:
        return err("缺少 req_id 或 new_info")

    col = get_collection("ai_requirements")
    req = col.find_one({"req_id": req_id}, {"_id": 0})
    if not req or not req.get("active_goal"):
        return ok({"alignment": 100, "action": "none", "reason": "无目标，跳过评估"})

    active = req["active_goal"]
    acceptance_text = "\n".join(f"- {a['desc']}" for a in active.get("acceptance", []))

    from llm.llm_factory import LLMFactory
    import json as _json

    prompt = f"""评估新信息是否与当前测试目标一致。

当前测试目标：
{acceptance_text}

新信息：
{new_info[:1500]}

输出严格 JSON：
{{"alignment": 0到100的整数, "reason": "一句话原因", "action": "none|expand|switch", "suggested_acceptance": ["如果需要扩展或切换，建议的新验收条件"]}}
只输出 JSON。"""

    result = LLMFactory.generate("gemini_flash", "你是目标对齐评估器，只输出JSON。", prompt)
    try:
        text = result["text"].strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        assessment = _json.loads(text.strip())
    except Exception:
        assessment = {"alignment": 80, "action": "none", "reason": "评估解析失败"}

    alignment = assessment.get("alignment", 80)
    action = assessment.get("action", "none")

    # 如果偏离，更新 pending_switch（覆盖之前的）
    if alignment < 60:
        col.update_one({"req_id": req_id}, {"$set": {
            "pending_switch": {
                "latest_info": new_info[:500],
                "alignment": alignment,
                "reason": assessment.get("reason", ""),
                "action": action,
                "suggested_acceptance": assessment.get("suggested_acceptance", []),
                "created_at": int(time.time()),
            }
        }})

    return ok(assessment)


@bp.route('/goal/switch', methods=['POST'])
@require_auth
def req_goal_switch():
    """用户确认切换目标"""
    data = request.get_json() or {}
    req_id = data.get('req_id', '')

    if not req_id:
        return err("缺少 req_id")

    col = get_collection("ai_requirements")
    req = col.find_one({"req_id": req_id}, {"_id": 0})
    if not req:
        return err("需求不存在")

    pending = req.get("pending_switch")
    if not pending:
        return err("无待切换目标")

    # 1. 旧目标归档
    old_goal = req.get("active_goal", {})
    history = req.get("goal_history", [])
    if old_goal:
        old_goal["status"] = "superseded"
        old_goal["superseded_reason"] = pending.get("reason", "用户手动切换")
        history.append(old_goal)

    # 2. 生成新目标
    suggested = pending.get("suggested_acceptance", [])
    new_version = (old_goal.get("version", 0) + 1) if old_goal else 1

    if suggested:
        new_acceptance = [{"id": f"ac_{i}", "desc": s, "status": "pending"} for i, s in enumerate(suggested)]
    else:
        # 没有建议时用 pending 的 latest_info 让 LLM 生成
        from llm.llm_factory import LLMFactory
        import json as _json
        prompt = f"基于以下信息生成3-5条测试验收条件，输出JSON数组：[\"条件1\",\"条件2\"]\n\n{pending.get('latest_info', '')}"
        result = LLMFactory.generate("gemini_flash", "只输出JSON数组。", prompt)
        try:
            items = _json.loads(result["text"].strip().strip("`").replace("json", ""))
            new_acceptance = [{"id": f"ac_{i}", "desc": s, "status": "pending"} for i, s in enumerate(items)]
        except Exception:
            new_acceptance = [{"id": "ac_0", "desc": pending.get("latest_info", "")[:100], "status": "pending"}]

    new_goal = {
        "version": new_version,
        "acceptance": new_acceptance,
        "source_doc": pending.get("latest_info", ""),
        "status": "active",
        "created_at": int(time.time()),
    }

    # 3. 旧 case 标记版本 + pending 标 obsolete
    old_version = old_goal.get("version", 0) if old_goal else 0
    cases_col = get_collection("ai_test_cases")
    cases_col.update_many(
        {"req_id": req_id, "goal_version": {"$exists": False}},
        {"$set": {"goal_version": old_version}}
    )
    cases_col.update_many(
        {"req_id": req_id, "status": "pending", "goal_version": old_version},
        {"$set": {"status": "obsolete"}}
    )

    # 4. 保存
    col.update_one({"req_id": req_id}, {"$set": {
        "active_goal": new_goal,
        "goal_history": history,
        "pending_switch": None,
    }})

    # 5. 记忆点
    get_collection("ai_memory_points").insert_one({
        "point_id": f"mp_{uuid.uuid4().hex[:8]}",
        "req_id": req_id,
        "agent_id": "system",
        "run_context": {"action": "goal_switch"},
        "summary": f"目标切换 v{old_version} → v{new_version}: {pending.get('reason', '')}",
        "key_facts": [a["desc"] for a in new_acceptance[:3]],
        "source": "goal_switch",
        "created_at": int(time.time()),
    })

    return ok({"new_version": new_version, "acceptance": new_acceptance})


@bp.route('/goal/current', methods=['GET'])
@require_auth
def req_goal_current():
    """获取当前目标 + pending_switch 状态"""
    req_id = request.args.get('req_id', '')
    if not req_id:
        return err("缺少 req_id")
    req = get_collection("ai_requirements").find_one({"req_id": req_id}, {"_id": 0, "active_goal": 1, "pending_switch": 1, "goal_history": 1})
    if not req:
        return err("需求不存在")
    return ok({
        "active_goal": req.get("active_goal"),
        "pending_switch": req.get("pending_switch"),
        "history_count": len(req.get("goal_history", [])),
    })


@bp.route('/chat', methods=['POST'])
@require_auth
def req_chat():
    """记忆体对话 — SSE 流式返回"""
    import re
    data = request.get_json() or {}
    req_id = data.get('req_id', '')
    question = data.get('question', '')

    if not req_id or not question:
        return err("缺少 req_id 或 question")

    # 收集上下文
    memory_points = list(get_collection("ai_memory_points").find(
        {"req_id": req_id}, {"_id": 0}
    ).sort("created_at", -1).limit(20))
    memory_text = "\n".join(
        f"[{p.get('run_context', {}).get('agent_name', '系统')}] {p.get('summary', '')}"
        for p in memory_points
    ) or "（暂无记忆）"

    req = get_collection("ai_requirements").find_one({"req_id": req_id}, {"_id": 0})
    goal_text = "（未设定目标）"
    if req and req.get("active_goal"):
        goal = req["active_goal"]
        goal_text = f"v{goal['version']}:\n" + "\n".join(f"- {a['desc']}" for a in goal.get("acceptance", []))

    # 知识库搜索
    stop_words = {'的', '了', '是', '在', '和', '与', '或', '有', '相关', '关于', '什么', '怎么', '如何', '哪些', '吗', '呢', '啊', '这个', '那个', '能', '要', '会', '没有', '知识库', '信息'}
    raw_words = [w for w in re.split(r'[\s,，。、?？!！]+', question) if w and w not in stop_words]
    keywords = []
    for w in raw_words:
        if len(w) <= 3:
            keywords.append(w)
        else:
            for i in range(0, len(w) - 1, 2):
                seg = w[i:i+2]
                if seg not in stop_words:
                    keywords.append(seg)
    keywords = list(dict.fromkeys(keywords))[:6]

    knowledge_text = ""
    if keywords:
        or_conditions = []
        for kw in keywords:
            or_conditions.extend([
                {"title": {"$regex": re.escape(kw), "$options": "i"}},
                {"tags": {"$regex": re.escape(kw), "$options": "i"}},
            ])
        kb_results = list(get_collection("ai_knowledge_base").find(
            {"$or": or_conditions}, {"_id": 0, "title": 1, "content": 1}
        ).limit(3))
        if kb_results:
            knowledge_text = "\n[知识库]\n" + "\n".join(f"• {k['title']}: {k.get('content', '')[:150]}" for k in kb_results)

    from llm.llm_factory import LLMFactory
    from flask import Response, stream_with_context

    system_prompt = """你是这个需求的记忆管家。基于记忆和知识库回答问题。如果找不到答案说"当前记忆中没有相关信息"。"""
    user_prompt = f"[目标]\n{goal_text}\n\n[记忆]\n{memory_text}{knowledge_text}\n\n[问题]\n{question}"

    # 保存用户消息
    get_collection("ai_conversations").insert_one({"req_id": req_id, "role": "user", "content": question, "timestamp": int(time.time())})

    def generate():
        provider = LLMFactory.create("gemini_flash")
        full_text = ""
        try:
            for chunk in provider.stream_generate(system_prompt, user_prompt):
                full_text += chunk
                yield f"data: {chunk}\n\n"
        except Exception as e:
            yield f"data: [ERROR]{str(e)}\n\n"
        yield "data: [DONE]\n\n"
        # 保存完整回复
        get_collection("ai_conversations").insert_one({"req_id": req_id, "role": "ai", "content": full_text, "timestamp": int(time.time())})

    return Response(stream_with_context(generate()), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@bp.route('/chat/history', methods=['GET'])
@require_auth
def req_chat_history():
    """获取对话历史"""
    req_id = request.args.get('req_id', '')
    if not req_id:
        return err("缺少 req_id")
    messages = list(get_collection("ai_conversations").find(
        {"req_id": req_id}, {"_id": 0}
    ).sort("timestamp", 1).limit(50))
    return ok({"messages": messages})
