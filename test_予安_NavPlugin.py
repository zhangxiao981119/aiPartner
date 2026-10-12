# -*- coding: utf-8 -*-
"""
予安_NavPlugin 测试（§16.4 + §16.6 契约接入）
运行：venv 的 python -m pytest test_予安_NavPlugin.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import 予安_StateEngine as se  # noqa: E402
import 予安_NavPlugin as nav_mod  # noqa: E402
from 予安_TopicEngine import TopicEngine  # noqa: E402

Sig, State, Consent, StateEngine = se.Sig, se.State, se.Consent, se.StateEngine


def make(scopes=None, with_topics=True):
    """可控时钟引擎 + 已注册的导航陪伴插件。fire() 自动跨过节流窗口。"""
    t = {"now": 1000.0}
    topics = TopicEngine() if with_topics else None
    eng = StateEngine(
        consent=Consent(master=True,
                        scopes=scopes if scopes is not None
                        else {"game", "read", "work", "nav"}),
        topics=topics, clock=lambda: t["now"],
    )
    nav = nav_mod.NavCompanionPlugin(topics=topics)
    eng.register_plugin(nav)
    eng.handle(Sig.SUMMON)

    def fire(event):
        t["now"] += 181.0
        return eng.handle(Sig.NAV_EVENT, {"event": event})

    return eng, nav, t, fire


def test_offer_then_decline_latches():
    """提议 → 婉拒 → 本段不再问（不骚扰）。"""
    eng, nav, t, fire = make()
    a = fire("nav_started")
    assert a is not None and "陪你走这段" in a.text
    a = fire("nav_decline")
    assert a is not None and "专心开车" in a.text
    assert fire("nav_started") is None               # 婉拒后不重复提议
    assert fire("traffic") is None                   # 也没进入陪伴


def test_confirm_gate_before_accept():
    """确认才进入：提议之后、接受之前，一切播报静默（不抢跑）。"""
    eng, nav, t, fire = make()
    fire("nav_started")
    assert fire("traffic") is None                   # 未确认：路况也不插话
    assert fire("long_road") is None
    a = fire("nav_accept")
    assert "我陪着" in a.text


def test_three_talks_and_arrival_reset():
    """确认后只说三类话：路况提醒 / 解闷 / 抵达道贺；到站复位。"""
    eng, nav, t, fire = make()
    fire("nav_started")
    fire("nav_accept")
    a = fire("traffic")
    assert "别急" in a.text
    a = fire("long_road")
    assert "给你说个事" in a.text and "想聊这个吗" not in a.text  # 解闷直接说，不问句
    a = fire("arrival")
    assert "到啦" in a.text
    assert nav.mode == "off"                          # 复位
    a = fire("nav_started")                           # 新一段行程 → 重新提议
    assert a is not None and "陪你走这段" in a.text


def test_turn_silent_without_consuming_throttle():
    """转向绝不插话，且安静 ≠ 互动：不消耗节流窗口，路况提醒不被挤掉。"""
    eng, nav, t, fire = make()
    fire("nav_started")
    fire("nav_accept")
    state_before = eng.state                          # 转向前她停在原地（S4）
    t["now"] += 181.0
    assert eng.handle(Sig.NAV_EVENT, {"event": "turn"}) is None   # 转向静默
    assert eng.state == state_before                  # 状态恢复原位，没起新话头
    a = eng.handle(Sig.NAV_EVENT, {"event": "traffic"})  # 不推进时间，立即发生
    assert a is not None and "别急" in a.text          # 提醒照常出口（窗口未被吃掉）


def test_highway_silent():
    """高速/复杂路况自动静默。"""
    eng, nav, t, fire = make()
    fire("nav_started")
    fire("nav_accept")
    assert fire("highway") is None


def test_fatigue_reminder_blocks_chat():
    """困意接 §8.1：温和提醒休息，之后不接话聊天、不递话题。"""
    eng, nav, t, fire = make()
    fire("nav_started")
    fire("nav_accept")
    a = fire("fatigue")
    assert "歇会儿" in a.text and a.animation == "stay_close"
    assert fire("long_road") is None                   # 不递话题解闷
    assert nav._fatigued is True
    fire("arrival")                                    # 到站复位
    assert nav._fatigued is False


def test_scope_unauthorized_silent():
    """§16.1 授权前置：出行场景未授权 → 整条导航线不感知。"""
    eng, nav, t, fire = make(scopes={"game", "read", "work"})
    assert fire("nav_started") is None
