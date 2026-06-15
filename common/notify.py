"""通知模块 — 钉钉机器人（Goal Runtime 人工介入/守护事件通知）

用于把"需要人类感知"的事件推到钉钉：
- 需要人工审批（高风险步骤 / 偏离目标的衍生分支）
- 目标偏移提醒
- Goal 完成 / 部分完成
- 守护态异常（持续模式下回归失败）
- 降级告知（中度以上）
"""
import time
import hmac
import hashlib
import base64
import urllib.parse
import os

# 钉钉机器人配置（可被环境变量覆盖）
DINGTALK_WEBHOOK = os.getenv(
    "DINGTALK_WEBHOOK",
    "https://oapi.dingtalk.com/robot/send?access_token=5e56da383088c1b82a9f2c5b7c1be01c37720687313d9130ea03d8c3a6d21c6e",
)
DINGTALK_SECRET = os.getenv(
    "DINGTALK_SECRET",
    "SEC7e2ae6ea8a99fd33c016c4f5da37222311f1d953cc1da263f5198f38df9b6889",
)

# 平台访问地址（拼接 Goal 详情链接，让钉钉消息可点回平台）
PLATFORM_URL = os.getenv("PLATFORM_URL", "http://great.holla.cool")


def _signed_url():
    """生成带签名的 webhook URL"""
    if DINGTALK_SECRET and DINGTALK_SECRET.startswith("SEC"):
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{DINGTALK_SECRET}"
        hmac_code = hmac.new(
            DINGTALK_SECRET.encode(), string_to_sign.encode(), digestmod=hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return f"{DINGTALK_WEBHOOK}&timestamp={timestamp}&sign={sign}"
    return DINGTALK_WEBHOOK


def send_text(content: str) -> bool:
    """发送纯文本通知"""
    import requests
    try:
        resp = requests.post(
            _signed_url(),
            json={"msgtype": "text", "text": {"content": content}},
            timeout=10,
        )
        return resp.status_code == 200 and resp.json().get("errcode") == 0
    except Exception:
        return False


def send_markdown(title: str, text: str) -> bool:
    """发送 markdown 通知（支持链接/格式）"""
    import requests
    try:
        resp = requests.post(
            _signed_url(),
            json={"msgtype": "markdown", "markdown": {"title": title, "text": text}},
            timeout=10,
        )
        return resp.status_code == 200 and resp.json().get("errcode") == 0
    except Exception:
        return False


# ==================== Goal Runtime 语义化通知 ====================

def _goal_link(goal_id: str) -> str:
    return f"{PLATFORM_URL}/goal/{goal_id}"


def notify_approval_required(goal_id: str, goal_title: str, step_name: str, reason: str, risk: str = "high") -> bool:
    """需要人工审批"""
    text = (
        f"### ⚠️ 需要审批\n"
        f"**Goal**: {goal_title}\n\n"
        f"**步骤**: {step_name}\n\n"
        f"**风险**: {risk}\n\n"
        f"**原因**: {reason}\n\n"
        f"[前往处理]({_goal_link(goal_id)})"
    )
    return send_markdown(f"审批 - {goal_title}", text)


def notify_goal_deviation(goal_id: str, goal_title: str, source: str, alignment: int, detail: str) -> bool:
    """目标偏移提醒"""
    text = (
        f"### 🎯 目标偏移提醒\n"
        f"**Goal**: {goal_title}\n\n"
        f"**来源**: {source}\n\n"
        f"**对齐度**: {alignment}%\n\n"
        f"{detail}\n\n"
        f"[查看详情]({_goal_link(goal_id)})"
    )
    return send_markdown(f"偏移 - {goal_title}", text)


def notify_goal_completed(goal_id: str, goal_title: str, partial: bool = False, gaps: list = None) -> bool:
    """Goal 完成 / 部分完成"""
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
            f"所有验收点已绑定通过证据。\n\n"
            f"[查看详情]({_goal_link(goal_id)})"
        )
    return send_markdown(f"完成 - {goal_title}", text)


def notify_guard_alert(goal_id: str, goal_title: str, detail: str) -> bool:
    """守护态异常（持续模式下回归失败）"""
    text = (
        f"### 🛡️ 守护告警\n"
        f"**Goal**: {goal_title}\n\n"
        f"持续守护中检测到回归失败：\n\n"
        f"{detail}\n\n"
        f"[立即查看]({_goal_link(goal_id)})"
    )
    return send_markdown(f"守护告警 - {goal_title}", text)


def notify_degraded(goal_id: str, goal_title: str, step_name: str, reason: str, impact: str) -> bool:
    """降级告知（中度以上）"""
    text = (
        f"### 🟠 降级告知\n"
        f"**Goal**: {goal_title}\n\n"
        f"**步骤**: {step_name}\n\n"
        f"**降级原因**: {reason}\n\n"
        f"**影响**: {impact}\n\n"
        f"[查看详情]({_goal_link(goal_id)})"
    )
    return send_markdown(f"降级 - {goal_title}", text)
