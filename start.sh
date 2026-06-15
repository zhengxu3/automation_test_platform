#!/bin/bash
# Linux 一键启动脚本（在 qa-code-analyzer 上执行）
cd ~/ai-platform
mkdir -p logs

# 停
pkill -f 'gunicorn.*gateway' 2>/dev/null
pkill -f 'ai_worker.main' 2>/dev/null
sleep 2

# 启 Gateway
APP_ENV=production nohup /home/ec2-user/miniconda3/envs/ai_platform/bin/gunicorn \
  -w 2 -b 127.0.0.1:5010 --timeout 120 \
  'gateway.app:create_app()' >> logs/gateway.out 2>&1 &

# 启 Worker
APP_ENV=production nohup /home/ec2-user/miniconda3/envs/ai_platform/bin/python \
  -u -m ai_worker.main >> logs/worker.out 2>&1 &

sleep 3

# 检测
echo "=== STATUS ==="
echo "Gateway: $(pgrep -c -f 'gunicorn.*gateway') procs"
echo "Worker: $(pgrep -c -f 'ai_worker.main') procs"
echo "Health: $(curl -sf http://127.0.0.1:5010/health)"
echo "Worker log:"
tail -2 logs/worker.out
