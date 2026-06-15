"""MongoDB 连接管理（单例 + 连接池复用）"""
import os
import yaml
import certifi
from pymongo import MongoClient

_client = None
_db = None
_config_cache = None


def _load_config():
    global _config_cache
    if _config_cache:
        return _config_cache
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local_path = os.path.join(base, "config.local.yaml")
    default_path = os.path.join(base, "config.yaml")
    path = local_path if os.path.exists(local_path) else default_path
    with open(path, 'r') as f:
        _config_cache = yaml.safe_load(f)
    return _config_cache


def get_db():
    """获取数据库实例（全局单例，连接池复用）"""
    global _client, _db
    if _db is not None:
        return _db
    config = _load_config()
    mongo_cfg = config.get("mongodb", {})
    uri = mongo_cfg.get("uri", "")
    env = os.getenv("APP_ENV", config.get("app", {}).get("env", "test"))
    db_name = mongo_cfg.get("db_name_test") if env == "test" else mongo_cfg.get("db_name")

    # 连接池配置：防止频繁连接被 Atlas 封禁
    _client = MongoClient(
        uri,
        tlsCAFile=certifi.where(),
        serverSelectionTimeoutMS=5000,
        maxPoolSize=10,          # 最大连接数（不会创建过多连接）
        minPoolSize=1,           # 保持最少 1 个连接存活
        maxIdleTimeMS=300000,    # 空闲 5 分钟后回收
        connectTimeoutMS=10000,  # 连接超时 10s
        retryWrites=True,
    )
    _db = _client[db_name]
    return _db


def get_collection(name):
    """获取集合（不会每次新建连接）"""
    return get_db()[name]


def close_db():
    """优雅关闭（进程退出时调用）"""
    global _client, _db
    if _client:
        _client.close()
        _client = None
        _db = None
