"""AI Service Gateway"""
import os
import threading
import time

from flask import Flask
from flask_cors import CORS

_goal_watchdog_started = False
_code_watcher_started = False


def _truthy(value) -> bool:
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def _start_goal_watchdog_once():
    """启动 Goal 看门狗：补救 gateway 重启窗口导致的 running 卡住。"""
    global _goal_watchdog_started
    if _goal_watchdog_started or not _truthy(os.getenv("GOAL_WATCHDOG_ENABLED", "1")):
        return
    _goal_watchdog_started = True

    interval = int(os.getenv("GOAL_WATCHDOG_INTERVAL_SEC", "30") or 30)
    stale = int(os.getenv("GOAL_WATCHDOG_STALE_SEC", "60") or 60)
    limit = int(os.getenv("GOAL_WATCHDOG_LIMIT", "100") or 100)

    def loop():
        from engine import goal_runtime, goal_scheduler
        while True:
            try:
                due = goal_runtime.process_due_plan_transitions(limit=limit)
                if due.get("processed"):
                    print(f"⏱️ Goal watchdog started {len(due['processed'])} due plan transition(s)")
                result = goal_scheduler.recover_stuck_goals(stale_seconds=stale, limit=limit)
                if result.get("recovered"):
                    print(f"🩺 Goal watchdog recovered {len(result['recovered'])} stuck goal(s)")
            except Exception as exc:
                print(f"⚠️ Goal watchdog error: {str(exc)[:200]}")
            time.sleep(max(interval, 5))

    threading.Thread(target=loop, name="goal-watchdog", daemon=True).start()


def _start_code_watcher_once():
    """启动代码自监控轮询线程。gateway 默认关闭（由 worker 负责），除非显式设 CODE_WATCHER_IN_GATEWAY=1。"""
    global _code_watcher_started
    if _code_watcher_started or not _truthy(os.getenv("CODE_WATCHER_IN_GATEWAY", "0")):
        return
    _code_watcher_started = True
    poll_interval = int(os.getenv("CODE_WATCHER_POLL_SEC", "10") or 10)

    def watcher_loop():
        from engine import code_watcher
        code_watcher.run_loop(interval=poll_interval)

    threading.Thread(target=watcher_loop, name="code-watcher", daemon=True).start()


def create_app():
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
    CORS(app, resources={"/*": {
        "origins": "*",
        "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type", "X-AI-Token"]
    }})

    # 健康检查
    @app.route('/health')
    def health():
        return {"status": "ok"}

    @app.before_request
    def _ensure_goal_watchdog():
        _start_goal_watchdog_once()
        _start_code_watcher_once()

    # 注册路由
    from gateway.routes.auth import bp as auth_bp
    from gateway.routes.agent import bp as agent_bp
    from gateway.routes.goal import bp as goal_bp
    from gateway.routes.knowledge import bp as knowledge_bp
    from gateway.routes.requirement import bp as req_bp
    from gateway.routes.repo import bp as repo_bp
    from gateway.routes.settings import bp as settings_bp
    from gateway.routes.upload import bp as upload_bp

    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(agent_bp, url_prefix='/ai/agent')
    app.register_blueprint(goal_bp, url_prefix='/ai/goal')
    app.register_blueprint(knowledge_bp, url_prefix='/ai/knowledge')
    app.register_blueprint(req_bp, url_prefix='/ai/req')
    app.register_blueprint(repo_bp, url_prefix='/ai/repo')
    app.register_blueprint(settings_bp, url_prefix='/ai/settings')
    app.register_blueprint(upload_bp, url_prefix='/ai/upload')

    # TODO: 后续注册
    # from gateway.routes.requirement import bp as req_bp
    # from gateway.routes.repo import bp as repo_bp
    # from gateway.routes.memory import bp as memory_bp
    # from gateway.routes.knowledge import bp as knowledge_bp
    # from gateway.routes.case import bp as case_bp
    # from gateway.routes.doc import bp as doc_bp
    # from gateway.routes.model import bp as model_bp
    # from gateway.routes.upload import bp as upload_bp

    return app
