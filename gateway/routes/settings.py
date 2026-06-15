"""平台设置路由

目前承载「钩子(webhook)相关配置」：
- hook_token：入站钩子 POST /ai/goal/webhook 的鉴权 token。可在设置页配置，免改 env 重启。
  鉴权优先级：环境变量 GOAL_HOOK_TOKEN > 这里保存的 token。
- webhook_base_url：对外可访问的网关基址（由部署的反代/域名决定，前端拼出完整钩子 URL 供复制）。

settings 落 ai_settings 集合，按 key 单文档存储（key="hook"）。
routes 只收请求 + 读写存储，不做编排逻辑。
"""
import os
import time

from flask import request
from flask import Blueprint
from common.auth import require_auth, get_current_user
from common.response import ok, err
from common.db import get_collection

bp = Blueprint('settings', __name__)

# 入站钩子的网关路径（注册在 /ai/goal/webhook）
WEBHOOK_PATH = "/ai/goal/webhook"


def _get_hook_settings() -> dict:
    doc = get_collection("ai_settings").find_one({"key": "hook"}, {"_id": 0})
    return doc or {}


@bp.route('/hook', methods=['GET'])
@require_auth
def get_hook_settings():
    """读取钩子配置。

    token_source 指明当前真正生效的 token 来源：
      env  → 由环境变量 GOAL_HOOK_TOKEN 提供（设置页改不了，需改部署）；
      db   → 由设置页保存；
      none → 未设置（钩子端点当前无鉴权，外放前务必设置）。
    """
    cfg = _get_hook_settings()
    env_token = os.getenv("GOAL_HOOK_TOKEN", "")
    db_token = cfg.get("token", "") or ""

    if env_token:
        token_source = "env"
    elif db_token:
        token_source = "db"
    else:
        token_source = "none"

    return ok({
        "webhook_path": WEBHOOK_PATH,
        "webhook_base_url": cfg.get("webhook_base_url", ""),
        "token": db_token,                 # 仅回显 DB 中保存的（env 的不下发）
        "token_source": token_source,
        "env_token_set": bool(env_token),
        "updated_at": cfg.get("updated_at", 0),
        "updated_by": cfg.get("updated_by", ""),
    })


@bp.route('/hook', methods=['POST'])
@require_auth
def save_hook_settings():
    """保存钩子配置（token / 网关基址）。

    token 为空字符串表示清空 DB token（回到无 DB token 状态）。
    注意：若环境变量 GOAL_HOOK_TOKEN 已设置，它始终优先，DB token 不生效。
    """
    data = request.get_json() or {}
    update = {
        "key": "hook",
        "updated_at": int(time.time()),
        "updated_by": get_current_user(),
    }
    if "token" in data:
        update["token"] = (data.get("token") or "").strip()
    if "webhook_base_url" in data:
        update["webhook_base_url"] = (data.get("webhook_base_url") or "").strip().rstrip("/")

    get_collection("ai_settings").update_one(
        {"key": "hook"}, {"$set": update}, upsert=True)

    return ok({"saved": True})


@bp.route('/llm', methods=['GET'])
@require_auth
def get_llm_info():
    """只读：当前平台主用大模型（供前端展示标识）。读 config，不暴露任何 key。"""
    try:
        from llm.llm_factory import LLMFactory
        cfg = LLMFactory._load_llm_config() or {}
        reg = LLMFactory.REGISTERED_MODELS
    except Exception:
        cfg, reg = {}, {}
    chain = cfg.get("fallback_chain") or []
    primary_id = chain[0] if chain else "gemini_flash"
    models_cfg = cfg.get("models") or {}
    model_name = (models_cfg.get(primary_id) or {}).get("model_name") \
        or (reg.get(primary_id) or {}).get("name") or primary_id
    return ok({
        "primary": {
            "model_id": primary_id,
            "name": (reg.get(primary_id) or {}).get("name", primary_id),
            "model_name": model_name,
            "provider": (models_cfg.get(primary_id) or {}).get("provider")
                        or (reg.get(primary_id) or {}).get("provider", ""),
        },
        "fallback_chain": chain,
    })
