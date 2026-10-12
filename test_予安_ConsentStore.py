# -*- coding: utf-8 -*-
"""§16.1 ConsentStore 持久化层测试（10 条）。"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import 予安_ConsentStore as cm
from 予安_ConsentStore import ConsentStore
import 予安_StateEngine as se

T = 1000.0
CLOCK = lambda: T


def make(tmp_path, raw=None):
    p = str(tmp_path / "consent.json")
    if raw is not None:
        with open(p, "w", encoding="utf-8") as f:
            f.write(raw)
    return ConsentStore(path=p, clock=CLOCK), p


def test_missing_file_fails_closed(tmp_path):
    """无授权文件 → 默认全关，绝不静默开启。"""
    store, _ = make(tmp_path)
    assert store.allows("game") is False
    assert store.status()["master"] is False


def test_corrupt_file_fails_closed(tmp_path):
    """授权文件损坏/字段缺失 → fail-closed，不抛异常。"""
    store, _ = make(tmp_path, raw='{"master": true, "scopes": ["gam')
    assert store.allows("game") is False
    store2, _ = make(tmp_path, raw=json.dumps({"master": True, "scopes": ["game"]}))  # 缺字段
    assert store2.allows("game") is False


def test_grant_requires_policy_ack(tmp_path):
    """没看过授权说明 → grant/set_master/reconfirm 全部拒绝。"""
    store, _ = make(tmp_path)
    for fn in (lambda: store.grant({"game"}),
               lambda: store.set_master(True),
               lambda: store.reconfirm()):
        try:
            fn()
            assert False, "应拒绝未确认授权说明的操作"
        except ValueError:
            pass
    assert store.allows("game") is False


def test_sensitive_scenes_hard_denied(tmp_path):
    """敏感场景（支付/医疗/私密浏览）授权拒收——即使将来有人把 payment 加进
    感知目录，硬边界也必须独立拦住（不是设置项，是硬边界）。"""
    store, _ = make(tmp_path)
    saved_catalog = dict(cm.SCENE_CATALOG)
    try:
        cm.SCENE_CATALOG["payment"] = "（未来某天被误加进目录的敏感场景）"
        try:
            store.grant({"payment"}, policy_ack=cm.POLICY_VERSION)
            assert False, "敏感场景应被硬边界拒收"
        except ValueError as e:
            assert "敏感场景" in str(e)                      # 拦它的是硬边界，不是白名单
        assert "payment" not in store._state["scopes"]
        store.grant({"game"}, policy_ack=cm.POLICY_VERSION)   # 正常场景不受影响
        assert store.allows("game") is True
    finally:
        cm.SCENE_CATALOG.clear()
        cm.SCENE_CATALOG.update(saved_catalog)


def test_unknown_scene_rejected(tmp_path):
    """白名单之外的场景无法授权（最小必要）。"""
    store, _ = make(tmp_path)
    try:
        store.grant({"screen_record"}, policy_ack=cm.POLICY_VERSION)
        assert False, "目录外场景应被拒绝"
    except ValueError:
        pass


def test_grant_persists_and_reloads(tmp_path):
    """授权落盘；重启（重新加载）后恢复；勿扰持久生效。"""
    store, p = make(tmp_path)
    store.grant({"game", "nav"}, policy_ack=cm.POLICY_VERSION)
    store2 = ConsentStore(path=p, clock=CLOCK)
    assert store2.allows("game") and store2.allows("nav")
    assert not store2.allows("work")
    store2.set_dnd(True)
    store3 = ConsentStore(path=p, clock=CLOCK)
    assert store3.allows("game") is False                 # 勿扰重启仍生效
    store3.set_dnd(False)
    assert store3.allows("game") is True


def test_revoke_fine_grained(tmp_path):
    """细粒度关闭单个场景，其余保留。"""
    store, _ = make(tmp_path)
    store.grant({"game", "read"}, policy_ack=cm.POLICY_VERSION)
    store.revoke({"game"})
    assert store.allows("game") is False and store.allows("read") is True


def test_policy_version_bump_invalidates(tmp_path):
    """条款升级 → 旧授权自动失效（needs_reconfirm），重新确认前拒绝一切感知。"""
    store, p = make(tmp_path)
    store.grant({"game"}, policy_ack=cm.POLICY_VERSION)
    assert store.allows("game") is True

    old = cm.POLICY_VERSION
    try:
        cm.POLICY_VERSION = "2027.01-1"
        assert store.allows("game") is False
        st = store.status()
        assert st["needs_reconfirm"] is True and st["master"] is False
        store2 = ConsentStore(path=p, clock=CLOCK)         # 重启加载也失效（_load 拦截）
        assert store2.allows("game") is False
        store2.reconfirm(policy_ack="2027.01-1")           # 重新确认新版条款
        assert store2.allows("game") is True
    finally:
        cm.POLICY_VERSION = old


def test_master_off_keeps_scopes(tmp_path):
    """总开关关闭 ≠ 清空授权：场景范围保留，随时可再开。"""
    store, _ = make(tmp_path)
    store.grant({"game"}, policy_ack=cm.POLICY_VERSION)
    store.set_master(False)
    assert store.allows("game") is False
    assert "game" in store._state["scopes"]
    store.set_master(True, policy_ack=cm.POLICY_VERSION)
    assert store.allows("game") is True


def test_engine_integration_snapshot_and_consent_off(tmp_path):
    """引擎联动：未授权召唤被拒 → 授权后 snapshot 可召唤 → 运行中撤销降级 S1。"""
    store, _ = make(tmp_path)
    eng = se.StateEngine(consent=store.snapshot(), clock=CLOCK)
    a = eng.handle(se.Sig.SUMMON)
    assert a.kind == "need_consent"                       # 未授权：只提示去设置

    store.grant({"game"}, policy_ack=cm.POLICY_VERSION)
    eng.consent = store.snapshot()
    eng.handle(se.Sig.SUMMON)                             # 授权后可召唤进 S3
    assert eng.state == se.State.PRESENCE

    store.revoke({"game"})                                # 运行中撤销
    eng.consent = store.snapshot()
    a = eng.handle(se.Sig.CONSENT_OFF)
    assert eng.state == se.State.IDLE                     # 降级 S1 待机
    assert a.kind == "exit_presence"
