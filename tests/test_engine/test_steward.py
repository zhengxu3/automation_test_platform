"""Steward 记忆体初始化测试 — 验证目标生成稳定性 + 偏移检测准确性

记忆体不稳定其他都甭谈。这套测试验证：
1. 目标生成：多次运行结构稳定（必含 goal_statement/acceptance/confidence）
2. 偏移检测：相关信息高对齐、无关信息低对齐（方向正确）
3. StructuredLLM 容错：能从脏输出里救回结构
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from engine import steward
from engine import source_profiler as sp
from llm import structured as st


# ==================== StructuredLLM 容错（纯函数，不调LLM）====================

class TestStructuredParse:
    def test_clean_json(self):
        assert st._parse('{"a": 1}') == {"a": 1}

    def test_markdown_wrapped(self):
        assert st._parse('```json\n{"a": 1}\n```') == {"a": 1}

    def test_prefix_suffix_text(self):
        assert st._parse('好的：{"b": 2} 完成') == {"b": 2}

    def test_trailing_comma(self):
        assert st._parse('{"c": 3,}') == {"c": 3}

    def test_smart_quotes(self):
        # 智能引号修复
        r = st._parse('{“d”: 4}')
        assert r == {"d": 4}

    def test_unparseable_returns_none(self):
        assert st._parse('这根本不是json') is None

    def test_validate_required(self):
        ok, _ = st._validate({"a": 1}, {"required": ["a", "b"]})
        assert not ok
        ok, _ = st._validate({"a": 1, "b": 2}, {"required": ["a", "b"]})
        assert ok

    def test_validate_types(self):
        ok, _ = st._validate({"n": "x"}, {"types": {"n": "int"}})
        assert not ok
        ok, _ = st._validate({"n": 5}, {"types": {"n": "int"}})
        assert ok


# ==================== 可行性画像（纯逻辑）====================

class TestFeasibility:
    def test_doc_only(self):
        p = sp.profile_sources([{"type": "doc"}])
        assert p["input_mode"] == "doc_only"
        assert "device_test" in p["blocked_evidence_types"]
        assert not p["executable"]

    def test_full_executable(self):
        p = sp.profile_sources([
            {"type": "doc"},
            {"type": "repo", "role": "android_client"},
            {"type": "environment", "apk_source": "ci", "test_accounts": ["a"], "device_id": "d"},
        ])
        assert p["input_mode"] == "full"
        assert "device_test" in p["allowed_evidence_types"]
        assert p["executable"]

    def test_upgrade_hint(self):
        p = sp.profile_sources([{"type": "repo", "role": "android_client"}])
        # 缺 apk/device/account 才能 device_test
        assert "device_test" in p["upgrade_hints"]
        assert "env:apk" in p["upgrade_hints"]["device_test"]


# ==================== 目标生成稳定性（真实调 LLM）====================

@pytest.mark.llm
class TestGoalGeneration:
    """多次运行验证结构稳定性"""

    def test_goal_structure_stable(self):
        """目标生成必含核心字段，多次运行结构一致"""
        for _ in range(3):
            result = steward.generate_goal(
                title="验证登录崩溃修复",
                doc_content="用户反馈登录页点击登录按钮偶现崩溃。需求：修复崩溃，保证登录流程稳定。",
                input_mode="doc_only",
            )
            assert "goal_statement" in result
            assert "acceptance" in result
            assert isinstance(result["acceptance"], list)
            assert "confidence" in result
            assert 0 <= result["confidence"] <= 1
            # 每个验收点有 id + bound_to + verdict
            for a in result["acceptance"]:
                assert "id" in a
                assert a["bound_to"] is None
                assert a["verdict"] == "pending"

    def test_goal_not_degraded(self):
        """正常输入不应降级"""
        result = steward.generate_goal(
            title="女女匹配策略验证",
            doc_content="验证女女匹配策略：当女性资源过多时，提升VIP用户匹配到女性的成功率。",
            input_mode="doc_only",
        )
        assert result["_meta"]["ok"], f"目标生成降级了: {result['_meta']}"
        assert len(result["acceptance"]) > 0, "应该拆出至少一个验收点"


# ==================== 偏移检测准确性（真实调 LLM）====================

@pytest.mark.llm
class TestAlignment:
    """验证偏移检测方向正确"""

    GOAL = "验证登录流程稳定，修复登录崩溃"
    ACCEPTANCE = [
        {"desc": "登录3步流程全部通过"},
        {"desc": "崩溃日志不再出现"},
    ]

    def test_aligned_info_high_score(self):
        """相关信息 → 高对齐度"""
        result = steward.assess_alignment(
            self.GOAL, self.ACCEPTANCE,
            "新提交：优化了登录按钮的点击响应，增加了空指针保护。",
        )
        assert result["alignment"] >= 60, f"相关信息对齐度应高，实际 {result['alignment']}: {result['reason']}"
        assert result["action"] in ("none", "expand")

    def test_unrelated_info_low_score(self):
        """无关信息 → 低对齐度 + 偏移动作"""
        result = steward.assess_alignment(
            self.GOAL, self.ACCEPTANCE,
            "新提交：重构了支付模块的订单结算逻辑，新增了优惠券系统。",
        )
        assert result["alignment"] < 60, f"无关信息对齐度应低，实际 {result['alignment']}: {result['reason']}"
        assert result["action"] in ("expand", "switch"), f"应该建议扩展或切换，实际 {result['action']}"

    def test_alignment_not_degraded(self):
        """偏移检测不应降级"""
        result = steward.assess_alignment(
            self.GOAL, self.ACCEPTANCE,
            "登录页增加了验证码功能。",
        )
        assert result["_meta"]["ok"], f"偏移检测降级了: {result['_meta']}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "llm"])
