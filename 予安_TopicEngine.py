# -*- coding: utf-8 -*-
"""予安 · 趣味话题引擎 TopicEngine（§16.3 生产版）
====================================================

无聊时的「热点谈资」线。与骨架版的三点本质区别：

1. **不问「想聊这个吗」**——伴侣不递卡片等确认，直接把热点当谈资说出来
   （LLM 口语转述，失败用模板兜底）；用户接没接话，从 TA 的**下一句反应**
   判断：接话/追问 → 愿聊；「换」「没意思」→ 换题；「不感兴趣」→ 屏蔽类别；
   沉默敷衍 → 自然收住，不追问不复读。
2. **热点来自真实数据源**——`TopicSource` 适配器协议，接新平台 = 加一个适配器
   （内置微博热搜公开接口与通用 HTTP JSON 适配器）；内置种子池只是
   **离线兜底**（源全挂时仍能聊，但永远优先真实热点）。
3. **LLM 走 API**——转述与反应分类交给 OpenAI 兼容协议的开源大模型
   （Qwen/DeepSeek/GLM，见 予安_LLM.py）；未配 key 或调用失败一律规则兜底，
   陪伴永不因模型掉线而中断。

其余 §16.3 原则不变：合规闸（敏感词/广告软推/争议类别，入池即拦）、
换题限额（MAX_SWITCHES=2，由 StateEngine 执行）、口味记忆（愿聊 +1、
跳过 -0.5 下限 0 不记仇、「不感兴趣」一键屏蔽类别）、口味本地持久化
（数据不出端，只存偏好分，不存任何行为记录）。

自包含，无三方依赖；直接运行打印一场生产口径演示（沙箱内真实源会降级）。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import tempfile
import urllib.request
from urllib.error import URLError

# ────────────────────────── 合规闸（硬规则，§16.3） ──────────────────────────

BANNED_WORDS = ("选举", "站队", "荐股", "涨停", "抄底", "建仓",
                "下单", "优惠", "链接在", "偏方", "包治", "点击领",
                "博彩", "返利", "加微信")
SPICY_TAGS = {"争议", "明星隐私"}          # 争议/隐私类别整类跳过

# ────────────────────────── 反应分类规则（LLM 兜底） ──────────────────────────

ENGAGED_WORDS = ("哈哈", "真的假的", "然后呢", "后来呢", "细节", "展开",
                 "讲讲", "笑死", "我去", "想听", "还有呢", "咋回事",
                 "啥情况", "是吗", "可以啊", "牛", "有点意思", "冲")
SWITCH_WORDS = ("换", "下一个", "没意思", "不好玩", "切了吧", "换话题",
                "跳过", "不聊这个")
BLOCK_WORDS = ("不感兴趣", "别聊这个", "不想听", "别说了", "屏蔽",
               "以后别推")


def classify_by_rules(reply: str) -> str:
    """规则兜底分类：block（显式拒绝）> engaged（接话）> switch（换题）>
    silent（沉默/敷衍/不相干 → 不硬聊，自然收住）。"""
    r = (reply or "").strip()
    if not r:
        return "silent"
    if any(w in r for w in BLOCK_WORDS):
        return "block"
    if any(w in r for w in ENGAGED_WORDS):
        return "engaged"
    if any(w in r for w in SWITCH_WORDS):
        return "switch"
    return "silent"


def categorize(title: str) -> str:
    """热搜标题 → 口味类别（规则分类；未知归「生活」）。"""
    rules = (("搞笑", ("笑", "搞笑", "沙雕", "梗", "猫", "狗", "萌", "宠")),
             ("美食", ("吃", "美食", "菜", "小吃", "外卖", "烤", "火锅", "奶茶", "咖啡", "早餐")),
             ("游戏", ("游戏", "玩家", "版本更新", "副本", "电竞", "手游", "Steam", "steam", "皮肤")),
             ("影视", ("剧", "电影", "影视", "综艺", "开播", "上映", "番", "追更")),
             ("冷知识", ("冷知识", "科普", "研究发现", "为什么", "原理", "真相")))
    for tag, words in rules:
        if any(w in title for w in words):
            return tag
    return "生活"


# ────────────────────────── 数据源适配器（生产接入点） ──────────────────────────

class TopicSource:
    """数据源适配器协议：fetch() → [(标题, 来源平台名)]。接新平台 = 实现它。"""

    name = "source"

    def fetch(self) -> list[tuple[str, str]]:      # pragma: no cover - 协议
        raise NotImplementedError


class WeiboHotSearchSource(TopicSource):
    """微博热搜公开接口（无需鉴权）。字段：data.realtime[].word。"""

    name = "微博热搜"
    URL = "https://weibo.com/ajax/side/hotSearch"

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout

    def fetch(self) -> list[tuple[str, str]]:
        req = urllib.request.Request(self.URL, headers={
            "User-Agent": "Mozilla/5.0 (YuanCompanion/1.0)"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = (data.get("data") or {}).get("realtime") or []
        return [((it.get("word") or it.get("note") or "").strip(), self.name)
                for it in items if (it.get("word") or it.get("note"))]


class HttpJsonSource(TopicSource):
    """通用 HTTP JSON 适配器：配置 URL + 字段路径即可接大多数平台/聚合服务。"""

    def __init__(self, name: str, url: str, list_path: str, title_field: str,
                 headers: dict | None = None, timeout: float = 5.0):
        self.name, self.url, self.list_path = name, url, list_path
        self.title_field, self.headers, self.timeout = title_field, headers or {}, timeout

    def fetch(self) -> list[tuple[str, str]]:
        req = urllib.request.Request(self.url, headers=self.headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for key in self.list_path.split("."):
            data = data[key]
        out = []
        for it in data:
            title = (it.get(self.title_field) if isinstance(it, dict) else str(it)) or ""
            title = title.strip()
            if title:
                out.append((title, self.name))
        return out


class OfflinePoolSource(TopicSource):
    """离线兜底池：真实源全挂时仍能聊（永远排在最后，真实热点优先）。"""

    name = "闲聊"
    SEEDS = [
        "那只网红橘猫又胖了，主人说它最近在减肥但偷吃被抓包",
        "有人拍了凌晨四点的早餐摊，说是城市醒来的第一口热气",
        "纪录片里的冷知识：蜂蜜是唯一不会变坏的食物",
        "一只边牧学会了自己开冰箱，主人在监控里看完全程",
    ]

    def fetch(self) -> list[tuple[str, str]]:
        return [(s, self.name) for s in self.SEEDS]


# ────────────────────────── 引擎本体 ──────────────────────────

class TopicEngine:
    """生产版话题引擎：数据源 → 合规闸 → 口味排序 → LLM 转述 → 反应分类。"""

    MAX_SWITCHES = 2                 # 同一轮最多换 2 次（§16.3，StateEngine 读取执行）

    def __init__(self, sources: list[TopicSource] | None = None, llm=None,
                 seed=None, taste_path: str | None = None,
                 offline_fallback: bool = True):
        self.llm = llm               # 可选：予安_LLM.LLMClient（注入或读 env）
        self.taste_path = taste_path
        self.pool: list[dict] = []
        self._shown: list[str] = []               # 最近已递出的话题指纹（防复读）
        self._switch_count = 0                    # 本轮已换次数（StateEngine 亦会限额）
        self.sources = sources if sources is not None else [WeiboHotSearchSource()]
        if offline_fallback:
            self.sources.append(OfflinePoolSource())

        taste, blocked = {}, set()
        if taste_path and os.path.exists(taste_path):
            try:
                with open(taste_path, "r", encoding="utf-8") as f:
                    disk = json.load(f)
                taste, blocked = disk.get("taste", {}), set(disk.get("blocked", []))
            except (OSError, ValueError):
                pass                              # 坏文件 → 从零记口味，不崩
        self.taste: dict[str, float] = dict(taste)
        self.blocked: set[str] = set(blocked)

        if seed is not None:                      # 测试/离线直灌：(摘要, 类别, 来源)
            for item in seed:
                self.ingest(item)
        elif offline_fallback:                    # 启动即有谈资：兜底池先垫底，
            for title, name in OfflinePoolSource().fetch():   # refresh() 后真实热点优先
                self.ingest((title, categorize(title), name))

    # ── 热点聚合 ──
    def refresh(self) -> int:
        """从各数据源拉热点入池（逐源降级：单源失败不影响其他）。
        返回本次新入池条数。宿主在启动/低频定时调用，不阻塞互动。"""
        added = 0
        for src in self.sources:
            try:
                items = src.fetch()
            except (OSError, URLError, ValueError, KeyError, TimeoutError):
                continue                          # 单源挂了：跳过，不硬聊不报错
            for title, source_name in items:
                if self.ingest((title, categorize(title), source_name)):
                    added += 1
        return added

    def ingest(self, item) -> bool:
        """收一条 (标题, 类别, 来源)；合规闸直接拦掉，返回是否入池。"""
        text, tag, source = item
        if tag in SPICY_TAGS:
            return False
        if any(w in text for w in BANNED_WORDS):
            return False
        fp = hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
        if any(t["fp"] == fp for t in self.pool):
            return False                          # 去重：同一热点不重复入池
        self.pool.append({"id": len(self.pool), "fp": fp, "title": text,
                          "tag": tag, "source": source, "share": None})
        return True

    # ── 谈资递出（不问「想聊吗」，直接说） ──
    def suggest(self) -> dict | None:
        """递一条谈资：按口味优先、不复读、带来源标注；全屏蔽/池空 → None。
        share = LLM 口语转述（缓存），模型不可用 → 模板兜底。"""
        cand = [t for t in self.pool
                if t["tag"] not in self.blocked and t["fp"] not in self._shown]
        if not cand:                              # 池子小转完一圈：允许复现，但不连续
            self._shown.clear()
            cand = [t for t in self.pool if t["tag"] not in self.blocked]
            if not cand:
                return None                       # 全被屏蔽：安静收住，不硬聊
        top = max(self.taste.get(t["tag"], 0.0) for t in cand)
        best = [t for t in cand if self.taste.get(t["tag"], 0.0) == top]
        pick = random.choice(best) if top > 0 and len(best) > 1 else best[0]
        self._shown.append(pick["fp"])
        if len(self._shown) > 2:
            self._shown.pop(0)
        pick["share"] = self._share(pick)
        return pick

    def _share(self, topic: dict) -> str:
        """口语转述：LLM 优先（只发标题，不带任何身份信息），失败模板兜底。"""
        cached = topic.get("share")
        if cached:
            return cached
        if self.llm is not None:
            text = self.llm.chat([
                {"role": "system", "content":
                    "把热点标题转述成跟好朋友闲聊的一句话，40字以内，口语自然，"
                    "像随口分享见闻。禁止提问、禁止出现「想聊」「要不要听」等引导语，"
                    "禁止感叹号连用。直接输出转述内容。"},
                {"role": "user", "content": topic["title"]},
            ], temperature=0.8)
            if text:
                return text[:80]
        return topic["title"]                     # 模板兜底：直接用原标题，前缀由挂接方加

    # ── 用户反应分类（看下一句，不问） ──
    def classify(self, reply: str) -> str:
        """把用户的回复分类为 engaged / switch / block / silent。
        LLM 分类优先（能懂「这也太离谱了吧哈哈哈哈」），规则兜底保底。"""
        if self.llm is not None:
            text = self.llm.chat([
                {"role": "system", "content":
                    "判断用户对朋友刚分享的一条趣闻的反应，只输出一个词：\n"
                    "engaged=接话/追问/表达兴趣；switch=想换话题；"
                    "block=明确不想听这类；silent=敷衍/沉默/答非所问。"},
                {"role": "user", "content": reply or "（没说话）"},
            ], temperature=0.0)
            if text:
                low = text.lower()
                for label in ("engaged", "switch", "block", "silent"):
                    if label in low:
                        return label
        return classify_by_rules(reply)

    # ── 口味记忆（本地持久化，数据不出端） ──
    def feedback(self, topic: dict, liked: bool) -> float:
        """接话 +1 / 跳过 -0.5；下限 0——不感兴趣不等于讨厌，不记仇。"""
        tag = topic["tag"]
        self.taste[tag] = max(0.0, self.taste.get(tag, 0.0) + (1.0 if liked else -0.5))
        self._persist()
        return self.taste[tag]

    def block_tag(self, tag: str) -> None:
        """「不感兴趣」一键屏蔽整个类别。"""
        self.blocked.add(tag)
        self._persist()

    def _persist(self) -> None:
        if not self.taste_path:
            return
        payload = json.dumps({"taste": self.taste, "blocked": sorted(self.blocked)},
                             ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.taste_path) or ".",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, self.taste_path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise


# ────────────────────────── 演示（生产口径） ──────────────────────────

if __name__ == "__main__":
    lines = []
    say = lines.append

    taste_path = os.path.join(os.getcwd(), "topic_taste_demo.json")
    if os.path.exists(taste_path):
        os.remove(taste_path)
    eng = TopicEngine(taste_path=taste_path)      # LLM 未配 env → 全程规则兜底

    say("【热点聚合】refresh() 尝试真实源（沙箱内大概率降级到离线兜底池）")
    added = eng.refresh()
    say(f"  新入池 {added} 条，池共 {len(eng.pool)} 条")

    say("\n【合规闸】")
    ok1 = eng.ingest(("这只猫币下周涨停，点击领优惠", "搞笑", "抖音"))
    ok2 = eng.ingest(("某明星恋情曝光细节全回顾", "明星隐私", "微博热搜"))
    say(f"  广告软推入池={ok1}（应 False），明星隐私入池={ok2}（应 False）")

    say("\n【直接开聊，不问「想聊吗」】")
    for i in range(3):
        t = eng.suggest()
        say(f"  予安：哎，给你说个事：{t['share']}（{t['source']}刷到的）")
        reply = ["哈哈哈真的假的？", "换", "不感兴趣，别聊这个"][i]
        act = eng.classify(reply)
        say(f"  用户：{reply}")
        say(f"    反应={act} → " +
            {"engaged": "接话开聊，口味 +1", "switch": "换下一条（本轮第 2 次）",
             "block": "屏蔽「" + t["tag"] + "」类别 + 换下一条"}[act])
        if act == "engaged":
            eng.feedback(t, liked=True)
        elif act == "switch":
            eng._switch_count += 1
        elif act == "block":
            eng.block_tag(t["tag"])

    say("\n【口味持久化】")
    eng2 = TopicEngine(taste_path=taste_path)
    say(f"  重启后口味：{eng2.taste}（愿聊过的类别优先）")
    say(f"  已屏蔽类别：{sorted(eng2.blocked)}")
    os.remove(taste_path)

    out = os.path.join(os.getcwd(), "topic_demo_out.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("demo written:", out)
