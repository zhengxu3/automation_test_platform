"""需求分析任务 — 解析文档 + 调用 LLM 分析"""
import asyncio
import re
import time
from ai_worker.base_task import BaseTaskHandler
from common.db import get_collection
from llm.llm_factory import LLMFactory

HANDLER_META = {
    "key": "requirement_analysis",
    "label": "需求分析",
    "description": "解析需求文档，提取测试点和验证场景",
    "inputs": [],
}


class RequirementAnalysisTask(BaseTaskHandler):
    """Task Type 20 — 通用智能体执行（需求分析/自定义 handler 分发）"""

    async def run(self):
        handler_class = self.payload.get("handler_class", "")
        if handler_class:
            return await self._dispatch_handler(handler_class)
        else:
            return await self._run_generic_analysis()

    # ======== 执行器分发 ========
    async def _dispatch_handler(self, handler_class: str):
        req_id = self.payload.get("req_id", "")
        agent_id = self.payload.get("agent_id", "")

        # 从 agent 表读 handler 注册信息
        agent = get_collection("ai_agents").find_one({"agent_id": agent_id}, {"_id": 0})
        if not agent:
            self.log(f"⚠️ 智能体 {agent_id} 不存在，降级为通用分析", "warning")
            return await self._run_generic_analysis()

        self.log(f"🚀 启动执行器: {handler_class}")

        # 准备 payload：合并 inputs 中的 repo 信息
        merged = {**self.payload}
        inputs = self.payload.get("inputs", {})
        for key, value in inputs.items():
            if "repo" in key and value:
                repo = get_collection("ai_git_repos").find_one({"repo_id": value}, {"_id": 0})
                if repo:
                    if repo.get("lock", {}).get("locked"):
                        raise ValueError(f"仓库 {repo['repo_name']} 被锁定")
                    self.log(f"📦 加载仓库: {repo['repo_name']}/{repo['branch']}")
                    if "single" in key:
                        merged["repo_name"] = repo["repo_name"]
                        merged["repo_path"] = repo["local_path"]
                        merged["base_branch"] = repo["branch"]
                        merged["target_branch"] = repo["branch"]
                    elif "master" in key or key == "repo_a":
                        merged["repo_name"] = repo["repo_name"]
                        merged["repo_path"] = repo["local_path"]
                        merged["base_branch"] = repo["branch"]
                    elif "branch" in key or key == "repo_b":
                        merged["target_branch"] = repo["branch"]
                        merged["target_repo_path"] = repo["local_path"]
            elif "repo" not in key:
                merged[key] = value
        merged["inputs"] = inputs

        # 动态导入执行器
        import importlib
        module_map = {
            "branch_review": ("ai_worker.tasks.branch_review_task", "BranchReviewTask"),
        }
        if handler_class not in module_map:
            self.log(f"⚠️ 未知执行器 [{handler_class}]，降级通用分析", "warning")
            return await self._run_generic_analysis()

        mod_path, cls_name = module_map[handler_class]
        mod = importlib.import_module(mod_path)
        TaskClass = getattr(mod, cls_name)
        handler = TaskClass(task_id=self.task_id, payload=merged)
        sub_output = await handler.run()

        self._update_agent_status(req_id, agent_id, "completed")
        return sub_output

    # ======== 通用 LLM 分析 ========
    async def _run_generic_analysis(self):
        req_id = self.payload["req_id"]
        agent_id = self.payload["agent_id"]

        self.log("═══ 需求分析智能体启动 ═══")
        self._update_agent_status(req_id, agent_id, "running")

        try:
            doc_content = self.payload.get("doc_content", "") or self.payload.get("description", "")
            if not doc_content or len(doc_content.strip()) < 20:
                raise ValueError("需求文档内容为空，无法分析")

            agent = get_collection("ai_agents").find_one({"agent_id": agent_id}, {"_id": 0})
            system_prompt = self.payload.get("system_prompt", "") or (agent or {}).get("system_prompt", "")
            model_id = self.payload.get("model_id", "") or (agent or {}).get("model_id", "gemini_flash")

            self.log(f"📄 文档: {self.payload.get('doc_filename', '手动输入')} ({len(doc_content)} 字符)")
            self.log(f"🧠 模型: {model_id}")

            complexity = self._assess_complexity(doc_content)
            self.log(f"📊 复杂度: {complexity}/8 ({'复杂模式' if complexity >= 4 else '标准模式'})")

            memory_context = self.payload.get("memory_context", "")

            if complexity >= 4:
                text, usage = await self._run_complex(system_prompt, doc_content, memory_context, model_id)
            else:
                text, usage = await self._run_simple(system_prompt, doc_content, memory_context, model_id)

            if len(text.strip()) < 50:
                raise ValueError(f"模型产出无效（仅 {len(text)} 字符）")

            self.log(f"📊 Token: {usage.get('total_tokens', 0)}")

            # 保存产出
            get_collection("ai_workspace_outputs").insert_one({
                "req_id": req_id, "agent_id": agent_id, "task_id": self.task_id,
                "content": text, "format": "markdown",
                "round": 1, "created_at": int(time.time()),
            })

            self.log(f"✅ 分析完成，产出 {len(text)} 字符", "success")

            # 解析 [DOC:xxx] 标记，拆成独立文档
            docs_saved = self._parse_and_save_docs(req_id, agent_id, text)
            if docs_saved:
                self.log(f"📄 已生成 {len(docs_saved)} 个文档: {', '.join(d['doc_type_label'] for d in docs_saved)}")

            # 从产出中提取测试用例写入 ai_test_cases
            case_titles = self._extract_cases(req_id, agent_id, text)

            # 写入记忆点
            import uuid as _uuid
            get_collection("ai_memory_points").insert_one({
                "point_id": f"mp_{_uuid.uuid4().hex[:8]}",
                "req_id": req_id,
                "agent_id": agent_id,
                "run_context": {"agent_name": "需求分析", "model_id": model_id},
                "summary": f"需求分析完成：生成{len(docs_saved)}个文档，提取测试用例，复杂度{complexity}/8",
                "key_facts": [d["doc_type_label"] for d in docs_saved],
                "source": "requirement_analysis",
                "pinned": False,
                "created_at": int(time.time()),
            })

            # 更新需求状态为 ready
            get_collection("ai_requirements").update_one(
                {"req_id": req_id}, {"$set": {"status": "ready", "updated_at": int(time.time())}}
            )
            self._update_agent_status(req_id, agent_id, "completed")

            # 结构化产出（goal 模式契约校验用；req 模式忽略返回值）
            acceptance_points = case_titles or [d["doc_type_label"] for d in docs_saved]
            return {
                "acceptance_points": acceptance_points,
                "test_cases": case_titles,
                "docs": [d["doc_type_label"] for d in docs_saved],
                "summary": f"需求分析完成：{len(docs_saved)}个文档，{len(case_titles)}条用例",
                "confidence": 0.85,
            }

        except Exception as e:
            self.log(f"❌ 分析失败: {e}", "error")
            get_collection("ai_requirements").update_one(
                {"req_id": req_id}, {"$set": {"status": "ready", "updated_at": int(time.time())}}
            )
            self._update_agent_status(req_id, agent_id, "error")
            raise

    async def _run_simple(self, system_prompt, doc_content, memory_context, model_id):
        user_prompt = f"""【已知信息】\n{memory_context or '(暂无)'}\n\n【需求文档】\n{doc_content}\n\n请按以下格式输出：\n\n## [DOC:requirement_breakdown] 需求拆解\n\n## [DOC:test_cases] 测试用例\n\n## [DOC:test_strategy] 测试策略"""
        result = await self._call_llm(system_prompt, user_prompt, model_id)
        return result["text"], result.get("usage", {})

    async def _run_complex(self, system_prompt, doc_content, memory_context, model_id):
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        # Step 1: 理解
        self.log("📖 Step 1/3: 结构化理解...")
        r1 = await self._call_llm(system_prompt, f"提取需求结构化信息（功能模块、业务规则、约束、异常场景）：\n\n{doc_content}", model_id)
        understanding = r1["text"]
        self._acc(total_usage, r1.get("usage", {}))

        # Step 2: 生成
        self.log("📝 Step 2/3: 生成文档...")
        r2 = await self._call_llm(system_prompt, f"基于理解生成分析：\n{understanding}\n\n输出：\n## [DOC:requirement_breakdown] 需求拆解\n## [DOC:test_cases] 测试用例\n## [DOC:test_strategy] 测试策略", model_id)
        text = r2["text"]
        self._acc(total_usage, r2.get("usage", {}))

        # Step 3: 自检
        self.log("🔍 Step 3/3: 自检遗漏...")
        r3 = await self._call_llm(system_prompt, f"对比原始需求和产出，有遗漏输出补充，无遗漏只输出\"无遗漏\"：\n\n原始：{doc_content[:2000]}\n\n产出：{text[:2000]}", model_id)
        if "无遗漏" not in r3["text"] and len(r3["text"].strip()) > 50:
            text += f"\n\n## 补充\n{r3['text']}"
            self.log("⚠️ 发现遗漏，已补充")
        else:
            self.log("✅ 自检通过")
        self._acc(total_usage, r3.get("usage", {}))

        return text, total_usage

    async def _call_llm(self, system_prompt, user_prompt, model_id):
        return await asyncio.to_thread(LLMFactory.generate, model_id, system_prompt, user_prompt)

    def _assess_complexity(self, doc):
        score = 0
        if len(doc) > 3000: score += 2
        elif len(doc) > 1000: score += 1
        sections = len(re.findall(r'[一二三四五六七八九十]、|#{1,3}\s|^\d+[\.\)、]', doc, re.MULTILINE))
        if sections > 6: score += 2
        elif sections > 3: score += 1
        ui_kw = ['页面', '按钮', '弹窗', '交互', '跳转', '输入框', '列表']
        if sum(1 for k in ui_kw if k in doc) >= 3: score += 2
        elif sum(1 for k in ui_kw if k in doc) >= 1: score += 1
        edge_kw = ['异常', '边界', '超时', '失败', '重试', '并发']
        if sum(1 for k in edge_kw if k in doc) >= 2: score += 1
        return min(score, 8)

    def _update_agent_status(self, req_id, agent_id, status):
        get_collection("ai_workspace_agents").update_one(
            {"req_id": req_id, "agent_id": agent_id},
            {"$set": {"status": status, "updated_at": int(time.time())}},
        )

    def _acc(self, total, new):
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            total[k] = total.get(k, 0) + new.get(k, 0)

    def _parse_and_save_docs(self, req_id, agent_id, output_text):
        """从产出解析 [DOC:xxx] 标记，拆成独立文档"""
        import uuid as _uuid
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
                "doc_id": f"doc_{_uuid.uuid4().hex[:8]}",
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

    def _extract_cases(self, req_id, agent_id, output_text):
        """从测试用例文档段提取 case 列表写入 ai_test_cases。返回用例标题列表。"""
        import uuid as _uuid
        # 找 test_cases 段落
        case_match = re.search(r'\[DOC:test_cases\]\s*(.+?)(?=##\s*\[DOC:|\Z)', output_text, re.DOTALL)
        if not case_match:
            return []
        case_text = case_match.group(1)
        # 按编号或 - 分割用例
        cases = re.findall(r'(?:^|\n)\s*(?:\d+[\.\)、]|-|\*)\s*(.+?)(?=\n\s*(?:\d+[\.\)、]|-|\*)|\Z)', case_text, re.DOTALL)
        if not cases:
            return []

        col = get_collection("ai_test_cases")
        titles = []
        for i, case_text_item in enumerate(cases[:50]):  # 最多50条
            title = case_text_item.strip().split('\n')[0][:200]
            if len(title) < 5:
                continue
            titles.append(title)
            col.insert_one({
                "case_id": f"case_{_uuid.uuid4().hex[:8]}",
                "req_id": req_id,
                "agent_id": agent_id,
                "title": title,
                "detail": case_text_item.strip(),
                "priority": "P1" if i < 5 else "P2",
                "status": "active",
                "source_round": 1,
                "created_at": int(time.time()),
            })
        self.log(f"🧪 提取 {len(titles)} 条测试用例")
        return titles
