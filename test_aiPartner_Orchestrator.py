# -*- coding: utf-8 -*-
"""aiPartner_Orchestrator 测试：授权闸 / 危机护栏 / LLM 降级 / 情绪贯穿 / 记轮 / 状态机直通。"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiPartner_CompanionManager as cm
import aiPartner_ConsentStore as cs
import aiPartner_Orchestrator as oc
from aiPartner_StateEngine import Sig

CLOCK = [1000.0]


class FakeLLM:
    """带 .chat() 的假客户端：记录调用、按序返回预设回复。"""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = []

    def chat(self, messages, **kw):
        self.calls.append(messages)
        return self.replies.pop(0) if self.replies else None


def make(template="知心姐姐", llm=None, consent=False):
    CLOCK[0] += 1.0
    companions = cm.CompanionManager(
        path=os.path.join(tempfile.mkdtemp(), "companions.json"), clock=lambda: CLOCK[0])
    companions.create(template=template)
    store = None
    if consent:
        store = cs.ConsentStore(
            path=os.path.join(tempfile.mkdtemp(), "consent.json"), clock=lambda: CLOCK[0])
        store.grant({"game", "read"}, policy_ack=cs.POLICY_VERSION)
        store.set_master(True, policy_ack=cs.POLICY_VERSION)
    orch = oc.Orchestrator(companions, consent=store, llm=llm, clock=lambda: CLOCK[0])
    return orch, companions, store


# ── 自由对话主路径 ──

def test_chat_llm_happy_path_and_turn_count():
    """LLM 在线 → 回包来自模型；系统提示里有人格与红线；聊上天才记轮。"""
    llm = FakeLLM(["累了吧，先靠一会儿。"])
    orch, companions, _ = make(llm=llm)
    r = orch.chat("今天好累啊")
    assert r["source"] == "llm" and r["reply_text"] == "累了吧，先靠一会儿。"
    assert r["persona"] == "aiPartner"
    assert companions.status()["turns"] == 1
    sysmsg = llm.calls[0][0]["content"]
    assert "「aiPartner」" in sysmsg and "【红线】" in sysmsg      # 人格段 + 护栏都在
    assert llm.calls[0][1] == {"role": "user", "content": "今天好累啊"}


def test_chat_fallback_when_llm_down():
    """LLM 未配置/超时返回 None → 按主导情绪兜底，陪伴不中断、照常记轮。"""
    llm = FakeLLM([None])                          # 模型掉线
    orch, companions, _ = make(llm=llm)
    r = orch.chat("在吗")
    assert r["source"] == "fallback" and r["reply_text"]
    assert r["reply_text"] in oc._FALLBACK_BY_EMOTION.values() or r["reply_text"] == oc._FALLBACK_DEFAULT
    assert companions.status()["turns"] == 1


def test_chat_without_llm_uses_fallback():
    orch, companions, _ = make(llm=None)
    r = orch.chat("在吗")
    assert r["source"] == "fallback" and companions.status()["turns"] == 1


# ── 护栏闸（先于 LLM，不记轮） ──

def test_no_companion_guard():
    """没有在场伴侣 → 温和引导创建，不调 LLM、不记轮。"""
    orch, companions, _ = make()
    companions.delete(companions.active()["id"])
    llm = FakeLLM(["你好"])
    orch.llm = llm
    r = orch.chat("在吗")
    assert r["source"] == "guard" and "创建" in r["reply_text"]
    assert llm.calls == []                                # 没聊上天：LLM 没被调用


def test_consent_off_guard():
    """授权关闭 → 待机提示，不调 LLM 不记轮；授权后放行。"""
    llm = FakeLLM(["在的"])
    orch, companions, store = make(consent=True, llm=llm)
    store.set_master(False, policy_ack=cs.POLICY_VERSION)
    r = orch.chat("在吗")
    assert r["source"] == "guard" and "待机" in r["reply_text"]
    assert llm.calls == [] and companions.status()["turns"] == 0
    store.set_master(True, policy_ack=cs.POLICY_VERSION)
    r = orch.chat("在吗")
    assert r["source"] == "llm" and r["reply_text"] == "在的"   # 授权后放行进 LLM


def test_dnd_blocks_chat():
    """勿扰 → snapshot.master 为 False → 同样闸住。"""
    orch, _, store = make(consent=True, llm=FakeLLM(["hi"]))
    store.set_dnd(True)
    assert orch.chat("在吗")["source"] == "guard"
    store.set_dnd(False)
    assert orch.chat("在吗")["source"] == "llm"           # 勿扰解除即放行


def test_crisis_word_guardrail_first():
    """危机词 → §8.1 前置接管（只陪伴不出主意 + 热线），且她真的担心。"""
    llm = FakeLLM(["让我看看"])
    orch, companions, _ = make(llm=llm)
    r = orch.chat("我吞了半瓶安眠药")
    assert r["source"] == "guardrail"
    assert "12356" in r["reply_text"] or "crisis_hotline" in r["reply_text"]
    assert llm.calls == []                                # 危机不经 LLM
    assert orch.mood.vec.get("worry", 0) > 0.2            # 她真的担心
    assert companions.status()["turns"] == 1              # 危机算一次真实互动


# ── 情绪贯穿 ──

def test_text_inferred_mood_event_updates_bond():
    """『谢谢你』→ user_cares → 亲密度上升，情绪进入回包。"""
    orch, _, _ = make()
    before = orch.mood.bond
    r = orch.chat("谢谢你，辛苦啦")
    assert orch.mood.bond > before
    assert r["emotion"]["bond"] == round(orch.mood.bond, 2)


def test_event_shares_mood_truth_with_chat():
    """事件路径与 chat 共享同一份情绪：召唤 → 游戏被击杀 → 事件回包带心疼 → chat 情绪延续。"""
    orch, _, _ = make(llm=FakeLLM(["嗯"]))
    orch.event(Sig.SUMMON)                                # 进在场态才能同频
    ev = orch.event(Sig.GAME_EVENT, {"event": "death"})
    assert ev["kind"] == "sync_react" and "心疼" in ev["reply_text"]
    r = orch.chat("唉")
    fine_name = (r["emotion"].get("fine") or {}).get("name")
    assert fine_name == "tender"                          # 心疼还在，没收走


# ── 冻结与状态 ──

def test_freeze_never_blocks_chat():
    """10 轮冻结只锁属性修改，不锁对话——聊到第 100 轮她还在、性格没变。"""
    orch, companions, _ = make()
    for _ in range(cm.EDIT_LOCK_TURNS + 1):
        companions.count_turn()
    r = orch.chat("还在吗")
    assert r["reply_text"] and r["persona"] == "aiPartner"
    with pytest.raises(cm.CompanionLockedError):          # 冻结依旧锁修改
        companions.update(companions.active()["id"], {"tags": ["高冷"]})


def test_status_overview():
    orch, _, store = make(consent=True, llm=FakeLLM([]))
    s = orch.status()
    assert s["consent"] is True and s["companion"] == "aiPartner"
    assert s["locked"] is False and s["llm"] is True and "mood" in s


# ── 结构化输出消费 + TTS 路由（§6 输出契约 / §13 声线） ──

def test_chat_structured_json_extracts_reply_and_routes_tts():
    """LLM 返回结构化 JSON → 抽出话术（不是整坨 JSON）+ timbre 路由到 voice_id。"""
    import json
    payload = json.dumps({
        "reply_text": "你今天辛苦啦，靠一会儿。",
        "emotion": "warm",
        "tts_params": {"timbre": "甜暖女声", "emotion_tag": "warm"}})
    llm = FakeLLM([payload])
    orch, _, _ = make(llm=llm)
    r = orch.chat("今天好累")
    assert r["source"] == "llm"
    assert r["reply_text"] == "你今天辛苦啦，靠一会儿。"        # 不是整坨 JSON
    assert r["tts_params"]["voice_id"] == "reserved:xiaoyu"     # timbre 路由到声线
    assert r["tts_params"]["emotion_tag"] == "warm"
    assert r["model_config_version"]


def test_chat_plain_text_has_no_tts_params():
    """纯文本返回（旧行为）→ 直接当话术，tts_params 为空，不崩。"""
    orch, _, _ = make(llm=FakeLLM(["在的"]))
    r = orch.chat("在吗")
    assert r["source"] == "llm" and r["reply_text"] == "在的"
    assert r["tts_params"] is None
