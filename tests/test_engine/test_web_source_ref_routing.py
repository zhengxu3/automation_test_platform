"""Web 仓库识别 + source_ref 从 discovery 贯通到 objective 的纯函数测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from engine import planner, steward, step_input_resolver
from engine.source_profiler import profile_sources


class _StructuredResult:
    def __init__(self, data):
        self.data = data
        self.ok = True
        self.attempts = 1
        self.degraded = False
        self.usage = {}


def test_frontend_repo_unlocks_web_test_with_base_url(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "package.json").write_text("{}")

    p = profile_sources([
        {"type": "repo", "repo_id": "repo_web", "local_path": str(web), "role": "frontend"},
        {"type": "environment", "base_url": "https://test.example.com"},
    ])

    repo = p["repos"][0]
    assert "repo:web" in repo["capabilities"]
    assert "repo:client" not in repo["capabilities"]
    assert "web_test" in p["allowed_evidence_types"]
    assert "device_test" not in p["allowed_evidence_types"]


def test_web_url_does_not_unlock_api_test_for_backend(tmp_path):
    backend = tmp_path / "api"
    backend.mkdir()
    (backend / "app.py").write_text("x")

    p = profile_sources([
        {"type": "repo", "repo_id": "repo_api", "local_path": str(backend), "role": "backend"},
        {"type": "environment", "web_url": "https://web.example.com"},
    ])

    assert "env:web_url" in p["available_capabilities"]
    assert "env:base_url" not in p["available_capabilities"]
    assert "api_test" not in p["allowed_evidence_types"]


def test_steward_fills_web_acceptance_source_ref(monkeypatch):
    def fake_generate_structured(*_args, **_kwargs):
        return _StructuredResult({
            "goal_statement": "验证 Web 登录",
            "acceptance": [
                {"id": "w1", "desc": "Web 登录页面可完成主流程", "side": "web", "evidence_type": "web_test"}
            ],
            "confidence": 0.9,
            "rationale": "stub",
        })

    monkeypatch.setattr(steward, "generate_structured", fake_generate_structured)

    result = steward.synthesize_goal_from_probe(
        "Web 登录",
        probe_outputs=[{
            "type": "code_scan",
            "source_ref": "repo_fe",
            "source_name": "login-web",
            "data": {"project_type": "frontend", "summary": "Web 前端", "testable_surfaces": ["登录"]},
        }],
        input_mode="repo_only",
        allowed_evidence=["static_analysis", "web_test"],
    )

    assert result["acceptance"][0]["source_ref"] == "repo_fe"


def test_planner_enriches_step_source_ref_from_acceptance():
    steps = [{
        "step_id": "s_web",
        "name": "验证 Web",
        "capability_key": "web_test",
        "depends_on": [],
        "serves_acceptance": ["w1"],
    }]
    caps = [{
        "capability_key": "web_test",
        "agent_id": "agent_web_test",
        "required_sources": ["repo:web", "env:web_url"],
        "produces_evidence": ["web_test"],
        "risk_level": "medium",
        "requires_approval": False,
        "fallback": "static_analysis",
        "can_execute_now": True,
    }]
    acceptance = [{"id": "w1", "desc": "Web 登录", "evidence_type": "web_test", "source_ref": "repo_fe"}]

    enriched = planner.enrich_steps(steps, caps, acceptance=acceptance)

    assert enriched[0]["source_ref"] == "repo_fe"
    assert enriched[0]["evidence_type"] == "web_test"


def test_web_test_resolver_uses_step_bound_repo_and_env():
    goal = {
        "title": "Web 登录",
        "budget": {"max_replans": 3},
        "sources": [
            {"type": "repo", "repo_id": "repo_api", "local_path": "/tmp/api", "role": "backend"},
            {"type": "repo", "repo_id": "repo_fe", "local_path": "/tmp/web", "role": "frontend"},
            {"type": "environment", "base_url": "https://test.example.com", "mock_web_test": True},
        ],
        "acceptance": [{"id": "w1", "desc": "Web 登录", "evidence_type": "web_test", "source_ref": "repo_fe"}],
    }
    step = {"step_id": "s_web", "source_ref": "repo_fe", "serves_acceptance": ["w1"]}

    inputs = step_input_resolver.resolve("web_test", goal, step=step, prior_artifacts=[
        {"type": "branch_review", "source_ref": "", "data": {"change_summary": "legacy-blank"}},
        {"type": "branch_review", "source_ref": "repo_api", "data": {"change_summary": "api"}},
        {"type": "branch_review", "source_ref": "repo_fe", "data": {"change_summary": "web"}},
    ])

    assert inputs["repo_name"] == "repo_fe"
    assert inputs["repo_path"] == "/tmp/web"
    assert inputs["base_url"] == "https://test.example.com"
    assert inputs["change_summary"] == "web"
    assert inputs["mock_mode"] is True
