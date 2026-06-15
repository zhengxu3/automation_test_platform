"""SourceProfiler 身份保留测试 — 锁死"多重性不被拍扁"前提

对应 source_profiler 头部不可违背前提 1/2/5：
  - available_capabilities 只表达能力存在，不表达数量
  - 多 repo 身份（source_id / repo_id / role / project_types / capabilities）逐个保留
  - Planner 该看到"N 个 repo 画像"，不是"有 repo"

纯函数测试：用 tmp_path 造真实仓库特征目录驱动 inspect_project，无需 DB。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from engine.source_profiler import profile_sources


def test_three_repos_preserve_identity(tmp_path):
    """3 个不同类型的 repo → profile.repos 必须是 3，且各自身份独立不串味。"""
    be = tmp_path / "api"; be.mkdir(); (be / "app.py").write_text("x")
    fe = tmp_path / "web"; fe.mkdir(); (fe / "package.json").write_text("{}")
    ad = tmp_path / "app"; ad.mkdir(); (ad / "build.gradle").write_text("x")

    sources = [
        {"type": "repo", "repo_id": "r_be", "local_path": str(be), "branch": "dev", "role": "backend"},
        {"type": "repo", "repo_id": "r_fe", "local_path": str(fe), "branch": "dev", "role": "frontend"},
        {"type": "repo", "repo_id": "r_ad", "local_path": str(ad), "branch": "dev", "role": "android_client"},
    ]
    p = profile_sources(sources)

    # ===== 前提 2/5：多重性逐个保留 =====
    assert p["repo_count"] == 3
    assert len(p["repos"]) == 3
    assert [r["source_id"] for r in p["repos"]] == ["src_repo_0", "src_repo_1", "src_repo_2"]

    by_id = {r["repo_id"]: r for r in p["repos"]}
    # 每个 repo 自己的 project_types / capabilities，不是全局拍扁
    assert by_id["r_be"]["project_types"] == ["backend"]
    assert "repo:backend" in by_id["r_be"]["capabilities"]
    assert by_id["r_fe"]["project_types"] == ["frontend"]
    assert "repo:web" in by_id["r_fe"]["capabilities"]
    assert "repo:client" not in by_id["r_fe"]["capabilities"]
    assert by_id["r_ad"]["project_types"] == ["android"]
    assert "repo:client" in by_id["r_ad"]["capabilities"]
    # 不串味：后端的能力不会沾到前端身上
    assert "repo:backend" not in by_id["r_fe"]["capabilities"]
    assert "repo:backend" not in by_id["r_ad"]["capabilities"]
    # 身份字段齐全（供下游 Task/Artifact/Evidence 引用）
    assert by_id["r_be"]["branch"] == "dev" and by_id["r_be"]["local_path"] == str(be)

    # ===== 前提 1：扁平集合只表达存在，不表达数量 =====
    assert set(p["available_capabilities"]) >= {"repo", "repo:backend", "repo:web", "repo:client"}
    assert p["available_capabilities"].count("repo") == 1  # 3 个 repo 在扁平集合里只剩一个 "repo"
    assert p["input_mode"] == "repo_only"


def test_multi_doc_preserve_identity():
    """多 doc / user_desc 也逐个保留身份，不塌成一个。"""
    sources = [
        {"type": "doc", "content": "A" * 10},
        {"type": "doc", "content": "B" * 20},
        {"type": "user_desc", "content": "C" * 5},
    ]
    p = profile_sources(sources)

    assert len(p["docs"]) == 3
    assert [d["source_id"] for d in p["docs"]] == ["src_doc_0", "src_doc_1", "src_doc_2"]
    assert p["docs"][0]["content_len"] == 10
    assert p["docs"][1]["content_len"] == 20
    assert p["docs"][2]["kind"] == "user_desc"
    assert p["repo_count"] == 0 and p["repos"] == []
    assert p["input_mode"] == "doc_only"


def test_backward_compat_fields_kept():
    """旧字段保留：available_capabilities / project_types / executable 等不变。"""
    p = profile_sources([{"type": "doc", "content": "x"}])
    for k in ("input_mode", "project_types", "available_sources", "available_capabilities",
              "allowed_evidence_types", "blocked_evidence_types", "upgrade_hints",
              "executable", "max_evidence_strength"):
        assert k in p, f"旧字段 {k} 丢失，破坏兼容"
    # 新字段也在
    for k in ("repos", "docs", "environments", "repo_count"):
        assert k in p


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
