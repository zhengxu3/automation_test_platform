"""Gateway 基础测试 — 验证骨架能跑通"""
import json


class TestHealth:
    def test_health_check(self, client):
        resp = client.get('/health')
        assert resp.status_code == 200
        assert resp.json['status'] == 'ok'


class TestAgent:
    def test_agent_list(self, client):
        resp = client.get('/ai/agent/list')
        assert resp.status_code == 200
        assert resp.json['result_code'] == 200
        assert 'agents' in resp.json['data']

    def test_agent_create_and_delete(self, client):
        # 创建
        resp = client.post('/ai/agent/create', json={
            "agent_name": "test_agent_unit",
            "description": "单元测试智能体",
            "category": "test",
            "inputs": [{"key": "repo_id", "required": True}],
            "outputs": [{"key": "diff_summary"}],
        })
        assert resp.status_code == 200
        agent_id = resp.json['data']['agent_id']
        assert agent_id.startswith('agent_')

        # 查询
        resp = client.get(f'/ai/agent/detail?agent_id={agent_id}')
        assert resp.status_code == 200
        assert resp.json['data']['agent_name'] == 'test_agent_unit'

        # 删除
        resp = client.post('/ai/agent/delete', json={"agent_id": agent_id})
        assert resp.status_code == 200

    def test_agent_create_missing_name(self, client):
        resp = client.post('/ai/agent/create', json={})
        assert resp.json['result_code'] == 400


class TestGoal:
    def test_goal_create(self, client):
        resp = client.post('/ai/goal/create', json={
            "title": "测试 Goal",
            "raw_input": "聊天页面 crash 修复",
        })
        assert resp.status_code == 200
        goal_id = resp.json['data']['goal_id']
        assert goal_id.startswith('goal_')

        # 查询（新结构：data.goal.status）
        resp = client.get(f'/ai/goal/detail?goal_id={goal_id}')
        assert resp.status_code == 200
        assert resp.json['data']['goal']['goal_id'] == goal_id
        assert 'steps' in resp.json['data']
        assert 'events' in resp.json['data']

        # 清理
        from common.db import get_collection
        get_collection("ai_goals").delete_one({"goal_id": goal_id})
        get_collection("ai_goal_steps").delete_many({"goal_id": goal_id})
        get_collection("ai_goal_events").delete_many({"goal_id": goal_id})

    def test_goal_list(self, client):
        resp = client.get('/ai/goal/list')
        assert resp.status_code == 200
        assert 'goals' in resp.json['data']
