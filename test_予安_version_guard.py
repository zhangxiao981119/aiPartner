# -*- coding: utf-8 -*-
"""予安 · 版本升级护栏 测试（pytest）
====================================
覆盖：
  - 纯逻辑分类：无 bump 改内容=BREAKING / 删项=BREAKING / 加项=OK /
    无意义 bump=WARN / review 类变更=WARN
  - 真实变异：删危机词、删输出契约字段 → 护栏必须拦下（exit 1）；
    加词且升版本 → 向后兼容不拦（exit 0）
  - 集成：当前代码对自身比对无变更
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import 予安_StateEngine as SE
import 予安_Prompt as PR
import 予安_Version as V
import 予安_MemoryStore as MS
import 予安_VoiceTemplate as VT
import 予安_LLM as LLM
from 予安_version_guard import (GOVERNORS, Entry, classify, diff, run_check,
                                save_baseline, snapshot_current, SET_KEYS,
                                REVIEW_KEYS, RESERVED_KEYS)


# ────────────────────────── 快照完整性 ──────────────────────────

def test_snapshot_current_covers_all_subsystems():
    snap = snapshot_current()
    assert set(snap.keys()) == set(GOVERNORS.keys())
    for e in snap.values():
        assert isinstance(e.version, str) and e.version
    assert snap["app"].version == V.APP_VERSION
    # memory_schema 已落地：content 为记录类型集合，且已移出预留、纳入 set 键
    assert set(snap["memory_schema"].content) == set(MS.MEMORY_FIELDS)
    assert "memory_schema" in SET_KEYS
    assert "memory_schema" not in RESERVED_KEYS
    # tts_voice 已落地：content 为声线 ID 集合（set 键）；tts_voice_params 为哈希（review 键）
    assert set(snap["tts_voice"].content) == set(VT.all_ids())
    assert "tts_voice" in SET_KEYS
    assert "tts_voice" not in RESERVED_KEYS
    assert isinstance(snap["tts_voice_params"].content, str) and snap["tts_voice_params"].content
    assert "tts_voice_params" in REVIEW_KEYS


# ────────────────────────── 纯逻辑分类（直接构造 Entry） ──────────────────────────

def test_classify_review_change_without_bump_is_breaking():
    c = classify("prompt_template", Entry("1.0", "hA", ""), Entry("1.0", "hB", ""))
    assert c is not None and c.level == "BREAKING"


def test_classify_set_removal_without_bump_is_breaking():
    c = classify("safety_rules", Entry("1.0", ["a", "b", "c"], ""),
                 Entry("1.0", ["a", "b"], ""))
    assert c is not None and c.level == "BREAKING"
    assert "c" in c.removed


def test_classify_removal_despite_bump_still_breaking():
    c = classify("output_schema", Entry("1.0", ["a", "b"], ""),
                 Entry("2.0", ["a"], ""))              # 升了版本仍删项
    assert c is not None and c.level == "BREAKING"
    assert "b" in c.removed


def test_classify_compatible_addition_is_ok():
    c = classify("safety_rules", Entry("1.0", ["a"], ""),
                 Entry("2.0", ["a", "b"], ""))         # 只新增
    assert c is not None and c.level == "OK"
    assert "b" in c.added


def test_classify_version_bumped_no_content_change_is_warn():
    c = classify("policy", Entry("1.0", "h", ""), Entry("2.0", "h", ""))
    assert c is not None and c.level == "WARN"


def test_classify_review_change_with_bump_is_warn():
    c = classify("prompt_template", Entry("1.0", "hA", ""), Entry("2.0", "hB", ""))
    assert c is not None and c.level == "WARN"


# ────────────────────────── 集成：当前代码对自身一致 ──────────────────────────

def test_full_snapshot_diff_is_clean():
    snap = snapshot_current()
    assert diff(snap, snap) == []


def test_real_baseline_check_passes(tmp_path):
    manifest = str(tmp_path / "manifest.json")
    save_baseline(manifest, snapshot_current())
    assert run_check(manifest, strict=False) == 0


# ────────────────────────── 真实变异（牙齿） ──────────────────────────

def test_mutant_removed_safety_word_is_blocked(tmp_path, monkeypatch):
    # 删一个危机词却没升版本 → 必须拦下（exit 1）
    manifest = str(tmp_path / "manifest.json")
    save_baseline(manifest, snapshot_current())        # 基线含完整词库
    original = set(SE.CRISIS_WORDS)
    reduced = original - {"跳楼"}
    assert reduced != original
    monkeypatch.setattr(SE, "CRISIS_WORDS", reduced)
    assert run_check(manifest, strict=False) == 1      # 阻断


def test_mutant_output_schema_key_removed_is_blocked(tmp_path, monkeypatch):
    manifest = str(tmp_path / "manifest.json")
    save_baseline(manifest, snapshot_current())
    new_block = PR._OUTPUT_BLOCK.replace(", animation", "")   # 删一个契约字段
    monkeypatch.setattr(PR, "_OUTPUT_BLOCK", new_block)
    assert run_check(manifest, strict=False) == 1


def test_compatible_addition_with_bump_passes(tmp_path, monkeypatch):
    # 加危机词 + 同步升版本 → 向后兼容，不拦（exit 0）
    manifest = str(tmp_path / "manifest.json")
    save_baseline(manifest, snapshot_current())
    monkeypatch.setattr(SE, "CRISIS_WORDS", set(SE.CRISIS_WORDS) | {"自残"})
    monkeypatch.setattr(SE, "SAFETY_RULES_VERSION", "2099.99-9")
    assert run_check(manifest, strict=False) == 0


def test_strict_mode_fails_on_warn(tmp_path, monkeypatch):
    # --strict：review 类变更（升版本+改文案）=WARN → 视为失败
    manifest = str(tmp_path / "manifest.json")
    save_baseline(manifest, snapshot_current())
    monkeypatch.setattr(PR, "_REDLINE_BLOCK", PR._REDLINE_BLOCK + "（补充）")
    monkeypatch.setattr(PR, "PROMPT_VERSION", "2099.99-9")
    assert run_check(manifest, strict=True) == 1


# ────────────────────────── memory_schema 治理（牙齿） ──────────────────────────

def test_memory_schema_governed_as_set_key():
    """memory_schema 已从预留升级为 set 键：删记录类型=BREAKING。"""
    assert "memory_schema" in SET_KEYS
    assert "memory_schema" not in RESERVED_KEYS


def test_mutant_removed_memory_type_is_blocked(tmp_path, monkeypatch):
    """删一个记忆记录类型却没升版本 → 必须拦下（exit 1）。"""
    manifest = str(tmp_path / "manifest.json")
    save_baseline(manifest, snapshot_current())
    reduced = MS.MEMORY_FIELDS - {"fact"}
    assert reduced != MS.MEMORY_FIELDS
    monkeypatch.setattr(MS, "MEMORY_FIELDS", reduced)
    assert run_check(manifest, strict=False) == 1


def test_mutant_removed_memory_type_despite_bump_still_blocked(tmp_path, monkeypatch):
    """删记录类型 + 升版本 → 仍是破坏性（删即 BREAKING）。"""
    manifest = str(tmp_path / "manifest.json")
    save_baseline(manifest, snapshot_current())
    monkeypatch.setattr(MS, "MEMORY_FIELDS", MS.MEMORY_FIELDS - {"relationship"})
    monkeypatch.setattr(MS, "MEMORY_SCHEMA_VERSION", "2099.99-9")
    assert run_check(manifest, strict=False) == 1


def test_compatible_memory_type_addition_with_bump_passes(tmp_path, monkeypatch):
    """加记录类型 + 同步升版本 → 向后兼容，不拦（exit 0）。"""
    manifest = str(tmp_path / "manifest.json")
    save_baseline(manifest, snapshot_current())
    monkeypatch.setattr(MS, "MEMORY_FIELDS", MS.MEMORY_FIELDS | {"episode"})
    monkeypatch.setattr(MS, "MEMORY_SCHEMA_VERSION", "2099.99-9")
    assert run_check(manifest, strict=False) == 0


# ────────────────────────── tts_voice 治理（牙齿） ──────────────────────────

def test_tts_voice_governed_as_set_and_review_keys():
    assert "tts_voice" in SET_KEYS
    assert "tts_voice" not in RESERVED_KEYS
    assert "tts_voice_params" in REVIEW_KEYS


def test_mutant_removed_voice_is_blocked(tmp_path, monkeypatch):
    """删一条声线却没升版本 → 必须拦下（exit 1）。"""
    manifest = str(tmp_path / "manifest.json")
    save_baseline(manifest, snapshot_current())
    reduced = [v for v in VT.VOICE_TEMPLATES if v["id"] != "xiaoyu"]
    assert len(reduced) == len(VT.VOICE_TEMPLATES) - 1
    monkeypatch.setattr(VT, "VOICE_TEMPLATES", reduced)
    assert run_check(manifest, strict=False) == 1


def test_mutant_removed_voice_despite_bump_still_blocked(tmp_path, monkeypatch):
    """删声线 + 升版本 → 仍是破坏性（删即 BREAKING）。"""
    manifest = str(tmp_path / "manifest.json")
    save_baseline(manifest, snapshot_current())
    reduced = [v for v in VT.VOICE_TEMPLATES if v["id"] != "langxing"]
    monkeypatch.setattr(VT, "VOICE_TEMPLATES", reduced)
    monkeypatch.setattr(VT, "TTS_VOICE_VERSION", "2099.99-9")
    assert run_check(manifest, strict=False) == 1


def test_compatible_voice_addition_with_bump_passes(tmp_path, monkeypatch):
    """加声线 + 同步升版本 → 向后兼容，不拦（exit 0）。"""
    manifest = str(tmp_path / "manifest.json")
    save_baseline(manifest, snapshot_current())
    new_voice = {"id": "test_voice", "name": "测试·声线", "gender": "female",
                 "timbre": "测试女声", "pitch": 0.0, "speed": 1.0,
                 "preview_text": "这是测试声线。", "voice_id": "reserved:test_voice"}
    monkeypatch.setattr(VT, "VOICE_TEMPLATES", VT.VOICE_TEMPLATES + [new_voice])
    monkeypatch.setattr(VT, "TTS_VOICE_VERSION", "2099.99-9")
    assert run_check(manifest, strict=False) == 0


def test_voice_param_change_with_bump_is_warn(tmp_path, monkeypatch):
    """改声线参数（pitch）+ 升版本 → review 键变更，WARN（严格模式拦下）。"""
    manifest = str(tmp_path / "manifest.json")
    save_baseline(manifest, snapshot_current())
    mutated = [dict(v, pitch=v["pitch"] + 0.5) if v["id"] == "chenzhou" else v
               for v in VT.VOICE_TEMPLATES]
    monkeypatch.setattr(VT, "VOICE_TEMPLATES", mutated)
    monkeypatch.setattr(VT, "TTS_VOICE_VERSION", "2099.99-9")
    assert run_check(manifest, strict=False) == 0   # WARN 非阻断
    assert run_check(manifest, strict=True) == 1     # 严格模式拦下


def test_mutant_alias_change_without_bump_is_blocked(tmp_path, monkeypatch):
    """改路由别名却没升版本 → tts_voice_params 内容变，BREAKING（exit 1）。"""
    manifest = str(tmp_path / "manifest.json")
    save_baseline(manifest, snapshot_current())
    monkeypatch.setattr(VT, "TIMBRE_ALIASES",
                        dict(VT.TIMBRE_ALIASES, **{"新的别名": "知性女声"}))
    assert run_check(manifest, strict=False) == 1


def test_alias_change_with_bump_is_warn(tmp_path, monkeypatch):
    """改路由别名 + 升版本 → review 键变更，WARN（严格模式拦下）。"""
    manifest = str(tmp_path / "manifest.json")
    save_baseline(manifest, snapshot_current())
    monkeypatch.setattr(VT, "TIMBRE_ALIASES",
                        dict(VT.TIMBRE_ALIASES, **{"新的别名": "知性女声"}))
    monkeypatch.setattr(VT, "TTS_VOICE_VERSION", "2099.99-9")
    assert run_check(manifest, strict=False) == 0
    assert run_check(manifest, strict=True) == 1


# ────────────────────────── model_config 治理（json 响应模式） ──────────────────────────

def test_mutant_json_mode_change_without_bump_is_blocked(tmp_path, monkeypatch):
    """改 json 响应模式却没升版本 → model_config 内容变，BREAKING（exit 1）。"""
    manifest = str(tmp_path / "manifest.json")
    save_baseline(manifest, snapshot_current())
    monkeypatch.setattr(LLM, "JSON_RESPONSE_MODE", "text")
    assert run_check(manifest, strict=False) == 1

