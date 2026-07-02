"""文件上传路由：需求文档 / 用例文件上传，供 goal_create 引用。"""
import os
import uuid
import time

from flask import Blueprint, request
from common.response import ok, err
from common.auth import require_auth

bp = Blueprint('upload', __name__)

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXTENSIONS = {'.md', '.txt', '.csv', '.xls', '.xlsx', '.doc', '.docx', '.pdf', '.json', '.yaml', '.yml'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


@bp.route('/file', methods=['POST'])
@require_auth
def upload_file():
    """上传文件，返回 file_id 供 goal_create sources 引用。

    form-data:
      file: 文件
      category: doc | testcase（默认 doc）
    """
    if 'file' not in request.files:
        return err("缺少 file 字段")
    f = request.files['file']
    if not f.filename:
        return err("文件名为空")

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return err(f"不支持的文件类型: {ext}，允许: {', '.join(sorted(ALLOWED_EXTENSIONS))}")

    category = request.form.get('category', 'doc')
    if category not in ('doc', 'testcase'):
        return err("category 只能是 doc 或 testcase")

    file_id = f"file_{uuid.uuid4().hex[:12]}"
    filename_safe = f"{file_id}{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename_safe)
    f.save(filepath)

    size = os.path.getsize(filepath)
    if size > MAX_FILE_SIZE:
        os.remove(filepath)
        return err(f"文件过大: {size} bytes，上限 {MAX_FILE_SIZE}")

    from common.db import get_collection
    get_collection("ai_uploaded_files").insert_one({
        "file_id": file_id,
        "original_name": f.filename,
        "stored_name": filename_safe,
        "category": category,
        "ext": ext,
        "size": size,
        "uploaded_at": int(time.time()),
    })

    return ok({"file_id": file_id, "filename": f.filename, "category": category, "size": size})


def read_file_content(file_id: str) -> dict | None:
    """读取已上传文件的文本内容。返回 {file_id, filename, category, content} 或 None。"""
    from common.db import get_collection
    meta = get_collection("ai_uploaded_files").find_one({"file_id": file_id}, {"_id": 0})
    if not meta:
        return None
    filepath = os.path.join(UPLOAD_DIR, meta["stored_name"])
    if not os.path.isfile(filepath):
        return None

    ext = meta.get("ext", "")
    content = ""
    if ext in ('.md', '.txt', '.csv', '.json', '.yaml', '.yml'):
        with open(filepath, 'r', encoding='utf-8', errors='replace') as fh:
            content = fh.read()
    elif ext in ('.xls', '.xlsx'):
        content = _read_excel(filepath)
    elif ext in ('.doc', '.docx'):
        content = _read_docx(filepath)
    elif ext == '.pdf':
        content = _read_pdf(filepath)
    else:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as fh:
            content = fh.read()

    return {
        "file_id": file_id,
        "filename": meta["original_name"],
        "category": meta["category"],
        "content": content,
    }


def _read_excel(path: str) -> str:
    """尝试读取 Excel，降级为空。"""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        lines = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                lines.append('\t'.join(str(c) if c is not None else '' for c in row))
        return '\n'.join(lines)
    except Exception:
        return "(Excel 解析失败，请转为 CSV 或 TXT 上传)"


def _read_docx(path: str) -> str:
    try:
        import docx
        doc = docx.Document(path)
        return '\n'.join(p.text for p in doc.paragraphs)
    except Exception:
        return "(DOCX 解析失败，请转为 TXT 上传)"


def _read_pdf(path: str) -> str:
    try:
        import PyPDF2
        with open(path, 'rb') as fh:
            reader = PyPDF2.PdfReader(fh)
            return '\n'.join(page.extract_text() or '' for page in reader.pages)
    except Exception:
        return "(PDF 解析失败，请转为 TXT 上传)"
