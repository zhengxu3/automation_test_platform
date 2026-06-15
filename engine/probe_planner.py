"""Discovery Probe Round — 目标探查轮规划（确定性，零 LLM）

Goal 创建后的第一件事不是生成目标，而是：记忆体按输入类型选探查智能体，
先跑一轮拿到对输入的结构化理解（需求拆解 / 代码画像），再据此生成目标。

探查轮 ≠ 第一版 plan，是 discovering 阶段的前置 discovery。
这里只做确定性规则（按可用能力查表），不急着上 LLM。
"""

# 探查能力选择规则（按可用源 → 探查能力 capability_key）
# 有 doc → 拆需求；有 repo → 扫代码反推；都有 → 两者都跑
PROBE_RULES = [
    # (判定函数, 追加的探查能力)
    (lambda caps: "doc" in caps or "user_desc" in caps, "requirement_analysis"),
    (lambda caps: "repo" in caps, "code_scan"),
]


def select_probe_capabilities(profile: dict) -> list:
    """根据可行性画像选探查能力（capability_key 列表，确定性）。

    profile: source_profiler.profile_sources 的输出，含 available_capabilities。
    返回去重保序的 capability_key 列表；无可用输入时返回 []。
    """
    caps = set(profile.get("available_capabilities", []))
    probes = []
    for predicate, capability in PROBE_RULES:
        if predicate(caps) and capability not in probes:
            probes.append(capability)
    return probes


def needs_probe(profile: dict) -> bool:
    """是否需要探查轮（有任何可探查的输入）。"""
    return bool(select_probe_capabilities(profile))
