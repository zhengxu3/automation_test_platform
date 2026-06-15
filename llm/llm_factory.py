#!/usr/bin/env python3.9
# -*- coding: utf-8 -*-
"""
@Author: Zheng.Xu
@Date: 2026/5/15
@Python：python 3.9
@Description: LLM 工厂模式 - 抽象基类 + Provider 实现 + 统一接口
"""
import os
from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """大模型 Provider 抽象基类 - 统一所有大模型的工作模式"""

    def __init__(self, model_id: str, model_name: str):
        self.model_id = model_id
        self.model_name = model_name

    @property
    def provider(self):
        return self.__class__.__name__

    @abstractmethod
    def generate(self, system_prompt: str, user_prompt: str) -> dict:
        """同步调用，返回 {"text": str, "usage": {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}}"""
        pass

    @abstractmethod
    def stream_generate(self, system_prompt: str, user_prompt: str):
        """流式调用，返回生成器"""
        pass

    def info(self) -> dict:
        """返回模型信息（供前端展示）"""
        return {"model_id": self.model_id, "model_name": self.model_name, "provider": self.provider}


class GeminiProvider(LLMProvider):
    """Google Gemini 系列"""

    MODEL_MAP = {
        "gemini_flash": "gemini-2.5-flash",
        "gemini_pro": "gemini-2.5-pro",
    }

    def __init__(self, model_id: str, model_name: str, api_key: str = ""):
        super().__init__(model_id, model_name)
        from google import genai
        key = api_key or os.getenv("GEMINI_API_KEY", "")
        self._client = genai.Client(api_key=key, http_options={'api_version': 'v1beta'})
        self._model = self.MODEL_MAP.get(model_id, "gemini-2.5-flash")

    def _build_config(self, system_prompt):
        from google.genai import types
        return types.GenerateContentConfig(
            system_instruction=system_prompt or "你是一个专业的AI助手。",
            temperature=0.3,
            safety_settings=[
                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=types.HarmBlockThreshold.BLOCK_NONE),
                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
            ]
        )

    def generate(self, system_prompt: str, user_prompt: str) -> dict:
        from google.genai import types
        config = self._build_config(system_prompt)
        response = self._client.models.generate_content(
            model=self._model,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])],
            config=config
        )
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if hasattr(response, 'usage_metadata') and response.usage_metadata:
            um = response.usage_metadata
            usage["prompt_tokens"] = getattr(um, 'prompt_token_count', 0) or 0
            usage["completion_tokens"] = getattr(um, 'candidates_token_count', 0) or 0
            usage["total_tokens"] = getattr(um, 'total_token_count', 0) or 0
        text = response.text or ""
        if usage["total_tokens"] == 0:
            usage["prompt_tokens"] = len(user_prompt) // 4
            usage["completion_tokens"] = len(text) // 4
            usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        return {"text": text, "usage": usage}

    def stream_generate(self, system_prompt: str, user_prompt: str):
        """流式生成（同步生成器）"""
        from google.genai import types
        config = self._build_config(system_prompt)
        response_stream = self._client.models.generate_content_stream(
            model=self._model,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])],
            config=config
        )
        for chunk in response_stream:
            if chunk.text:
                yield chunk.text


class KiroProvider(LLMProvider):
    """Kiro CLI 调用（通过命令行 headless 模式）"""

    MODEL_MAP = {
        "kiro_default": "kiro_default",
        "kiro_planner": "kiro_planner",
    }

    def __init__(self, model_id: str, model_name: str):
        super().__init__(model_id, model_name)
        self._agent = self.MODEL_MAP.get(model_id, "kiro_default")

    def generate(self, system_prompt: str, user_prompt: str) -> dict:
        """通过 kiro-cli headless 模式调用"""
        import subprocess
        prompt = f"{system_prompt}\n\n{user_prompt}" if system_prompt else user_prompt
        try:
            result = subprocess.run(
                ["kiro-cli", "chat", "--no-interactive", "--trust-all-tools", "--agent", self._agent, prompt],
                capture_output=True, text=True, timeout=300
            )
            text = result.stdout.strip() if result.returncode == 0 else f"Kiro 调用失败: {result.stderr}"
        except Exception as e:
            text = f"Kiro 调用异常: {str(e)}"
        usage = {"prompt_tokens": len(prompt) // 4, "completion_tokens": len(text) // 4, "total_tokens": (len(prompt) + len(text)) // 4}
        return {"text": text, "usage": usage}

    def stream_generate(self, system_prompt: str, user_prompt: str):
        """Kiro 不支持流式，直接返回完整结果"""
        yield self.generate(system_prompt, user_prompt)


class DeepSeekProvider(LLMProvider):
    """DeepSeek（OpenAI 兼容接口）"""

    def __init__(self, model_id: str, model_name: str, api_key: str = "", base_url: str = ""):
        super().__init__(model_id, model_name)
        from openai import OpenAI
        key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
        self._client = OpenAI(api_key=key, base_url=base_url or "https://api.deepseek.com/v1")

    def generate(self, system_prompt: str, user_prompt: str) -> dict:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})
        response = self._client.chat.completions.create(model="deepseek-chat", messages=messages, temperature=0.3)
        text = response.choices[0].message.content or ""
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if response.usage:
            usage = {"prompt_tokens": response.usage.prompt_tokens, "completion_tokens": response.usage.completion_tokens, "total_tokens": response.usage.total_tokens}
        return {"text": text, "usage": usage}

    def stream_generate(self, system_prompt: str, user_prompt: str):
        yield self.generate(system_prompt, user_prompt)


# ==================== 工厂 ====================

class LLMFactory:
    """
    大模型工厂 - fallback 链 + 从 config.yaml 读取 key
    """

    REGISTERED_MODELS = {
        "gemini_flash": {"name": "Gemini 2.5 Flash", "provider": "gemini", "description": "快速响应，适合日常分析"},
        "gemini_pro": {"name": "Gemini 2.5 Pro", "provider": "gemini", "description": "深度推理，适合复杂代码分析"},
        "gemini-2.5": {"name": "Gemini 2.5 Flash", "provider": "gemini", "description": "Gemini 2.5 别名"},
        "deepseek": {"name": "DeepSeek Chat", "provider": "deepseek", "description": "低成本备用"},
        "kiro_default": {"name": "Kiro Default", "provider": "kiro", "description": "Kiro CLI 默认智能体"},
        "kiro_planner": {"name": "Kiro Planner", "provider": "kiro", "description": "Kiro 规划智能体"},
    }

    PROVIDER_MAP = {
        "gemini": GeminiProvider,
        "kiro": KiroProvider,
        "deepseek": DeepSeekProvider,
        "openai_compatible": DeepSeekProvider,
    }

    _config_cache = None

    @classmethod
    def _load_llm_config(cls):
        """从 config.yaml 读取 LLM 配置"""
        if cls._config_cache:
            return cls._config_cache
        import yaml
        # 新项目：从项目根目录读 config.local.yaml 或 config.yaml
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        local_path = os.path.join(base, "config.local.yaml")
        default_path = os.path.join(base, "config.yaml")
        config_path = local_path if os.path.exists(local_path) else default_path
        try:
            with open(config_path, "r") as f:
                cfg = yaml.safe_load(f)
            cls._config_cache = cfg.get("llm", {})
        except Exception:
            cls._config_cache = {}
        return cls._config_cache

    @staticmethod
    def get_model_list():
        """返回所有已注册模型"""
        return [{"model_id": k, **v} for k, v in LLMFactory.REGISTERED_MODELS.items()]

    @classmethod
    def create(cls, model_id: str) -> LLMProvider:
        """根据 model_id 生产 Provider 实例（从 config 读 key）"""
        reg = cls.REGISTERED_MODELS.get(model_id)
        if not reg:
            reg = cls.REGISTERED_MODELS["gemini_flash"]
            model_id = "gemini_flash"
        provider_name = reg["provider"]
        provider_cls = cls.PROVIDER_MAP.get(provider_name, GeminiProvider)

        # 从 config.yaml 读取 key
        llm_cfg = cls._load_llm_config()
        model_cfg = llm_cfg.get("models", {}).get(model_id, {})
        api_key = model_cfg.get("api_key", "")
        base_url = model_cfg.get("base_url", "")

        if provider_name == "deepseek" or provider_name == "openai_compatible":
            return provider_cls(model_id=model_id, model_name=reg["name"], api_key=api_key, base_url=base_url)
        elif provider_name == "gemini":
            return provider_cls(model_id=model_id, model_name=reg["name"], api_key=api_key)
        else:
            return provider_cls(model_id=model_id, model_name=reg["name"])

    @classmethod
    def generate(cls, model_id: str, system_prompt: str, user_prompt: str) -> dict:
        """带 fallback 链的生成：优先用指定模型，失败则按链顺序降级"""
        llm_cfg = cls._load_llm_config()
        fallback_chain = llm_cfg.get("fallback_chain", ["gemini_flash", "deepseek"])

        # 把指定模型放第一位
        chain = [model_id] + [m for m in fallback_chain if m != model_id]

        last_error = None
        for mid in chain:
            if mid not in cls.REGISTERED_MODELS:
                continue
            # 检查该模型是否有 key 配置
            model_cfg = llm_cfg.get("models", {}).get(mid, {})
            if not model_cfg.get("api_key"):
                continue
            try:
                provider = cls.create(mid)
                result = provider.generate(system_prompt, user_prompt)
                if result and result.get("text"):
                    result["actual_model"] = mid
                    result["requested_model"] = model_id
                    return result
            except Exception as e:
                last_error = e
                continue

        # 全部失败
        raise Exception(f"所有模型调用失败，最后错误: {last_error}")

    @classmethod
    def record_usage(cls, model_id, usage, caller="unknown", req_id=""):
        """记录一次完整的 LLM 调用消耗"""
        if not usage or not usage.get("total_tokens"):
            return
        try:
            import time
            from common.db import get_collection
            get_collection("ai_llm_usage").insert_one({
                "model_id": model_id,
                "caller": caller,
                "req_id": req_id,
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "created_at": int(time.time()),
            })
        except Exception:
            pass

    @staticmethod
    def register_model(model_id: str, name: str, provider: str, description: str = ""):
        """动态注册新模型"""
        LLMFactory.REGISTERED_MODELS[model_id] = {"name": name, "provider": provider, "description": description}
