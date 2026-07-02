"""test_case_matcher: 确定性匹配逻辑"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from engine.case_matcher import match_cases


def test_module_hit():
    cases = [{"case_id": "TC-001", "title": "登录校验", "module": "LoginController", "api_info": {}}]
    r = match_cases(["LoginController", "OrderService"], [], cases)
    assert len(r["hit_cases"]) == 1
    assert r["hit_cases"][0]["case_id"] == "TC-001"
    assert "LoginController" in r["hit_cases"][0]["hit_reason"]


def test_endpoint_hit():
    cases = [{"case_id": "TC-002", "title": "下单", "module": "", "api_info": {"endpoint": "/api/order"}}]
    interface_doc = [{"endpoint": "/api/order", "method": "POST"}]
    r = match_cases([], interface_doc, cases)
    assert len(r["hit_cases"]) == 1
    assert r["hit_cases"][0]["case_id"] == "TC-002"


def test_uncovered():
    cases = [{"case_id": "TC-001", "title": "登录", "module": "LoginController", "api_info": {}}]
    r = match_cases(["LoginController", "UserService"], [], cases)
    assert len(r["hit_cases"]) == 1
    assert len(r["uncovered_changes"]) == 1
    assert r["uncovered_changes"][0]["module"] == "UserService"


def test_no_cases():
    r = match_cases(["Foo"], [{"endpoint": "/bar"}], [])
    assert r == {"hit_cases": [], "ripple_cases": [], "uncovered_changes": [], "summary": {}}


def test_case_insensitive():
    cases = [{"case_id": "TC-003", "title": "test", "module": "logincontroller", "api_info": {}}]
    r = match_cases(["LoginController"], [], cases)
    assert len(r["hit_cases"]) == 1


def test_mixed():
    cases = [
        {"case_id": "TC-001", "title": "登录", "module": "LoginController", "api_info": {}},
        {"case_id": "TC-002", "title": "下单", "module": "", "api_info": {"endpoint": "/api/order"}},
        {"case_id": "TC-003", "title": "支付", "module": "PayService", "api_info": {}},
    ]
    r = match_cases(
        ["LoginController", "OrderService"],
        [{"endpoint": "/api/order"}],
        cases,
    )
    assert len(r["hit_cases"]) == 2  # TC-001 module hit, TC-002 endpoint hit
    assert "OrderService" not in [c["module"] for c in r["uncovered_changes"]]  # covered by endpoint
    assert r["summary"]["should_rerun"] == ["TC-001", "TC-002"]
