"""api_test 消费 branch_review.interface_doc 的纯函数测试。"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import device_worker.tasks.api_test_task as api_task
from device_worker.tasks.api_test_task import (
    _endpoints_from_interface_doc,
    _load_interface_doc,
    _mock_api_run,
    _run_script,
    execute_api_test,
)


def test_endpoints_from_interface_doc_preserve_contract_fields():
    doc = {
        "affected_endpoints": [
            {
                "method": "post",
                "path": "/login",
                "request": {"phone": "string"},
                "responses": {"success": {"code": "OK"}},
            }
        ]
    }

    endpoints = _endpoints_from_interface_doc(doc)

    assert endpoints == [
        {
            "method": "POST",
            "path": "/login",
            "request": {"phone": "string"},
            "responses": {"success": {"code": "OK"}},
        }
    ]


def test_load_interface_doc_accepts_json_string():
    doc = _load_interface_doc({
        "interface_doc": '{"affected_endpoints":[{"method":"GET","path":"/health"}]}'
    })

    assert doc["affected_endpoints"][0]["path"] == "/health"


class _FakeCollection:
    def __init__(self, docs=None, default_find_one=None):
        self.docs = list(docs or [])
        self.default_find_one = default_find_one

    def insert_one(self, doc):
        self.docs.append(doc)

    def find_one(self, query, projection=None):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()):
                return doc
        return self.default_find_one

    def update_one(self, query, update, upsert=False):
        self.last_update = {"query": query, "update": update, "upsert": upsert}


class _FakeDb:
    def __init__(self):
        self.events = _FakeCollection()
        self.goals = _FakeCollection()
        self.suites = _FakeCollection(default_find_one={"suite_id": "suite_fake", "script": "# fake script"})

    def __getitem__(self, key):
        if key == "ai_goal_events":
            return self.events
        if key == "ai_goals":
            return self.goals
        if key == "ai_api_test_suites":
            return self.suites
        raise KeyError(key)


def test_execute_api_test_emits_codegen_progress_when_blocked():
    db = _FakeDb()

    result = execute_api_test(
        {"goal_id": "goal_evt", "step_id": "s_api", "task_id": "gtask_evt"},
        db=db,
        log=lambda *_a, **_k: None,
    )

    assert result["test_result"] == "blocked"
    assert len(db.events.docs) == 1
    ev = db.events.docs[0]
    assert ev["event"] == "codegen_progress"
    assert ev["actor"] == "device_worker"
    assert ev["payload"]["stage"] == "blocked"
    assert ev["payload"]["step_id"] == "s_api"


def test_run_script_returns_script_and_result_paths(tmp_path):
    script = """
import json
import os

with open(os.environ["RESULT_PATH"], "w", encoding="utf-8") as f:
    json.dump({"all_passed": True, "cases_passed": 1, "cases_failed": 0, "cases": []}, f)
"""

    run = _run_script(script, "http://127.0.0.1:1", str(tmp_path))

    assert run["script_error"] is False
    assert run["script_path"].endswith("api_test_script.py")
    assert run["result_path"].endswith("result.json")
    assert os.path.exists(run["script_path"])
    assert os.path.exists(run["result_path"])


def test_execute_api_test_mock_mode_emits_mock_result_without_base_url():
    db = _FakeDb()
    db.goals.docs.append({"goal_id": "goal_mock", "replan_count": 0})

    result = execute_api_test(
        {
            "goal_id": "goal_mock",
            "step_id": "s_api",
            "task_id": "gtask_mock",
            "mock_mode": True,
            "mock_fail_rounds": 1,
            "interface_doc": {"affected_endpoints": [{"method": "GET", "path": "/health"}]},
        },
        db=db,
        log=lambda *_a, **_k: None,
    )

    assert result["test_result"] == "fail"
    stages = [e["payload"]["stage"] for e in db.events.docs]
    assert "script_reused" in stages
    assert "mock_result" in stages
    assert "finished" in stages


def test_mock_fail_rounds_clamped_below_replan_budget():
    db = _FakeDb()
    db.goals.docs.append({
        "goal_id": "goal_mock_budget",
        "replan_count": 1,
        "budget": {"max_replans": 2},
    })

    result = _mock_api_run(
        {
            "goal_id": "goal_mock_budget",
            "step_id": "s_api",
            "task_id": "gtask_mock_budget",
            "mock_fail_rounds": 9,
        },
        [{"method": "GET", "path": "/health"}],
        db=db,
    )

    assert result["effective_fail_rounds"] == 1
    assert result["all_passed"] is True
    stages = [e["payload"]["stage"] for e in db.events.docs]
    assert "mock_config_adjusted" in stages


def test_execute_api_test_mock_force_regenerate_skips_persisted_script(monkeypatch):
    db = _FakeDb()
    db.goals.docs.append({"goal_id": "goal_mock_regen", "replan_count": 0})
    monkeypatch.setattr(api_task, "generate_test_script", lambda *_a, **_k: "# generated")

    result = execute_api_test(
        {
            "goal_id": "goal_mock_regen",
            "step_id": "s_api",
            "task_id": "gtask_mock_regen",
            "mock_mode": True,
            "mock_regenerate_each_round": True,
            "mock_fail_rounds": 0,
            "interface_doc": {"affected_endpoints": [{"method": "GET", "path": "/health"}]},
        },
        db=db,
        log=lambda *_a, **_k: None,
    )

    assert result["test_result"] == "pass"
    stages = [e["payload"]["stage"] for e in db.events.docs]
    assert "script_regenerate_forced" in stages
    assert "script_generated" in stages
    assert "script_reused" not in stages
