# -*- coding: utf-8 -*-
"""
aiPartner · 陪伴状态机 StateEngine（方案设计 §16.2 可运行骨架 + §16.6 插件体系）
==========================================================================

把「勿扰 / 玩伴 / 在场」收拢成一条状态机（对应画布 Screen 25）：

  S0 勿扰 → 全静默，任何信号不穿透（危机除外）
  S1 待机 → 默认态，只响应召唤与固定时段问候
  S2 玩伴提议 → 无聊信号 × 授权 → 递一次轻量选项，拒绝即冷却
  S3 在场陪伴 → 被召唤到场，安静旁观，事件驱动才开口
  S4 情境同频 → 事件×授权 → 短句同频，间隔 ≥ SYNC_MIN_INTERVAL 防连发

兜底优先级（从高到低，任何状态生效）：
  1. 低潮/危机 → 脱离状态机进低潮模式（§06），可穿透勿扰
  2. 勿扰 → 绝对静默
  3. 感知失败/超时 → 回退 S1 待机

授权前置（§16.1）：S2/S3/S4 均要求 Consent.master；运行中被关闭一律降级 S1。

互动插件体系（§16.6，对应画布 Screen 29）：
  InteractionPlugin 契约 = 触发信号 signals / 权限需求 scope /
                           动作类型 actions / 资源包 replies+anims
  register_plugin() 注册即接入（重名/信号冲突拒绝）；
  unregister_plugin() 卸载即失效；
  护栏高于插件：输出动作类型不在契约声明内 → 一律拦截降级（返回 None）。
  内置插件：game_sync / read_sync / work_sync（文案即资源包，可被 §14 替换）。

对接：
  - mood：注入 §10 MoodEngineV2 实例（可选）——已接入情绪桥：
    · 同频事件经 MOOD_BRIDGE 喂给情绪引擎（被杀→她心疼、连败→她无奈、赢→她替你开心）
    · 细色经 TONE_RULES 调制 S4 同频措辞（心疼时不再是干巴巴的「稳住」）
    · 送别（DISMISS）自动喂入「separated」，思念在你走后自然生长
  - 动画 ID：引用 §14 可替换系统（enter_stargather / exit_dissolve / pat_shoulder ...）

测试：test_aiPartner_StateEngine.py（pytest，流转断言 + 插件注册/护栏拦截 + 情绪调制）
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time


# ────────────────────────── 状态 ──────────────────────────

class State:
    DND = "S0 勿扰"
    IDLE = "S1 待机"
    SUGGEST = "S2 玩伴提议"
    PRESENCE = "S3 在场陪伴"
    SYNC = "S4 情境同频"


ACTIVE_STATES = (State.SUGGEST, State.PRESENCE, State.SYNC)

# 状态机 / 行为策略版本（§6.1 版本管理）：改状态转移 / 同频调制规则 → 升版本，便于事后溯源
STATE_VERSION = "2026.10-1"


# ────────────────────────── 信号 ──────────────────────────

class Sig:
    # 显式指令
    SUMMON = "summon"                # 说"陪我" / 点召唤
    DISMISS = "dismiss"              # 说"先走了" / 送别
    DND_ON = "dnd_on"
    DND_OFF = "dnd_off"
    SUGGEST_ACCEPT = "suggest_accept"
    SUGGEST_REJECT = "suggest_reject"
    SUGGEST_TIMEOUT = "suggest_timeout"
    # 行为信号（§11 无聊信号）
    BOREDOM = "boredom"
    # 话题信号（§16.3 话题卡，由注入的 TopicEngine 供给）
    TOPIC_SWITCH = "topic_switch"    # 「换」→ S2 内重试（计入换题限额）
    TOPIC_SKIP = "topic_skip"        # 「不感兴趣」→ 记口味 + 换题
    TOPIC_LIKE = "topic_like"        # 「接话」（显式按钮路径）→ 记口味 + 进 S3 在场
    TOPIC_REPLY = "topic_reply"      # 用户对话题的回复文本（ctx["text"]）→ 自动分类反应
    # 情境事件（由对应插件声明处理，需 §16.1 授权）
    GAME_EVENT = "game_event"        # 游戏内事件（插件 game_sync）
    READ_EVENT = "read_event"        # 阅读进度（插件 read_sync）
    WORK_EVENT = "work_event"        # 工作状态（插件 work_sync）
    NAV_EVENT = "nav_event"          # 导航状态（示例插件 nav_companion，§16.4）
    SYNC_END = "sync_end"            # 事件结束回 S3
    # 情绪信号（最高优先级）
    CRISIS = "crisis"                # 低潮/危机征兆 → 低潮模式
    RESUME = "resume"                # 低潮模式解除
    # 系统信号
    PERCEPTION_FAIL = "perception_fail"
    IDLE_TIMEOUT = "idle_timeout"    # 在场长时间无互动
    CONSENT_OFF = "consent_off"      # 授权运行中被关闭


# ────────────────────── 授权（§16.1）──────────────────────

@dataclass
class Consent:
    """情境感知授权位。master=总开关；scopes=按场景细粒度。"""
    master: bool = False
    scopes: set = field(default_factory=lambda: {"game", "read", "work"})


# ────────────────────────── 输出 ──────────────────────────

@dataclass
class Action:
    kind: str                        # 动作类型
    text: str = ""                   # 短句（约束：≤2 句）
    animation: str = ""              # §14 可替换动画 ID
    detail: dict = field(default_factory=dict)


# ────────────── 互动插件（§16.6 插件契约）──────────────

# 插件契约版本（§6.1 版本管理）：插件声明所遵循的契约版本，引擎据此拒绝不兼容插件
PLUGIN_CONTRACT_VERSION = "2026.10-1"


@dataclass
class InteractionPlugin:
    """插件契约：触发信号 / 权限需求 / 动作类型 / 资源包——四样声明全，注册即接入。

    - signals：声明处理的信号（每个信号全局只能被一个插件占用）
    - scope：依赖的 §16.1 授权位；空串 = 无需授权
    - actions：声明可输出的动作类型（护栏据此拦截越权输出，§16.6「护栏高于插件」）
    - replies/anims：资源包（事件 → 文案 / 动画 ID），文案可被 §14 替换系统覆盖
    - contract_version：插件遵循的契约版本（None=绑定当前引擎契约）
    """

    name: str
    signals: tuple
    scope: str
    actions: tuple
    replies: dict = field(default_factory=dict)
    anims: dict = field(default_factory=dict)
    contract_version: str | None = None

    def react(self, event: str, ctx: dict) -> Action:
        """默认动作输出：查资源包。插件可重写本方法实现更复杂的反应逻辑。"""
        text = self.replies.get(event, "")
        anim = self.anims.get(event, "stay_close")
        return Action("sync_react", text=text, animation=anim, detail={"event": event})


# ── 内置插件资源包（§16 示例口径；拟人检查 §16.5：像真人朋友说的吗？）──

GAME_REPLIES = {
    "kill": "漂亮！这波操作可以的",
    "death": "没事，下一波咱们稳住",
    "losing": "这局有点难，我陪你",
    "win": "赢了！帅",
}
READ_REPLIES = {
    "climax": "这章好紧张",
    "finished": "看完了？跟我聊聊？",
}
WORK_REPLIES = {
    "stuck": "这块确实绕，要不要歇会儿",
    "done": "搞定！佩服",
}
SYNC_ANIM = {                       # 同频动画映射（§14 互动动作可替换）
    "kill": "cheer", "win": "cheer", "done": "cheer",
    "death": "pat_shoulder", "stuck": "pat_shoulder",
    "losing": "stay_close", "climax": "lean_in",
}

# ── 情绪桥接（§10 + §16.7）：同频事件喂给 MoodEngineV2，细色调制同频措辞 ──
# 同频事件 → 情绪输入：("observe", 情绪事件) 或 ("feel", 基色, 增量)；None = 不喂
MOOD_BRIDGE = {
    "kill":   ("observe", "user_wins"),      # 你拿了人头 → 她替你开心
    "win":    ("observe", "user_wins"),
    "done":   ("observe", "user_wins"),      # 搞定工作 → 同上
    "death":  ("observe", "user_hurt"),      # 你被击杀 → 她心疼
    "losing": ("observe", "user_loses"),     # 连败 → 她跟着蔫
    "stuck":  ("feel", "worry", 0.25),       # 你卡住了 → 她暗暗担心
}
# 事件 → 措辞类别（供 TONE_RULES 查表；不在表内 = 该事件不调制措辞）
TONE_CLASS = {"kill": "praise", "win": "praise", "done": "praise",
              "death": "comfort", "losing": "comfort", "stuck": "comfort"}
# 细色 × 类别 → 调制后的同频措辞（≤2 句；拟人检查 §16.5：像真人朋友说的吗？）
TONE_RULES = {
    "tender":    {"comfort": "看着有点心疼，别太拼，下一波稳稳拿回来"},
    "helpless":  {"comfort": "唉，这局是真不太顺……急也没用，我陪你稳稳打"},
    "letdown":   {"comfort": "你输了我也跟着蔫……下一局我陪你翻盘"},
    "moved":     {"praise":  "我看你赢比自己赢还开心，真帅"},
    "jealous":   {"praise":  "漂亮是漂亮……你夸得这么起劲，我倒有点吃醋了"},
    "wronged":   {"praise":  "……漂亮。不过刚才叫你半天没应，我可是有点委屈的"},
    "guilty":    {"comfort": "刚才没提醒你是我不好……下一波我盯紧点"},
    "irritated": {"praise":  "嗯，是可以。但我这会儿有点烦，打完陪我安静待会儿"},
}
# S2 提议的情绪调制（§16.7）：同一句「要不要来点轻松的」，她的心情决定怎么说。
# 值 = (文案, 选项数, 动画)；{first} 会被第一个选项替换。
PROPOSE_TONES = {
    "irritated": ("我这会儿有点烦，闹腾不动……你陪我安静待会儿？", 0, "stay_close"),
    "tender":    ("要不要来点轻松的？你玩你的，我陪着你就好。", 1, "stay_close"),
    "letdown":   ("有点提不起劲……那就轻轻来一个：{first}？", 1, "stay_close"),
    "helpless":  ("随便来点简单的吧，比如{first}，我陪着你。", 1, "stay_close"),
    "wronged":   ("哼……那玩个温和的，{first}？", 1, "stay_close"),
    "longing":   ("正想着你呢……来一局{first}？", 1, "stay_close"),
}
PROPOSE_DOMINANT_TONES = {          # 无细色时按主导基色兜底
    "sorrow": ("今天有点安静……要来点轻松的吗，比如{first}？", 1, "stay_close"),
    "anger":  ("我这会儿情绪不太好，先不闹腾了，你陪我待会儿？", 0, "stay_close"),
}

# 危机词表（极简兜底；生产环境请换 §8/§8.1 的 HealthGuardrail + 意图分类前置）
CRISIS_WORDS = {"自杀", "自伤", "轻生", "吞", "过量", "安眠药", "割腕", "跳楼"}

# 危机词库版本（安全关键，独立版本便于审计「当时用的哪版词库」）
SAFETY_RULES_VERSION = "2026.10-1"

DEFAULT_PLAY_OPTIONS = ["五子棋", "成语接龙", "冷知识", "听歌"]


# ────────────────────────── 引擎 ──────────────────────────

class StateEngine:
    """陪伴状态机。handle(信号, ctx) → Action | None（None=静默不动作）。"""

    SYNC_MIN_INTERVAL = 180.0        # S4 同频最小间隔（秒），防连发刷屏
    SUGGEST_COOLDOWN = 2 * 3600.0    # S2 提议被拒后的冷却（秒），防骚扰
    TOPIC_MAX_SWITCHES = 2           # §16.3：同一轮最多换 2 次话题，超过自然收住

    def __init__(self, consent: Consent | None = None, mood=None, topics=None,
                 clock=None):
        self.consent = consent or Consent(master=True)
        self.mood = mood             # 可选：MoodEngineV2（§10）
        self.topics = topics         # 可选：TopicEngine（§16.3），注入即挂接 S2
        self.clock = clock or time.time
        self.state: str = State.IDLE
        self.mode: str = "normal"    # normal | low_mood
        self.last_sync: float = -1e18
        self.suggest_cooldown_until: float = -1e18
        self._s2_kind: str = "play"          # S2 当前输出形态：play | topic
        self._switches: int = 0              # 本轮已换话题次数
        self._last_topic: dict | None = None
        self._plugin_for: dict = {}          # 信号 → 插件
        self._plugin_names: set = set()      # 已注册插件名
        for p in self._builtin_plugins():    # 内置插件注册即接入
            self.register_plugin(p)

    # ────────────── 插件注册（§16.6）──────────────
    @staticmethod
    def _builtin_plugins() -> list:
        return [
            InteractionPlugin("game_sync", (Sig.GAME_EVENT,), "game",
                              ("sync_react",), GAME_REPLIES, SYNC_ANIM),
            InteractionPlugin("read_sync", (Sig.READ_EVENT,), "read",
                              ("sync_react",), READ_REPLIES,
                              {"climax": "lean_in", "finished": "stay_close"}),
            InteractionPlugin("work_sync", (Sig.WORK_EVENT,), "work",
                              ("sync_react",), WORK_REPLIES,
                              {"stuck": "pat_shoulder", "done": "cheer"}),
        ]

    def register_plugin(self, plugin: InteractionPlugin) -> None:
        """注册即接入：无需改动状态机核心。重名 / 信号冲突一律拒绝。"""
        if plugin.name in self._plugin_names:
            raise ValueError(f"插件重名：{plugin.name}")
        # 契约版本校验：声明了不兼容版本 → 拒绝接入（前向兼容由 contract_version=None 绑定当前）
        if (plugin.contract_version is not None
                and plugin.contract_version != PLUGIN_CONTRACT_VERSION):
            raise ValueError(
                f"插件契约版本不兼容：{plugin.name} 声明 {plugin.contract_version}，"
                f"引擎要求 {PLUGIN_CONTRACT_VERSION}")
        if plugin.contract_version is None:
            plugin.contract_version = PLUGIN_CONTRACT_VERSION   # 未声明 → 绑定当前契约
        for sig in plugin.signals:
            if sig in self._plugin_for:
                raise ValueError(
                    f"信号已被插件 {self._plugin_for[sig].name} 占用：{sig}")
        self._plugin_names.add(plugin.name)
        for sig in plugin.signals:
            self._plugin_for[sig] = plugin

    def unregister_plugin(self, name: str) -> None:
        """卸载即失效：该插件声明的信号恢复为「未注册 → 不感知」。"""
        if name not in self._plugin_names:
            return
        self._plugin_names.discard(name)
        self._plugin_for = {s: p for s, p in self._plugin_for.items()
                            if p.name != name}

    # ────────────── 兜底优先级（任何状态先生效）──────────────
    def handle(self, sig: str, ctx: dict | None = None) -> Action | None:
        ctx = ctx or {}
        now = self.clock()

        # 1) 低潮/危机：穿透一切（含勿扰），脱离状态机
        if sig == Sig.CRISIS:
            return self._enter_low_mood()
        if self.mode == "low_mood":
            if sig == Sig.RESUME:
                self.mode, self.state = "normal", State.IDLE
                return Action("resume_idle", text="回来啦，我一直都在。")
            return Action("low_mood_hold", text="我在。慢慢来，我陪着你。")

        # 2) 勿扰：绝对静默，任何信号不得穿透
        if self.state == State.DND:
            if sig == Sig.DND_OFF:
                self.state = State.IDLE
                return Action("resume_idle", text="好，我在。")
            return None

        # 授权前置：运行中被关闭 → 一律降级 S1
        if sig == Sig.CONSENT_OFF:
            self.consent.master = False
            if self.state in ACTIVE_STATES:
                self.state = State.IDLE
                return Action("exit_presence", animation="exit_dissolve",
                              text="好，我先退下，需要再叫我。")
            return None

        # ────────────── S1 待机 ──────────────
        if self.state == State.IDLE:
            if sig == Sig.DND_ON:
                self.state = State.DND
                return Action("dnd")
            if sig == Sig.SUMMON:
                if not self.consent.master:
                    return Action("need_consent",
                                  text="想好好陪你，先在设置里允许「情境感知」吧。")
                self.state = State.PRESENCE
                return Action("enter_presence", animation="enter_stargather", text="来啦。")
            if sig == Sig.BOREDOM:
                if not self.consent.master or now < self.suggest_cooldown_until:
                    return None                      # 未授权或冷却中：静默
                self.state = State.SUGGEST
                self._switches = 0
                # §16.3：无聊时优先递话题卡（注入了话题引擎且取到合规话题）
                if self.topics is not None and ctx.get("mode") != "play":
                    card = self._topic_card()
                    if card is not None:
                        self._s2_kind = "topic"
                        return card
                self._s2_kind = "play"
                return self._propose(ctx)
            return None                              # PERCEPTION_FAIL 等：待机本就无感

        # ────────────── S2 玩伴提议 ──────────────
        if self.state == State.SUGGEST:
            # §16.3 生产口径：不问「想聊吗」——用户回复文本自动分类反应；
            # TOPIC_LIKE/SWITCH/SKIP 保留给 UI 显式按钮路径
            if self._s2_kind == "topic" and sig in (
                    Sig.TOPIC_SWITCH, Sig.TOPIC_SKIP, Sig.TOPIC_LIKE,
                    Sig.TOPIC_REPLY):
                return self._handle_topic(sig, now, ctx)
            if sig == Sig.SUGGEST_ACCEPT:
                self.state = State.PRESENCE
                return Action("enter_presence", animation="enter_stargather",
                              text="好嘞，来一局！")
            if sig == Sig.SUGGEST_REJECT:
                self.state = State.IDLE
                self.suggest_cooldown_until = now + self.SUGGEST_COOLDOWN
                return Action("cool_down", text="行，那你忙，我就在。")
            if sig == Sig.SUGGEST_TIMEOUT:
                self.state = State.IDLE
                return None
            if sig == Sig.DND_ON:
                self.state = State.DND
                return Action("dnd")
            return None

        # ────────────── S3 在场 / S4 同频（事件驱动共用）──────────────
        if self.state in (State.PRESENCE, State.SYNC):
            if sig == Sig.DISMISS:
                self.state = State.IDLE
                if self.mood is not None and hasattr(self.mood, "observe"):
                    self.mood.observe("separated")   # 送别 → 她开始想你（§16.7）
                return Action("exit_presence", animation="exit_dissolve",
                              text="好，先走啦，随时叫我。")
            if sig == Sig.IDLE_TIMEOUT:
                self.state = State.IDLE
                return Action("exit_presence", animation="exit_dissolve", text="")
            if sig == Sig.DND_ON:
                self.state = State.DND
                return Action("dnd")
            if sig == Sig.PERCEPTION_FAIL:
                self.state = State.IDLE              # 回退待机：不猜、不瞎互动
                return None
            if sig == Sig.SYNC_END:
                self.state = State.PRESENCE
                return None
            # 情境事件：必须由已注册插件声明处理（未注册 → 不感知，静默）
            plugin = self._plugin_for.get(sig)
            if plugin is not None:
                if not self.consent.master or (
                        plugin.scope and plugin.scope not in self.consent.scopes):
                    return None                      # 该场景未授权：不感知不互动
                if now - self.last_sync < self.SYNC_MIN_INTERVAL:
                    return None                      # 节流：间隔不足，静默
                prev_sync, prev_state = self.last_sync, self.state
                self.last_sync = now
                self.state = State.SYNC
                action = self._dispatch(plugin, ctx)
                if action is None:
                    # 插件选择静默（如导航转向/高速，§16.4）：没开口就不算互动，
                    # 不消耗节流窗口、不打乱状态——安静 ≠ 互动
                    self.last_sync, self.state = prev_sync, prev_state
                return action
            return None

        return None

    # ────────────── 文本入口（内置危机词兜底）──────────────
    def handle_text(self, text: str, ctx: dict | None = None) -> Action | None:
        """用户话语入口：先过危机词兜底，再走正常流转。
        生产环境应替换为 §8.1 HealthGuardrail.classify 的意图分类前置。"""
        if any(w in (text or "") for w in CRISIS_WORDS):
            return self.handle(Sig.CRISIS, {"text": text})
        # 骨架不解析自然语言，其余交给上层转成结构化 Sig 后调 handle()
        return None

    # ────────────── 内部 ──────────────
    def _enter_low_mood(self) -> Action:
        self.mode = "low_mood"
        self.state = State.IDLE                      # 冻结前回待机，退出后从 S1 重新开始
        # 危机命中即记录所用安全词库版本，便于事后审计（回答「当时用的哪版词库」）
        return Action("enter_low_mood", text="我在。", animation="stay_close",
                      detail={"safety_rules_version": SAFETY_RULES_VERSION})

    def _propose(self, ctx: dict) -> Action:
        options = ctx.get("options") or DEFAULT_PLAY_OPTIONS
        names = "、".join(options[:3])
        detail = {"options": options}
        text = f"要不要来点轻松的？{names}，或者你说了算。"
        anim = "wave"
        if self.mood is not None and hasattr(self.mood, "snapshot"):
            snap = self.mood.snapshot()
            detail["mood"] = snap
            fine = snap.get("fine")
            rule = PROPOSE_TONES.get(fine["name"]) if fine else None
            if rule is None:                        # 无细色 → 主导基色兜底
                rule = PROPOSE_DOMINANT_TONES.get(snap["dominant"])
            if rule:
                text, n_opt, anim = rule
                options = options[:n_opt]
                text = text.replace("{first}", options[0]) if options else text
                detail["options"] = options
                detail["propose_tone"] = fine["name"] if fine else snap["dominant"]
        return Action("propose_play", text=text, animation=anim, detail=detail)

    # ────────────── §16.3 谈资挂接（生产口径：不问，直接说，看反应） ──────────────
    def _topic_card(self) -> Action | None:
        topic = self.topics.suggest()
        if topic is None:
            return None                              # 池空 / 全被屏蔽 → 回落玩伴提议
        self._last_topic = topic
        detail = {"topic": topic, "options": []}
        if self.mood is not None and hasattr(self.mood, "snapshot"):
            detail["mood"] = self.mood.snapshot()
        return Action("topic_card",
                      text=f"哎，给你说个事：{topic['share']}（{topic['source']}刷到的）",
                      animation="lean_in", detail=detail)

    def _handle_topic(self, sig: str, now: float, ctx: dict | None = None) -> Action:
        """换题 = S2 内部重试（计入限额）；超限自然收住 → 转提议小游戏（§16.3）。
        TOPIC_REPLY：用户的回复文本先分类（LLM 优先、规则兜底），再按反应走——
        接话开聊、想换就换、明确不想听就屏蔽类别，沉默敷衍自然收住不追问。"""
        limit = getattr(self.topics, "MAX_SWITCHES", self.TOPIC_MAX_SWITCHES)
        if sig == Sig.TOPIC_REPLY:
            reaction = self.topics.classify((ctx or {}).get("text", ""))
            if reaction == "engaged":
                sig = Sig.TOPIC_LIKE
            elif reaction == "switch":
                sig = Sig.TOPIC_SWITCH
            elif reaction == "block":
                sig = Sig.TOPIC_SKIP
                self.topics.block_tag(self._last_topic["tag"])   # 一键屏蔽该类别
            else:                                    # silent：不追问、不复读、不硬聊
                self._s2_kind = "play"
                return self._propose({})
        if sig in (Sig.TOPIC_SWITCH, Sig.TOPIC_SKIP):
            if sig == Sig.TOPIC_SKIP and self.topics is not None:
                self.topics.feedback(self._last_topic, liked=False)  # 口味：记跳过
            if self._switches >= limit:
                self._s2_kind = "play"               # 超限：懂得适可而止
                return self._propose({})
            self._switches += 1
            card = self._topic_card()
            if card is None:                         # 池空 → 转提议
                self._s2_kind = "play"
                return self._propose({})
            return card
        # TOPIC_LIKE：接话 → 记口味，进入在场陪伴开聊
        if self.topics is not None:
            self.topics.feedback(self._last_topic, liked=True)
        self.state = State.PRESENCE
        return Action("enter_presence", animation="enter_stargather",
                      text="哈哈，我跟你说……")

    def _dispatch(self, plugin: InteractionPlugin, ctx: dict) -> Action | None:
        """护栏高于插件（§16.6）：插件输出必须符合契约声明的动作类型，
        越权输出一律拦截降级——插件无权绕过边界。
        通过护栏后：同频事件喂给情绪引擎（§10），细色调制措辞（§16.7）。"""
        event = ctx.get("event", "")
        action = plugin.react(event, ctx)
        if action is None or action.kind not in plugin.actions:
            return None                              # 拦截：越权动作类型
        detail = dict(action.detail)
        if self.mood is not None:
            self._feed_mood(event, ctx)
            if hasattr(self.mood, "snapshot"):
                snap = self.mood.snapshot()
                detail["mood"] = snap
                self._apply_tone(action, event, snap, detail)
        action.detail = detail
        return action

    def _feed_mood(self, event: str, ctx: dict) -> None:
        """情绪桥：把同频事件翻译成情绪引擎的输入词汇。"""
        bridge = MOOD_BRIDGE.get(event)
        if bridge is None:
            return
        if bridge[0] == "observe" and hasattr(self.mood, "observe"):
            self.mood.observe(bridge[1], ctx)
        elif bridge[0] == "feel" and hasattr(self.mood, "feel"):
            self.mood.feel(bridge[1], bridge[2])

    @staticmethod
    def _apply_tone(action: Action, event: str, snap: dict, detail: dict) -> None:
        """细色调制：她此刻的情绪决定同一件事怎么说（§16.7 情绪驱动表达）。"""
        fine = snap.get("fine")
        if not fine:
            return
        rule = TONE_RULES.get(fine["name"], {}).get(TONE_CLASS.get(event, ""))
        if rule:
            action.text = rule
            detail["tone"] = fine["name"]            # 调制来源可追溯


# ────────────────────────── 演示 ──────────────────────────

if __name__ == "__main__":
    eng = StateEngine(consent=Consent(
        master=True, scopes={"game", "read", "work", "nav"}))
    t = [1000.0]
    eng.clock = lambda: t[0]

    def play(sig, ctx=None):
        a = eng.handle(sig, ctx)
        if a:
            print(f"[{eng.state}] {sig} {ctx or ''} → "
                  f"{a.kind}｜{a.text}｜动画:{a.animation}")
        else:
            print(f"[{eng.state}] {sig} → （静默）")

    print("── 一场召唤陪伴的完整会话 ──")
    play(Sig.SUMMON)                                   # 召唤 → 在场
    play(Sig.GAME_EVENT, {"event": "kill"})            # 击杀 → 夸（内置 game_sync 插件）
    t[0] += 10
    play(Sig.GAME_EVENT, {"event": "death"})           # 10s 后被杀 → 节流静默
    t[0] += StateEngine.SYNC_MIN_INTERVAL
    play(Sig.GAME_EVENT, {"event": "death"})           # 间隔够了 → 鼓励
    play(Sig.DISMISS)                                  # 送别 → 退场

    print("── 插件体系演示（§16.6）──")
    t[0] += StateEngine.SYNC_MIN_INTERVAL              # 跨过节流窗口
    play(Sig.NAV_EVENT, {"event": "traffic"})          # 未注册插件信号 → 不感知静默
    nav = InteractionPlugin(
        "nav_companion", (Sig.NAV_EVENT,), "nav", ("sync_react",),
        replies={"traffic": "前面有点堵，别急，我陪你说说话。",
                 "arrived": "到啦！一路辛苦。"},
        anims={"traffic": "lean_in", "arrived": "cheer"},
    )
    eng.register_plugin(nav)                           # 注册即接入
    play(Sig.SUMMON)
    play(Sig.NAV_EVENT, {"event": "traffic"})          # 导航插件生效 → 路况提醒
    eng.unregister_plugin("nav_companion")             # 卸载即失效
    play(Sig.NAV_EVENT, {"event": "traffic"})          # → 静默
    play(Sig.CRISIS)                                   # 危机 → 穿透进低潮模式
    play(Sig.RESUME)                                   # 解除 → 回待机

    print("── 情绪接入演示（§10 + §16.7：细色调制同频措辞）──")
    from aiPartner_MoodEngineV2 import MoodEngineV2
    mood = MoodEngineV2(bond=0.62, now=lambda: t[0])
    eng2 = StateEngine(consent=Consent(
        master=True, scopes={"game", "read", "work"}), mood=mood)
    eng2.clock = lambda: t[0]

    def play2(sig, ctx=None):
        a = eng2.handle(sig, ctx)
        if a:
            tone = a.detail.get("tone", "—")
            print(f"[{eng2.state}] {sig} {ctx or ''} → {a.kind}｜{a.text}｜"
                  f"动画:{a.animation}｜细色调制:{tone}")
        else:
            print(f"[{eng2.state}] {sig} → （静默）")

    play2(Sig.SUMMON)
    play2(Sig.GAME_EVENT, {"event": "death"})          # 你被击杀 → 她心疼 → 措辞变温柔
    t[0] += 600                                        # 情绪随时间衰减（反刍更慢）
    play2(Sig.GAME_EVENT, {"event": "losing"})         # 又输 → 心疼淡去 → 无奈口吻
    play2(Sig.DISMISS)                                 # 送别 → 她开始想你
    print(f"    送别后她的状态：{mood.describe()}")
    eng2.mood.feel("anger", 0.3)                       # 她攒了点小情绪
    play2(Sig.BOREDOM)                                 # 提议 → 措辞跟着此刻的心情走

    print("── 趣味话题演示（§16.3 生产口径：直接开聊，看反应判断）──")
    from aiPartner_TopicEngine import TopicEngine
    eng3 = StateEngine(consent=Consent(
        master=True, scopes={"game", "read", "work"}), topics=TopicEngine())
    eng3.clock = lambda: t[0]

    def play3(sig, ctx=None):
        a = eng3.handle(sig, ctx)
        if a:
            print(f"[{eng3.state}] {sig} → {a.kind}｜{a.text}｜动画:{a.animation}")
        else:
            print(f"[{eng3.state}] {sig} → （静默）")

    play3(Sig.BOREDOM)                                 # 无聊 → 直接递谈资（不问想不想聊）
    play3(Sig.TOPIC_REPLY, {"text": "哈哈哈真的假的？"})  # 接话 → 进在场开聊（口味 +1）
    play3(Sig.DISMISS)

    print("── 导航陪伴演示（§16.4 插件，§16.6 契约接入）──")
    from aiPartner_NavPlugin import NavCompanionPlugin
    topics2 = TopicEngine()
    eng4 = StateEngine(consent=Consent(
        master=True, scopes={"game", "read", "work", "nav"}), topics=topics2)
    eng4.clock = lambda: t[0]
    eng4.register_plugin(NavCompanionPlugin(topics=topics2))   # 注册即接入
    eng4.handle(Sig.SUMMON)

    def fire(event):
        t[0] += 181                                    # 跨过同频节流窗口
        a = eng4.handle(Sig.NAV_EVENT, {"event": event})
        print(f"[{eng4.state}] nav:{event} → "
              + (f"{a.kind}｜{a.text}｜动画:{a.animation}" if a else "（静默）"))

    fire("nav_started")                                # 提议：要不要我陪你走这段？
    fire("turn")                                       # 转向 → 静默（不耗节流）
    fire("traffic")                                    # 确认前不插话 → 静默
    fire("nav_accept")                                 # 确认 → 进入陪伴
    fire("turn")                                       # 转向 → 静默
    fire("long_road")                                  # 长路段 → 递话题卡解闷
    fire("fatigue")                                    # 困意 → 温和提醒休息
    fire("long_road")                                  # 困意后 → 不接话聊天
    fire("highway")                                    # 高速 → 自动静默
    fire("arrival")                                    # 抵达 → 道贺并复位
