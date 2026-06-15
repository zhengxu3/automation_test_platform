"""测试公共 fixtures"""
import os
import sys
import pytest

os.environ['APP_ENV'] = 'test'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def app():
    from gateway.app import create_app
    app = create_app()
    app.config['TESTING'] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_headers():
    return {"X-AI-Token": "test_dev", "Content-Type": "application/json"}
