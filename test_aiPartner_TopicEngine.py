# -*- coding: utf-8 -*-
"""aiPartner_TopicEngine 生产版测试（§16.3）。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiPartner_TopicEngine as tmod  # noqa: E402
from aiPartner_TopicEngine import TopicEngine, classify_by_rules, categorize  # noqa: E402

SEED = [("A 段子", "搞笑", "微博热搜"), ("B 剧集", "影视", "抖音"),
        ("C 食谱", "美食", "小红书"), ("D 补丁", "游戏", "快手")]


class FakeLLM:
    """可注入假模型：按脚本返回，记录调用。"""

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, temperature=0.7):
        self.calls.append(messages[0]["content"][:20])
        return self.replies.pop(0) if self.replies else None


def make(llm=None, taste_path=None):
    return TopicEngine(seed=SEED, llm=llm, taste_path=taste_path)


def test_ingest_compliance_gate():
    """合规闸：广告软推 / 争议类别 / 敏感词，入池即拦。"""
    eng = TopicEngine(seed=[])
    assert eng.ingest(("这游戏币下周涨停，点击领优惠", "搞笑", "抖音")) is False
    assert eng.ingest(("某明星恋情细节全回顾", "明星隐私", "微博热搜")) is False
    assert eng.ingest(("民间偏方包治失眠", "冷知识", "快手")) is False
    assert eng.ingest(("正常的趣闻一条", "冷知识", "微博热搜")) is True
    assert eng.ingest(("正常的趣闻一条", "冷知识", "微博热搜")) is False  # 去重
    assert len(eng.pool) == 1


def test_suggest_never_asks_and_cites_source():
    """生产口径：直接递谈资，绝不出现「想聊/要不要听」类问句，带来源标注。"""
    eng = make()
    t1 = eng.suggest()
    assert "question" not in t1
    assert "想聊" not in t1["share"] and "要不要" not in t1["share"]
    assert t1["share"] and t1["source"] and t1["tag"]
    t2 = eng.suggest()
    assert t2["fp"] != t1["fp"]                     # 不复读


def test_llm_share_preferred_template_fallback():
    """转述：LLM 可用 → 用模型转述；失败 → 原标题兜底，永不阻塞。"""
    llm = FakeLLM(["那只橘猫胖得快抱不动啦"])
    eng = make(llm=llm)
    t = eng.suggest()
    assert t["share"] == "那只橘猫胖得快抱不动啦"
    t2 = eng.suggest()                              # 脚本空了 → 模板兜底
    assert t2["share"] == t2["title"]


def test_classify_rules_fallback():
    """规则兜底分类：接话 / 换题 / 屏蔽 / 沉默各就各位。"""
    assert classify_by_rules("哈哈哈真的假的？然后呢") == "engaged"
    assert classify_by_rules("换一个吧，没意思") == "switch"
    assert classify_by_rules("不感兴趣，别聊这个") == "block"
    assert classify_by_rules("嗯") == "silent"
    assert classify_by_rules("") == "silent"
    assert classify_by_rules("帮我订明天的闹钟") == "silent"   # 答非所问


def test_classify_llm_preferred():
    """LLM 分类优先：文本像接话但模型判 block → block（模型更懂反讽）。"""
    llm = FakeLLM(["block"])
    eng = make(llm=llm)
    assert eng.classify("呵呵，行吧") == "block"


def test_taste_memory_orders_suggestion():
    """口味记忆：愿聊过的类别优先递出。"""
    eng = make()
    eng.feedback(eng.pool[3], liked=True)           # 游戏 +1
    assert eng.suggest()["tag"] == "游戏"
    eng2 = make()
    eng2.feedback(eng2.pool[1], liked=True)         # 影视 +1
    assert eng2.suggest()["tag"] == "影视"


def test_feedback_floor_no_grudge():
    """口味下限 0：不感兴趣不等于讨厌，不记仇。"""
    eng = make()
    topic = eng.pool[0]
    for _ in range(3):
        eng.feedback(topic, liked=False)
    assert eng.taste["搞笑"] == 0.0
    eng.feedback(topic, liked=True)
    assert eng.taste["搞笑"] == 1.0


def test_block_tag_and_all_blocked_returns_none():
    eng = make()
    eng.block_tag("搞笑")
    for _ in range(6):
        assert eng.suggest()["tag"] != "搞笑"
    for tag in ("影视", "美食", "游戏"):
        eng.block_tag(tag)
    assert eng.suggest() is None                    # 全屏蔽：安静收住，不硬聊


def test_taste_persists_and_reloads(tmp_path):
    """口味持久化：重启（重新加载）后偏好与屏蔽类别原样恢复。"""
    p = str(tmp_path / "taste.json")
    eng = make(taste_path=p)
    eng.feedback(eng.pool[3], liked=True)
    eng.block_tag("影视")
    eng2 = TopicEngine(seed=SEED, taste_path=p)
    assert eng2.taste.get("游戏") == 1.0
    assert "影视" in eng2.blocked


def test_refresh_pulls_from_sources_and_degrades(tmp_path):
    """refresh()：从注入的数据源拉热点，单源失败不影响其他；全失败不崩。"""

    class OkSource:
        name = "测试源"

        def fetch(self):
            return [("玩家自制 mod 官方下场合作", self.name),
                    ("荐股大师带你抄底", self.name)]          # 合规闸应拦第二条

    class DeadSource:
        name = "挂了的源"

        def fetch(self):
            raise OSError("network down")

    eng = TopicEngine(sources=[OkSource(), DeadSource()], offline_fallback=False)
    added = eng.refresh()
    assert added == 1                               # 好的入池，荐股被闸，挂的跳过
    assert eng.pool[0]["source"] == "测试源"
    dead = TopicEngine(sources=[DeadSource()], offline_fallback=False)
    assert dead.refresh() == 0 and dead.suggest() is None   # 全挂：不硬聊


def test_categorize_titles():
    """标题规则分类：未知归「生活」。"""
    assert categorize("橘猫把主人的键盘踩出了猫爪印") == "搞笑"
    assert categorize("这条街的早餐摊火了") == "美食"
    assert categorize("某游戏版本更新后玩家乐疯了") == "游戏"
    assert categorize("研究发现蜂蜜不会变质") == "冷知识"
    assert categorize("今天天气不错") == "生活"
