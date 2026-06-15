#!/bin/bash
# ============================================
# 统一部署脚本 — AI 平台 + 工具平台
# 用法:
#   bash deploy_platform.sh ai        — 部署 AI 平台（Linux）
#   bash deploy_platform.sh tools     — 部署工具平台（Mac local-server）
#   bash deploy_platform.sh status    — 查看所有服务状态
# ============================================

LINUX="qa-code-analyzer"
MAC="local-server"
SSH_TIMEOUT=8

# AI 平台（重构项目）
AI_LOCAL="/Users/admin/Documents/ai-service"
AI_WEB_LOCAL="/Users/admin/Documents/ai-service-web"
AI_REMOTE="~/ai-platform"
AI_PYTHON="/home/ec2-user/miniconda3/envs/ai_platform/bin/python"
AI_GUNICORN="/home/ec2-user/miniconda3/envs/ai_platform/bin/gunicorn"

# 工具平台（老项目）
TOOLS_LOCAL="/Users/admin/PycharmProjects/automation_match_test"
TOOLS_WEB_LOCAL="/Users/admin/WebstormProjects/auto_tools"
TOOLS_REMOTE="~/Documents/project_all/python_web/automation_match_test"
TOOLS_VUE_REMOTE="~/Documents/project_all/admin_vue"
MAC_PYTHON="/opt/homebrew/Caskroom/miniconda/base/envs/py39/bin/python"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

run_remote() {
    local host=$1; shift
    timeout $SSH_TIMEOUT ssh -o ConnectTimeout=5 -o ServerAliveInterval=3 "$host" "$@" 2>/dev/null
}

# ========== AI 平台部署 ==========
deploy_ai() {
    echo -e "${YELLOW}━━━ AI 平台部署（Linux → great.holla.cool）━━━${NC}"

    echo -e "${YELLOW}[1/4] 同步后端代码...${NC}"
    rsync -az --delete --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' --exclude='.pytest_cache' \
        "$AI_LOCAL/" "$LINUX:$AI_REMOTE/" 2>/dev/null
    echo -e "${GREEN}  ✅ 后端同步完成${NC}"

    echo -e "${YELLOW}[2/4] 重启 Gateway...${NC}"
    run_remote $LINUX "pkill -f 'gunicorn.*gateway' 2>/dev/null"
    sleep 1
    ssh -o ConnectTimeout=5 -f $LINUX "cd $AI_REMOTE && APP_ENV=production $AI_GUNICORN -w 2 -b 127.0.0.1:5010 --timeout 120 'gateway.app:create_app()' >> logs/gateway.out 2>&1" 2>/dev/null
    sleep 2
    local health=$(run_remote $LINUX "curl -sf http://127.0.0.1:5010/health")
    if [[ "$health" == *"ok"* ]]; then
        echo -e "${GREEN}  ✅ Gateway 运行中${NC}"
    else
        echo -e "${RED}  ❌ Gateway 启动失败，查看: ssh $LINUX 'cd $AI_REMOTE && $AI_PYTHON -c \"from gateway.app import create_app; create_app()\"'${NC}"
    fi

    echo -e "${YELLOW}[3/4] 重启 AI Worker...${NC}"
    run_remote $LINUX "pkill -f 'ai_worker.main' 2>/dev/null"
    sleep 1
    ssh -o ConnectTimeout=5 -f $LINUX "cd $AI_REMOTE && APP_ENV=production $AI_PYTHON -u -m ai_worker.main >> logs/worker.out 2>&1" 2>/dev/null
    sleep 2
    local wk=$(run_remote $LINUX "pgrep -f 'ai_worker.main' | head -1")
    if [ -n "$wk" ]; then
        echo -e "${GREEN}  ✅ AI Worker PID: $wk${NC}"
    else
        echo -e "${RED}  ❌ Worker 启动失败，查看: ssh $LINUX 'tail -20 $AI_REMOTE/logs/worker.out'${NC}"
    fi

    echo -e "${YELLOW}[4/4] Nginx 配置...${NC}"
    local nginx_ok=$(run_remote $LINUX "test -f /etc/nginx/conf.d/ai-platform.conf && echo 'yes'")
    if [ "$nginx_ok" = "yes" ]; then
        echo -e "${GREEN}  ✅ Nginx 已配置${NC}"
    else
        echo -e "${YELLOW}  ⚠️  Nginx 未配置，执行: bash deploy_platform.sh nginx${NC}"
    fi

    echo ""
    echo -e "${GREEN}━━━ AI 平台部署完成 ━━━${NC}"
}

# ========== 工具平台部署 ==========
deploy_tools() {
    echo -e "${YELLOW}━━━ 工具平台部署（Mac local-server）━━━${NC}"

    echo -e "${YELLOW}[1/3] 同步后端 + 热重载 Flask...${NC}"
    rsync -az --exclude='__pycache__' --exclude='*.pyc' --exclude='.venv' \
        "$TOOLS_LOCAL/app/" "$MAC:$TOOLS_REMOTE/app/" 2>/dev/null
    rsync -az --exclude='__pycache__' "$TOOLS_LOCAL/core/" "$MAC:$TOOLS_REMOTE/core/" 2>/dev/null
    rsync -az --exclude='__pycache__' "$TOOLS_LOCAL/worker/" "$MAC:$TOOLS_REMOTE/worker/" 2>/dev/null
    rsync -az --exclude='__pycache__' "$TOOLS_LOCAL/ai_core/" "$MAC:$TOOLS_REMOTE/ai_core/" 2>/dev/null
    scp -q "$TOOLS_LOCAL/web_run.py" "$MAC:$TOOLS_REMOTE/web_run.py" 2>/dev/null
    run_remote $MAC "kill -HUP \$(pgrep -f 'gunicorn.*web_run' | head -1) 2>/dev/null"
    echo -e "${GREEN}  ✅ Flask 已热重载${NC}"

    echo -e "${YELLOW}[2/3] 重启 Mac Worker...${NC}"
    run_remote $MAC "pkill -f 'python.*worker.main' 2>/dev/null; sleep 1"
    run_remote $MAC "cd $TOOLS_REMOTE && APP_ENV=production nohup $MAC_PYTHON -m worker.main > logs/worker.out 2>&1 &"
    sleep 2
    echo -e "${GREEN}  ✅ Worker 已重启${NC}"

    echo -e "${YELLOW}[3/3] 构建前端...${NC}"
    cd "$TOOLS_WEB_LOCAL" && npm run build:prod 2>&1 | tail -2
    rsync -az --delete "$TOOLS_WEB_LOCAL/dist/" "$MAC:$TOOLS_VUE_REMOTE/" 2>/dev/null
    echo -e "${GREEN}  ✅ 前端部署完成${NC}"

    echo ""
    echo -e "${GREEN}━━━ 工具平台部署完成 ━━━${NC}"
}

# ========== Nginx 配置 ==========
setup_nginx() {
    echo -e "${YELLOW}配置 Nginx...${NC}"
    timeout 15 ssh -o ConnectTimeout=5 $LINUX "sudo tee /etc/nginx/conf.d/ai-platform.conf > /dev/null << 'NGINX'
server {
    listen 80;
    server_name great.holla.cool;

    root /home/ec2-user/ai-platform/dist;
    index index.html;

    location / {
        try_files \$uri \$uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:5010/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;
        client_max_body_size 100m;
    }

    location /tool-api/ {
        proxy_pass http://10.40.18.23:5005/;
        proxy_set_header Host \$host;
        proxy_read_timeout 30s;
    }

    location /device-api/ {
        proxy_pass http://10.40.18.40:5007/;
        proxy_read_timeout 30s;
    }

    location /device-ws/ {
        proxy_pass http://10.40.18.40:5008/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \"upgrade\";
        proxy_read_timeout 3600s;
    }
}
NGINX
sudo nginx -t && sudo systemctl reload nginx && echo 'OK'" 2>&1
    echo -e "${GREEN}✅ Nginx 完成${NC}"
}

# ========== 状态 ==========
show_status() {
    echo -e "${YELLOW}━━━ 服务状态 ━━━${NC}"
    echo ""
    echo "  [AI 平台 - Linux]"
    echo "    Gateway: $(run_remote $LINUX "pgrep -c -f 'gunicorn.*gateway'" || echo 0) 进程"
    echo "    Worker:  $(run_remote $LINUX "pgrep -c -f 'ai_worker.main'" || echo 0) 进程"
    echo "    Health:  $(run_remote $LINUX "curl -sf http://127.0.0.1:5010/health" || echo "无响应")"
    echo ""
    echo "  [工具平台 - Mac]"
    echo "    Flask:   $(run_remote $MAC "pgrep -c -f 'gunicorn.*web_run'" || echo 0) 进程"
    echo "    Worker:  $(run_remote $MAC "pgrep -c -f 'worker.main'" || echo 0) 进程"
    echo ""
}

# ========== 入口 ==========
case ${1:-help} in
    ai)       deploy_ai ;;
    tools)    deploy_tools ;;
    nginx)    setup_nginx ;;
    status)   show_status ;;
    all)      deploy_ai; echo ""; deploy_tools ;;
    *)
        echo "用法: bash deploy_platform.sh <command>"
        echo ""
        echo "  ai       部署 AI 平台（Linux: Gateway + Worker）"
        echo "  tools    部署工具平台（Mac: Flask + Worker + 前端）"
        echo "  nginx    配置 Linux Nginx"
        echo "  status   查看所有服务状态"
        echo "  all      全部部署"
        ;;
esac
