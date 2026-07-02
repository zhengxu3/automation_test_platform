"""通知模块 — 钉钉机器人（支持 Goal 级/全局级自定义 webhook）

通知层级：Goal.notifications > ai_settings.notification > 环境变量默认值
没配就不发。
"""
import time
import hmac
import hashlib
import base64
import urllib.parse
import os

# 环境变量兜底（最低优先级）
_DEFAULT_WEBHOOK = os.getenv("DINGTALK_WEBHOOK", "")
_DEFAULT_SECRET = os.getenv("DINGTALK_SECRET", "")
PLATFORM_URL = os.getenv("PLATFORM_URL", "http://great.holla.cool")


def _sign_url(webhook: str, secret: str) -> str:
    """生成带签名的 webhook URL"""
    if secret and secret.startswith("SEC"):
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(
            secret.encode(), string_to_sign.encode(), digestmod=hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return f"{webhook}&timestamp={timestamp}&sign={sign}"
    return webhook


def _send_markdown(webhook: str, secret: str, title: str, text: str) -> bool:
    """发送 markdown 到指定 webhook"""
    if not webhook:
        return False
    import requests
    try:
        url = _sign_url(webhook, secret)
        resp = requests.post(url, json={"msgtype": "markdown", "markdown": {"title": title, "text": text}}, timeout=10)
        return resp.status_code == 200 and resp.json().get("errcode") == 0
    except Exception:
        return False


def _get_notification_configs(goal: dict = None) -> list:
    """获取通知配置列表（Goal 级优先，回退全局，再回退环境变量）"""
    # Goal 级
    if goal and goal.get("notifications"):
        return goal["notifications"]
    # 全局（DB）
    from common.db import get_collection
    settings = get_collection("ai_settings").find_one({"key": "notification"}, {"_id": 0})
    if settings and settings.get("configs"):
        return settings["configs"]
    # 环境变量兜底
    if _DEFAULT_WEBHOOK:
        return [{"type": "dingtalk", "webhook": _DEFAULT_WEBHOOK, "secret": _DEFAULT_SECRET}]
    return []


def _notify_all(goal: dict, title: str, text: str, event: str = "") -> bool:
    """按配置发送通知到所有配置的渠道"""
    configs = _get_notification_configs(goal)
    sent = False
    for cfg in configs:
        # 过滤事件类型（配了 events 列表时只发匹配的）
        events_filter = cfg.get("events")
        if events_filter and event and event not in events_filter:
            continue
        if cfg.get("type") == "dingtalk":
            ok = _send_markdown(cfg.get("webhook", ""), cfg.get("secret", ""), title, text)
            sent = sent or ok
    return sent


def _goal_link(goal_id: str) -> str:
    return f"{PLATFORM_URL}/goal/{goal_id}"


# ==================== 代码监控通知 ====================

def notify_code_detected(goal: dict, repo_name: str, branch: str, commit: str, message: str = "") -> bool:
    """代码提交感知 — 检测到新提交时立即发送"""
    goal_id = goal.get("goal_id", "")
    goal_title = goal.get("title", "")
    t = time.strftime("%H:%M:%S")
    text = (
        f"### 🔔 代码提交检测\n"
        f"**任务**: {goal_title}\n\n"
        f"**仓库**: {repo_name}\n\n"
        f"**分支**: {branch}\n\n"
        f"**提交**: {commit[:8]} {message}\n\n"
        f"**时间**: {t}\n\n"
        f"**状态**: 已触发分析...\n\n"
        f"[查看详情]({_goal_link(goal_id)})"
    )
    return _notify_all(goal, f"提交检测 - {goal_title}", text, "code_detected")


def notify_analysis_done(goal: dict, repo_name: str, branch: str, commit: str,
                         touched_sides: list = None, changed_files: list = None,
                         risk: str = "", round_num: int = 0) -> bool:
    """代码分析完成 — 爆炸范围分析结束后发送"""
    goal_id = goal.get("goal_id", "")
    goal_title = goal.get("title", "")
    sides_text = "、".join({"backend": "后端", "web": "前端", "client": "客户端"}.get(s, s) for s in (touched_sides or []))
    files_text = "\n".join(f"  - {f}" for f in (changed_files or [])[:5])
    if len(changed_files or []) > 5:
        files_text += f"\n  - ...还有 {len(changed_files) - 5} 个"
    text = (
        f"### 📋 代码分析完成\n"
        f"**仓库**: {repo_name}@{branch}\n\n"
        f"**提交**: {commit[:8]}\n\n"
        f"**💥 爆炸范围**: {sides_text or '未识别'}\n\n"
        f"**📝 变更文件**:\n{files_text}\n\n"
        f"**⚠️ 风险**: {risk or '待评估'}\n\n"
        f"**🎯 Goal**: [{goal_title}]({_goal_link(goal_id)}) 第 {round_num} 轮验证\n\n"
    )
    return _notify_all(goal, f"分析完成 - {repo_name}", text, "analysis_done")


# ==================== Goal Runtime 通知 ====================

def notify_goal_completed(goal: dict, partial: bool = False, gaps: list = None) -> bool:
    """Goal 完成 / 部分完成"""
    goal_id = goal.get("goal_id", "")
    goal_title = goal.get("title", "")
    if partial:
        gap_text = "\n".join(f"- {g}" for g in (gaps or []))
        text = (
            f"### 🟠 Goal 部分完成\n"
            f"**{goal_title}**\n\n"
            f"**未覆盖项**:\n{gap_text}\n\n"
            f"[查看详情]({_goal_link(goal_id)})"
        )
    else:
        text = (
            f"### ✅ Goal 完成\n"
            f"**{goal_title}**\n\n"
            f"所有验收点已通过。\n\n"
            f"[查看详情]({_goal_link(goal_id)})"
        )
    return _notify_all(goal, f"{'部分完成' if partial else '完成'} - {goal_title}", text, "goal_completed" if not partial else "goal_partial")


def _risk_level(blast_radius: list, ripple_methods: list, uncovered: list) -> tuple:
    """计算风险等级和原因。返回 (等级, 原因)"""
    score = 0
    reasons = []
    if len(blast_radius or []) >= 5:
        score += 2
        reasons.append(f"改动 {len(blast_radius)} 个方法")
    elif blast_radius:
        score += 1
    if len(ripple_methods or []) >= 3:
        score += 2
        reasons.append(f"波及 {len(ripple_methods)} 个上游调用方")
    elif ripple_methods:
        score += 1
    if len(uncovered or []) >= 2:
        score += 1
        reasons.append(f"{len(uncovered)} 处变更无 Case 覆盖")
    # 检查是否涉及核心接口（支付/鉴权/登录）
    for br in (blast_radius or []):
        name = (br.get("class_name", "") + br.get("name", "")).lower()
        if any(k in name for k in ("pay", "auth", "login", "token", "order", "charge")):
            score += 2
            reasons.append("涉及核心链路")
            break
    if score >= 4:
        return "🔴 高", "；".join(reasons[:2])
    if score >= 2:
        return "🟡 中", "；".join(reasons[:2])
    return "🟢 低", reasons[0] if reasons else "改动范围小"


def notify_case_reminder(goal_id: str, goal_title: str, reminder: dict, platform_url: str = "",
                         commit_info: dict = None, event_id: str = "",
                         blast_radius: list = None, change_summary: str = "",
                         ripple_methods: list = None, reach_analysis: dict = None) -> bool:
    """结构化 Case 提醒通知。

    结构：① 提交概览 → ② 影响范围(修改+波及+风险) → ③ 变更分析 → ④ 触达建议
    设计原则：
    - 主动修改：加风险等级 + 一句话说明
    - 波及：按被调用方法分组（一对多），不逐条列
    - 触达建议：先判断能否触达，能→怎么操作，不能→给建议
    - 已有 Case 覆盖的不展示（一眼能看懂）
    """
    from common.db import get_collection
    url = platform_url or PLATFORM_URL
    hit = reminder.get("hit_cases", [])
    uncovered = reminder.get("uncovered_changes", [])
    ci = commit_info or {}

    # ── 风险计算 ──
    risk_label, risk_reason = _risk_level(blast_radius, ripple_methods, uncovered)

    # ── 标题 ──
    sections = [f"### 📋 代码守护 — {goal_title}\n"]

    # ── ① 提交概览 ──
    if ci:
        repo = ci.get("repo_name", "")
        branch = ci.get("branch", "")
        commit_hash = ci.get("commit", "")[:8]
        msg = ci.get("message", "")[:80]
        changed_files = ci.get("changed_files", [])
        sides = ci.get("touched_sides", [])
        sides_text = "/".join({"backend": "后端", "web": "前端", "client": "客户端"}.get(s, s) for s in sides)
        file_list = " ".join(f"`{f.rsplit('/', 1)[-1]}`" for f in changed_files[:5])
        if len(changed_files) > 5:
            file_list += f" +{len(changed_files) - 5}"
        sections.append(
            f"📦 `{repo}` `{branch}` · `{commit_hash}` · {len(changed_files)}文件 · {sides_text}\n"
            f"> {msg}\n"
        )

    # ── ② 影响范围 ──
    if blast_radius or ripple_methods:
        # 主动修改：风险等级 + 简要说明
        if blast_radius:
            br_lines = []
            for br in blast_radius:
                cn = br.get("class_name", "")
                nm = br.get("name", "")
                route = br.get("route", "")
                desc = (br.get("description") or "")[:60]
                line = f"• `{cn}.{nm}`" if cn else f"• `{nm}`"
                if route:
                    line += f" → {route}"
                br_lines.append(line)
            sections.append(
                f"**✏️ 主动修改（{len(blast_radius)} 个方法）** · 风险 {risk_label}\n"
                + "\n".join(br_lines) + "\n"
            )

        # 波及影响：按"被调用的方法"分组（一对多）+ 场景推断
        if ripple_methods:
            # 按 reason 中提到的被调用方法分组
            groups = {}
            for r in ripple_methods:
                cn = r.get("class_name", "")
                nm = r.get("name", "")
                reason = r.get("reason", "")
                caller = f"{cn}.{nm}" if cn else nm
                # 从 reason 提取被调用的方法名作为 key
                target = reason
                for prefix in ("调用了 ", "调用 ", "calls "):
                    if prefix in reason:
                        target = reason.split(prefix, 1)[1].strip()
                        break
                groups.setdefault(target, []).append(caller)

            rp_lines = []
            for target, callers in groups.items():
                caller_text = "、".join(f"`{c}`" for c in callers[:4])
                if len(callers) > 4:
                    caller_text += f" +{len(callers) - 4}"
                line = f"• `{target}` ← {caller_text}"
                # 如果有场景推断，附加场景
                ra = (reach_analysis or {})
                # 尝试匹配调用方的场景
                scenario_hints = set()
                for c in callers:
                    s = ra.get(c) or {}
                    if s.get("scenario"):
                        scenario_hints.add(s["scenario"])
                if scenario_hints:
                    line += f"\n  📍 场景: {'; '.join(list(scenario_hints)[:2])}"
                rp_lines.append(line)
            sections.append(
                f"**🌊 波及影响（{len(ripple_methods)} 个调用方）**\n"
                + "\n".join(rp_lines) + "\n"
            )

    # ── ③ 变更分析 ──
    if change_summary:
        s = change_summary
        for prefix in ("好的，", "好的,", "作为代码审查专家，", "作为代码审查专家,", "我对本次变更分析如下：", "我对本次变更分析如下:"):
            s = s.replace(prefix, "")
        s = s.strip()
        if s:
            sections.append(f"**📝 变更分析**\n> {s}\n")

    # ── ④ 触达建议（核心：能否触达 + 怎么操作）──
    # 有 Case 覆盖的不用展示
    if uncovered:
        ra = reach_analysis or {}
        reach_lines = []
        for u in uncovered[:5]:
            module = u.get("module", "")
            belongs = u.get("belongs_to", "")
            suggestion = (u.get("suggestion") or "")
            related = u.get("related_cases", [])

            # 优先用 LLM 推断的场景
            scenario = ra.get(module) or ra.get(f"{module}.{(blast_radius or [{}])[0].get('name', '')}") or {}
            if not scenario:
                # 尝试模糊匹配
                for k, v in ra.items():
                    if module.lower() in k.lower() or k.lower() in module.lower():
                        scenario = v
                        break

            if related:
                # 有关联 case：能触达
                rc_text = "、".join(str(r) for r in related[:2])
                reach_lines.append(f"• **{module}** — ✅ 可触达 → {rc_text}")
            elif scenario.get("reachable"):
                # LLM 推断可触达：给出具体步骤
                page = scenario.get("page", "")
                steps = scenario.get("steps", "")
                sc = scenario.get("scenario", "")
                line = f"• **{module}** — ✅ 可触达"
                if sc:
                    line += f"\n  📍 场景: {sc}"
                if page:
                    line += f"\n  📱 入口: {page}"
                if steps:
                    line += f"\n  👉 操作: {steps[:100]}"
                reach_lines.append(line)
            elif scenario and not scenario.get("reachable"):
                # LLM 判断不可直接触达
                reason = scenario.get("reason", "")
                reach_lines.append(f"• **{module}** — ⚠️ 不可直接触达\n  💡 {reason[:80] if reason else '需确认客户端哪个场景调用此方法'}")
            elif belongs:
                reach_lines.append(f"• **{module}** — 🔍 可通过【{belongs}】玩法触达")
            elif suggestion:
                sug_short = suggestion.split("。")[0][:80]
                reach_lines.append(f"• **{module}** — 💡 {sug_short}")
            else:
                reach_lines.append(f"• **{module}** — ⚠️ 无法确定触达路径，需确认客户端哪个场景会调用此方法")

        if len(uncovered) > 5:
            reach_lines.append(f"• ...还有 {len(uncovered) - 5} 处")

        sections.append(
            f"**🎯 触达建议（{len(uncovered)} 处变更未被 Case 覆盖）**\n"
            + "\n".join(reach_lines) + "\n"
        )
    elif not hit:
        sections.append("**✅ 本次变更影响范围已被 Case 覆盖**\n")

    # ── 底部链接 ──
    detail_link = f"{url}/goal/{goal_id}/event/{event_id}" if event_id else f"{url}/goal/{goal_id}"
    sections.append(f"\n[👉 查看完整分析详情]({detail_link})")

    text = "\n".join(sections)
    goal = get_collection("ai_goals").find_one({"goal_id": goal_id}, {"_id": 0}) or {}
    return _notify_all(goal, f"代码守护 - {goal_title}", text, "case_reminder")

def notify_goal_blocked(goal: dict, reason: str = "") -> bool:
    """Goal 阻塞"""
    goal_id = goal.get("goal_id", "")
    goal_title = goal.get("title", "")
    text = (
        f"### ⛔ Goal 阻塞\n"
        f"**{goal_title}**\n\n"
        f"**原因**: {reason}\n\n"
        f"[前往处理]({_goal_link(goal_id)})"
    )
    return _notify_all(goal, f"阻塞 - {goal_title}", text, "goal_blocked")
