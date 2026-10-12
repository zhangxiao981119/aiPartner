# -*- coding: utf-8 -*-
"""予安 · 版本注册表（§6.1 版本管理总入口）

聚合所有子系统的版本号，供上线核对与遥测。各版本常量仍定义在其子系统旁
（延续 POLICY_VERSION / PROMPT_VERSION 的范式），本文件只做聚合引用，不重复定义。

版本分类：
  - 数据 schema 版本：profile_schema（伴侣档案）/ memory_schema（长期记忆，已由 予安_MemoryStore 落地，按伴侣分桶持久化）
  - 行为 / 策略版本：state_machine / safety_rules（防溯源不了）
  - 契约 / 接口版本：output_schema / plugin_contract / model_config（防上下游不认）
  - 授权 / 文案版本：policy / prompt_template（沿用既有）
  - 配置 / 资源版本：tts_voice（声线模版增删）/ tts_voice_params（声线参数，已由 予安_VoiceTemplate 落地）
  - 发布版本：app（整体，聚合回滚用）
"""

from 予安_ConsentStore import POLICY_VERSION
from 予安_Prompt import PROMPT_VERSION, OUTPUT_SCHEMA_VERSION
from 予安_CompanionManager import PROFILE_SCHEMA_VERSION
from 予安_StateEngine import (STATE_VERSION, SAFETY_RULES_VERSION,
                             PLUGIN_CONTRACT_VERSION)
from 予安_LLM import MODEL_CONFIG_VERSION
from 予安_MemoryStore import MEMORY_SCHEMA_VERSION   # 长期记忆（已落地：予安_MemoryStore）
from 予安_VoiceTemplate import TTS_VOICE_VERSION    # 声线模版（已落地：予安_VoiceTemplate）

# 整体发布版本（语义版本）：聚合回滚 / 崩溃上报关联 / 遥测
APP_VERSION = "2026.10-1"

VERSION_REGISTRY = {
    "app": APP_VERSION,
    "policy": POLICY_VERSION,                   # 授权说明（ConsentStore）
    "prompt_template": PROMPT_VERSION,          # System Prompt 文案（Prompt）
    "output_schema": OUTPUT_SCHEMA_VERSION,     # LLM 返回 JSON 契约（Prompt）
    "profile_schema": PROFILE_SCHEMA_VERSION,   # 伴侣档案（CompanionManager）
    "memory_schema": MEMORY_SCHEMA_VERSION,     # 长期记忆（已落地：予安_MemoryStore，按伴侣分桶）
    "tts_voice": TTS_VOICE_VERSION,             # 声线模版增删（VoiceTemplate，set 键）
    "tts_voice_params": TTS_VOICE_VERSION,      # 声线参数（VoiceTemplate，review 键）
    "state_machine": STATE_VERSION,             # 状态机 / 行为（StateEngine）
    "safety_rules": SAFETY_RULES_VERSION,       # 危机词库（StateEngine）
    "plugin_contract": PLUGIN_CONTRACT_VERSION, # 插件契约（StateEngine）
    "model_config": MODEL_CONFIG_VERSION,       # 模型 / 调用配置（LLM）
}


def all_versions() -> dict:
    """当前所有子系统版本号的快照（可喂遥测 / 崩溃上报 / 上线核对）。"""
    return dict(VERSION_REGISTRY)


_LABELS = {
    "policy": "授权说明", "prompt_template": "Prompt 文案",
    "output_schema": "输出契约", "profile_schema": "伴侣档案",
    "memory_schema": "长期记忆", "state_machine": "状态机/行为",
    "safety_rules": "危机词库", "plugin_contract": "插件契约",
    "model_config": "模型配置", "tts_voice": "声线模版",
    "tts_voice_params": "声线参数",
}


def summary() -> str:
    """可读的版本总览，用于上线核对——打印即知这次带了哪套子版本。"""
    lines = [f"予安 v{VERSION_REGISTRY['app']} 版本总览"]
    for key in ("policy", "prompt_template", "output_schema", "profile_schema",
                "memory_schema", "tts_voice", "tts_voice_params", "state_machine",
                "safety_rules", "plugin_contract", "model_config"):
        lines.append(f"  {_LABELS[key]}：{VERSION_REGISTRY[key]}")
    return "\n".join(lines)
