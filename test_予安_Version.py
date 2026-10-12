# -*- coding: utf-8 -*-
"""予安_Version 测试：版本注册表覆盖所有子系统 + 可读总览（§6.1 版本管理）。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import 予安_Version as vmod

EXPECTED_KEYS = {"app", "policy", "prompt_template", "output_schema",
                 "profile_schema", "memory_schema", "state_machine",
                 "safety_rules", "plugin_contract", "model_config"}


def test_registry_covers_all_subsystems():
    """注册表覆盖所有子系统，且每个版本号都是非空字符串（可喂遥测）。"""
    vs = vmod.all_versions()
    assert EXPECTED_KEYS <= set(vs.keys())
    assert all(isinstance(v, str) and v for v in vs.values())


def test_summary_readable_and_has_app_version():
    """可读总览包含整体发布版本与各子系统中文标签。"""
    s = vmod.summary()
    assert "予安 v" in s
    for label in ("授权说明", "Prompt 文案", "输出契约", "伴侣档案",
                  "长期记忆", "状态机/行为", "危机词库", "插件契约", "模型配置"):
        assert label in s


def test_memory_schema_reserved_for_pending_module():
    """记忆 schema 版本已预留（记忆模块尚未落盘），常量存在供后续接入。"""
    assert vmod.MEMORY_SCHEMA_VERSION and isinstance(vmod.MEMORY_SCHEMA_VERSION, str)
