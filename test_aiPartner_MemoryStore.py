# -*- coding: utf-8 -*-
"""aiPartner_MemoryStore 测试（pytest）
====================================
覆盖：
  - schema 版本落盘 / 旧版前向迁移 / 损坏文件 fail-safe
  - add + summarize_for_prompt（情感记忆 / 关系史+fact 折叠）
  - summarize 上限（token 不撑爆）/ 按伴侣分桶隔离 / status 形态
  - 类型白名单拒收未知类型
"""

import json
import os
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import aiPartner_MemoryStore as MS


def _store():
    return MS.MemoryStore(path=os.path.join(tempfile.mkdtemp(), "memory.json"),
                          clock=lambda: 1000.0)


# ── schema 版本与持久化 ──

def test_memory_schema_version_persisted():
    s = _store()
    cid = "c1"
    s.add(cid, "affective", "用户加班会焦虑", emotion="worry")
    # 重新加载，确认 schema_version 随文件落盘
    reloaded = MS.MemoryStore(path=s.path, clock=lambda: 2000.0)
    assert reloaded.status(cid)["schema_version"] == MS.MEMORY_SCHEMA_VERSION


def test_pre_versioning_file_loads_as_current():
    """旧版 schema_version 文件能加载为当前版（前向兼容，不报错不丢数据）。"""
    path = os.path.join(tempfile.mkdtemp(), "memory.json")
    data = {"schema_version": "1999.01-0",          # 一个更早的版本号
            "memories": {"c1": [{"type": "affective", "text": "旧记忆",
                                 "emotion": "worry", "at": 1.0}]},
            "audit": []}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    s = MS.MemoryStore(path=path, clock=lambda: 2000.0)
    assert s.status("c1")["schema_version"] == MS.MEMORY_SCHEMA_VERSION
    assert s.count("c1") == 1
    assert s.summarize_for_prompt("c1")[0] == "旧记忆"


def test_old_schema_forward_migrated_flag():
    """_migrate 在旧文件上返回 True，并补默认 weight/at。"""
    path = os.path.join(tempfile.mkdtemp(), "memory.json")
    data = {"schema_version": "1999.01-0",
            "memories": {"c1": [{"type": "affective", "text": "缺字段的记忆"}]},
            "audit": []}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    s = MS.MemoryStore(path=path, clock=lambda: 2000.0)
    rec = s.recall("c1")[0]
    assert rec["weight"] == 1.0 and rec["at"] == 0.0     # 缺字段补默认


def test_corrupt_file_failsafe():
    """损坏文件 → 空记忆，绝不虚构一段关系。"""
    path = os.path.join(tempfile.mkdtemp(), "memory.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{corrupted!")
    s = MS.MemoryStore(path=path, clock=lambda: 3000.0)
    assert s.count("c1") == 0
    assert s.summarize_for_prompt("c1") == ("（暂无）", "（暂无）")


# ── 写入与召回 ──

def test_add_and_summarize():
    s = _store()
    cid = "c1"
    s.add(cid, "affective", "用户提过加班到凌晨会焦虑", emotion="worry")
    s.add(cid, "relationship", "上周陪她熬过项目deadline")
    s.add(cid, "fact", "用户教过五子棋要堵活三", weight=1.2)
    aff, rel = s.summarize_for_prompt(cid)
    assert "加班" in aff                                  # 情感记忆单独成段
    assert "deadline" in rel and "五子棋" in rel           # relationship + fact 折叠进关系史
    assert s.recall(cid, "affective")                      # 可按类型召回
    assert s.count(cid) == 3


def test_unknown_kind_rejected():
    s = _store()
    with pytest.raises(MS.MemoryKindError):
        s.add("c1", "daydream", "不该有的类型")


def test_summarize_caps_token_bloat():
    """情感记忆超过上限只取最近 N 条，防止撑爆 Prompt token。"""
    s = _store()
    cid = "c1"
    for i in range(20):
        s.add(cid, "affective", f"事件{i}", at=float(i))
    aff, _ = s.summarize_for_prompt(cid, max_affective=8)
    assert aff.count("；") == 7                            # 8 条 → 7 个分隔符


def test_per_companion_isolation():
    """不同伴侣的记忆互不串桶。"""
    s = _store()
    s.add("c1", "affective", "c1 的焦虑")
    s.add("c2", "affective", "c2 的开心", emotion="joy")
    assert "c1 的焦虑" in s.summarize_for_prompt("c1")[0]
    assert "c1 的焦虑" not in s.summarize_for_prompt("c2")[0]
    assert s.status("c1")["kinds"] == ["affective"]


def test_status_shape():
    s = _store()
    cid = "c1"
    s.add(cid, "affective", "x")
    st = s.status(cid)
    assert st["schema_version"] == MS.MEMORY_SCHEMA_VERSION
    assert st["count"] == 1
    assert st["kinds"] == ["affective"]
