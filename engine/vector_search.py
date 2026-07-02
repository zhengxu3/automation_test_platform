"""代码 AST 节点向量存取 — 调用链查询"""
from common.db import get_collection

COLLECTION = "code_ast_nodes"


def get_nodes_by_files(repo_name: str, branch: str, file_paths: list) -> list:
    """获取指定文件的 AST 节点（直接变更点）。"""
    if not file_paths:
        return []
    # 兼容带/不带前缀 /
    normalized = []
    for f in file_paths:
        normalized.append(f if f.startswith("/") else "/" + f)
        normalized.append(f.lstrip("/"))
    query = {
        "repo_name": repo_name,
        "file_path": {"$in": normalized},
        "status": {"$ne": "deleted"},
    }
    return list(get_collection(COLLECTION).find(query, {"_id": 0, "embedding": 0}).limit(200))


def find_reverse_dependencies(repo_name: str, branch: str, method_names: list) -> list:
    """反向查找：谁的 calls_out 包含这些方法名？（上游调用方 = 波及范围）"""
    if not method_names:
        return []
    query = {
        "repo_name": repo_name,
        "status": {"$ne": "deleted"},
        "calls_out": {"$in": method_names},
    }
    return list(get_collection(COLLECTION).find(query, {"_id": 0, "embedding": 0}).limit(100))
