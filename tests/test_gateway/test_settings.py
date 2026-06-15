"""Settings 路由测试 — 钩子配置读写 + webhook DB token 鉴权回退"""

from gateway.routes.goal import _environment_source_from_webhook, _webhook_changed_files


class TestHookSettings:
    def _cleanup(self):
        from common.db import get_collection
        get_collection("ai_settings").delete_one({"key": "hook"})

    def test_get_default_when_empty(self, client):
        self._cleanup()
        resp = client.get('/ai/settings/hook')
        assert resp.status_code == 200
        d = resp.json['data']
        assert d['webhook_path'] == '/ai/goal/webhook'
        # 无 env、无 db → none（env_token_set 取决于运行环境，仅在未设 env 时断言 none）
        if not d['env_token_set']:
            assert d['token_source'] == 'none'
            assert d['token'] == ''
        self._cleanup()

    def test_save_and_readback(self, client):
        self._cleanup()
        resp = client.post('/ai/settings/hook', json={
            "token": "  secret-123  ",
            "webhook_base_url": "https://gw.example.com/",
        })
        assert resp.status_code == 200
        assert resp.json['data']['saved'] is True

        resp = client.get('/ai/settings/hook')
        d = resp.json['data']
        assert d['webhook_base_url'] == "https://gw.example.com"   # 去尾斜杠
        if not d['env_token_set']:
            assert d['token'] == "secret-123"                      # 去首尾空白
            assert d['token_source'] == 'db'
        self._cleanup()

    def test_webhook_rejects_wrong_db_token(self, client):
        """设置页保存 token 后，webhook 用错 token 应被拒（仅在未设 env token 时验证）。"""
        import os
        if os.getenv("GOAL_HOOK_TOKEN"):
            return  # env token 优先，跳过
        self._cleanup()
        client.post('/ai/settings/hook', json={"token": "db-token-xyz"})

        # 错 token → 401 语义（result_code 401）
        resp = client.post('/ai/goal/webhook',
                           json={"repo_id": "nonexistent-repo", "branch": "main"},
                           headers={"X-Hook-Token": "wrong"})
        assert resp.json['result_code'] == 401

        self._cleanup()

    def test_clear_token(self, client):
        self._cleanup()
        client.post('/ai/settings/hook', json={"token": "tmp"})
        client.post('/ai/settings/hook', json={"token": ""})
        resp = client.get('/ai/settings/hook')
        d = resp.json['data']
        if not d['env_token_set']:
            assert d['token'] == ''
            assert d['token_source'] == 'none'
        self._cleanup()


def test_webhook_changed_files_from_commits():
    files = _webhook_changed_files({
        "commits": [
            {"added": ["backend/a.py"], "modified": ["web/App.vue"], "removed": []},
            {"added": [], "modified": ["backend/a.py"], "removed": ["README.md"]},
        ]
    })

    assert files == ["README.md", "backend/a.py", "web/App.vue"]


def test_webhook_environment_inherits_repo_and_payload_overrides():
    env = _environment_source_from_webhook(
        {"environment": {"base_url": "https://payload-api.example.com", "web_test_mock": True}},
        {"environment": {"base_url": "https://repo-api.example.com", "web_url": "https://repo-web.example.com"},
         "test_accounts": [{"user": "demo"}]},
    )

    assert env == {
        "type": "environment",
        "base_url": "https://payload-api.example.com",
        "web_url": "https://repo-web.example.com",
        "test_accounts": [{"user": "demo"}],
        "web_test_mock": True,
    }
