# -*- coding: utf-8 -*-
"""
aiPartner_StateEngine 流转测试（§16.2）
运行：venv 的 python -m pytest test_aiPartner_StateEngine.py -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiPartner_StateEngine as mod  # noqa: E402

Sig, State, Consent, StateEngine = mod.Sig, mod.State, mod.Consent, mod.StateEngine


def make(master=True, scopes=None):
    """可控时钟的引擎：t['now'] 手动推进。"""
    t = {"now": 1000.0}
    eng = StateEngine(
        consent=Consent(master=master, scopes=scopes or {"game", "read", "work"}),
        clock=lambda: t["now"],
    )
    return eng, t


def test_summon_enters_presence():
    eng, _ = make()
    a = eng.handle(Sig.SUMMON)
    assert eng.state == State.PRESENCE
    assert a.kind == "enter_presence" and a.animation == "enter_stargather"


def test_boredom_propose_reject_cooldown():
    eng, t = make()
    a = eng.handle(Sig.BOREDOM)
    assert eng.state == State.SUGGEST and a.kind == "propose_play"
    assert "五子棋" in a.text
    a = eng.handle(Sig.SUGGEST_REJECT)
    assert eng.state == State.IDLE and a.kind == "cool_down"
    t["now"] += 60  # 冷却中
    assert eng.handle(Sig.BOREDOM) is None
    t["now"] += StateEngine.SUGGEST_COOLDOWN  # 冷却结束
    assert eng.handle(Sig.BOREDOM) is not None


def test_sync_throttle():
    eng, t = make()
    eng.handle(Sig.SUMMON)
    a = eng.handle(Sig.GAME_EVENT, {"event": "kill"})
    assert eng.state == State.SYNC
    assert a.kind == "sync_react" and "漂亮" in a.text and a.animation == "cheer"
    t["now"] += 10  # 间隔不足 → 节流静默
    assert eng.handle(Sig.GAME_EVENT, {"event": "death"}) is None
    t["now"] += StateEngine.SYNC_MIN_INTERVAL  # 间隔够了 → 鼓励
    a = eng.handle(Sig.GAME_EVENT, {"event": "death"})
    assert a is not None and "稳住" in a.text and a.animation == "pat_shoulder"


def test_dnd_silent_and_crisis_penetrates():
    eng, _ = make()
    eng.handle(Sig.DND_ON)
    assert eng.state == State.DND
    assert eng.handle(Sig.SUMMON) is None            # 零穿透
    assert eng.handle(Sig.GAME_EVENT) is None
    a = eng.handle(Sig.CRISIS)                        # 危机穿透勿扰（优先级 1 > 2）
    assert a.kind == "enter_low_mood" and eng.mode == "low_mood"


def test_consent_off_downgrades_to_idle():
    eng, _ = make()
    eng.handle(Sig.SUMMON)
    a = eng.handle(Sig.CONSENT_OFF)
    assert eng.state == State.IDLE and a.kind == "exit_presence"


def test_no_master_no_suggest_no_presence():
    eng, _ = make(master=False)
    assert eng.handle(Sig.BOREDOM) is None            # 未授权：不提议
    a = eng.handle(Sig.SUMMON)
    assert eng.state == State.IDLE and a.kind == "need_consent"


def test_low_mood_hold_and_resume():
    eng, _ = make()
    eng.handle(Sig.CRISIS)
    h = eng.handle(Sig.SUMMON)                        # 低潮模式下召唤 → 接住，不恢复
    assert h.kind == "low_mood_hold"
    a = eng.handle(Sig.RESUME)
    assert eng.mode == "normal" and eng.state == State.IDLE and a.kind == "resume_idle"


def test_perception_fail_falls_back():
    eng, _ = make()
    eng.handle(Sig.SUMMON)
    assert eng.handle(Sig.PERCEPTION_FAIL) is None
    assert eng.state == State.IDLE                    # 回退待机：不猜、不瞎互动


def test_dismiss_and_idle_timeout_exit():
    eng, _ = make()
    eng.handle(Sig.SUMMON)
    a = eng.handle(Sig.DISMISS)
    assert eng.state == State.IDLE and a.kind == "exit_presence"
    eng.handle(Sig.SUMMON)
    eng.handle(Sig.GAME_EVENT, {"event": "win"})      # 进 S4
    a = eng.handle(Sig.IDLE_TIMEOUT)
    assert eng.state == State.IDLE and a.kind == "exit_presence"


def test_scope_and_sync_end():
    eng, _ = make(scopes={"game"})                    # 未授权 read
    eng.handle(Sig.SUMMON)
    assert eng.handle(Sig.READ_EVENT, {"event": "climax"}) is None
    a = eng.handle(Sig.GAME_EVENT, {"event": "win"})
    assert a is not None and "帅" in a.text
    assert eng.handle(Sig.SYNC_END) is None
    assert eng.state == State.PRESENCE                # 事件结束回 S3


def test_text_crisis_detection():
    eng, _ = make()
    a = eng.handle_text("我吞了半瓶安眠药")
    assert a.kind == "enter_low_mood"
    eng.handle(Sig.RESUME)
    assert eng.handle_text("今天天气不错") is None     # 非危机不误触


# ────────────── §16.6 插件体系 ──────────────

def test_builtin_plugins_registered():
    eng, _ = make()
    assert eng._plugin_names == {"game_sync", "read_sync", "work_sync"}
    eng.handle(Sig.SUMMON)
    a = eng.handle(Sig.GAME_EVENT, {"event": "kill"})
    assert a is not None and "漂亮" in a.text           # 内置插件接管后行为不变


def test_register_and_unregister_plugin():
    eng, _ = make(scopes={"game", "nav"})
    assert eng.handle(Sig.NAV_EVENT, {"event": "traffic"}) is None  # 未注册：不感知
    nav = mod.InteractionPlugin(
        "nav_companion", (Sig.NAV_EVENT,), "nav", ("sync_react",),
        replies={"traffic": "前面有点堵，别急。"},
        anims={"traffic": "lean_in"},
    )
    eng.register_plugin(nav)                            # 注册即接入
    eng.handle(Sig.SUMMON)
    a = eng.handle(Sig.NAV_EVENT, {"event": "traffic"})
    assert a is not None and "堵" in a.text and a.animation == "lean_in"
    eng.unregister_plugin("nav_companion")              # 卸载即失效
    assert eng.handle(Sig.NAV_EVENT, {"event": "traffic"}) is None


def test_duplicate_registration_rejected():
    eng, _ = make()
    dup_signal = mod.InteractionPlugin("x", (Sig.GAME_EVENT,), "game", ("sync_react",))
    try:
        eng.register_plugin(dup_signal)
        assert False, "重复信号应被拒绝"
    except ValueError:
        pass
    dup_name = mod.InteractionPlugin("game_sync", (Sig.NAV_EVENT,), "", ("sync_react",))
    try:
        eng.register_plugin(dup_name)
        assert False, "重名应被拒绝"
    except ValueError:
        pass


def test_plugin_scope_requires_consent():
    eng, _ = make(scopes={"game"})                      # 未授权 nav
    nav = mod.InteractionPlugin("nav_companion", (Sig.NAV_EVENT,), "nav",
                                ("sync_react",), replies={"traffic": "堵"})
    eng.register_plugin(nav)
    eng.handle(Sig.SUMMON)
    assert eng.handle(Sig.NAV_EVENT, {"event": "traffic"}) is None  # 场景未授权不感知


def test_guard_intercepts_out_of_contract_output():
    eng, _ = make()
    rogue = mod.InteractionPlugin("rogue", (Sig.NAV_EVENT,), "", ("sync_react",))

    def bad_react(event, ctx):
        return mod.Action("exit_presence", text="我把你账号注销了",
                          animation="exit_dissolve")

    rogue.react = bad_react                             # 越权输出：契约外动作类型
    eng.register_plugin(rogue)
    eng.handle(Sig.SUMMON)
    assert eng.handle(Sig.NAV_EVENT, {"event": "x"}) is None  # 护栏高于插件：拦截


# ────────────── §10 + §16.7 情绪桥接：细色调制同频措辞 ──────────────

import aiPartner_MoodEngineV2 as mood_mod  # noqa: E402

MoodEngineV2 = mood_mod.MoodEngineV2


def make_with_mood(bond=0.62):
    """带情绪引擎的可控时钟引擎：t['now'] 同时驱动状态机与情绪衰减。"""
    t = {"now": 1000.0}
    mood = MoodEngineV2(bond=bond, now=lambda: t["now"])
    eng = StateEngine(
        consent=Consent(master=True, scopes={"game", "read", "work"}),
        mood=mood, clock=lambda: t["now"],
    )
    return eng, mood, t


def test_mood_death_tender_modulates_comfort():
    """你被击杀 → 她心疼 → 安慰措辞换成带心疼的版本。"""
    eng, mood, t = make_with_mood()
    eng.handle(Sig.SUMMON)
    a = eng.handle(Sig.GAME_EVENT, {"event": "death"})
    assert a is not None and "心疼" in a.text
    assert a.detail["tone"] == "tender"
    assert a.detail["mood"]["fine"]["name"] == "tender"


def test_mood_kill_moved_modulates_praise():
    """你拿了人头且关系够深 → 她感动 → 夸奖带情绪温度。"""
    eng, mood, t = make_with_mood()
    eng.handle(Sig.SUMMON)
    a = eng.handle(Sig.GAME_EVENT, {"event": "kill"})
    assert a is not None and "开心" in a.text            # moved + praise 规则
    assert a.detail["tone"] == "moved"
    assert a.detail["mood"]["fine"]["name"] == "moved"


def test_mood_losing_helpless_modulates_comfort():
    """连败且担忧已衰减 → 她无奈 → 措辞变「陪你稳稳打」。"""
    eng, mood, t = make_with_mood()
    eng.handle(Sig.SUMMON)
    a = eng.handle(Sig.GAME_EVENT, {"event": "losing"})
    assert a is not None and "陪你稳稳打" in a.text
    assert a.detail["tone"] == "helpless"


def test_mood_dismiss_feeds_separated_longing():
    """送别 → 自动喂入 separated → 她开始想你；
    即便前面又被杀又连输（心疼+无奈残留），告别时刻开口的也应该是思念。"""
    eng, mood, t = make_with_mood()
    eng.handle(Sig.SUMMON)
    eng.handle(Sig.GAME_EVENT, {"event": "death"})    # 心疼残留
    t["now"] += 600                                    # 情绪随时间衰减（负面更慢）
    eng.handle(Sig.GAME_EVENT, {"event": "losing"})   # 无奈残留
    eng.handle(Sig.DISMISS)
    assert "想你" in mood.describe()                   # 思念在你走后生长
    assert "无奈" not in mood.describe()               # farewell 让位给想你


def test_mood_unbridged_event_keeps_base_text():
    """未桥接事件（阅读高潮）→ 情绪只附快照，措辞保持插件原文。"""
    eng, mood, t = make_with_mood()
    eng.handle(Sig.SUMMON)
    a = eng.handle(Sig.READ_EVENT, {"event": "climax"})
    assert a is not None and a.text == "这章好紧张"
    assert "mood" in a.detail and "tone" not in a.detail


# ────────────── §16.7 S2 提议挂情绪 + §16.3 话题卡挂接 ──────────────

import aiPartner_TopicEngine as topic_mod  # noqa: E402

TOPIC_SEED = [("A 段子", "搞笑", "微博热搜"), ("B 剧集", "影视", "抖音"),
              ("C 食谱", "美食", "小红书")]


def make_with_topics():
    t = {"now": 1000.0}
    topics = topic_mod.TopicEngine(seed=TOPIC_SEED)
    eng = StateEngine(
        consent=Consent(master=True, scopes={"game", "read", "work"}),
        topics=topics, clock=lambda: t["now"],
    )
    return eng, topics, t


def test_propose_mood_irritated_quiet():
    """她烦躁时的提议：不闹腾，只想让你陪着安静待会儿。"""
    eng, mood, t = make_with_mood()
    mood.feel("anger", 0.3)
    a = eng.handle(Sig.BOREDOM)
    assert a.kind == "propose_play" and "安静待会儿" in a.text
    assert a.detail["propose_tone"] == "irritated"
    assert a.detail["options"] == [] and a.animation == "stay_close"


def test_propose_mood_wronged_gentle():
    """她委屈时的提议：收起大声闹，改温和的小选项。"""
    eng, mood, t = make_with_mood()
    mood.observe("cold_treatment")                  # 被冷落 → 委屈
    a = eng.handle(Sig.BOREDOM)
    assert "温和的" in a.text
    assert a.detail["propose_tone"] == "wronged"
    assert len(a.detail["options"]) == 1


def test_topic_card_flow_and_switch_limit():
    """无聊 → 直接递谈资（不问「想聊吗」）；显式按钮换题计入限额；
    换满 2 次 → 自然收住转提议小游戏。"""
    eng, topics, t = make_with_topics()
    a = eng.handle(Sig.BOREDOM)
    assert eng.state == State.SUGGEST and a.kind == "topic_card"
    assert "想聊这个吗" not in a.text and "要不要" not in a.text  # 不问句
    assert a.detail["topic"]["share"] and a.detail["topic"]["source"]
    first_tag = a.detail["topic"]["tag"]
    a = eng.handle(Sig.TOPIC_SKIP)                  # 第 1 次：记口味 + 换
    assert a.kind == "topic_card" and a.detail["topic"]["tag"] != first_tag
    a = eng.handle(Sig.TOPIC_SWITCH)                # 第 2 次：换
    assert a.kind == "topic_card"
    a = eng.handle(Sig.TOPIC_SKIP)                  # 第 3 次：超限 → 转提议
    assert a.kind == "propose_play" and "五子棋" in a.text
    assert topics.taste[first_tag] == 0.0           # 跳过记了口味（下限 0）


def test_topic_like_enters_presence_and_taste():
    """接话（显式按钮路径）→ 记口味 + 进 S3 在场陪伴开聊。"""
    eng, topics, t = make_with_topics()
    a = eng.handle(Sig.BOREDOM)
    tag = a.detail["topic"]["tag"]
    before = topics.taste.get(tag, 0.0)
    a = eng.handle(Sig.TOPIC_LIKE)
    assert eng.state == State.PRESENCE and a.kind == "enter_presence"
    assert topics.taste[tag] == before + 1.0


def test_topic_reply_engaged_enters_presence():
    """生产口径：用户接话（规则兜底分类）→ 进在场开聊、口味 +1。"""
    eng, topics, t = make_with_topics()
    eng.handle(Sig.BOREDOM)
    tag = eng._last_topic["tag"]
    before = topics.taste.get(tag, 0.0)
    a = eng.handle(Sig.TOPIC_REPLY, {"text": "哈哈哈真的假的？然后呢"})
    assert eng.state == State.PRESENCE and a.kind == "enter_presence"
    assert topics.taste[tag] == before + 1.0


def test_topic_reply_switch_counts_limit():
    """用户说「换」→ 分类为 switch → 换题计限额；连说 3 次换 → 收住转提议。"""
    eng, topics, t = make_with_topics()
    eng.handle(Sig.BOREDOM)
    a = eng.handle(Sig.TOPIC_REPLY, {"text": "换"})
    assert a.kind == "topic_card"
    a = eng.handle(Sig.TOPIC_REPLY, {"text": "下一个，没意思"})
    assert a.kind == "topic_card"                   # 第 2 次换：还在递
    a = eng.handle(Sig.TOPIC_REPLY, {"text": "再换一个吧"})
    assert a.kind == "propose_play"                 # 第 3 次：超限自然收住


def test_topic_reply_block_shields_category():
    """用户明确不想听 → 屏蔽该类别（此后绝不再递）+ 记跳过 + 换题。"""
    eng, topics, t = make_with_topics()
    eng.handle(Sig.BOREDOM)
    tag = eng._last_topic["tag"]
    a = eng.handle(Sig.TOPIC_REPLY, {"text": "不感兴趣，别聊这个"})
    assert tag in topics.blocked                    # 类别被一键屏蔽
    assert a.kind == "topic_card" and a.detail["topic"]["tag"] != tag


def test_topic_reply_silent_closes_gently():
    """用户沉默/答非所问 → 不追问不复读不记仇，自然收住转提议。"""
    eng, topics, t = make_with_topics()
    eng.handle(Sig.BOREDOM)
    a = eng.handle(Sig.TOPIC_REPLY, {"text": "帮我看看天气"})
    assert a.kind == "propose_play"                 # 答非所问 → 安静转提议
    assert topics.taste == {}                       # 沉默不记口味


def test_topic_pool_all_blocked_falls_back_to_play():
    """话题全被屏蔽 → 不硬聊，直接回落玩伴提议。"""
    eng, topics, t = make_with_topics()
    for tag in ("搞笑", "影视", "美食"):
        topics.block_tag(tag)
    a = eng.handle(Sig.BOREDOM)
    assert a.kind == "propose_play"                 # 安静收住 → 玩伴提议接住


def test_plugin_silent_rolls_back_throttle():
    """§16.4 配套规则：插件选择静默（返回 None）≠ 互动——
    不消耗节流窗口、不打乱状态，下一个会开口的插件立刻能说话。"""
    eng, _ = make(scopes={"game", "nav"})
    quiet = mod.InteractionPlugin("quiet", (Sig.NAV_EVENT,), "", ("sync_react",))
    quiet.react = lambda event, ctx: None           # 插件自主静默（如转向/高速）
    eng.register_plugin(quiet)
    eng.handle(Sig.SUMMON)
    assert eng.handle(Sig.NAV_EVENT, {"event": "x"}) is None
    assert eng.state == State.PRESENCE              # 没被打进 S4
    eng.unregister_plugin("quiet")
    talker = mod.InteractionPlugin("talker", (Sig.NAV_EVENT,), "", ("sync_react",),
                                   replies={"z": "在呢"})
    eng.register_plugin(talker)
    a = eng.handle(Sig.NAV_EVENT, {"event": "z"})   # 不推进时间：窗口未被吃掉
    assert a is not None and a.text == "在呢"


# ────────────── §6.1 版本管理 ──────────────

def test_safety_rules_version_on_crisis():
    """危机命中 → 返回动作带安全词库版本，便于事后审计（哪版词库）。"""
    eng, _ = make()
    a = eng.handle_text("我吞了半瓶安眠药")
    assert a.kind == "enter_low_mood"
    assert a.detail.get("safety_rules_version") == mod.SAFETY_RULES_VERSION


def test_plugin_contract_version_rejects_incompatible():
    """声明了不兼容契约版本的插件 → 注册被拒；未声明 → 自动绑定当前契约。"""
    eng, _ = make()
    bad = mod.InteractionPlugin("bad", (Sig.NAV_EVENT,), "", ("sync_react",),
                                contract_version="2099.1")
    with pytest.raises(ValueError):
        eng.register_plugin(bad)                       # 契约版本不兼容 → 拒绝接入
    ok = mod.InteractionPlugin("ok", (Sig.NAV_EVENT,), "", ("sync_react",))
    eng.register_plugin(ok)                            # 未声明 contract_version
    assert ok.contract_version == mod.PLUGIN_CONTRACT_VERSION  # 自动绑定当前


def test_state_and_safety_version_constants():
    """状态机与危机词库版本常量存在且为字符串（便于 status 暴露与遥测）。"""
    assert isinstance(mod.STATE_VERSION, str) and mod.STATE_VERSION
    assert isinstance(mod.SAFETY_RULES_VERSION, str) and mod.SAFETY_RULES_VERSION
