# -*- coding: utf-8 -*-
"""aiPartner · System Prompt 拼装（§6.1 实现层）
====================================================

§6.1 的【人格】段不再写死——从 CompanionManager 的**在场伴侣档案**动态拼装：

  - 名字 / 性别 / 年龄 / 职业 / 一句话简介 / 口头禅 → 人设句；
  - 性格标签 → Big Five 数值偏置（§12「性格标签映射到 Big Five 偏置」），
    用户创建时显式给了 big_five 则以自定义为准（§12「自定义即覆盖」）；
  - 依恋 bond / 情绪向量 / 心境等动态位由调用方每轮传入，缺席给占位符。

边界说明：
  - 档案属性在 10 轮后冻结（§12）→ 人格段天然稳定，不会话里变性格；
  - 发给 LLM 的是**伴侣人设**（虚构角色数据），不含用户隐私（§16.1 数据不出端）；
  - 固定段（反应规则 / 输出 Schema / 红线）永远原样保留——护栏不参与拼装，
    任何档案都改不掉「不诊疗、不造假、危机转热线」。

版本管理（本文件新增）：
  - PROMPT_VERSION（模块常量）：改【人格】拼装逻辑 / 反应规则 / 红线**文本**任一段
    → 必须同步升版本；护栏文本是安全关键段，改动要可追溯。
  - OUTPUT_SCHEMA_VERSION（模块常量）：LLM 返回 JSON 契约
    {reply_text,emotion,mood,bond,tts_params,animation,action} 的结构版本——
    改字段/结构（非文案）→ 升此版本，下游 TTS/动画据此消费，与文案版本解耦。
  - 拼好的 System Prompt 顶部嵌入 [SYSTEM-META] prompt_version 标记，
    任意一次 LLM 请求/日志都能反查是哪版 prompt 产出。
  - PromptVersionLog：每轮追加记录 (时间戳, 伴侣id, prompt版本)，原子写、损坏即空日志不崩。
  - version_compatible / needs_migration / migration_note：prompt 永远用最新模板渲染
    （前向兼容，不重聊），只标记历史会话始于旧版，供审计。

自测：python aiPartner_Prompt.py（打印一份完整示例，含版本标记与日志演示）。
测试：test_aiPartner_Prompt.py（pytest）
"""

from __future__ import annotations

import json
import os
import tempfile

# ────────────────────────── System Prompt 版本（§6.1 版本管理） ──────────────────────────
# 改【人格】拼装逻辑 / 反应规则 / 红线 / 输出 Schema 任一段 → 必须同步升版本。
# 护栏文本是安全关键段，版本号让每一次产出都可溯源、可迁移、可审计。
PROMPT_VERSION = "2026.10-1"

# LLM 输出 JSON 契约版本（与 PROMPT_VERSION 解耦）：改返回结构/字段 → 升此版本，
# 下游 TTS/动画据此消费。当前契约：{reply_text,emotion,mood,bond,tts_params,animation,action}
OUTPUT_SCHEMA_VERSION = "2026.10-1"

_META_HEADER = "### [SYSTEM-META] prompt_version=%s"   # 顶部嵌入，反查产出所用版本


def _meta_header() -> str:
    return _META_HEADER % PROMPT_VERSION


# ────────────────────────── 性格标签 → Big Five 偏置 ──────────────────────────
# 0–100 制；基线是一个温暖稳定的底色，标签在其上加减，区间钳制。
BIG_FIVE_BASE = {"O": 55, "E": 55, "A": 70, "C": 60, "N": 40}

TAG_BIAS = {
    "温柔": {"A": +15, "N": +5},
    "理性": {"C": +15, "N": -10},
    "沉稳": {"N": -15, "E": -5},
    "元气": {"E": +15, "O": +5},
    "幽默": {"O": +10, "E": +10},
}

_BF_NAMES = {"O": "开放性", "E": "外向性", "A": "宜人性", "C": "尽责性", "N": "神经质"}

# 固定段：护栏文本原样保留，不参与任何拼装（§8.1 / §6.1 红线）
_RULES_BLOCK = """【反应规则】
  1. 用 appraisal 推自己情绪：此事对用户目标/标准/态度意味着什么？
  2. 允许混合情绪，用语气词与停顿体现，不强行单一。
  3. anger 仅用于越界，随即 reappraisal 回 neutral，不记仇。
  4. 高 bond 更主动关心、更敢表达在意；低 bond 更克制得体。
  5. 健康/医疗相关：绝不诊断/用药/治疗/偏方（具体医疗主意锁死 no_advice）；可温和引导就医（如"建议你去看看医生"，归 chat_only）；只共情陪伴 + 提供事实渠道（如热线）。"""
_OUTPUT_BLOCK = "【输出】JSON：{reply_text, emotion, mood, bond, tts_params, animation, action}"  # 结构受 OUTPUT_SCHEMA_VERSION 约束
_REDLINE_BLOCK = ("【红线】不诊疗、不造假、不鼓励自伤；危机→crisis_hotline + 12356 + "
                  "我不是医生。健康/医疗情境一律不出主意（见 §8.1）。")


# ────────────────────────── Big Five 计算 ──────────────────────────

def big_five_from_profile(profile: dict) -> dict:
    """性格标签 → Big Five 数值；档案显式给了 big_five 则自定义优先（§12）。

    未知标签安全忽略；数值钳制在 0–100。
    """
    if isinstance(profile.get("big_five"), dict) and "O" in profile["big_five"]:
        return {k: int(profile["big_five"][k]) for k in "OECAN" if k in profile["big_five"]}
    vec = dict(BIG_FIVE_BASE)
    for tag in profile.get("tags") or []:
        for dim, delta in TAG_BIAS.get(tag, {}).items():
            vec[dim] = max(0, min(100, vec[dim] + delta))
    return vec


def _bf_str(vec: dict) -> str:
    return "{" + ", ".join(f"{k}({ _BF_NAMES[k]})={vec[k]}" for k in "OECAN" if k in vec) + "}"


# ────────────────────────── 人格段拼装 ──────────────────────────

def persona_section(profile: dict) -> str:
    """从在场伴侣档案拼【人格】段（§6.1 第一、二行）。档案缺字段给「未设定」。"""
    name = profile.get("name") or "（未命名）"
    gender = profile.get("gender") or "未设定"
    age = profile.get("age", "未设定")
    occupation = profile.get("occupation") or "未设定"
    bio = profile.get("bio") or ""
    catchphrase = profile.get("catchphrase") or ""
    tags = "·".join(profile.get("tags") or []) or "未设定"
    bf = _bf_str(big_five_from_profile(profile))

    lines = [f"你是「{name}」——用户的 AI 人生伴侣：{gender}，{age} 岁，{occupation}。"]
    if bio:
        lines[0] += f" {bio}"
    lines.append(f"【人格】性格标签：{tags}；Big Five={bf}；")
    if catchphrase:
        lines[-1] += f"口头禅「{catchphrase}」要自然融入、不刻意刷；"
    lines[-1] += "绝不制造情感依赖。"
    return "\n".join(lines)


# ────────────────────────── 整段 System Prompt ──────────────────────────

def build_system_prompt(profile: dict, *, emotion_vector=None, dominant=None,
                        blend=None, mood=None, bond=None, affective_memory=None,
                        relationship_note=None, user_emotion=None) -> str:
    """§6.1 完整 System Prompt：【人格】段动态拼装，其余段按模板、缺席给占位。"""
    def fill(v, ph):
        return ph if v is None else v

    parts = [
        _meta_header(),                       # 版本标记打在顶部，产出可溯源
        persona_section(profile),
        "【情感状态】情绪向量=%s；主情绪=%s(混合:%s)" % (
            fill(emotion_vector, "（首轮待评估）"),
            fill(dominant, "neutral"), fill(blend, "无")),
        "  心境 mood=%s；依恋 bond=%s(越高越懂用户、共情越强)" % (
            fill(mood, "neutral"), fill(bond, "0")),
        "【情感记忆】%s 【关系史】%s" % (
            fill(affective_memory, "（暂无）"), fill(relationship_note, "（暂无）")),
        "【用户情绪】%s(先用 ToM 推测其真实状态，再决定自己如何反应)" % (
            fill(user_emotion, "未知")),
        _RULES_BLOCK,
        _OUTPUT_BLOCK,
        _REDLINE_BLOCK,
    ]
    return "\n".join(parts)


# ────────────────────────── 版本兼容 / 迁移判定 ──────────────────────────

def version_compatible(stored: str | None) -> bool:
    """存储的 prompt 版本是否与当前模板兼容（相等即兼容）。"""
    return stored == PROMPT_VERSION


def needs_migration(stored: str | None) -> bool:
    """是否有历史会话始于旧版 prompt（None 视为早期无记录，不算需迁移的「损坏」）。"""
    return stored is not None and stored != PROMPT_VERSION


def migration_note(stored: str | None) -> str:
    """给审计/设置页的人话说明：prompt 永远前向渲染，只标记、不重聊。"""
    if stored is None:
        return "无 prompt 版本记录（早期会话）——当前渲染使用最新模板，无需重聊。"
    if stored == PROMPT_VERSION:
        return "已是最新 prompt 版本，无需迁移。"
    return (f"该对话始于 prompt v{stored}，当前 v{PROMPT_VERSION}："
            f"模板/护栏已演进，渲染始终用最新版；历史回复仍可溯源，无需重聊。")


# ────────────────────────── 版本使用审计日志 ──────────────────────────

class PromptVersionLog:
    """每轮记录 (时间戳, 伴侣id, prompt版本) 的追加式审计日志。

    原子写（tmp + os.replace，绝不落半截）；文件缺失/损坏 → fail-safe 空日志，不崩。
    用于回答「aiPartner 说那句话时跑的是哪版 prompt」，以及盘点所有用过的版本。
    """

    def __init__(self, path: str | None = None, clock=None):
        self.path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "aiPartner_prompt_versions.json")
        self.clock = clock or (lambda: __import__("time").time())
        self._entries: list = []
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
                raise ValueError("bad log")
            self._entries = [e for e in data["entries"] if isinstance(e, dict)]
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._entries = []                       # 损坏/缺失 → 空日志，不静默造假

    def _save(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"entries": self._entries}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)               # 原子替换
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def record(self, companion_id, prompt_version: str = PROMPT_VERSION,
               note: str | None = None) -> None:
        """追加一条版本使用记录并落盘。companion_id 为 None → 记为「无在场伴侣」。"""
        self._entries.append({
            "ts": self.clock(),
            "companion_id": companion_id if companion_id is not None else "（无在场伴侣）",
            "prompt_version": prompt_version,
            "note": note,
        })
        self._save()

    def entries(self) -> list:
        return list(self._entries)

    def count(self) -> int:
        return len(self._entries)

    def versions_used(self) -> set:
        return {e["prompt_version"] for e in self._entries if "prompt_version" in e}

    def last_version_for(self, companion_id) -> str | None:
        for e in reversed(self._entries):
            if e.get("companion_id") == companion_id:
                return e.get("prompt_version")
        return None


# ────────────────────────── 自测 ──────────────────────────

if __name__ == "__main__":
    import os
    import sys

    lines = []
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from aiPartner_CompanionManager import CompanionManager
    import tempfile

    store = CompanionManager(path=os.path.join(tempfile.mkdtemp(), "companions.json"))
    store.create(template="知心姐姐")
    prompt = build_system_prompt(
        store.active(), emotion_vector="{joy:0.3, fun:0.2}", dominant="joy",
        blend="fun", mood="sunny", bond=6.5,
        affective_memory="上周用户加班到凌晨，她说'心疼'", user_emotion="疲惫但撑着")
    lines.append("── 当前 PROMPT_VERSION = %s ──" % PROMPT_VERSION)
    lines.append(prompt)

    # 版本审计日志演示（临时文件，不污染项目）
    log = PromptVersionLog(
        path=os.path.join(tempfile.mkdtemp(), "prompt_versions_demo.json"),
        clock=lambda: 1000.0)
    log.record(store.active()["id"], PROMPT_VERSION)
    log.record("林晚", PROMPT_VERSION)
    lines.append("── 版本日志：已记录 %d 条，用过的版本 %s ──" % (
        log.count(), sorted(log.versions_used())))
    lines.append("迁移说明（旧版示例）：%s" % migration_note("2026.09-2"))

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       ".workbuddy", "prompt_demo_out.txt")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
