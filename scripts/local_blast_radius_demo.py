"""本地模拟：单库代码监测 + 向量搜索爆炸范围分析
用法: .venv/bin/python scripts/local_blast_radius_demo.py
"""
import subprocess
import os
import sys
import requests
import yaml

# 配置
REPO_PATH = os.path.expanduser("~/Documents/work_code/hay-android")
REPO_NAME = "hay-android"
BRANCH = "dev_4v4"
BASE_REF = "HEAD~1"
HEAD_REF = "HEAD"

# 读 Gemini key
config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.local.yaml")
with open(config_path) as f:
    cfg = yaml.safe_load(f)
API_KEY = cfg.get("llm", {}).get("models", {}).get("gemini_flash", {}).get("api_key", "")


def git_diff_files(repo_path, base, head):
    """获取变更文件列表"""
    r = subprocess.run(
        ["git", "diff", f"{base}..{head}", "--name-only"],
        cwd=repo_path, capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return []
    return [f for f in r.stdout.strip().split('\n') if f]


def git_diff_content(repo_path, base, head, max_chars=4000):
    """获取 diff 内容"""
    r = subprocess.run(
        ["git", "diff", f"{base}..{head}", "--stat"],
        cwd=repo_path, capture_output=True, text=True, timeout=10)
    stat = r.stdout[:1000] if r.returncode == 0 else ""

    r = subprocess.run(
        ["git", "diff", f"{base}..{head}", "-U3"],
        cwd=repo_path, capture_output=True, text=True, timeout=10)
    diff = r.stdout[:max_chars] if r.returncode == 0 else ""
    return stat, diff


def classify_side(files):
    """确定性分类改动侧"""
    from engine.blast_radius import classify_file_side
    sides = set()
    for f in files:
        s = classify_file_side(f)
        if s:
            sides.add(s)
    return sides


def embed_text(text, api_key):
    """单条文本 embedding（768维，与 DB 向量维度一致）"""
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent"
    body = {"content": {"parts": [{"text": text[:2048]}]}, "outputDimensionality": 768}
    resp = requests.post(f"{url}?key={api_key}", json=body, timeout=30)
    if resp.status_code == 200:
        return resp.json().get("embedding", {}).get("values", [])
    return None


def vector_search(query_embedding, repo_name, top_k=5):
    """从 ai_code_vectors 搜索最相关的代码块（Gemini 768维）"""
    from common.db import get_collection
    import numpy as np

    col = get_collection("ai_code_vectors")
    docs = list(col.find(
        {"repo_name": repo_name, "embedding": {"$exists": True}},
        {"_id": 0, "file_path": 1, "line_start": 1, "content": 1, "embedding": 1}
    ).limit(1000))

    if not docs or not query_embedding:
        return []

    q = np.array(query_embedding)
    results = []
    for doc in docs:
        emb = np.array(doc.get("embedding", []))
        if len(emb) != len(q):
            continue
        sim = np.dot(q, emb) / (np.linalg.norm(q) * np.linalg.norm(emb) + 1e-9)
        results.append({
            "file_path": doc.get("file_path", ""),
            "line_start": doc.get("line_start", 0),
            "content_preview": (doc.get("content") or "")[:80],
            "similarity": float(sim),
        })

    results.sort(key=lambda x: x["similarity"], reverse=True)
    return results[:top_k]


def call_chain_search(changed_files, repo_name):
    """基于代码内容搜索关联：从 ai_code_vectors 找和改动文件同模块的代码"""
    from common.db import get_collection
    col = get_collection("ai_code_vectors")

    # 找出改动文件所在的包/目录
    changed_dirs = set()
    for f in changed_files:
        parts = f.rsplit("/", 1)
        if len(parts) > 1:
            changed_dirs.add(parts[0])

    # 搜同目录但不是改动文件的代码块（可能受影响）
    related = []
    for d in list(changed_dirs)[:5]:
        results = list(col.find(
            {"repo_name": repo_name, "file_path": {"$regex": d}, "file_path": {"$nin": changed_files}},
            {"_id": 0, "file_path": 1, "line_start": 1, "content": 1}
        ).limit(3))
        for r in results:
            related.append({"file": r["file_path"], "line": r.get("line_start", 0), "reason": f"同模块: {d}"})

    return related, changed_dirs


def main():
    print(f"📦 仓库: {REPO_PATH}")
    print(f"🔀 对比: {BASE_REF}..{HEAD_REF}")
    print()

    # 1. 获取变更文件
    files = git_diff_files(REPO_PATH, BASE_REF, HEAD_REF)
    if not files:
        print("❌ 没有变更文件")
        return
    print(f"📝 变更文件 ({len(files)}):")
    for f in files[:10]:
        print(f"   {f}")
    if len(files) > 10:
        print(f"   ... 还有 {len(files)-10} 个")
    print()

    # 2. 确定性 side 分类
    sides = classify_side(files)
    print(f"💥 触及侧: {sorted(sides) or '未识别'}")
    print()

    # 3. 获取 diff 内容
    stat, diff = git_diff_content(REPO_PATH, BASE_REF, HEAD_REF)
    print(f"📊 Diff stat:\n{stat}")

    # 4. 调用链分析：改动文件中的方法被谁调用
    print("🔗 调用链分析（谁调用了改动的方法）...")
    code_files = [f for f in files if f.endswith(('.java', '.kt', '.swift', '.py', '.php'))]
    callers, changed_methods = call_chain_search(code_files[:10], REPO_NAME)
    if changed_methods:
        print(f"   改动涉及方法: {list(changed_methods)[:8]}")
    if callers:
        print(f"   被以下代码调用 ({len(callers)}):")
        for c in callers[:8]:
            print(f"      {c['file']} (reason: {c['reason']})")
    else:
        print("   (未找到调用方，或向量库无该 repo 数据)")
    print()

    # 5. 向量语义搜索：改动代码相关的其他模块
    print("🔍 向量语义搜索...")
    query_parts = []
    for f in code_files[:3]:
        fpath = os.path.join(REPO_PATH, f)
        if os.path.exists(fpath):
            with open(fpath, 'r', errors='replace') as fh:
                query_parts.append(fh.read()[:1000])

    if query_parts and API_KEY:
        query_text = "\n".join(query_parts)[:2000]
        query_emb = embed_text(query_text, API_KEY)
        if query_emb:
            related = vector_search(query_emb, REPO_NAME)
            if related:
                print(f"   语义关联代码块 (top {len(related)}):")
                for r in related:
                    print(f"   [{r['similarity']:.3f}] {r['file_path']}:L{r.get('line_start', '')}")
                    if r.get('content_preview'):
                        print(f"            {r['content_preview']}")
            else:
                print("   (embedding 维度不匹配或无数据)")
    else:
        print("   (跳过：无代码文件或缺 API Key)")
    print()

    # 5. 输出总结
    print("=" * 60)
    print("📋 爆炸范围总结:")
    print(f"   变更文件: {len(files)} 个")
    print(f"   触及侧: {sorted(sides) or ['未识别']}")
    print(f"   需要重验: {'api_test' if 'backend' in sides else ''} {'web_test' if 'web' in sides else ''} {'device_test' if 'client' in sides else ''} {'static_analysis (always)'}")
    print()


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    main()
