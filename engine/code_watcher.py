"""代码自监控：后台轮询检测 repo 新提交，触发爆炸范围分析。"""
import subprocess
import time
from common.db import get_collection

_MIN_INTERVAL = 10
_NON_TERMINAL = ["discovering", "planning", "running", "verifying", "replanning",
                 "awaiting_approval", "guarding", "partial_completed", "paused", "blocked"]


def _git_fetch_and_head(local_path: str, branch: str) -> str | None:
    """fetch remote 并返回 origin/{branch} 的 commit hash，失败返回 None。"""
    try:
        subprocess.run(["git", "fetch", "origin", branch], cwd=local_path,
                       capture_output=True, timeout=30)
        r = subprocess.run(["git", "rev-parse", f"origin/{branch}"],
                           cwd=local_path, capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    # 本地仓（无 remote）直接读 HEAD
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"],
                           cwd=local_path, capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def poll_once():
    """扫一遍所有活跃 goal 中 watch=True 的 repo，检测新提交。"""
    goals_col = get_collection("ai_goals")
    now = int(time.time())

    # 查非终态且有 watch repo 的 goal
    cursor = goals_col.find(
        {"status": {"$in": _NON_TERMINAL},
         "sources": {"$elemMatch": {"type": "repo", "watch": True}}},
        {"_id": 0, "goal_id": 1, "sources": 1, "status": 1}
    )

    for goal in cursor:
        goal_id = goal["goal_id"]
        sources = goal.get("sources", [])
        for i, src in enumerate(sources):
            if src.get("type") != "repo" or not src.get("watch"):
                continue

            interval = max(src.get("watch_interval", 60), _MIN_INTERVAL)
            last_poll = src.get("_last_poll_at", 0)
            if now - last_poll < interval:
                continue

            local_path = src.get("local_path", "")
            branch = src.get("branch", "main") or "main"
            if not local_path:
                continue

            new_head = _git_fetch_and_head(local_path, branch)
            if not new_head:
                # 更新 poll 时间避免疯狂重试
                goals_col.update_one(
                    {"goal_id": goal_id},
                    {"$set": {f"sources.{i}._last_poll_at": now}})
                continue

            last_seen = src.get("last_seen_commit", "")

            # 更新 poll 时间
            goals_col.update_one(
                {"goal_id": goal_id},
                {"$set": {f"sources.{i}._last_poll_at": now}})

            if new_head == last_seen:
                continue  # 无变化

            # 检测到新提交！
            # running 中不能触发新轮 → 记入 pending_commits，等本轮结束后处理
            from engine.goal_runtime import trigger_code_update_round

            status = goal.get("status", "")
            if status in ("discovering", "planning", "running", "verifying", "replanning"):
                # 在途：只记录 pending，不触发，不空转
                pending = src.get("_pending_commit") or ""
                if pending != new_head:
                    goals_col.update_one(
                        {"goal_id": goal_id},
                        {"$set": {f"sources.{i}._pending_commit": new_head,
                                  f"sources.{i}._pending_before": last_seen}})
                continue

            # 非在途状态（guarding/partial_completed/paused/blocked）→ 直接触发
            # 先检查有没有之前攒的 pending（取最新的 after）
            pending_commit = src.get("_pending_commit") or ""
            pending_before = src.get("_pending_before") or last_seen
            actual_after = pending_commit if pending_commit else new_head
            actual_before = pending_before if pending_commit else last_seen

            # 先回写 commit/last_before 到 source（trigger 内 step_input_resolver 需要读）
            goals_col.update_one(
                {"goal_id": goal_id},
                {"$set": {f"sources.{i}.commit": actual_after,
                          f"sources.{i}.last_before": actual_before}})

            # trigger 分析
            result = trigger_code_update_round(
                goal_id,
                reason=f"自监控检测到新提交 {actual_after[:8]}",
                changed_repo_id=src.get("repo_id", ""),
                before_ref=actual_before,
                after_ref=actual_after,
            )
            if result.get("ok") and not result.get("skipped"):
                goals_col.update_one(
                    {"goal_id": goal_id},
                    {"$set": {f"sources.{i}.last_seen_commit": actual_after},
                     "$unset": {f"sources.{i}._pending_commit": "",
                                f"sources.{i}._pending_before": ""}})
                print(f"🔭 code_watcher: {goal_id} repo[{i}] 新提交 {actual_after[:8]}，已触发")
                try:
                    from common.notify import notify_code_detected
                    _git_url = src.get("git_url", "")
                    _repo_label = _git_url.split("/")[-1].replace(".git", "") if "/" in _git_url else os.path.basename(local_path)
                    _role = src.get("role", "")
                    _display_name = f"{_role + ' · ' if _role else ''}{_repo_label}"
                    notify_code_detected(goal, repo_name=_display_name, branch=branch, commit=actual_after)
                except Exception:
                    pass


def run_loop(interval: int = 10):
    """主循环，供后台线程调用。"""
    print(f"🔭 code_watcher 启动 | 轮询间隔 {interval}s")
    while True:
        try:
            poll_once()
        except Exception as exc:
            print(f"⚠️ code_watcher error: {str(exc)[:200]}")
        time.sleep(max(interval, 5))
