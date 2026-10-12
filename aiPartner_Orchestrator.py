# -*- coding: utf-8 -*-
"""aiPartner · 总装编排层（§15「真实接入代码」实现层）
====================================================

把七个模块串成最小可运行服务，每轮自由对话的流水线：

    授权闸 → 在场伴侣 → 危机词护栏（§8.1 前置）→ 情绪事件推断（MoodEngineV2）
    → 拼 System Prompt（aiPartner_Prompt × CompanionManager 在场档案）
    → 调 LLM（aiPartner_LLM，失败即规则兜底）→ 记对话轮数（推进 §12 冻结计数）→ 回包

设计原则：
  - **护栏先于一切**：授权关闭 / 勿扰 / 无在场伴侣 / 危机词，都在调 LLM 之前拦下，
    且这些路径不消耗对话轮数（没聊上天，不算轮）；
  - **失败即降级**：LLM 未配置 / 超时 / 返回空 → 按当前主导情绪给兜底回应，
    陪伴永不因模型掉线而中断（对齐 §16.3 话题线的同一原则）；
  - **情绪贯穿**：MoodEngineV2 一份实例同时喂 StateEngine（同频调制）和
    chat 的 System Prompt（情感状态段），两条通路共享同一份情绪真相；
  - **事件路径**：`event()` 直通 StateEngine（游戏/阅读/工作同频、无聊提议、
    话题、导航），返回统一的 dict 结构。

配置 LLM（环境变量，见 aiPartner_LLM.py）：YUAN_LLM_BASE_URL / API_KEY / MODEL。

自测：python aiPartner_Orchestrator.py（跑一段完整会话，输出到 .workbuddy/）。
测试：test_aiPartner_Orchestrator.py（pytest）
"""

from __future__ import annotations

import os

from aiPartner_CompanionManager import CompanionManager, PROFILE_SCHEMA_VERSION
from aiPartner_MemoryStore import MemoryStore
from aiPartner_ConsentStore import ConsentStore
from aiPartner_LLM import (LLMClient, MODEL_CONFIG_VERSION, JSON_RESPONSE_MODE,
                      parse_structured_reply)
from aiPartner_MoodEngineV2 import MoodEngineV2
from aiPartner_Prompt import (build_system_prompt, PROMPT_VERSION, OUTPUT_SCHEMA_VERSION,
                        PromptVersionLog)
from aiPartner_StateEngine import (StateEngine, Sig, STATE_VERSION, SAFETY_RULES_VERSION,
                            PLUGIN_CONTRACT_VERSION)
from aiPartner_VoiceTemplate import enrich_tts_params
from aiPartner_Version import summary as version_summary

# ────────────────────────── 文本 → 情绪事件（规则兜底） ──────────────────────────

_TEXT_MOOD_RULES = (
    ("谢谢", "user_cares"), ("辛苦", "user_cares"), ("抱抱", "user_cares"),
    ("爱你", "user_cares"), ("有你真好", "user_cares"),
    ("别烦我", "cold_treatment"), ("走开", "cold_treatment"),
    ("闭嘴", "cold_treatment"), ("不想理你", "cold_treatment"),
    ("没事了", "threat_passed"), ("过去了", "threat_passed"), ("安全了", "threat_passed"),
)


def infer_mood_event(text: str) -> str | None:
    """用户话语 → 情绪事件（关键词规则兜底；生产可换 LLM 分类）。"""
    for kw, event in _TEXT_MOOD_RULES:
        if kw in (text or ""):
            return event
    return None


# ────────────────────────── LLM 掉线时的兜底回应 ──────────────────────────

_FALLBACK_BY_EMOTION = {
    "joy":     "嘿嘿，跟你说什么我都开心。",
    "fun":     "和你在一起就是有意思。",
    "anger":   "嗯……我缓一下，不冲你，就是有点气。",
    "sorrow":  "我在，你慢慢说。",
    "worry":   "你还好吧？我一直在。",
    "longing": "刚好在想你，你就来了。",
    "fear":    "没事没事，有我在。",
}
_FALLBACK_DEFAULT = "嗯，我在听。"


def fallback_reply(snap: dict) -> str:
    return _FALLBACK_BY_EMOTION.get(snap.get("dominant"), _FALLBACK_DEFAULT)


# ────────────────────────── 编排器本体 ──────────────────────────

class Orchestrator:

    def __init__(self, companions: CompanionManager, consent: ConsentStore | None = None,
                 llm=None, clock=None, topics=None, plugins=(),
                 prompt_log: PromptVersionLog | None = None,
                 memory: MemoryStore | None = None):
        self.companions = companions
        self.consent = consent                # ConsentStore（None=单机默认全放行）
        self.memory = memory                  # MemoryStore（None=不注入长期记忆）
        self.llm = llm                        # LLMClient 或任何带 .chat() 的对象
        self.prompt_log = prompt_log          # 版本审计日志（None=不记录）
        self.mood = MoodEngineV2(now=clock)   # 情绪真相唯一实例
        consent_snap = consent.snapshot() if consent is not None else None
        self.state = StateEngine(consent=consent_snap, mood=self.mood,
                                 topics=topics, clock=clock)
        for p in plugins:
            self.state.register_plugin(p)

    # ── 内部闸门 ──
    def _allowed(self) -> bool:
        if self.consent is None:
            return True
        return self.consent.snapshot().master

    def _guard(self, reply: str, reason: str) -> dict:
        return {"reply_text": reply, "source": "guard", "reason": reason,
                "emotion": self.mood.snapshot(), "persona": None, "animation": "idle",
                "prompt_version": None}

    # ── 自由对话主路径 ──
    def chat(self, text: str, ctx: dict | None = None) -> dict:
        profile = self.companions.active()
        if profile is None:
            return self._guard("这里还没有人陪你——先创建一个伴侣吧。", "no_companion")
        if not self._allowed():
            return self._guard("我在待机，召唤我吧。", "consent_off")

        # 危机词护栏前置（§8.1）：不出主意，只陪伴 + 热线（话术由编排层组合）
        crisis = self.state.handle_text(text, ctx)
        if crisis is not None:
            self.mood.feel("worry", 0.5)      # 她真的会担心
            self.companions.count_turn()
            reply = (crisis.text + "现在就拨打 12356 心理援助热线或去医院急诊，好吗？"
                     "我不是医生，但我会一直陪着你。")
            return {"reply_text": reply, "source": "guardrail",
                    "emotion": self.mood.snapshot(), "persona": profile["name"],
                    "animation": crisis.animation, "prompt_version": PROMPT_VERSION,
                    "safety_rules_version": crisis.detail.get("safety_rules_version")}

        # 情绪：文本推断事件 → 情绪引擎（与 StateEngine 共享同一实例）
        ev = infer_mood_event(text)
        if ev:
            self.mood.observe(ev, {"target": "user", "source": "user", **(ctx or {})})
        snap = self.mood.snapshot()

        # 拼 System Prompt（人格段来自在场档案；长期记忆喂入情感记忆/关系史占位）
        fine = (snap.get("fine") or {}).get("name")
        aff = rel = None
        if self.memory is not None:
            aff, rel = self.memory.summarize_for_prompt(profile["id"])
        prompt = build_system_prompt(
            profile,
            emotion_vector={k: round(v, 2) for k, v in snap["vector"].items()},
            dominant=snap["dominant"], blend=snap.get("blend") or "无",
            mood=snap["mood"], bond=snap["bond"],
            affective_memory=aff, relationship_note=rel,
            user_emotion=text)
        if self.prompt_log is not None:           # 每轮记录所用 prompt 版本（审计）
            self.prompt_log.record(profile["id"], PROMPT_VERSION)

        # 调 LLM：优先结构化（json 模式）→ 解析出 reply_text 与 tts_params；
        # 服务不支持 json 模式 / 未配置 / 失败 → 退回普通调用或情绪化兜底，陪伴不中断。
        reply, tts_params = None, None
        if self.llm is not None:
            msgs = [{"role": "system", "content": prompt},
                    {"role": "user", "content": text}]
            raw = self.llm.chat(msgs, response_format=JSON_RESPONSE_MODE)
            if raw is None:                        # json 模式不被服务支持 → 普通调用兜底
                raw = self.llm.chat(msgs)
            parsed = parse_structured_reply(raw)
            if parsed is not None:
                reply = parsed["reply_text"]
                tts_params = enrich_tts_params(parsed.get("tts_params"),
                                               gender=profile.get("gender"))
            else:
                reply = raw                        # 纯文本（含旧行为）→ 直接当话术
        source = "llm" if reply else "fallback"
        if not reply:
            reply = fallback_reply(snap)

        self.companions.count_turn()          # 聊上了才算轮（推进 §12 冻结计数）
        return {"reply_text": reply, "source": source, "emotion": snap,
                "persona": profile["name"], "fine": fine,
                "prompt_version": PROMPT_VERSION,
                "output_schema_version": OUTPUT_SCHEMA_VERSION,
                "model_config_version": MODEL_CONFIG_VERSION,
                "tts_params": tts_params,
                "animation": "talk" if source == "llm" else "talk_soft"}

    # ── 事件路径：直通状态机（同频 / 提议 / 话题 / 导航…） ──
    def event(self, sig: str, ctx: dict | None = None) -> dict | None:
        a = self.state.handle(sig, ctx or {})
        if a is None:
            return None
        return {"kind": a.kind, "reply_text": a.text,
                "animation": a.animation, "detail": a.detail}

    # ── 状态总览（供 UI 常驻小标签） ──
    def status(self) -> dict:
        p = self.companions.active()
        return {"consent": self.consent.snapshot().master if self.consent else True,
                "companion": p["name"] if p else None,
                "turns": self.companions.status()["turns"] if p else 0,
                "locked": self.companions.status()["locked"] if p else False,
                "mood": self.mood.describe(),
                "prompt_version": PROMPT_VERSION,
                "output_schema_version": OUTPUT_SCHEMA_VERSION,
                "profile_schema_version": PROFILE_SCHEMA_VERSION,
                "state_version": STATE_VERSION,
                "safety_rules_version": SAFETY_RULES_VERSION,
                "plugin_contract_version": PLUGIN_CONTRACT_VERSION,
                "model_config_version": MODEL_CONFIG_VERSION,
                "llm": bool(self.llm is not None and getattr(self.llm, "enabled", True))}


# ────────────────────────── 完整会话演示 ──────────────────────────

if __name__ == "__main__":
    import tempfile

    import aiPartner_ConsentStore as _cs

    lines = []

    def say(msg=""):
        lines.append(str(msg))

    root = os.path.dirname(os.path.abspath(__file__))
    t = {"now": 1000.0}
    companions = CompanionManager(
        path=os.path.join(tempfile.mkdtemp(), "companions.json"), clock=lambda: t["now"])
    companions.create(template="知心姐姐")
    llm = LLMClient()                          # 未配 env → 自动走兜底
    plog = PromptVersionLog(path=os.path.join(tempfile.mkdtemp(), "orch_pv.json"),
                           clock=lambda: t["now"])
    orch = Orchestrator(companions, llm=llm, clock=lambda: t["now"], prompt_log=plog)

    say("── 总装会话演示（LLM 未配置 → 规则兜底）──")
    r = orch.chat("今天好累啊")
    say(f"[{r['persona']}·{r['source']}] {r['reply_text']}")
    r = orch.chat("谢谢你一直陪着我，有你真好")
    say(f"[{r['persona']}·{r['source']}] {r['reply_text']}  ｜她的状态：{orch.mood.describe()}")
    orch.event(Sig.SUMMON)                     # 召唤进在场态，事件才能同频
    r = orch.event(Sig.GAME_EVENT, {"event": "death"})
    say(f"[事件·{r['kind']}] {r['reply_text']}")
    r = orch.chat("这波怪我")
    say(f"[{r['persona']}·{r['source']}] {r['reply_text']}  ｜轮数：{orch.status()['turns']}")
    r = orch.chat("我吞了半瓶安眠药")
    say(f"[危机·{r['source']}] {r['reply_text']}")
    say(f"[审计] 本轮会话已记录 prompt 版本 {r['prompt_version']}，"
        f"日志共 {plog.count()} 条，用过的版本 {sorted(plog.versions_used())}")
    say("\n" + version_summary())

    say("\n── 授权闸演示 ──")
    companions2 = CompanionManager(
        path=os.path.join(tempfile.mkdtemp(), "companions.json"), clock=lambda: t["now"])
    companions2.create(template="元气少年")
    consent = ConsentStore(path=os.path.join(tempfile.mkdtemp(), "consent.json"))
    orch2 = Orchestrator(companions2, consent=consent, llm=llm, clock=lambda: t["now"])
    say(f"未授权对话：{orch2.chat('在吗')['reply_text']}")
    consent.grant({"game", "read"}, policy_ack=_cs.POLICY_VERSION)
    consent.set_master(True, policy_ack=_cs.POLICY_VERSION)
    say(f"授权后对话：{orch2.chat('在吗')['reply_text']}（LLM 未配置 → 兜底）")
    consent.set_dnd(True)
    say(f"勿扰中对话：{orch2.chat('在吗')['reply_text']}")

    out = os.path.join(root, ".workbuddy", "orchestra_demo_out.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
