"""多库 + 多文档 + 多轮 诊断模拟测试（现状探针，非"应然"断言）

目的：把设计讨论里怀疑的缺口变成实证。重点观察——
  1. 多 repo（前端/后端/安卓）提交时，探查轮到底装了几个分析智能体、分析了哪个库
  2. 多 doc 提交时，需求分析拿到的是哪份
  3. 多轮 replan 是否每轮都 append-only 留痕（superseded/plan_version/round/events）

测试不直接改代码；用 print 输出现状画像，断言只锁"当前真实行为"，
便于后续修复后对比（修复多库扇出时，本文件的现状断言应当被改写）。

风格对齐 test_probe_round.py / test_replan.py：真实 test DB + 显式 cleanup + stub LLM。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import time
import uuid
import pytest

from common.db import get_collection
from engine import goal_runtime
from engine import goal_scheduler as sched
from engine import planner
from engine import steward
from engine import probe_planner
from engine.source_profiler import profile_sources


# ==================== 公共：seed 智能体 + 多源 goal ====================

def _seed_agent(capability_key, name, required_sources, produces_evidence):
    agent_id = f"agent_{capability_key}_{uuid.uuid4().hex[:6]}"
    get_collection("ai_agents").insert_one({
        "agent_id": agent_id, "agent_name": name, "category": "analysis",
        "capability_key": capability_key, "handler_class": capability_key,
        "model_id": "gemini_flash", "system_prompt": f"{name}", "status": "active",
        "capability_contract": {
            "purpose": name, "required_sources": required_sources,
            "produces_evidence": produces_evidence, "risk_level": "low",
            "requires_approval": False, "mutates": False,
            "timeout_sec": 300, "retryable": True, "fallback": None,
        },
    })
    return agent_id


def _multi_source_goal(goal_id):
    """3 个 repo（前端/后端/安卓）+ 2 份 doc 的 goal。"""
    get_collection("ai_goals").insert_one({
        "goal_id": goal_id,
        "title": "多端登录改版验证",
        "status": "discovering",
        "completion_policy": "auto_complete",
        "budget": {"max_replans": 3},
        "plan_version": 1, "round": 1, "replan_count": 0,
        "sources": [
            {"type": "repo", "repo_id": "repo_fe", "repo_name": "login-web",
             "local_path": "/tmp/login-web", "branch": "dev", "role": "frontend"},
            {"type": "repo", "repo_id": "repo_be", "repo_name": "login-api",
             "local_path": "/tmp/login-api", "branch": "dev", "role": "backend"},
            {"type": "repo", "repo_id": "repo_android", "repo_name": "login-android",
             "local_path": "/tmp/login-android", "branch": "dev", "role": "android_client"},
            {"type": "doc", "content": "PRD-A：手机号+验证码登录主流程与错误提示。"},
            {"type": "doc", "content": "PRD-B：第三方 OAuth 登录与账号合并规则。"},
        ],
        "acceptance": [], "created_at": int(time.time()),
    })


def _cleanup(goal_id, agent_ids):
    get_collection("ai_agents").delete_many({"agent_id": {"$in": agent_ids}})
    for c in ["ai_goals", "ai_goal_steps", "ai_goal_events", "ai_goal_evidence",
              "ai_goal_artifacts", "ai_goal_summary", "ai_task_queue", "ai_memory_points",
              "ai_workspace_agents"]:
        get_collection(c).delete_many({"goal_id": goal_id})


# ==================== 诊断 1：多库多文档探查应对 ====================

class TestMultiRepoMultiDocProbe:
    def test_probe_fanout_current_behavior(self):
        """现状探针：3 repo + 2 doc 进来，discovery plan 装了几个分析智能体、分析了哪个库。"""
        goal_id = f"goal_diag_{uuid.uuid4().hex[:6]}"
        agent_ids = [
            _seed_agent("requirement_analysis", "需求分析", ["doc"], ["doc_review", "testcase_generated"]),
            _seed_agent("code_scan", "代码扫描", ["repo"], []),
            _seed_agent("branch_review", "代码分析", ["repo"], ["static_analysis"]),
        ]
        _multi_source_goal(goal_id)

        try:
            # --- 画像层：3 个 repo 的能力识别 ---
            goal = get_collection("ai_goals").find_one({"goal_id": goal_id}, {"_id": 0})
            profile = profile_sources(goal["sources"])
            probe_caps = probe_planner.select_probe_capabilities(profile)

            repo_count = sum(1 for s in goal["sources"] if s["type"] == "repo")
            doc_count = sum(1 for s in goal["sources"] if s["type"] == "doc")

            print("\n========== 诊断1：多库多文档探查 ==========")
            print(f"输入：{repo_count} 个 repo + {doc_count} 份 doc")
            print(f"画像 input_mode = {profile['input_mode']}")
            print(f"画像 available_capabilities = {profile['available_capabilities']}")
            print(f"探查轮选中能力 probe_caps = {probe_caps}")

            # --- 启动 discovery plan，看实际装了几个实例、入队几个任务 ---
            goal_runtime.discover_and_plan(goal_id)

            discovery_steps = list(get_collection("ai_goal_steps").find(
                {"goal_id": goal_id, "plan_kind": "discovery"}, {"_id": 0}))
            discovery_insts = list(get_collection("ai_workspace_agents").find(
                {"goal_id": goal_id, "phase": "step"}, {"_id": 0}))
            discovery_tasks = list(get_collection("ai_task_queue").find(
                {"goal_id": goal_id, "payload.phase": "step"}, {"_id": 0}))

            print(f"实际 discovery step 数 = {len(discovery_steps)}  (capabilities={[i['capability_key'] for i in discovery_steps]})")
            print(f"实际安装智能体实例数 = {len(discovery_insts)}  (capabilities={[i['capability_key'] for i in discovery_insts]})")
            print(f"实际入队 discovery 任务数 = {len(discovery_tasks)}")
            # 每个代码分析任务实际拿到的 repo_path
            for t in discovery_tasks:
                cap = t["payload"]["capability_key"]
                inputs = t["payload"].get("inputs", {})
                print(f"  - {cap}: repo_path={inputs.get('repo_path', '-')} "
                      f"branch={inputs.get('branch', inputs.get('base_branch', '-'))} "
                      f"doc_len={len(inputs.get('doc_content', '')) if inputs.get('doc_content') else 0}")

            # ===== 修复后断言：多 repo 扇出（每库一个代码分析 step，各绑自己的 repo）=====
            # probe_caps 层仍去重（扇出发生在 discovery plan 构建，不在 probe_caps）
            assert probe_caps.count("code_scan") == 1, "probe_caps 去重，扇出在 discovery plan 层"
            # 修复①：代码分析 discovery step 数 == repo 数（3 库各一个，不再塌缩）
            code_discovery_steps = [i for i in discovery_steps if i["capability_key"] in ("code_scan", "branch_review")]
            print(f"代码分析 discovery step 数 = {len(code_discovery_steps)}  vs  repo 数 = {repo_count}")
            assert len(code_discovery_steps) == repo_count, (
                f"修复后：代码分析 step 数应 == repo 数（{repo_count}），实际 {len(code_discovery_steps)}")
            # 修复②：每个 code_scan step 绑定不同 source_ref，分析的是各自的 repo（全覆盖，非只第一个）
            analyzed_paths = sorted(
                t["payload"]["inputs"].get("repo_path", "")
                for t in discovery_tasks if t["payload"]["capability_key"] in ("code_scan", "branch_review"))
            print(f"实际被分析的 repo 路径 = {analyzed_paths}")
            assert set(analyzed_paths) == {"/tmp/login-web", "/tmp/login-api", "/tmp/login-android"}, (
                f"修复后：3 个库应被逐一分析，实际 {analyzed_paths}")
            # 修复③：source_ref 各不相同，绑定到各自仓库
            refs = sorted(i.get("source_ref", "") for i in code_discovery_steps)
            assert refs == ["repo_android", "repo_be", "repo_fe"], f"每库各绑 source_ref，实际 {refs}"

            print("结论：多库扇出已修复——每个 repo 一个代码分析 step，按 source_ref 绑定各自仓库。")
            print("==========================================\n")
        finally:
            _cleanup(goal_id, agent_ids)


# ==================== 诊断 2：多轮 plan 是否每轮留痕 ====================

class TestMultiRoundRecording:
    def test_each_round_appended_and_recorded(self, monkeypatch):
        """多轮 replan：每轮 plan append-only 留痕，plan_version/round 递增，事件齐全。"""
        goal_id = f"goal_mround_{uuid.uuid4().hex[:6]}"
        agent_ids = [
            _seed_agent("requirement_analysis", "需求分析", ["doc"], ["doc_review", "testcase_generated"]),
        ]
        # 直接造 running 态 + 3 个验收点（每轮拿下一个，需要跑 3 轮）
        get_collection("ai_goals").insert_one({
            "goal_id": goal_id, "title": "多轮留痕", "goal_statement": "验证多轮记录",
            "status": "running", "completion_policy": "auto_complete",
            "feasibility": {"allowed_evidence_types": ["static_analysis"],
                            "producible_evidence_types": ["static_analysis"]},
            "budget": {"max_replans": 5}, "plan_version": 1, "round": 1, "replan_count": 0,
            "acceptance": [
                {"id": "a1", "desc": "主流程", "evidence_type": "static_analysis", "bound_to": None, "verdict": "pending"},
                {"id": "a2", "desc": "OAuth", "evidence_type": "static_analysis", "bound_to": None, "verdict": "pending"},
                {"id": "a3", "desc": "账号合并", "evidence_type": "static_analysis", "bound_to": None, "verdict": "pending"},
            ],
            "created_at": int(time.time()),
        })

        def _cap(_profile):
            return [{"capability_key": "requirement_analysis", "agent_id": agent_ids[0],
                     "purpose": "需求分析", "required_sources": [], "produces_evidence": ["static_analysis"],
                     "risk_level": "low", "requires_approval": False, "fallback": None,
                     "can_execute_now": True}]

        # 每轮只服务"第一个尚未达成"的验收点（制造"需要多轮"，且与已 pass 的不重复）
        calls = {"n": 0}
        def fake_plan(goal_statement, acceptance, profile, capabilities,
                      memory_context="", prior_context="", model_id="gemini_pro"):
            unmet = [a["id"] for a in acceptance if a.get("verdict") != "pass"]
            serve = unmet[0] if unmet else acceptance[0]["id"]
            calls["n"] += 1
            return {"steps": [{"step_id": "s1", "name": f"R{calls['n'] + 1}-攻克{serve}",
                               "capability_key": "requirement_analysis", "depends_on": [],
                               "serves_acceptance": [serve], "rationale": serve}],
                    "plan_summary": f"round{calls['n'] + 1}", "confidence": 0.9, "_meta": {}}

        monkeypatch.setattr(planner, "discover_capabilities", _cap)
        monkeypatch.setattr(planner, "generate_plan", fake_plan)
        monkeypatch.setattr(steward, "evaluate_and_remember", lambda *a, **k: {"conclusion": "stub"})
        monkeypatch.setattr(steward, "retrieve_memory", lambda *a, **k: "")

        # R1 初始 plan（手放一个服务 a1 的 step）
        get_collection("ai_goal_steps").insert_one({
            "goal_id": goal_id, "step_id": "s1", "name": "R1-攻克a1",
            "capability_key": "requirement_analysis", "agent_id": agent_ids[0],
            "depends_on": [], "serves_acceptance": ["a1"], "evidence_type": "static_analysis",
            "can_execute": True, "requires_approval": False, "risk_level": "low",
            "retryable": True, "fallback": None, "status": "pending", "attempts": [], "plan_version": 1,
        })

        try:
            print("\n========== 诊断2：多轮 plan 留痕 ==========")
            sched.advance(goal_id)  # R1 提交
            # 逐轮完成：每轮拿下一个验收点 → critic replan → 下一轮
            for rnd in range(1, 4):
                sched.on_step_done(goal_id, "s1", {"acceptance_points": [f"point{rnd}"],
                                                   "summary": f"R{rnd}完成一个验收点"})
                goal = get_collection("ai_goals").find_one({"goal_id": goal_id}, {"_id": 0})
                active = list(get_collection("ai_goal_steps").find(
                    {"goal_id": goal_id, "superseded_by": {"$exists": False}}, {"_id": 0}))
                superseded = list(get_collection("ai_goal_steps").find(
                    {"goal_id": goal_id, "superseded_by": {"$exists": True}}, {"_id": 0}))
                passed = [a["id"] for a in goal["acceptance"] if a.get("verdict") == "pass"]
                print(f"R{rnd} 后: status={goal['status']} plan_version={goal['plan_version']} "
                      f"round={goal['round']} replan_count={goal['replan_count']} "
                      f"passed={passed} 活跃step={len(active)} 历史step={len(superseded)}")

            goal = get_collection("ai_goals").find_one({"goal_id": goal_id}, {"_id": 0})
            superseded = list(get_collection("ai_goal_steps").find(
                {"goal_id": goal_id, "superseded_by": {"$exists": True}}, {"_id": 0}))
            events = [e["event"] for e in get_collection("ai_goal_events").find({"goal_id": goal_id})]

            # ===== 应然断言：每轮留痕 =====
            assert goal["status"] == "completed", "三轮各拿下一个验收点 → 最终完成"
            assert goal["round"] == 3 and goal["plan_version"] == 3
            assert goal["replan_count"] == 2  # R1→R2、R2→R3 共 2 次 replan
            # append-only：被取代的历史 step 累积保留（R1、R2 各一条）
            assert len(superseded) == 2, f"历史 step 应保留 2 条，实际 {len(superseded)}"
            sv = sorted(s["superseded_by"] for s in superseded)
            assert sv == [2, 3], f"superseded_by 应为 [2,3]，实际 {sv}"
            # 事件留痕
            assert events.count("replan_triggered") == 2
            assert events.count("plan_generated") >= 2
            assert "critic_decision" in events
            print(f"最终: superseded_by={sv}, replan_triggered={events.count('replan_triggered')} 次")
            print("结论：每轮 plan 都 append-only 留痕，可回放。← 设计如此，符合预期")
            print("==========================================\n")
        finally:
            _cleanup(goal_id, agent_ids)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
