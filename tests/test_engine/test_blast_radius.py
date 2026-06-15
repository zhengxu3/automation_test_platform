"""爆炸范围 side 分类 + gating 选择 + mock 运行时成功率控制 的纯函数测试。"""
import os
import sys
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from engine import blast_radius as br
from engine import goal_runtime
from common.db import get_collection
from device_worker.tasks.api_test_task import _code_has_bug, _mock_should_fail


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _init_git_repo(repo):
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Tester")


# ==================== 文件 → side 分类 ====================

def test_classify_file_side_by_dir_and_ext():
    assert br.classify_file_side("backend/app.py") == "backend"
    assert br.classify_file_side("server/routes/login.go") == "backend"
    assert br.classify_file_side("web/src/views/Login.vue") == "web"
    assert br.classify_file_side("frontend/package.json") == "web"
    assert br.classify_file_side("android/app/src/main/AndroidManifest.xml") == "client"
    assert br.classify_file_side("app/LoginActivity.kt") == "client"
    assert br.classify_file_side("DemoShop/LoginViewController.swift") == "client"
    assert br.classify_file_side("README.md") == ""


def test_changed_sides_from_files_mixed():
    files = ["backend/auth/login.py", "web/src/Login.vue", "app/Login.kt", "docs/readme.md"]
    assert br.changed_sides_from_files(files) == {"backend", "web", "client"}


def test_evidence_side_mapping():
    assert br.evidence_side("api_test") == "backend"
    assert br.evidence_side("web_test") == "web"
    assert br.evidence_side("device_test") == "client"
    assert br.evidence_side("doc_review") == ""


# ==================== gating：只重置被触及 side 的验收点 ====================

def _acc():
    return [
        {"id": "code1", "evidence_type": "static_analysis"},
        {"id": "api1", "evidence_type": "api_test"},
        {"id": "web1", "evidence_type": "web_test"},
        {"id": "doc1", "evidence_type": "doc_review"},
    ]


def test_acceptance_to_reset_backend_only():
    # 只改后端 → 重置 api + 代码分析；web 不动
    ids = br.acceptance_to_reset(_acc(), {"backend"})
    assert ids == {"api1", "code1"}


def test_acceptance_to_reset_web_only():
    ids = br.acceptance_to_reset(_acc(), {"web"})
    assert ids == {"web1", "code1"}


def test_acceptance_to_reset_mixed():
    ids = br.acceptance_to_reset(_acc(), {"backend", "web"})
    assert ids == {"api1", "web1", "code1"}


def test_acceptance_to_reset_empty_returns_none():
    # 探测不出 side → None（调用方退回全部重置兼容）
    assert br.acceptance_to_reset(_acc(), set()) is None


# ==================== 提交代码模拟：pass/fail 由提交的代码决定 ====================


def test_code_has_bug_detects_marker(tmp_path):
    repo = tmp_path / "demo-shop"
    (repo / "backend").mkdir(parents=True)
    (repo / "web" / "src").mkdir(parents=True)
    # 后端有缺陷标记，前端干净
    (repo / "backend" / "routes.py").write_text("def f():\n    return 1  # MOCK_BUG\n")
    (repo / "web" / "src" / "Login.vue").write_text("<template></template>\n")

    assert _code_has_bug(str(repo), "backend") is True
    assert _code_has_bug(str(repo), "web") is False


def test_mock_should_fail_is_code_driven_when_source_present(tmp_path):
    repo = tmp_path / "demo-shop"
    (repo / "backend").mkdir(parents=True)
    (repo / "backend" / "routes.py").write_text("x = 1  # MOCK_BUG\n")
    inputs = {"repo_path": str(repo)}
    # 后端源码含 MOCK_BUG → 失败（以提交的代码为准，轮次兜底无关）
    assert _mock_should_fail(inputs, "backend", 1, 0) is True
    # 提交修复（无标记）→ 通过
    (repo / "backend" / "routes.py").write_text("x = 1\n")
    assert _mock_should_fail(inputs, "backend", 99, 9) is False


def test_mock_should_fail_falls_back_without_repo_source():
    # 无 repo_path / 无该 side 源码 → 退回轮次兜底
    assert _mock_should_fail({}, "backend", 2, 3) is True
    assert _mock_should_fail({}, "backend", 5, 3) is False


def test_code_has_bug_client_kotlin(tmp_path):
    repo = tmp_path / "demo-android"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "LoginActivity.kt").write_text("fun f() {}  // MOCK_BUG\n")
    assert _code_has_bug(str(repo), "client") is True
    assert _code_has_bug(str(repo), "backend") is False
    # 提交修复（移除标记）→ client 通过
    (repo / "app" / "LoginActivity.kt").write_text("fun f() {}\n")
    assert _mock_should_fail({"repo_path": str(repo)}, "client", 1, 0) is False


def test_acceptance_to_reset_client_only():
    acc = [
        {"id": "code1", "evidence_type": "static_analysis"},
        {"id": "api1", "evidence_type": "api_test"},
        {"id": "web1", "evidence_type": "web_test"},
        {"id": "dev1", "evidence_type": "device_test"},
    ]
    # 只改客户端 → 重置 device_test + 代码分析；api/web 不动
    assert br.acceptance_to_reset(acc, {"client"}) == {"dev1", "code1"}


def test_git_changed_files_single_commit_does_not_list_whole_repo(tmp_path):
    repo = tmp_path / "single"
    _init_git_repo(repo)
    (repo / "backend").mkdir()
    (repo / "backend" / "app.py").write_text("print('root')\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "root")

    result = br.git_changed_files_result(str(repo))

    assert result["ok"] is True
    assert result["files"] == []
    assert result["reason"] == "single_commit_no_parent"


def test_git_changed_files_uses_explicit_before_after(tmp_path):
    repo = tmp_path / "range"
    _init_git_repo(repo)
    (repo / "backend").mkdir()
    (repo / "backend" / "app.py").write_text("print('v1')\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "root")
    before = _git(repo, "rev-parse", "HEAD")

    (repo / "web").mkdir()
    (repo / "web" / "Login.vue").write_text("<template />\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "web change")
    after = _git(repo, "rev-parse", "HEAD")

    result = br.git_changed_files_result(str(repo), base_ref=before, head_ref=after)

    assert result["ok"] is True
    assert result["files"] == ["web/Login.vue"]
    assert br.changed_sides_from_files(result["files"]) == {"web"}


def test_code_update_round_ignores_known_non_test_files():
    goal_id = "goal_blast_readme_only"
    cols = ["ai_goals", "ai_goal_steps", "ai_goal_events", "ai_goal_evidence",
            "ai_goal_artifacts", "ai_goal_summary", "ai_task_queue"]
    for c in cols:
        get_collection(c).delete_many({"goal_id": goal_id})

    get_collection("ai_goals").insert_one({
        "goal_id": goal_id,
        "title": "README only",
        "status": "guarding",
        "completion_policy": "continuous",
        "auto_replan": True,
        "sources": [{"type": "repo", "repo_id": "repo_docs", "local_path": ""}],
        "acceptance": [{"id": "api1", "desc": "API", "evidence_type": "api_test",
                        "verdict": "pass", "bound_to": "ev1"}],
        "goal_statement": "x",
        "goal_confidence": 0.8,
        "feasibility": {"allowed_evidence_types": ["api_test"]},
        "evidence_policy": {},
        "plan_version": 1,
        "round": 1,
    })
    get_collection("ai_goal_steps").insert_one({
        "goal_id": goal_id, "step_id": "s1", "status": "completed", "plan_version": 1,
    })

    try:
        result = goal_runtime.trigger_code_update_round(
            goal_id,
            reason="README update",
            changed_repo_id="repo_docs",
            before_ref="abc",
            after_ref="def",
            changed_files=["README.md"],
        )

        assert result["ok"] is True and result["skipped"] is True
        goal = get_collection("ai_goals").find_one({"goal_id": goal_id}, {"_id": 0})
        assert goal["status"] == "guarding"
        assert goal["plan_version"] == 1
        step = get_collection("ai_goal_steps").find_one({"goal_id": goal_id, "step_id": "s1"}, {"_id": 0})
        assert "superseded_by" not in step
        ev = get_collection("ai_goal_events").find_one({"goal_id": goal_id, "event": "code_update_ignored"}, {"_id": 0})
        assert ev and ev["payload"]["changed_files_by_repo"]["repo_docs"] == ["README.md"]
    finally:
        for c in cols:
            get_collection(c).delete_many({"goal_id": goal_id})
