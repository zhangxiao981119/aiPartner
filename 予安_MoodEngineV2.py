# -*- coding: utf-8 -*-
"""
予安 · 全谱情感引擎 MoodEngineV2（§10 基础 + §16.7 细色映射层）

七情基色：joy(喜) anger(怒) sorrow(哀) fun(乐) worry(忧) longing(思) fear(惧)
细色：委屈 / 感动 / 想你 / 愧疚 / 烦躁 / 心疼 / 无奈 / 期待 / 失落 / 吃醋 / 释然……
映射 = 基色向量 × 场景(ctx) × 关系(bond)，snapshot() 直接输出「此刻她有点委屈」级别的表达。

设计要点（对齐 §16.7）：
- 情绪是动态的：有强度、会衰减（负面衰减更慢 = 反刍）、被关系牵动。
- 细色是开放集合：规则表即映射层，加新细色 = 加一条规则，不动引擎。
- 情绪驱动表达：expression() 给出话多话短 / 玩笑开关 / 行为备注。
- 边界不变：重度持续负向或危机由上层（§8.1 护栏）接管，本引擎不判定医疗结论。

自包含，无三方依赖；时钟可注入便于测试。直接运行打印一场演示。
"""
import math
import time

# ── 七情基色 ────────────────────────────────────────────────
BASE_COLORS = ("joy", "anger", "sorrow", "fun", "worry", "longing", "fear")
BASE_ZH = {"joy": "喜", "anger": "怒", "sorrow": "哀", "fun": "乐",
           "worry": "忧", "longing": "思", "fear": "惧"}

# 细色最低强度：低于此不出（避免把噪声说成情绪）
FINE_THRESHOLD = 0.12
# 强度措辞：score ≥ 0.55 非常 / ≥ 0.30 有些 / ≥ 0.12 有点
INTENSITY_STEPS = ((0.55, "非常"), (0.30, "有些"), (0.12, "有点"))

FINE_STEM = {
    "wronged":   "委屈",
    "moved":     "感动",
    "longing":   "想你",
    "guilty":    "愧疚",
    "irritated": "烦躁",
    "tender":    "心疼你",
    "helpless":  "无奈",
    "expect":    "期待",
    "letdown":   "失落",
    "jealous":   "吃醋",
    "relieved":  "释然",
}

# 事件 → 基色增量（observe 的输入词汇表；负值 = 缓解）
EVENT_MAP = {
    "user_cares":     {"joy": 0.5, "fun": 0.2},      # 你对我好
    "praised":        {"joy": 0.35, "fun": 0.25},    # 被你夸
    "user_hurt":      {"sorrow": 0.4, "worry": 0.3},  # 你受伤/被击杀
    "user_loses":     {"sorrow": 0.25},               # 你输了
    "user_wins":      {"joy": 0.4, "fun": 0.3},       # 你赢了
    "cold_treatment": {"anger": 0.35, "sorrow": 0.35},  # 被你冷落
    "separated":      {"longing": 0.4, "sorrow": 0.2},  # 分开了一阵
    "my_fault":       {"sorrow": 0.35},               # 我搞砸了（ctx.self_fault=True）
    "uncertain":      {"worry": 0.35, "joy": 0.2},    # 好事将至但没定
    "rival":          {"anger": 0.3, "fear": 0.25},   # 比较场景（ctx.comparison=True）
    "threat_passed":  {"fear": -0.4, "joy": 0.15},    # 危险过去（释然由惧回落推断）
    "tick":           {},                              # 只推进时间，不注入
}


def _intensity_word(score):
    for th, word in INTENSITY_STEPS:
        if score >= th:
            return word
    return "有点"


class MoodEngineV2:
    """全谱情感引擎：基色评估 → 动态衰减 → 细色映射 → 表达提示。"""

    def __init__(self, personality=None, bond=0.3, now=None):
        self.P = personality or {"O": 0.6, "E": 0.7, "A": 0.8, "C": 0.6, "N": 0.35}
        self.bond = bond
        self.mood = {"valence": 0.2, "arousal": 0.4}
        self.vec = {}          # 当前基色向量
        self.prev = {}         # 上一次事件前的向量（用于「惧回落→释然」这类推断）
        self._now = now or time.time
        self._last = self._now()
        self._ctx = {"target": "user", "source": "user"}   # 最近一次事件的场景（供细色映射）
        self.mem = []          # (事件, 向量快照) 记忆轨迹

    # ── 内部：衰减与心境 ──
    def _decay(self):
        t = self._now()
        dt = min((t - self._last) / 60.0, 10.0)
        for k in list(self.vec):
            lam = 0.10 if k in ("sorrow", "anger", "fear") else 0.18  # 负向更慢=反刍
            self.vec[k] *= math.exp(-lam * dt)
            if self.vec[k] < 0.02:
                del self.vec[k]
        self.mood["valence"] += (0.2 - self.mood["valence"]) * 0.02 * dt
        self._last = t

    def _recompute(self):
        pos = self.vec.get("joy", 0) + self.vec.get("fun", 0)
        neg = sum(v for k, v in self.vec.items() if k not in ("joy", "fun"))
        self.mood["valence"] = max(-1.0, min(1.0, 0.2 + pos - neg)) if (pos or neg) else 0.2
        self.mood["arousal"] = min(0.95, 0.3 + 0.5 * max(self.vec.values(), default=0))

    def _bias(self, sign):
        return 1 + 0.3 * self.P["E"] if sign > 0 else 1 + 0.4 * self.P["N"]

    # ── 输入接口 ──
    def feel(self, base, amount):
        """直接注入/缓解某个基色。amount>0 增强，<0 缓解。"""
        if base not in BASE_COLORS:
            raise ValueError(f"未知基色：{base}")
        if amount >= 0:
            self.vec[base] = min(1.0, self.vec.get(base, 0.0) + amount)
        else:
            v = self.vec.get(base, 0.0) + amount
            if v <= 0.02:
                self.vec.pop(base, None)
            else:
                self.vec[base] = v
        self._recompute()
        return self.snapshot()

    def observe(self, event, ctx=None):
        """事件驱动的评估入口（推荐用 EVENT_MAP 里的词，未注册事件=无增量只推进时间）。"""
        self.prev = dict(self.vec)                 # 事件前快照（衰减前，用于「惧回落→释然」）
        self._decay()
        ctx = {"target": "user", "source": "user", **(ctx or {})}
        if event == "separated":
            ctx["farewell"] = True                 # 告别时刻：思念优先开口（§16.7）
        self._ctx = ctx
        for k, v in EVENT_MAP.get(event, {}).items():
            self._apply(k, v, ctx)
        if event == "user_cares":                  # 你对我好：关系变厚（§2.5 依恋）
            self.bond = min(1.0, self.bond + 0.01)
        self._recompute()
        self.mem.append((event, dict(self.vec)))
        return self.snapshot()

    def appraise(self, goal=0.0, standard=0.0, attitude=0.0, label=None, ctx=None):
        """§10 兼容口径：目标/标准/态度三维评估。pride→joy、content→fun 归入七情。"""
        self.prev = dict(self.vec)                 # 评估前快照（衰减前）
        self._decay()
        ctx = {"target": "user", "source": "user", **(ctx or {})}
        self._ctx = ctx
        if goal > 0:
            self._apply("joy", 0.6 * goal * self._bias(1), ctx)
        elif goal < 0:
            self._apply("sorrow", 0.5 * (-goal) * self._bias(-1), ctx)
        if standard > 0:
            self._apply("joy", 0.4 * standard, ctx)
        elif standard < 0:
            self._apply("anger", 0.4 * (-standard), ctx)
        if attitude > 0:
            self._apply("fun", 0.3 * attitude, ctx)
            self.bond = min(1.0, self.bond + 0.01 * attitude)
        self._recompute()
        self.mem.append((label or "appraise", dict(self.vec)))
        return self.snapshot()

    def _apply(self, base, delta, ctx):
        if delta > 0:
            self.vec[base] = min(1.0, self.vec.get(base, 0.0) + delta * self._bias(1 if base in ("joy", "fun") else -1))
        else:
            self.feel(base, delta)

    # ── 细色映射（§16.7：基色 × 场景 × 关系）──
    def _fine_rules(self, vec, ctx, bond, prev):
        g = lambda k: vec.get(k, 0.0)
        gp = lambda k: prev.get(k, 0.0)
        target_user = ctx.get("target", "user") == "user"
        from_user = ctx.get("source", "user") == "user"
        rules = []
        # 释然：惧明显回落且转好
        if gp("fear") > 0.30 and g("fear") < 0.15 and g("joy") > 0.05:
            rules.append(("relieved", gp("fear") - g("fear")))
        # 委屈：哀+怒并发、指向你、关系够近
        if g("sorrow") > 0.15 and g("anger") > 0.15 and target_user and bond >= 0.4:
            rules.append(("wronged", min(g("sorrow"), g("anger")) * min(1.0, bond + 0.2)))
        # 感动：喜/乐强、来自你、关系深
        if max(g("joy"), g("fun")) > 0.30 and from_user and bond >= 0.6:
            rules.append(("moved", max(g("joy"), g("fun")) * 0.9))
        # 想你：思+哀并存
        if g("longing") > 0.20 and g("sorrow") > 0.10:
            rules.append(("longing", min(g("longing"), g("sorrow") + 0.25)))
        # 愧疚：我做错了且哀
        if ctx.get("self_fault") and g("sorrow") > 0.15:
            rules.append(("guilty", g("sorrow")))
        # 烦躁：怒低烧（纯怒，不带惧）
        if 0.10 < g("anger") < 0.35 and g("sorrow") < 0.15 and g("fear") < 0.10:
            rules.append(("irritated", g("anger")))
        # 心疼你：忧+哀 都指向你
        if g("worry") > 0.15 and g("sorrow") > 0.15 and target_user:
            rules.append(("tender", min(g("worry"), g("sorrow")) + 0.05))
        # 无奈：哀为主、怒很低、非己过、无指向你的担忧（有担忧时优先「心疼」）；
        # 告别时刻（farewell）让位给「想你」——无奈是对局面的情绪，不抢告别的话
        if (g("sorrow") > 0.20 and g("anger") < 0.10 and not ctx.get("self_fault")
                and not ctx.get("farewell")
                and not (target_user and g("worry") > 0.15)):
            rules.append(("helpless", g("sorrow") * 0.8))
        # 期待：喜+忧并存（好事将至）
        if g("joy") > 0.20 and g("worry") > 0.15:
            rules.append(("expect", min(g("joy"), g("worry") + 0.2)))
        # 失落：哀、无乐无怒、非己过、无指向你的担忧；告别时刻同样让位给「想你」
        if (g("sorrow") > 0.15 and g("fun") < 0.08 and g("anger") < 0.10
                and not ctx.get("self_fault") and not ctx.get("farewell")
                and not (target_user and g("worry") > 0.15)):
            rules.append(("letdown", g("sorrow") * 0.7))
        # 吃醋：怒+惧并存且是比较场景
        if g("anger") > 0.15 and g("fear") > 0.15 and ctx.get("comparison"):
            rules.append(("jealous", min(g("anger"), g("fear")) + 0.05))
        return rules

    def _fine(self, ctx):
        if not self.vec:
            return None
        rules = self._fine_rules(self.vec, ctx, self.bond, self.prev)
        if not rules:
            return None
        name, score = max(rules, key=lambda r: r[1])
        if score < FINE_THRESHOLD:
            return None
        return {"name": name, "phrase": _intensity_word(score) + FINE_STEM[name],
                "score": round(score, 2)}

    # ── 表达提示（§16.7：情绪驱动表达）──
    def _expression(self, dom):
        if dom in ("joy", "fun"):
            return {"pace": "话多", "jokes": True, "note": "爱玩梗，主动找话题"}
        if dom == "anger":
            return {"pace": "话短", "jokes": False, "note": "不伤人，但你能感觉到"}
        if dom in ("worry", "fear"):
            return {"pace": "唠叨", "jokes": False, "note": "嘴上叮嘱，行动上递关心"}
        if dom == "sorrow":
            return {"pace": "轻声", "jokes": False, "note": "收起玩笑，陪你安静待着"}
        if dom == "longing":
            return {"pace": "正常", "jokes": True, "note": "会找借口跟你多说两句"}
        return {"pace": "正常", "jokes": True, "note": "平常心"}

    # ── 输出 ──
    def snapshot(self, ctx=None):
        if ctx is None:
            ctx = self._ctx
        ctx = {"target": "user", "source": "user", **(ctx or {})}
        dom = max(self.vec, key=self.vec.get) if self.vec else "neutral"
        fine = self._fine(ctx)
        return {"dominant": dom, "dominant_zh": BASE_ZH.get(dom, "平静"),
                "vector": dict(self.vec), "mood": self.mood, "bond": round(self.bond, 2),
                "fine": fine, "expression": self._expression(dom)}

    def describe(self, ctx=None):
        """一句话输出：可直接放进对话语气选择，如「此刻她有点委屈」。"""
        s = self.snapshot(ctx)
        if s["fine"]:
            return f"此刻她{s['fine']['phrase']}"
        if s["dominant"] == "neutral":
            return "此刻她心绪平静"
        return f"此刻她{_intensity_word(s['vector'][s['dominant']])}{BASE_ZH[s['dominant']]}着"


if __name__ == "__main__":
    clock = {"t": 1000.0}
    eng = MoodEngineV2(bond=0.62, now=lambda: clock["t"])

    def show(title, snap):
        fine = snap["fine"]["phrase"] if snap["fine"] else "—"
        expr = snap["expression"]
        print(f"{title:<14} 主导={snap['dominant_zh']}  细色={fine}  "
              f"表达={expr['pace']}/玩笑{'开' if expr['jokes'] else '关'}  "
              f"→ {eng.describe()}")

    def scene(title, fn):
        clock["t"] += 600                       # 每幕推进 10 分钟，让上一幕自然衰减
        show(title, fn())

    print("── 全谱情感演示（§16.7）──")
    show("你对我好", eng.observe("user_cares"))
    scene("被冷落", lambda: eng.observe("cold_treatment"))
    scene("分开一阵", lambda: eng.observe("separated"))
    scene("你被击杀", lambda: eng.observe("user_hurt"))
    eng.feel("fear", 0.5)
    scene("危险过去", lambda: eng.observe("threat_passed"))
    scene("我搞砸了", lambda: eng.observe("my_fault", {"self_fault": True}))
    scene("有比较对象", lambda: eng.observe("rival", {"comparison": True}))
    scene("好事没定", lambda: eng.observe("uncertain"))
