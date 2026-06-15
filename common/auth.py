"""认证装饰器"""
import os
import time
from functools import wraps
from flask import request
from common.response import err


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # 本地开发自动注入
        if os.getenv('APP_ENV', 'test') != 'production':
            request.user = {"username": "dev", "name": "开发者", "role": "admin", "email": "dev@local"}
            return f(*args, **kwargs)

        # 生产：验证 token
        token = request.headers.get('X-AI-Token', '')
        if not token:
            return err("未登录", 401), 401

        from common.db import get_collection
        session = get_collection("ai_sessions").find_one({"token": token}, {"_id": 0})
        if not session:
            return err("登录已失效", 401), 401
        if int(time.time()) - session.get('created_at', 0) > 86400 * 7:
            get_collection("ai_sessions").delete_one({"token": token})
            return err("登录已过期", 401), 401

        request.user = session
        return f(*args, **kwargs)
    return decorated


def get_current_user():
    return getattr(request, 'user', {}).get('username', 'dev')
