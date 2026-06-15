"""StepInputResolver + AgentRuntime 单测"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import uuid
import pytest

from common.db import get_collection
from engine import step_input_resolver as sir
from engine import agent_runtime as art


# ==================== StepInputResolver ====================

class TestStepInputResolver:
    def test_requirement_analysis_takes_doc(self):
        goal = {"sources": [{"type": "doc", "content": "登录需求文档"}]}
        out = sir.resolve("requirement_analysis", goal)
        assert out["doc_content"] == "登录需求文档"

    def test_requirement_analysis_fallback_to_statement(self):
        goal = {"sources": [], "goal_statement": "验证登录"}
        assert sir.resolve("requirement_analysis", goal)["doc_content"] == "验证登录"

    def test_code_scan_takes_repo_path(self):
        goal = {"sources": [{"type": "repo", "repo_id": "android", "local_path": "/x/android", "branch": "dev"}]}
        out = sir.resolve("code_scan", goal)
        assert out["repo_path"] == "/x/android" and out["repo_name"] == "android" and out["branch"] == "dev"

    def test_branch_review_takes_repo_and_branches(self):
        goal = {"sources": [{"type": "repo", "repo_id": "be", "local_path": "/x/be", "branch": "feat"}]}
        out = sir.resolve("branch_review", goal)
        assert out["repo_path"] == "/x/be"
        assert out["base_branch"] == "feat" and out["target_branch"] == "feat"
        assert out["inputs"]["mode"] == "最近更新"

    def test_alignment_takes_acceptance_and_prior(self):
        goal = {"sources": [], "acceptance": [{"id": "a1", "desc": "x"}], "goal_statement": "g"}
        prior = [{"type": "branch_review", "summary": "改了登录"}]
        out = sir.resolve("alignment_analysis", goal, prior_artifacts=prior)
        assert out["change_summary"] == "改了登录" and len(out["acceptance"]) == 1

    def test_api_test_takes_interface_doc_from_branch_review(self):
        goal = {
            "sources": [
                {"type": "repo", "repo_id": "be", "local_path": "/x/be", "branch": "feat"},
                {"type": "environment", "base_url": "http://127.0.0.1:8770",
                 "test_accounts": [{"phone": "13800000000", "password": "x"}]},
            ],
            "goal_statement": "验证登录接口",
        }
        interface_doc = {
            "affected_endpoints": [
                {"method": "POST", "path": "/login", "responses": {"success": {"code": "OK"}}}
            ]
        }
        prior = [{"type": "branch_review", "data": {
            "interface_doc": interface_doc,
            "affected_modules": ["app.py"],
            "change_summary": "登录接口新增锁定逻辑",
        }}]

        out = sir.resolve("api_test", goal, prior_artifacts=prior)

        assert out["interface_doc"] == interface_doc
        assert out["base_url"] == "http://127.0.0.1:8770"
        assert out["affected_modules"] == ["app.py"]


# ==================== AgentRuntime ====================

@pytest.fixture
def seed_agent():
    agent_id = f"agent_test_{uuid.uuid4().hex[:6]}"
    agent = {
        "agent_id": agent_id, "agent_name": "测试需求分析", "category": "analysis",
        "capability_key": "requirement_analysis", "handler_class": "requirement_analysis",
        "model_id": "gemini_flash", "system_prompt": "分析", "status": "active",
    }
    get_collection("ai_agents").insert_one(dict(agent))
    yield agent
    get_collection("ai_agents").delete_many({"agent_id": agent_id})


class TestAgentRuntime:
    def test_agent_by_capability(self, seed_agent):
        found = art.agent_by_capability("requirement_analysis")
        assert found is not None and found["status"] == "active"

    def test_register_candidate_agents_persistent(self, seed_agent):
        """注册阶段：候选智能体快照进注册表，持久单实例、append-only、重复注册不增不删。"""
        goal_id = f"goal_art_{uuid.uuid4().hex[:6]}"
        try:
            caps = [{"agent_id": seed_agent["agent_id"], "agent_name": seed_agent["agent_name"],
                     "capability_key": "requirement_analysis", "produces_evidence": ["doc_review"],
                     "risk_level": "low", "can_execute_now": True}]
            art.register_candidate_agents(goal_id, caps)
            inst = get_collection("ai_workspace_agents").find_one(
                {"goal_id": goal_id, "agent_id": seed_agent["agent_id"]})
            assert inst is not None and inst["status"] == "registered" and inst["can_execute"] is True
            assert inst["runs"] == 0
            # 重复注册：仍单实例（持久），不覆盖运行历史
            art.register_candidate_agents(goal_id, caps)
            cnt = get_collection("ai_workspace_agents").count_documents(
                {"goal_id": goal_id, "agent_id": seed_agent["agent_id"]})
            assert cnt == 1
        finally:
            for c in ["ai_workspace_agents", "ai_task_queue", "ai_goal_artifacts", "ai_goal_steps"]:
                get_collection(c).delete_many({"goal_id": goal_id})

    def test_install_then_enqueue(self, seed_agent):
        """持久实例(goal_id+agent_id)：install 注册 → enqueue 标 running + 累计 runs + 记录 served_steps。"""
        goal_id = f"goal_art_{uuid.uuid4().hex[:6]}"
        try:
            art.install_agent(seed_agent, goal_id=goal_id)
            inst = get_collection("ai_workspace_agents").find_one(
                {"goal_id": goal_id, "agent_id": seed_agent["agent_id"]})
            assert inst is not None and inst["status"] == "registered" and inst["runs"] == 0

            task_id = art.enqueue_agent_task(
                seed_agent, {"doc_content": "x"}, goal_id=goal_id, step_id="s1", phase="step")
            task = get_collection("ai_task_queue").find_one({"task_id": task_id})
            assert task["task_type"] == 40
            assert task["payload"]["handler_class"] == "requirement_analysis"
            assert task["payload"]["goal_id"] == goal_id

            # 同一持久实例：running + runs=1 + 参与 s1（不新建按 step 的实例）
            insts = list(get_collection("ai_workspace_agents").find(
                {"goal_id": goal_id, "agent_id": seed_agent["agent_id"]}))
            assert len(insts) == 1
            assert insts[0]["status"] == "running" and insts[0]["runs"] == 1
            assert "s1" in insts[0].get("served_steps", [])

            # 再派一步：runs 累加、served_steps 追加，仍单实例（不覆盖）
            art.enqueue_agent_task(seed_agent, {"doc_content": "y"}, goal_id=goal_id, step_id="s2", phase="step")
            inst = get_collection("ai_workspace_agents").find_one(
                {"goal_id": goal_id, "agent_id": seed_agent["agent_id"]})
            assert inst["runs"] == 2 and set(inst["served_steps"]) == {"s1", "s2"}
        finally:
            for c in ["ai_workspace_agents", "ai_task_queue", "ai_goal_artifacts", "ai_goal_steps"]:
                get_collection(c).delete_many({"goal_id": goal_id})

    def test_collect_probe_outputs(self, seed_agent):
        goal_id = f"goal_art_{uuid.uuid4().hex[:6]}"
        try:
            art.install_agent(seed_agent, goal_id=goal_id)   # status=registered（非终态）
            r = art.collect_probe_outputs(goal_id)
            assert r["all_done"] is False
            art.mark_agent("completed", agent_id=seed_agent["agent_id"], goal_id=goal_id)
            get_collection("ai_goal_artifacts").insert_one({
                "goal_id": goal_id, "phase": "probe", "type": "requirement_analysis",
                "summary": "拆了3个验收点", "created_at": int(time.time())})
            r = art.collect_probe_outputs(goal_id)
            assert r["all_done"] is True and len(r["outputs"]) == 1
        finally:
            for c in ["ai_workspace_agents", "ai_task_queue", "ai_goal_artifacts"]:
                get_collection(c).delete_many({"goal_id": goal_id})


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
