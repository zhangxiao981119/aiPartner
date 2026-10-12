# -*- coding: utf-8 -*-
"""予安 · LLM 客户端（§16.3 生产版 · OpenAI 兼容协议）
====================================================

用当前主流开源大模型（Qwen / DeepSeek / GLM / Kimi 等均开源权重、
其官方 API 或推理服务商均提供 OpenAI 兼容的 /chat/completions 接口），
以标准 API 调用形式接入，本客户端零三方依赖（stdlib urllib）。

配置（环境变量，未配置则客户端禁用，调用方走规则兜底——绝不阻塞陪伴）：
    YUAN_LLM_BASE_URL   如 https://api.deepseek.com/v1 或
                        https://dashscope.aliyuncs.com/compatible-mode/v1（Qwen）
                        https://open.bigmodel.cn/api/paas/v4（GLM）
    YUAN_LLM_API_KEY    对应平台的 API Key
    YUAN_LLM_MODEL      如 deepseek-chat / qwen-plus / glm-4-flash

设计原则（对齐 §16.5「允许不完美」与 §16.1「数据不出端」的边界取舍）：
- 只发必要上下文：转述/分类请求只带话题标题与用户回复，不带任何身份信息；
- 失败即降级：未配置 / 超时 / 限流 / 网络错误 → 返回 None，调用方走规则兜底；
- 可注入：测试传 fake client，不触网。

自测：python 予安_LLM.py（仅打印配置状态，不真实调用）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request


# 模型 / 调用配置版本（§6.1 版本管理）：换模型 / 改 temperature / 改 few-shot / 改输出模式 → 升版本；
# 每次请求携带 X-Yuan-Config-Version 头，便于后端遥测与回溯「当时用的哪套配置」。
# 2026.10-1 首版；2026.10-2 增结构化输出（json 响应模式 + parse_structured_reply）。
MODEL_CONFIG_VERSION = "2026.10-2"

# 结构化输出：OpenAI 兼容的 response_format.type。让模型返回可解析 JSON（§6 输出契约），
# Orchestrator 据此抽出 reply_text 与 tts_params（再由 予安_VoiceTemplate 路由到 voice_id）。
JSON_RESPONSE_MODE = "json_object"

# 输出契约键（对齐 予安_Prompt._OUTPUT_BLOCK）：解析时只认这些键、缺失补 None。
# 有「契约键一致性」测试钉死它与 Prompt 的 _OUTPUT_BLOCK 不脱钩。
REPLY_CONTRACT_KEYS = ("reply_text", "emotion", "mood", "bond",
                       "tts_params", "animation", "action")


def parse_structured_reply(raw: str | None) -> dict | None:
    """把 LLM 原始返回解析成输出契约 dict（fail-safe）。

    - 支持纯 JSON 与 ```json ... ``` / ``` ... ``` 围栏；
    - 必须含非空字符串 reply_text，否则返回 None（调用方按纯文本兜底）；
    - 其余契约键缺失一律补 None（结构稳定，下游不必判键在不在）。
    """
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    # 去掉 markdown 代码围栏
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    obj = None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError, json.JSONDecodeError):
        # 退一步：截取首个 { 到末个 } 之间再试（容忍模型前言后语）
        i, j = text.find("{"), text.rfind("}")
        if i != -1 and j > i:
            try:
                obj = json.loads(text[i:j + 1])
            except (ValueError, TypeError, json.JSONDecodeError):
                obj = None
    if not isinstance(obj, dict):
        return None
    reply = obj.get("reply_text")
    if not isinstance(reply, str) or not reply.strip():
        return None
    return {k: obj.get(k) for k in REPLY_CONTRACT_KEYS}


class LLMClient:
    """OpenAI 兼容 /chat/completions 客户端（同步、带超时与有限重试）。"""

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: float = 8.0, retries: int = 1):
        env = os.environ.get
        self.base_url = (base_url or env("YUAN_LLM_BASE_URL") or "").rstrip("/")
        self.api_key = api_key or env("YUAN_LLM_API_KEY") or ""
        self.model = model or env("YUAN_LLM_MODEL") or ""
        self.timeout = timeout
        self.retries = retries
        self.config_version = MODEL_CONFIG_VERSION

    @property
    def enabled(self) -> bool:
        """三项配置齐全才可用；否则调用方直接走规则兜底。"""
        return bool(self.base_url and self.api_key and self.model)

    def chat(self, messages: list[dict], temperature: float = 0.7,
             response_format: str | None = None) -> str | None:
        """对话补全。任何失败返回 None（含未配置）——失败即降级。

        response_format: 可选，传 JSON_RESPONSE_MODE 时要求模型返回 JSON 对象
        （OpenAI 兼容 response_format.type）；默认 None 时不带该字段，行为与旧版一致。
        """
        if not self.enabled:
            return None
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format:
            body["response_format"] = {"type": response_format}
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}",
                     "X-Yuan-Config-Version": self.config_version})
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                content = data["choices"][0]["message"]["content"]
                return content.strip() if content else None
            except (urllib.error.URLError, TimeoutError, OSError,
                    KeyError, IndexError, ValueError, json.JSONDecodeError):
                if attempt >= self.retries:
                    return None
                time.sleep(0.5 * (attempt + 1))   # 线性退避，有限重试
        return None


# ────────────────────────── 自测 ──────────────────────────

if __name__ == "__main__":
    c = LLMClient()
    print("配置状态：", "已启用" if c.enabled else "未配置（调用方将走规则兜底）")
    if c.enabled:
        print("  base_url:", c.base_url, "| model:", c.model, "| key: ***")
