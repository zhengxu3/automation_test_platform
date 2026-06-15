"""探查轮规划测试 — probe_planner 确定性选探查能力 + code_scan 契约"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import probe_planner
from engine.contracts import NODE_CONTRACTS, check_success


def _profile(caps):
    return {"available_capabilities": caps}


class TestProbePlanner:
    def test_doc_only_picks_requirement_analysis(self):
        assert probe_planner.select_probe_capabilities(_profile(["doc"])) == ["requirement_analysis"]

    def test_repo_only_picks_code_scan(self):
        assert probe_planner.select_probe_capabilities(_profile(["repo", "repo:client"])) == ["code_scan"]

    def test_full_picks_both_in_order(self):
        assert probe_planner.select_probe_capabilities(
            _profile(["doc", "repo"])) == ["requirement_analysis", "code_scan"]

    def test_user_desc_counts_as_doc(self):
        assert probe_planner.select_probe_capabilities(_profile(["user_desc"])) == ["requirement_analysis"]

    def test_empty_input_no_probe(self):
        assert probe_planner.select_probe_capabilities(_profile([])) == []
        assert probe_planner.needs_probe(_profile([])) is False

    def test_needs_probe_true_when_repo(self):
        assert probe_planner.needs_probe(_profile(["repo"])) is True


class TestCodeScanContract:
    def test_contract_registered(self):
        assert "code_scan" in NODE_CONTRACTS
        c = NODE_CONTRACTS["code_scan"]
        assert c["produces_evidence"] == []   # 探查不产证据
        assert c["mutates"] is False

    def test_success_needs_summary_and_profile(self):
        assert check_success("code_scan", {"summary": "x", "project_type": "android"}) is True
        assert check_success("code_scan", {"summary": "x", "testable_surfaces": ["登录"]}) is True
        assert check_success("code_scan", {"summary": "x"}) is False   # 只有摘要不够
        assert check_success("code_scan", {"project_type": "android"}) is False  # 缺摘要


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
