# -*- coding: utf-8 -*-
"""予安_CompanionManager 测试：创建/切换/删除 + 10 轮属性冻结 + 测试模式审计。"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import 予安_CompanionManager as cm

CLOCK = [1000.0]


def make(tmp_path, template=None, profile=None):
    CLOCK[0] += 1.0
    m = cm.CompanionManager(path=os.path.join(tmp_path, "companions.json"),
                            clock=lambda: CLOCK[0])
    if template or profile:
        m.create(profile or {}, template=template)
    return m


# ── 创建 ──

def test_create_from_template_becomes_active_when_empty(tmp_path):
    """空档案创建 → 模版字段齐备，自动成为在场伴侣。"""
    m = make(tmp_path)
    p = m.create(template="知心姐姐")
    assert p["id"] == "c1" and p["turns"] == 0
    a = m.active()
    assert a["name"] == "知心姐姐" or a["gender"] == "女"
    assert a["tags"] == ["温柔", "理性"] and a["voice"] == "甜暖·小雨"
    assert a["id"] == p["id"]                       # 空档案 → 新伴侣自动在场


def test_create_does_not_steal_active(tmp_path):
    """已有在场伴侣时新增 → 不抢在场位，切换才换人。"""
    m = make(tmp_path, template="知心姐姐")
    p2 = m.create({"name": "林晚"})
    assert m.active()["id"] != p2["id"]             # 新增不自动切换
    m.switch(p2["id"])
    assert m.active()["name"] == "林晚"


def test_create_rejects_non_editable_fields(tmp_path):
    """档案属性白名单之外的字段拒收（含 turns/id 等内部字段）。"""
    m = make(tmp_path)
    for bad in ({"turns": 999}, {"id": "hacked"}, {"bond": 0.9}):
        with pytest.raises(ValueError):
            m.create(bad)


# ── 冻结规则（核心） ──

def test_edit_allowed_up_to_ten_turns(tmp_path):
    """对话 ≤ 10 轮仍可修改属性；count_turn 累积并持久化。"""
    m = make(tmp_path, template="元气少年")
    for _ in range(cm.EDIT_LOCK_TURNS):
        m.count_turn()
    assert m.status()["turns"] == cm.EDIT_LOCK_TURNS
    assert m.status()["locked"] is False            # 恰满 10 轮：还没「超过」
    p = m.update(m.active()["id"], {"catchphrase": "换个口头禅"})
    assert p["catchphrase"] == "换个口头禅"


def test_edit_frozen_after_ten_turns(tmp_path):
    """对话超过 10 轮 → 任何属性修改被拒，档案原样不动。"""
    m = make(tmp_path, template="元气少年")
    for _ in range(cm.EDIT_LOCK_TURNS + 1):         # 超过 10 轮
        m.count_turn()
    before = m.active()
    with pytest.raises(cm.CompanionLockedError):
        m.update(before["id"], {"tags": ["高冷"], "name": "陌生人"})
    after = m.active()
    assert after["tags"] == before["tags"] and after["name"] == before["name"]
    assert m.status()["locked"] is True


def test_test_mode_bypasses_lock_and_audits(tmp_path):
    """测试模式放行冻结档案，且留审计记录（生产排查有据可查）。"""
    m = make(tmp_path, template="元气少年")
    for _ in range(cm.EDIT_LOCK_TURNS + 1):
        m.count_turn()
    p = m.update(m.active()["id"], {"age": 21}, test_mode=True)
    assert p["age"] == 21
    audit = m._state["audit"]
    assert len(audit) == 1 and audit[0]["changes"] == {"age": 21}
    assert audit[0]["cid"] == p["id"]
    # 非测试模式照旧拒绝：放行只发生在显式 test_mode
    with pytest.raises(cm.CompanionLockedError):
        m.update(p["id"], {"age": 30})


# ── 切换 / 删除 ──

def test_switch_unknown_id(tmp_path):
    m = make(tmp_path, template="知心姐姐")
    with pytest.raises(ValueError):
        m.switch("nope")


def test_delete_removes_and_clears_active(tmp_path):
    """删除在场伴侣 → 在场位清空；删除他人 → 在场不变；不存在 → 报错。"""
    m = make(tmp_path, template="知心姐姐")
    other = m.create({"name": "林晚"})
    m.delete(other["id"])
    assert m.active()["name"] == "予安"              # 删别人不影响在场
    with pytest.raises(ValueError):
        m.switch(other["id"])
    active_id = m.active()["id"]
    m.delete(active_id)
    assert m.active() is None and m.list_all() == []


# ── 持久化与 fail-safe ──

def test_persistence_across_restart(tmp_path):
    """重启恢复：伴侣名单、轮数、在场位都在——冻结状态跟着档案走。"""
    m = make(tmp_path, template="知心姐姐")
    other = m.create({"name": "林晚"})
    m.switch(other["id"])
    for _ in range(cm.EDIT_LOCK_TURNS + 1):
        m.count_turn()
    m2 = cm.CompanionManager(path=m.path, clock=lambda: CLOCK[0] + 1)
    assert m2.active()["name"] == "林晚"
    assert m2.status()["turns"] == cm.EDIT_LOCK_TURNS + 1
    assert m2.status()["locked"] is True
    with pytest.raises(cm.CompanionLockedError):    # 重启后冻结依然生效
        m2.update(other["id"], {"name": "X"})


def test_corrupted_file_failsafe_empty(tmp_path):
    """文件损坏 → fail-safe 空档案，绝不虚构一个伴侣。"""
    path = os.path.join(tmp_path, "companions.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{corrupted!")
    m = cm.CompanionManager(path=path, clock=lambda: CLOCK[0] + 1)
    assert m.list_all() == [] and m.active() is None
    # 且能从空档案继续正常工作
    p = m.create({"name": "予安"})
    assert m.active()["id"] == p["id"]


# ── 档案 schema 版本（§6.1 版本管理） ──

def test_profile_schema_version_persisted(tmp_path):
    """新档案落盘带 schema_version；重启后 status 仍暴露当前版本。"""
    m = make(tmp_path, template="知心姐姐")
    assert m.status()["schema_version"] == cm.PROFILE_SCHEMA_VERSION
    with open(m.path, "r", encoding="utf-8") as f:        # 落盘文件确实带版本
        assert json.load(f)["schema_version"] == cm.PROFILE_SCHEMA_VERSION
    m2 = cm.CompanionManager(path=m.path, clock=lambda: CLOCK[0] + 1)
    assert m2.status()["schema_version"] == cm.PROFILE_SCHEMA_VERSION
    assert m2.active()["name"] == "予安"                   # 数据没丢


def test_pre_versioning_file_loads_as_current(tmp_path):
    """旧版（无 schema_version 字段）档案仍能加载，视为当前 schema、不丢数据。"""
    path = os.path.join(tmp_path, "companions.json")
    legacy = {"companions": [{"id": "c1", "name": "阿旧", "tags": ["沉稳"], "turns": 3,
                              "created_at": 1.0}],
              "active_id": "c1", "next_id": 2, "audit": []}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(legacy, f, ensure_ascii=False)
    m = cm.CompanionManager(path=path, clock=lambda: CLOCK[0] + 1)
    assert m.active()["name"] == "阿旧"                    # 数据没丢
    assert m.status()["schema_version"] == cm.PROFILE_SCHEMA_VERSION


def test_old_schema_version_forward_migrated(tmp_path):
    """显式旧 schema 版本加载 → 前向迁移到当前版并落盘。"""
    path = os.path.join(tmp_path, "companions.json")
    old = {"companions": [{"id": "c1", "name": "阿旧", "tags": [], "turns": 0,
                           "created_at": 1.0}],
           "active_id": "c1", "next_id": 2, "audit": [],
           "schema_version": "2026.09-1"}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(old, f, ensure_ascii=False)
    m = cm.CompanionManager(path=path, clock=lambda: CLOCK[0] + 1)
    assert m.status()["schema_version"] == cm.PROFILE_SCHEMA_VERSION
    with open(path, "r", encoding="utf-8") as f:          # 迁移已落盘
        assert json.load(f)["schema_version"] == cm.PROFILE_SCHEMA_VERSION
