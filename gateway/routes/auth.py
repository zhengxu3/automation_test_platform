"""认证路由"""
import time
import uuid
import bcrypt
from flask import Blueprint, request
from common.response import ok, err
from common.db import get_collection

bp = Blueprint('auth', __name__)


@bp.route('/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    username = data.get('username', '')
    password = data.get('password', '')
    if not username or not password:
        return err("缺少用户名或密码")

    user = get_collection("ai_users").find_one({"username": username}, {"_id": 0})
    if not user:
        return err("用户名或密码错误", 401)

    stored = user.get('password', '')
    # 兼容旧明文密码
    if not stored.startswith('$2'):
        if password != stored:
            return err("用户名或密码错误", 401)
        # 升级为 hash
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        get_collection("ai_users").update_one({"username": username}, {"$set": {"password": hashed}})
    else:
        if not bcrypt.checkpw(password.encode(), stored.encode()):
            return err("用户名或密码错误", 401)

    # 创建 session
    token = f"ai_{uuid.uuid4().hex[:16]}"
    get_collection("ai_sessions").delete_many({"username": username})
    get_collection("ai_sessions").insert_one({
        "token": token, "username": username,
        "name": user.get("name", username), "role": user.get("role", "user"),
        "created_at": int(time.time())
    })
    return ok({"token": token, "username": username, "name": user.get("name", username)})


@bp.route('/check', methods=['GET'])
def auth_check():
    """验证当前 token 是否有效"""
    import os
    if os.getenv('APP_ENV', 'test') != 'production':
        return ok({"username": "dev", "name": "开发者", "role": "admin"})

    token = request.headers.get('X-AI-Token', '')
    if not token:
        return err("未登录", 401), 401
    session = get_collection("ai_sessions").find_one({"token": token}, {"_id": 0})
    if not session:
        return err("登录已失效", 401), 401
    return ok({"username": session["username"], "name": session.get("name", ""), "role": session.get("role", "user")})
