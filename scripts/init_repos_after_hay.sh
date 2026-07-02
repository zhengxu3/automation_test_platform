#!/bin/bash
# 监测 hay-android 向量化完成后，初始化 holla-api + telebird-ios 并向量化
# 用法: bash ~/ai-platform/scripts/init_repos_after_hay.sh

cd ~/ai-platform
PYTHON=/home/ec2-user/miniconda3/envs/ai_platform/bin/python
export APP_ENV=production

echo "🔭 监测 hay-android 向量化进度..."

while true; do
    STATUS=$($PYTHON -c "
from common.db import get_collection
repos = list(get_collection('ai_git_repos').find({'repo_id': {'\$in': ['300230163197', '5dde89168dfe']}}, {'_id': 0, 'repo_id': 1, 'branch': 1, 'status': 1}))
vectorizing = [r for r in repos if r.get('status') == 'vectorizing']
done = [r for r in repos if r.get('status') in ('vectorized', 'vector_ready')]
failed = [r for r in repos if r.get('status') == 'vector_failed']
if failed:
    print('FAILED')
    for r in failed: print(f'  ❌ {r[\"branch\"]}: {r[\"status\"]}')
elif not vectorizing:
    print('DONE')
    for r in done: print(f'  ✅ {r[\"branch\"]}: {r[\"status\"]}')
else:
    print('WAITING')
    for r in vectorizing: print(f'  ⏳ {r[\"branch\"]}: vectorizing...')
")

    if echo "$STATUS" | grep -q "^DONE"; then
        echo "$STATUS"
        echo ""
        echo "✅ hay-android 向量化完成！开始初始化其他库..."
        break
    elif echo "$STATUS" | grep -q "^FAILED"; then
        echo "$STATUS"
        echo "⚠️  有失败的，继续等待（可能 worker 会重试）..."
    else
        echo "$(date '+%H:%M:%S') $STATUS"
    fi
    sleep 15
done

echo ""
echo "========== 初始化 holla-api (主分支) =========="
cd /data/repos

if [ -d "holla-api/.git" ]; then
    echo "holla-api 已存在，更新..."
    cd holla-api && git fetch origin && git checkout main 2>/dev/null || git checkout master
    git pull
    cd /data/repos
else
    echo "克隆 holla-api..."
    git clone git@github.com:holla-world/holla-api.git holla-api
fi

# 检测主分支
HOLLA_BRANCH=$(cd holla-api && git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|.*/||')
[ -z "$HOLLA_BRANCH" ] && HOLLA_BRANCH="master"
echo "holla-api 主分支: $HOLLA_BRANCH"

echo ""
echo "========== 初始化 telebird-ios (主分支 + 4.51.0) =========="

if [ -d "telebird-ios/.git" ]; then
    echo "telebird-ios 已存在，更新..."
    cd telebird-ios && git fetch origin && git pull origin main 2>/dev/null
    cd /data/repos
else
    echo "克隆 telebird-ios..."
    git clone git@github.com:holla-world/telebird-ios.git telebird-ios
fi

TELE_BRANCH=$(cd telebird-ios && git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|.*/||')
[ -z "$TELE_BRANCH" ] && TELE_BRANCH="main"
echo "telebird-ios 主分支: $TELE_BRANCH"

# checkout 4.51.0 分支
cd telebird-ios && git fetch origin 4.51.0 && git checkout 4.51.0 2>/dev/null
cd /data/repos

echo ""
echo "========== 注册到 ai_git_repos + 向量化 =========="
cd ~/ai-platform

$PYTHON -c "
import uuid, time
from common.db import get_collection

col = get_collection('ai_git_repos')
queue = get_collection('ai_task_queue')

repos_to_add = [
    {'repo_name': 'holla-api', 'git_url': 'git@github.com:holla-world/holla-api.git', 'branch': '$HOLLA_BRANCH', 'local_path': '/data/repos/holla-api', 'language': 'php'},
    {'repo_name': 'telebird-ios', 'git_url': 'git@github.com:holla-world/telebird-ios.git', 'branch': '$TELE_BRANCH', 'local_path': '/data/repos/telebird-ios', 'language': 'ios'},
    {'repo_name': 'telebird-ios', 'git_url': 'git@github.com:holla-world/telebird-ios.git', 'branch': '4.51.0', 'local_path': '/data/repos/telebird-ios', 'language': 'ios', 'parent': True},
]

for r in repos_to_add:
    # 检查是否已存在
    existing = col.find_one({'git_url': r['git_url'], 'branch': r['branch']})
    if existing:
        repo_id = existing['repo_id']
        col.update_one({'repo_id': repo_id}, {'\$set': {'status': 'vectorizing', 'local_path': r['local_path'], 'updated_at': int(time.time())}})
        print(f'已存在，更新: {r[\"repo_name\"]}@{r[\"branch\"]} ({repo_id})')
    else:
        repo_id = uuid.uuid4().hex[:12]
        # 找父库（telebird-ios 4.51.0 挂在主库下面）
        parent_id = None
        if r.get('parent'):
            parent = col.find_one({'git_url': r['git_url'], 'branch': {'\\$ne': r['branch']}, 'parent_repo_id': None})
            parent_id = parent['repo_id'] if parent else None
        col.insert_one({
            'repo_id': repo_id, 'repo_name': r['repo_name'], 'git_url': r['git_url'],
            'branch': r['branch'], 'local_path': r['local_path'], 'language': r['language'],
            'parent_repo_id': parent_id, 'status': 'vectorizing',
            'lock': {'locked': False, 'locked_by': '', 'locked_at': 0},
            'last_commit': '', 'last_vectorized_commit': '', 'error_msg': '',
            'created_at': int(time.time()), 'updated_at': int(time.time()),
        })
        print(f'新建: {r[\"repo_name\"]}@{r[\"branch\"]} ({repo_id})')

    # 入队向量化
    queue.insert_one({
        'task_id': uuid.uuid4().hex[:16], 'task_type': 10, 'status': 1,
        'payload': {'repo_id': repo_id, 'repo_name': r['repo_name'], 'local_path': r['local_path'], 'language': r['language'], 'branch': r['branch']},
        'created_at': int(time.time()),
    })
    print(f'  → 向量化任务已入队')
print()
print('🎉 全部完成！向量化任务已入队，worker 会按序执行。')
"

echo ""
echo "========== 持续监测所有向量化进度 =========="
while true; do
    $PYTHON -c "
from common.db import get_collection
repos = list(get_collection('ai_git_repos').find({'status': 'vectorizing'}, {'_id': 0, 'repo_name': 1, 'branch': 1}))
if repos:
    print(f'⏳ 还有 {len(repos)} 个在向量化:')
    for r in repos:
        print(f'   {r[\"repo_name\"]}@{r[\"branch\"]}')
else:
    print('✅ 全部向量化完成！')
    exit(0)
" && break
    sleep 20
done

echo ""
echo "🎉 所有库初始化+向量化完成！"
