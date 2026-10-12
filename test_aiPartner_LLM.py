# -*- coding: utf-8 -*-
"""aiPartner_LLM 测试：配置版本常量 + 请求携带配置版本头 + 结构化输出解析（§6.1 / §6）。"""

import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiPartner_LLM as llm_mod

LLMClient = llm_mod.LLMClient


def test_config_version_constant_and_attr():
    """模型/调用配置版本常量存在，且客户端实例携带该版本。"""
    assert llm_mod.MODEL_CONFIG_VERSION and isinstance(llm_mod.MODEL_CONFIG_VERSION, str)
    c = LLMClient(base_url="http://x", api_key="k", model="m")
    assert c.config_version == llm_mod.MODEL_CONFIG_VERSION


def test_request_carries_config_version_header(monkeypatch):
    """每次请求携带 X-Yuan-Config-Version 头，便于后端遥测与回溯。"""

    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"hi"}}]}'

    def fake_urlopen(req, timeout=0):
        captured["req"] = req
        return FakeResp()

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
    c = LLMClient(base_url="http://x", api_key="k", model="m")
    out = c.chat([{"role": "user", "content": "hi"}])
    assert out == "hi"
    # HTTP 头大小写不敏感：urllib 内部规约为小写键，比对时统一小写
    headers = {k.lower(): v for k, v in captured["req"].headers.items()}
    assert headers.get("x-yuan-config-version") == llm_mod.MODEL_CONFIG_VERSION


def test_disabled_client_returns_none(monkeypatch):
    """未配置 → 禁用，chat 返回 None（调用方走规则兜底，不触网）。"""
    monkeypatch.setattr(llm_mod.os, "environ", {})
    c = LLMClient()                       # 三项皆空
    assert c.enabled is False
    assert c.chat([{"role": "user", "content": "hi"}]) is None


# ────────────────────────── 结构化输出（§6 输出契约消费） ──────────────────────────

def test_json_response_mode_constant():
    assert llm_mod.JSON_RESPONSE_MODE == "json_object"


def test_parse_structured_reply_valid_json():
    raw = json.dumps({"reply_text": "在的", "emotion": "warm",
                      "tts_params": {"timbre": "甜暖女声"}})
    d = llm_mod.parse_structured_reply(raw)
    assert d is not None and d["reply_text"] == "在的"
    assert d["tts_params"] == {"timbre": "甜暖女声"}
    # 缺失的契约键补 None（结构稳定，下游不必判键在不在）
    assert d["mood"] is None and d["animation"] is None and d["action"] is None


def test_parse_structured_reply_plain_text_returns_none():
    assert llm_mod.parse_structured_reply("累了吧，先歇会儿。") is None
    assert llm_mod.parse_structured_reply("") is None
    assert llm_mod.parse_structured_reply(None) is None


def test_parse_structured_reply_json_without_reply_text_returns_none():
    assert llm_mod.parse_structured_reply('{"emotion":"warm"}') is None
    assert llm_mod.parse_structured_reply('{"reply_text":"   "}') is None


def test_parse_structured_reply_fenced_and_prose_wrapped():
    assert llm_mod.parse_structured_reply(
        '```json\n{"reply_text": "嗯"}\n```')["reply_text"] == "嗯"
    # 模型前言后语包裹 JSON → 截取花括号再解析
    assert llm_mod.parse_structured_reply(
        '好的，这是结果：{"reply_text": "在呢"} 完毕')["reply_text"] == "在呢"


def test_reply_contract_keys_match_output_block():
    """契约键一致性：REPLY_CONTRACT_KEYS 必须与 Prompt._OUTPUT_BLOCK 不脱钩。"""
    from aiPartner_version_guard import _parse_output_keys
    import aiPartner_Prompt as PR
    assert set(llm_mod.REPLY_CONTRACT_KEYS) == set(_parse_output_keys(PR._OUTPUT_BLOCK))


def test_chat_passes_response_format(monkeypatch):
    """传 response_format 时，请求体带 response_format.type；不传则不带（旧行为）。"""
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"{}"}}]}'

    def fake_urlopen(req, timeout=0):
        captured["body"] = req.data
        return FakeResp()

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
    c = LLMClient(base_url="http://x", api_key="k", model="m")
    c.chat([{"role": "user", "content": "hi"}], response_format="json_object")
    body = json.loads(captured["body"].decode("utf-8"))
    assert body["response_format"] == {"type": "json_object"}
    # 不传 response_format → 请求体不含该键（向后兼容）
    c.chat([{"role": "user", "content": "hi"}])
    body2 = json.loads(captured["body"].decode("utf-8"))
    assert "response_format" not in body2
