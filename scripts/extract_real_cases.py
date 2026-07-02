"""后台提取真实 case 并写入 DB，替换假数据"""
import fitz, sys, json, time
sys.path.insert(0, '/Users/admin/Documents/ai-service')
from llm.structured import generate_structured
from common.db import get_collection

GOAL_ID = "goal_b04aec33"
FILES = [
    '/Users/admin/Downloads/QA-135136150-230626-1050-8.pdf',
    '/Users/admin/Downloads/QA-135136154-230626-1050-4.pdf',
    '/Users/admin/Downloads/QA-135136575-230626-1050-6.pdf',
]

SYSTEM = "你是测试用例提取专家。从文本中识别并提取每一条测试用例。完整保留原始用例内容(步骤/预期结果)不修改不缩写。module取业务模块名。"
BATCH = 12000

all_cases = []
for f in FILES:
    doc = fitz.open(f)
    text = '\n'.join(page.get_text() for page in doc)
    doc.close()
    fname = f.split('/')[-1]
    print(f"[{time.strftime('%H:%M:%S')}] {fname} ({len(text)} chars)...", flush=True)

    for start in range(0, len(text), BATCH - 500):
        batch = text[start:start + BATCH]
        r = generate_structured(
            system_prompt=SYSTEM,
            user_prompt=f'提取所有测试用例返回JSON：{{"cases":[{{"case_id":"TC-001","title":"原始标题","module":"业务模块","steps":"完整步骤","expected":"完整预期","priority":"P1"}}]}}\n\n文本：\n{batch}',
            schema={'required': ['cases'], 'types': {'cases': 'list'}},
            default={'cases': []},
            require_confidence=False,
        )
        batch_cases = r.data.get('cases', [])
        all_cases.extend(batch_cases)
        print(f"  batch {start//BATCH}: {len(batch_cases)} cases", flush=True)

    print(f"  subtotal: {len(all_cases)}", flush=True)

# 去重 + 编号
seen = set()
cases = []
now = int(time.time())
for c in all_cases:
    title = c.get("title", "").strip()
    if not title or title in seen:
        continue
    seen.add(title)
    cases.append({
        "case_id": f"TC-{len(cases)+1:03d}",
        "goal_id": GOAL_ID,
        "title": title,
        "module": c.get("module", ""),
        "steps": c.get("steps", ""),
        "expected": c.get("expected", ""),
        "priority": c.get("priority", "P1"),
        "api_info": {},
        "source_file": "",
        "created_at": now,
    })

print(f"\ntotal unique: {len(cases)}", flush=True)
get_collection("ai_goal_cases").delete_many({"goal_id": GOAL_ID})
if cases:
    get_collection("ai_goal_cases").insert_many(cases)
print(f"DB updated: {len(cases)} cases inserted", flush=True)
json.dump(cases, open('/tmp/real_cases.json','w'), ensure_ascii=False, indent=2)
print("done", flush=True)
