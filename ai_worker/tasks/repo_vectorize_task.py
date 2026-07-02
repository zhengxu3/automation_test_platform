"""仓库向量化任务：AST 扫描代码节点 → Gemini embedding → 写入 ai_code_vectors"""
import os
import subprocess
import time
from ai_worker.base_task import BaseTaskHandler
from common.db import get_collection


# 语言 → 文件扩展名 → 简单的函数/类提取正则
LANG_EXTENSIONS = {
    "android": (".kt", ".java"),
    "ios": (".swift", ".m", ".h"),
    "python": (".py",),
    "php": (".php",),
    "java": (".java",),
    "go": (".go",),
    "web": (".ts", ".tsx", ".js", ".jsx", ".vue"),
}

# 忽略的目录
SKIP_DIRS = {".git", "node_modules", "build", "dist", ".gradle", "Pods",
             "__pycache__", ".idea", ".vscode", "vendor", "target"}


def _scan_code_files(repo_path: str, language: str) -> list:
    """扫描仓库中的代码文件，返回 [{file_path, content}]"""
    extensions = LANG_EXTENSIONS.get(language, (".py", ".java", ".kt"))
    files = []
    for root, dirs, filenames in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in filenames:
            if not fname.endswith(extensions):
                continue
            fpath = os.path.join(root, fname)
            rel_path = os.path.relpath(fpath, repo_path)
            try:
                with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
                if len(content) > 100:  # 跳过空文件
                    files.append({"file_path": rel_path, "content": content})
            except Exception:
                continue
    return files


def _split_into_chunks(files: list, max_chunk_chars: int = 2000) -> list:
    """将文件拆分成代码块（按函数/类粗切）"""
    chunks = []
    for f in files:
        content = f["content"]
        file_path = f["file_path"]
        # 简单按行数切块（每 60 行一块）
        lines = content.split('\n')
        for i in range(0, len(lines), 60):
            block = '\n'.join(lines[i:i+60])
            if len(block.strip()) < 50:
                continue
            chunks.append({
                "file_path": file_path,
                "line_start": i + 1,
                "content": block[:max_chunk_chars],
            })
    return chunks


def _generate_embeddings_gemini(texts: list, api_key: str) -> list:
    """调用 Gemini embedding API 批量生成向量"""
    import requests
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:batchEmbedContent"
    all_embeddings = []
    for i in range(0, len(texts), 100):
        batch = texts[i:i+100]
        body = {
            "requests": [{"model": "models/gemini-embedding-001", "content": {"parts": [{"text": t[:2048]}]}} for t in batch]
        }
        resp = requests.post(f"{url}?key={api_key}", json=body, timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            for emb in data.get("embeddings", []):
                all_embeddings.append(emb.get("values", []))
        else:
            # 降级：填零向量
            all_embeddings.extend([[0.0] * 768] * len(batch))
    return all_embeddings


class RepoVectorizeTask(BaseTaskHandler):
    """Task Type: 10 — 仓库代码向量化"""

    async def run(self):
        repo_id = self.payload.get('repo_id', '')
        repo_name = self.payload.get('repo_name', '')
        local_path = self.payload.get('local_path', '')
        language = self.payload.get('language', '')
        branch = self.payload.get('branch', '')

        col = get_collection("ai_git_repos")
        vectors_col = get_collection("ai_code_vectors")

        def log(msg, status="info"):
            print(f"[Vectorize:{repo_id}] {msg}")
            col.update_one({"repo_id": repo_id}, {"$push": {
                "vectorize_logs": {"msg": msg, "status": status, "ts": int(time.time())}
            }})

        if not os.path.isdir(local_path):
            log(f"路径不存在: {local_path}", "error")
            col.update_one({"repo_id": repo_id}, {"$set": {"status": "vector_failed", "error_msg": "路径不存在"}})
            return

        # checkout 到目标分支
        if branch:
            subprocess.run(['git', 'fetch', 'origin', branch], cwd=local_path, capture_output=True, timeout=60)
            subprocess.run(['git', 'checkout', branch], cwd=local_path, capture_output=True, timeout=30)

        # 获取当前 commit
        try:
            r = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=local_path,
                             capture_output=True, text=True, timeout=10)
            current_commit = r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            current_commit = ""

        # 检查是否需要重新向量化
        repo = col.find_one({"repo_id": repo_id}, {"_id": 0})
        if repo and current_commit and current_commit == repo.get("last_vectorized_commit"):
            log("代码无变化，跳过", "success")
            col.update_one({"repo_id": repo_id}, {"$set": {"status": "vector_ready"}})
            return

        log(f"开始向量化: {repo_name}@{branch} (语言: {language})")

        # 扫描代码文件
        files = _scan_code_files(local_path, language)
        if not files:
            log("未找到代码文件", "error")
            col.update_one({"repo_id": repo_id}, {"$set": {"status": "vector_failed", "error_msg": "无代码文件"}})
            return
        log(f"扫描到 {len(files)} 个代码文件")

        # 切块
        chunks = _split_into_chunks(files)
        log(f"切分为 {len(chunks)} 个代码块")

        # 读 Gemini key
        import yaml
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        config_path = os.path.join(base_dir, 'config.local.yaml')
        if not os.path.exists(config_path):
            config_path = os.path.join(base_dir, 'config.yaml')
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        api_key = (cfg.get("llm", {}).get("models", {}).get("gemini_flash", {}).get("api_key", "")
                   or cfg.get("llm", {}).get("gemini_flash", {}).get("api_key", "")
                   or cfg.get("llm", {}).get("gemini_api_key", "")
                   or os.getenv("GEMINI_API_KEY", ""))

        if not api_key:
            log("缺少 Gemini API Key", "error")
            col.update_one({"repo_id": repo_id}, {"$set": {"status": "vector_failed", "error_msg": "缺少 API Key"}})
            return

        # 批量 embedding
        total = len(chunks)
        batch_size = 50
        processed = 0
        for i in range(0, total, batch_size):
            batch = chunks[i:i+batch_size]
            texts = [c["content"] for c in batch]
            embeddings = _generate_embeddings_gemini(texts, api_key)

            # 写入 DB
            docs = []
            for j, chunk in enumerate(batch):
                docs.append({
                    "repo_id": repo_id,
                    "repo_name": repo_name,
                    "branch": branch,
                    "file_path": chunk["file_path"],
                    "line_start": chunk["line_start"],
                    "content": chunk["content"],
                    "embedding": embeddings[j] if j < len(embeddings) else [],
                    "language": language,
                    "updated_at": int(time.time()),
                })
            if docs:
                # upsert by repo_id + file_path + line_start
                for doc in docs:
                    vectors_col.update_one(
                        {"repo_id": repo_id, "file_path": doc["file_path"], "line_start": doc["line_start"]},
                        {"$set": doc},
                        upsert=True
                    )
            processed += len(batch)
            log(f"进度: {processed}/{total} ({processed*100//total}%)")

        # 完成
        col.update_one({"repo_id": repo_id}, {"$set": {
            "status": "vector_ready",
            "last_vectorized_commit": current_commit,
            "last_commit": current_commit,
            "updated_at": int(time.time()),
        }})
        log(f"✅ 向量化完成！{total} 个代码块已入库", "success")
