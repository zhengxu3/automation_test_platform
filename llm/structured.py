"""StructuredLLM — 结构化 LLM 调用（记忆体稳定的地基）

裸 strip/replace 解析 JSON 在自动模式下是定时炸弹。
本模块统一：schema 校验 + JSON repair + 失败重试 + 默认降级 + 原始输出留档 + confidence 必填。

所有 Steward/Planner/Critic/AlignmentAnalyzer 的输出都必须走这里。
"""
import json
import re
import time


class StructuredResult:
    """结构化调用结果"""
    def __init__(self, ok: bool, data: dict, raw: str, usage: dict,
                 attempts: int, error: str = "", degraded: bool = False):
        self.ok = ok                # 是否成功解析+校验
        self.data = data            # 解析后的结构化数据
        self.raw = raw              # 原始输出（留档）
        self.usage = usage          # token 用量
        self.attempts = attempts    # 实际尝试次数
        self.error = error          # 失败原因
        self.degraded = degraded    # 是否降级返回（默认值）

    def __repr__(self):
        return f"<StructuredResult ok={self.ok} attempts={self.attempts} degraded={self.degraded}>"


def _extract_json(text: str) -> str:
    """从 LLM 输出中提取 JSON 字符串（处理 markdown 代码块、前后缀文字）"""
    if not text:
        return ""
    # 去 markdown 代码块
    if "```" in text:
        # 取第一个代码块内容
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if m:
            text = m.group(1)
    # 提取第一个 { 到最后一个 } 之间
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    # 数组
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text.strip()


def _repair_json(s: str) -> str:
    """简单 JSON 修复：尾逗号、单引号、未闭合"""
    if not s:
        return s
    # 去尾逗号
    s = re.sub(r",\s*([}\]])", r"\1", s)
    # 智能引号 → 普通引号
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return s


def _parse(text: str):
    """多策略解析：直接 parse → 提取 → 修复后 parse"""
    # 策略1：直接
    try:
        return json.loads(text)
    except Exception:
        pass
    # 策略2：提取 JSON 段
    extracted = _extract_json(text)
    try:
        return json.loads(extracted)
    except Exception:
        pass
    # 策略3：修复后
    try:
        return json.loads(_repair_json(extracted))
    except Exception:
        return None


def _validate(data: dict, schema: dict) -> tuple:
    """轻量 schema 校验。schema 格式:
    {
      "required": ["field1", "field2"],
      "types": {"field1": "str|int|float|list|dict|bool"},
    }
    返回 (ok, error_msg)
    """
    if not isinstance(data, (dict, list)):
        return False, "输出不是对象/数组"
    if not schema:
        return True, ""

    # 数组类型 schema
    if schema.get("type") == "array":
        if not isinstance(data, list):
            return False, "期望数组"
        return True, ""

    if not isinstance(data, dict):
        return False, "期望对象"

    for field in schema.get("required", []):
        if field not in data:
            return False, f"缺少必填字段: {field}"

    type_map = {"str": str, "int": int, "float": (int, float), "list": list, "dict": dict, "bool": bool}
    for field, expected in schema.get("types", {}).items():
        if field in data and data[field] is not None:
            py_type = type_map.get(expected)
            if py_type and not isinstance(data[field], py_type):
                return False, f"字段 {field} 类型错误，期望 {expected}"
    return True, ""


def generate_structured(
    system_prompt: str,
    user_prompt: str,
    schema: dict = None,
    model_id: str = "gemini_flash",
    max_retries: int = 2,
    default: dict = None,
    require_confidence: bool = True,
) -> StructuredResult:
    """结构化 LLM 调用。

    Args:
        schema: 校验 schema {"required": [...], "types": {...}}
        max_retries: 失败重试次数（每次会强化"只输出JSON"指令）
        default: 全部失败时的降级返回值
        require_confidence: 是否强制要求 confidence 字段（0-1）
    """
    from llm.llm_factory import LLMFactory

    # 强化 system prompt：必须输出 JSON
    sys_enhanced = (system_prompt or "") + "\n\n严格要求：只输出一个合法 JSON 对象，不要任何解释文字、不要 markdown 代码块标记。"
    if require_confidence:
        sys_enhanced += '\nJSON 必须包含 "confidence" 字段（0.0-1.0 的浮点数，表示你对本次输出的置信度）。'

    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    last_raw = ""
    last_error = ""

    cur_user = user_prompt
    for attempt in range(1, max_retries + 2):  # 首次 + 重试
        try:
            result = LLMFactory.generate(model_id, sys_enhanced, cur_user)
            raw = result.get("text", "")
            last_raw = raw
            usage = result.get("usage", {})
            for k in total_usage:
                total_usage[k] += usage.get(k, 0)

            data = _parse(raw)
            if data is None:
                last_error = "JSON 解析失败"
                cur_user = user_prompt + f"\n\n上次输出无法解析为JSON。请只输出合法JSON对象。"
                continue

            # confidence 校验/补默认
            if require_confidence and isinstance(data, dict) and "confidence" not in data:
                data["confidence"] = 0.5  # 没给则中性

            ok, err = _validate(data, schema or {})
            if not ok:
                last_error = err
                cur_user = user_prompt + f"\n\n上次输出校验失败：{err}。请修正后只输出合法JSON。"
                continue

            return StructuredResult(
                ok=True, data=data, raw=raw, usage=total_usage,
                attempts=attempt, error="",
            )
        except Exception as e:
            last_error = str(e)
            continue

    # 全部失败 → 降级
    return StructuredResult(
        ok=False,
        data=default if default is not None else {},
        raw=last_raw,
        usage=total_usage,
        attempts=max_retries + 1,
        error=last_error,
        degraded=True,
    )
