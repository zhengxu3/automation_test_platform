"""统一状态机 — Goal Runtime 状态边界

核心铁律：所有核心状态只能通过 transition() 修改，路由/任务不得直接写 status。
非法转移直接拒绝（代码强制，非约定）。
"""


class IllegalTransition(Exception):
    """非法状态转移"""
    pass


# ==================== Goal 状态机 ====================

GOAL_STATES = [
    "discovering",        # 可行性画像中
    "planning",           # 规划中
    "awaiting_approval",  # 等待人工批准计划/目标
    "running",            # 执行中
    "blocked",            # 阻塞（缺资源/等人工）
    "verifying",          # 验收中
    "replanning",         # 重新规划（偏移/质量不达标）
    "guarding",           # 持续守护态（达标但持续模式不停）
    "completed",          # 完成
    "partial_completed",  # 部分完成
    "failed",             # 失败
    "cancelled",          # 取消
    "paused",             # 暂停
]

GOAL_TRANSITIONS = {
    "discovering":       {"planning", "blocked", "failed", "cancelled"},
    "planning":          {"awaiting_approval", "running", "blocked", "failed", "cancelled"},
    "awaiting_approval": {"running", "replanning", "cancelled", "paused"},
    "running":           {"planning", "verifying", "blocked", "replanning", "paused", "failed", "cancelled"},
    "blocked":           {"running", "replanning", "paused", "failed", "cancelled"},
    "verifying":         {"completed", "partial_completed", "guarding", "running", "replanning", "blocked"},
    "replanning":        {"awaiting_approval", "running", "failed", "cancelled"},
    "guarding":          {"running", "completed", "cancelled"},  # 守护态：新提交回 running / 人工结束
    "paused":            {"running", "cancelled"},
    # 终态
    "completed":         set(),
    "partial_completed": {"running", "guarding"},  # 部分完成可补充资源继续
    "failed":            set(),
    "cancelled":         set(),
}

# ==================== Step 状态机 ====================

STEP_STATES = [
    "pending",    # 等待（依赖未就绪）
    "ready",      # 就绪（依赖满足，可执行）
    "running",    # 执行中
    "waiting",    # 等审批/等设备
    "completed",  # 完成
    "degraded",   # 降级运行（用 fallback）
    "retrying",   # 重试中
    "failed",     # 失败
    "blocked",    # 阻塞（前置条件不满足）
    "skipped",    # 跳过
]

STEP_TRANSITIONS = {
    "pending":   {"ready", "blocked", "skipped", "cancelled"},
    "ready":     {"running", "waiting", "blocked", "skipped"},
    "running":   {"completed", "degraded", "retrying", "failed", "waiting", "blocked"},
    "waiting":   {"running", "blocked", "skipped", "cancelled"},
    "retrying":  {"running", "degraded", "failed"},
    "degraded":  {"completed", "failed"},
    "blocked":   {"ready", "skipped", "failed", "running"},
    "completed": set(),
    "failed":    {"retrying", "skipped"},
    "skipped":   set(),
    "cancelled": set(),
}

# ==================== Evidence 状态机 ====================

EVIDENCE_VERDICTS = ["pending", "pass", "fail", "partial", "blocked",
                     "prepared",          # 准备级证据已就绪（用例生成/需求拆解），未业务验证
                     "not_applicable"]    # 验证级步骤未做任何实证（如 branch_review 无 diff），诚实非 pass

# ==================== 转移校验 ====================

def can_transition(entity_type: str, from_state: str, to_state: str) -> bool:
    table = {
        "goal": GOAL_TRANSITIONS,
        "step": STEP_TRANSITIONS,
    }.get(entity_type)
    if table is None:
        raise ValueError(f"未知实体类型: {entity_type}")
    allowed = table.get(from_state, set())
    return to_state in allowed


def assert_transition(entity_type: str, from_state: str, to_state: str):
    if from_state == to_state:
        return  # 幂等：同状态允许
    if not can_transition(entity_type, from_state, to_state):
        raise IllegalTransition(
            f"{entity_type}: 非法转移 {from_state} → {to_state}"
        )


# ==================== Engine 独占的状态写入 ====================

def transition(db, collection: str, id_field: str, entity_id: str,
               entity_type: str, to_state: str, event: str = "",
               extra: dict = None, actor: str = "system") -> dict:
    """唯一的状态写入入口。校验合法性 → 更新 → 记录事件。
    路由和任务必须调这个，不得直接 update status。
    """
    import time

    doc = db[collection].find_one({id_field: entity_id})
    if not doc:
        raise ValueError(f"{entity_type} 不存在: {entity_id}")

    from_state = doc.get("status", "")
    assert_transition(entity_type, from_state, to_state)

    update = {"status": to_state, "updated_at": int(time.time())}
    if extra:
        update.update(extra)
    db[collection].update_one({id_field: entity_id}, {"$set": update})

    # 记录状态转移事件（可回放）
    goal_id = doc.get("goal_id") or (entity_id if entity_type == "goal" else "")
    db["ai_goal_events"].insert_one({
        "goal_id": goal_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "event": event or f"{from_state}→{to_state}",
        "from_state": from_state,
        "to_state": to_state,
        "actor": actor,
        "timestamp": int(time.time()),
    })

    return {"from": from_state, "to": to_state}


def emit_event(db, goal_id: str, event_type: str, payload: dict, actor: str = "system"):
    """记录非状态转移的事件（如 steward 思考、降级、审批）。供前端实时流 + 回放。"""
    import time
    doc = {
        "goal_id": goal_id,
        "entity_type": "event",
        "event": event_type,
        "payload": payload,
        "actor": actor,
        "timestamp": int(time.time()),
    }
    db["ai_goal_events"].insert_one(doc)
    return doc
