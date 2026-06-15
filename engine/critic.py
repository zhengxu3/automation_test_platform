"""Critic — Goal 质检/复审角色（确定性判定，零 LLM）

职责：一轮执行验收后，判断该 complete / replan / partial。
为什么确定性：控制流（要不要再跑一轮）必须稳定可预测，不能让 LLM 拍脑袋导致空转或死循环。
LLM 只负责"生成不同的新 plan"（在 planner），不负责"要不要 replan"。

判定逻辑（第一性原理：只在"还能拿到证据"时才值得再跑一轮）：
- 全部验收 pass            → complete
- 有未达成验收，且其证据类型当前可产出(∈allowed_evidence)，且没超 replan 预算 → replan
- 连续两轮未达成集合完全一致 → blocked（防止同因震荡烧 token）
- 否则（剩余验收当前能力达不到 / 超预算）               → partial（诚实停在部分完成，不假装、不空转）
"""
import hashlib
import json


def _unmet_signature(unmet: list) -> dict:
    """未达标原因签名：同一批验收点、同一证据类型、同一判定反复失败即视作同因震荡。"""
    rows = []
    for a in sorted(unmet or [], key=lambda x: x.get("id", "")):
        rows.append({
            "id": a.get("id", ""),
            "evidence_type": a.get("evidence_type", ""),
            "verdict": a.get("verdict", ""),
            "reason": a.get("reason") or a.get("failure_reason") or a.get("blocked_reason") or "",
        })
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    return {"hash": hashlib.sha1(raw.encode("utf-8")).hexdigest(), "rows": rows}


def decide_after_verify(goal: dict) -> dict:
    """验收后决策。输入 goal 文档（含 acceptance/feasibility/replan_count/budget）。

    返回:
      {"decision": "complete|replan|partial|blocked",
       "unmet": [acceptance_id...],
       "achievable_unmet": [acceptance_id...],
       "reason": str}
    """
    acceptance = goal.get("acceptance", []) or []
    passed = [a for a in acceptance if a.get("verdict") == "pass"]
    unmet = [a for a in acceptance if a.get("verdict") != "pass"]
    from engine.contracts import VERIFICATION_EVIDENCE
    # 准备级(prepared)是终态资产（用例已生成/需求已拆解），无法靠重跑变 pass，不阻塞"完成"。
    # 但"只有准备级、无任何业务验证"不算完成（诚实停 partial）→ 完成需至少一个验证级 pass。
    blocking_unmet = [a for a in unmet if a.get("verdict") != "prepared"]
    has_verification_pass = any(
        a.get("verdict") == "pass" and a.get("evidence_type") in VERIFICATION_EVIDENCE
        for a in acceptance)

    # 全部通过 → 完成
    if acceptance and not unmet:
        return {"decision": "complete", "unmet": [], "achievable_unmet": [],
                "reason": f"全部 {len(passed)} 个验收点已绑定通过证据"}

    # 验证侧全部达标（剩余只是已就绪的准备级资产）→ 完成
    if acceptance and not blocking_unmet and has_verification_pass:
        return {"decision": "complete", "unmet": [], "achievable_unmet": [],
                "reason": f"验证级验收已全部通过（{len(passed)} pass），其余为已就绪准备级资产"}

    # 无验收点 → 无可证明，停在部分完成
    if not acceptance:
        return {"decision": "partial", "unmet": [], "achievable_unmet": [],
                "reason": "没有可验证的验收点"}

    # 真实可达天花板：优先 producible（active 智能体实际能产），回退 allowed（源理论上限）。
    # 用 producible 后，"源理论能产但没有手能干"的验收点不再触发 replan 空转，直接停 partial。
    feasibility = goal.get("feasibility", {})
    allowed = set(feasibility.get("producible_evidence_types") or
                  feasibility.get("allowed_evidence_types", []))
    # 只有"验证级 + 当前可产"的未达成验收才值得 replan 去拿真正的 pass。
    # 准备级(prepared，如 testcase_generated)已到能力天花板，无法靠重规划变 pass → 不空转。
    from engine.contracts import VERIFICATION_EVIDENCE
    # not_applicable = 本轮实证"不适用/无可分析"（如 branch_review 无 diff）。重规划同一能力
    # 只会再次 no_change，不会变 pass → 不算可达成，避免空转 replan。
    achievable_unmet = [a for a in unmet
                        if a.get("evidence_type") in allowed
                        and a.get("evidence_type") in VERIFICATION_EVIDENCE
                        and a.get("verdict") != "not_applicable"]

    replan_count = goal.get("replan_count", 0)
    max_replans = goal.get("budget", {}).get("max_replans", 3)
    sig = _unmet_signature(unmet)

    # 还有"够得着"的未达成验收 + 预算未尽 → 值得再换个 plan 攻一轮
    if achievable_unmet and replan_count < max_replans:
        last_sig = goal.get("last_replan_unmet_signature", "")
        if goal.get("auto_replan", True) and replan_count > 0 and last_sig and last_sig == sig["hash"]:
            return {
                "decision": "blocked",
                "unmet": [a["id"] for a in unmet],
                "achievable_unmet": [a["id"] for a in achievable_unmet],
                "unmet_signature": sig["hash"],
                "unmet_signature_rows": sig["rows"],
                "reason": ("连续两轮自动重规划的未达标集合与原因完全一致，"
                           "疑似真实 hard bug 或测试脚本/环境底层不兼容，已中断自动重跑，等待人工介入"),
            }
        return {
            "decision": "replan",
            "unmet": [a["id"] for a in unmet],
            "achievable_unmet": [a["id"] for a in achievable_unmet],
            "unmet_signature": sig["hash"],
            "unmet_signature_rows": sig["rows"],
            "reason": (f"{len(passed)}/{len(acceptance)} 通过；"
                       f"{len(achievable_unmet)} 个可达成验收未拿下，重规划第 {replan_count + 1} 轮攻克"),
        }

    # 否则诚实停在部分完成
    na = [a for a in unmet if a.get("verdict") == "not_applicable"]
    if replan_count >= max_replans:
        reason = f"已重规划 {replan_count} 轮（上限 {max_replans}），剩余验收仍未达成"
    elif na:
        reason = (f"{len(passed)}/{len(acceptance)} 通过；{len(na)} 个验收无可分析实证"
                  f"（not_applicable，如无代码变更），当前能力无法据此判 pass")
    else:
        reason = "剩余未达成验收的证据类型当前能力无法产出（需补充输入源/环境）"
    return {
        "decision": "partial",
        "unmet": [a["id"] for a in unmet],
        "achievable_unmet": [a["id"] for a in achievable_unmet],
        "unmet_signature": sig["hash"],
        "unmet_signature_rows": sig["rows"],
        "reason": reason,
    }
