"""知识库管理路由"""
import time
import uuid
from flask import Blueprint, request
from common.auth import require_auth
from common.response import ok, err
from common.db import get_collection

bp = Blueprint('knowledge', __name__)
COL = "ai_knowledge_base"


@bp.route('/list', methods=['GET'])
@require_auth
def knowledge_list():
    """知识列表（分页，按 tags/project/keyword 过滤）"""
    tags = request.args.get('tags', '')
    project = request.args.get('project', '')
    keyword = request.args.get('keyword', '').strip()
    page = int(request.args.get('page', 1))
    page_size = int(request.args.get('page_size', 20))

    query = {}
    if tags:
        query["tags"] = {"$in": [t.strip() for t in tags.split(',')]}
    if project:
        query["project"] = project
    if keyword:
        query["title"] = {"$regex": keyword, "$options": "i"}

    col = get_collection(COL)
    total = col.count_documents(query)
    items = list(col.find(query, {"_id": 0, "content": 0, "embedding": 0})
                 .sort("created_at", -1)
                 .skip((page - 1) * page_size)
                 .limit(page_size))
    return ok({"items": items, "total": total, "page": page})


@bp.route('/detail', methods=['GET'])
@require_auth
def knowledge_detail():
    """知识详情（含完整内容，不含 embedding）"""
    knowledge_id = request.args.get('knowledge_id', '')
    if not knowledge_id:
        return err("缺少 knowledge_id")
    item = get_collection(COL).find_one({"knowledge_id": knowledge_id}, {"_id": 0, "embedding": 0})
    if not item:
        return err("知识不存在", 404)
    return ok({"item": item})


@bp.route('/search', methods=['GET'])
@require_auth
def knowledge_search():
    """搜索（向量语义搜索 + 关键词 fallback）"""
    import re
    q = request.args.get('q', '')
    top_k = int(request.args.get('top_k', 5))
    if not q:
        return ok({"results": [], "method": "none"})

    # 优先向量搜索
    try:
        from google import genai
        client = genai.Client(api_key=_get_embedding_key())
        embed_result = client.models.embed_content(model="gemini-embedding-001", contents=q)
        query_vec = embed_result.embeddings[0].values

        results = list(get_collection(COL).aggregate([
            {"$vectorSearch": {"index": "vector_index", "path": "embedding", "queryVector": query_vec, "numCandidates": 50, "limit": top_k}},
            {"$project": {"_id": 0, "title": 1, "tags": 1, "knowledge_id": 1, "content": {"$substrBytes": ["$content", 0, 200]}, "score": {"$meta": "vectorSearchScore"}}}
        ]))
        if results:
            return ok({"results": results, "method": "vector"})
    except Exception:
        pass

    # fallback: 关键词
    stop_words = {'的', '了', '是', '在', '和', '与', '或', '有', '什么', '怎么', '如何', '哪些'}
    words = [w for w in re.split(r'[\s,，。、?？!！]+', q) if w and w not in stop_words and len(w) >= 2]
    keywords = words[:4]
    if not keywords:
        keywords = [q[:4]]
    or_conditions = []
    for kw in keywords:
        or_conditions.extend([
            {"title": {"$regex": re.escape(kw), "$options": "i"}},
            {"tags": {"$regex": re.escape(kw), "$options": "i"}},
        ])
    results = list(get_collection(COL).find({"$or": or_conditions}, {"_id": 0, "embedding": 0, "content": 0}).limit(top_k))
    return ok({"results": results, "method": "keyword"})


def _get_embedding_key():
    import yaml, os
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    local_path = os.path.join(base, "config.local.yaml")
    with open(local_path, 'r') as f:
        cfg = yaml.safe_load(f)
    return cfg.get("llm", {}).get("models", {}).get("gemini_flash", {}).get("api_key", "")


@bp.route('/upload', methods=['POST'])
@require_auth
def knowledge_upload():
    """上传文件（PDF/MD/TXT）解析为文本返回"""
    file = request.files.get('file')
    if not file or not file.filename:
        return err("请上传文件")

    filename = file.filename.lower()
    content = ''

    if filename.endswith('.pdf'):
        import fitz
        pdf_bytes = file.read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        content = '\n'.join(page.get_text() for page in doc)
        doc.close()
    elif filename.endswith(('.md', '.txt', '.markdown')):
        content = file.read().decode('utf-8', errors='replace')
    else:
        return err("仅支持 PDF/MD/TXT 文件")

    title = file.filename.rsplit('.', 1)[0] if '.' in file.filename else file.filename
    return ok({"title": title, "content": content, "filename": file.filename})


@bp.route('/create', methods=['POST'])
@require_auth
def knowledge_create():
    """手动创建知识条目"""
    data = request.get_json() or {}
    title = data.get('title', '').strip()
    if not title:
        return err("标题不能为空")

    kid = f"k_{uuid.uuid4().hex[:8]}"
    doc = {
        "knowledge_id": kid,
        "title": title,
        "content": data.get("content", ""),
        "tags": data.get("tags", []),
        "project": data.get("project", ""),
        "source_type": data.get("source_type", "manual"),  # manual/archive/code_infer
        "source_req_id": data.get("source_req_id", ""),
        "confidence": {"overall": "full", "verified": [], "unverified": []},
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    get_collection(COL).insert_one(doc)
    return ok({"knowledge_id": kid})


@bp.route('/update', methods=['POST'])
@require_auth
def knowledge_update():
    """更新知识"""
    data = request.get_json() or {}
    kid = data.get('knowledge_id', '')
    if not kid:
        return err("缺少 knowledge_id")
    allowed = {"title", "content", "tags", "project", "confidence"}
    update = {k: v for k, v in data.items() if k in allowed}
    update["updated_at"] = int(time.time())
    get_collection(COL).update_one({"knowledge_id": kid}, {"$set": update})
    return ok({"knowledge_id": kid})


@bp.route('/<kid>', methods=['DELETE'])
@require_auth
def knowledge_delete(kid):
    """删除知识"""
    get_collection(COL).delete_one({"knowledge_id": kid})
    return ok({"deleted": True})


@bp.route('/tags', methods=['GET'])
@require_auth
def knowledge_tags():
    """获取所有已有标签（去重）"""
    pipeline = [{"$unwind": "$tags"}, {"$group": {"_id": "$tags"}}, {"$sort": {"_id": 1}}]
    tags = [doc["_id"] for doc in get_collection(COL).aggregate(pipeline)]
    return ok({"tags": tags})


@bp.route('/stats', methods=['GET'])
@require_auth
def knowledge_stats():
    """知识库统计"""
    col = get_collection(COL)
    total = col.count_documents({})
    by_source = list(col.aggregate([{"$group": {"_id": "$source_type", "count": {"$sum": 1}}}]))
    by_project = list(col.aggregate([{"$group": {"_id": "$project", "count": {"$sum": 1}}}]))
    return ok({
        "total": total,
        "by_source": {d["_id"] or "unknown": d["count"] for d in by_source},
        "by_project": {d["_id"] or "global": d["count"] for d in by_project},
    })


@bp.route('/graph', methods=['GET'])
@require_auth
def knowledge_graph():
    """知识图谱（节点=知识点，边=标签重叠+来源关联+内容相似度）"""
    import re as _re
    from collections import Counter as _Counter

    items = list(get_collection(COL).find({}, {"_id": 0, "knowledge_id": 1, "title": 1, "tags": 1, "type": 1, "source_req_id": 1, "source_type": 1, "created_at": 1, "content": 1}))
    nodes = []
    for i in items:
        nodes.append({
            "id": i.get("knowledge_id", ""),
            "title": i.get("title", ""),
            "tags": i.get("tags", []),
            "type": i.get("source_type", ""),
            "source": i.get("source_req_id", ""),
            "created_at": i.get("created_at", 0),
        })

    def _tokens(text):
        return _re.findall(r'[\u4e00-\u9fff]|[a-zA-Z0-9]+', (text or '').lower())

    doc_tokens = [_tokens(i.get("title", "") + " " + (i.get("content", "") or "")[:500]) for i in items]

    edges = []
    seen = set()
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            a_id, b_id = nodes[i]["id"], nodes[j]["id"]
            key = (a_id, b_id)
            shared = set(nodes[i]["tags"]) & set(nodes[j]["tags"])
            if len(shared) >= 2:
                edges.append({"from": a_id, "to": b_id, "type": "strong"})
                seen.add(key)
            elif len(shared) == 1:
                edges.append({"from": a_id, "to": b_id, "type": "weak"})
                seen.add(key)
            if key not in seen and nodes[i]["source"] and nodes[i]["source"] == nodes[j]["source"]:
                edges.append({"from": a_id, "to": b_id, "type": "source"})
                seen.add(key)
            if key not in seen and doc_tokens[i] and doc_tokens[j]:
                set_i, set_j = set(doc_tokens[i]), set(doc_tokens[j])
                inter = len(set_i & set_j)
                union = len(set_i | set_j)
                if union > 0 and inter / union > 0.15:
                    edges.append({"from": a_id, "to": b_id, "type": "similar"})

    return ok({"nodes": nodes, "edges": edges})
