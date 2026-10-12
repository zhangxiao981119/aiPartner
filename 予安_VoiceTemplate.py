# -*- coding: utf-8 -*-
"""予安 · TTS 声线模版（§13 声线模版 + §6 输出 Schema 的 tts_params）

为什么独立成模块：
  §13 定义了 6 套声线模版（女声 3 / 男声 3），声线独立于性格、可自由组合、
  可试听与选用。此前它只是文档设计（文档 499 行标注「TTS 映射待接」），
  代码里没有落地，自然也无版本化。本模块把这套静态声线清单落成代码，
  并纳入版本升级护栏（§17）——声线是有限预设清单，删一条=用户选不了（破坏），
  加一条=新增可选项（兼容），改某条参数=内容变更需复核。

与输出契约的关系：
  LLM 返回的 `tts_params` 含 pitch/speed/timbre/emotion_tag（见 §6）。
  本模版提供每条声线的 baseline pitch/speed/timbre；运行时情绪引擎在 baseline
  之上叠加 emotion_tag 偏移。emotion_tag 是情绪态产物，不属于静态模版字段。

运行时路由（「TTS 映射」的另一半，已落地）：
  LLM 建议的 timbre 标签经 resolve() 路由到具体声线模版（拿到 voice_id 交给 TTS 引擎）；
  enrich_tts_params() 把 LLM 的 tts_params 补全成可交引擎的结构（补基线 pitch/speed）。
  匹配走「精确 → 宽容别名 TIMBRE_ALIASES → 性别首选 GENDER_DEFAULT → 全局兜底
  DEFAULT_VOICE_ID」回退链，fail-safe 永不返回 None。

明确不做（范围克制，避免 scope creep）：
  - 不接真实 TTS 引擎 SDK（voice → 音频），那是更大的集成；真接时调用 enrich_tts_params。
  - 不做声线选用落库（属 CompanionManager 已有的声线属性字段；可选 preferred_vid 由上层传入）。
  - 不生成试听音频，`preview_text` 仅提供试听文案，真试听由前端 / TTS 负责。

版本纪律：本模块任一声线的 id / 参数 / 路由别名（TIMBRE_ALIASES）变更，必须同步升
TTS_VOICE_VERSION，并在 `予安_version_guard.py` 中通过 tts_voice（set 键，声线增删）/
tts_voice_params（review 键，参数 + 别名）溯源。
"""

from __future__ import annotations

import copy

# 版本号：2026.10-1 落地声线清单；2026.10-2 新增运行时路由（timbre→voice）+ 宽容别名表。
TTS_VOICE_VERSION = "2026.10-2"

# 声线模版清单（§13）。每条字段：
#   id          唯一键，护栏 set 键以此为集合（删=阻断、加=兼容）
#   name        展示名（甜暖·小雨 等）
#   gender      "female" / "male"
#   timbre      TTS 引擎的 timbre 标签
#   pitch       语调基线（-1~1），情绪引擎运行时叠加偏移
#   speed       语速基线（0.5~2）
#   preview_text 试听文案
#   voice_id    预留：真实 TTS 引擎的 speaker id（当前未接引擎）
VOICE_TEMPLATES = [
    # ── 女声 ──
    {"id": "xiaoyu", "name": "甜暖·小雨", "gender": "female", "timbre": "甜暖女声",
     "pitch": 0.05, "speed": 0.95,
     "preview_text": "今天也要开开心心的哦，小雨陪着你~", "voice_id": "reserved:xiaoyu"},
    {"id": "qingyue", "name": "知性·清越", "gender": "female", "timbre": "知性女声",
     "pitch": 0.02, "speed": 1.0,
     "preview_text": "这个问题，我们慢慢理清楚就好。", "voice_id": "reserved:qingyue"},
    {"id": "mianmian", "name": "慵懒·眠眠", "gender": "female", "timbre": "慵懒女声",
     "pitch": -0.03, "speed": 0.85,
     "preview_text": "困了吗？那我们轻轻说，不急的。", "voice_id": "reserved:mianmian"},
    # ── 男声 ──
    {"id": "chenzhou", "name": "温润·沉舟", "gender": "male", "timbre": "温润男声",
     "pitch": -0.05, "speed": 0.9,
     "preview_text": "有我在，天塌下来也有人扛着。", "voice_id": "reserved:chenzhou"},
    {"id": "langxing", "name": "少年·朗星", "gender": "male", "timbre": "少年男声",
     "pitch": 0.04, "speed": 1.05,
     "preview_text": "走！有新发现，带你去看看！", "voice_id": "reserved:langxing"},
    {"id": "shanlan", "name": "低沉·山澜", "gender": "male", "timbre": "低沉男声",
     "pitch": -0.02, "speed": 0.95,
     "preview_text": "别担心，听我慢慢说。", "voice_id": "reserved:shanlan"},
]


# ────────────────────────── 查询 API ──────────────────────────

def list_voices(gender: str | None = None) -> list[dict]:
    """列出声线模版（深拷贝，避免外部修改污染常量）。

    gender: None=全部；"female" / "male"=按性别过滤。
    """
    out = [copy.deepcopy(v) for v in VOICE_TEMPLATES]
    if gender is not None:
        out = [v for v in out if v.get("gender") == gender]
    return out


def get_voice(vid: str) -> dict | None:
    """取某声线模版的完整参数；未知 id 返回 None（fail-safe）。"""
    for v in VOICE_TEMPLATES:
        if v["id"] == vid:
            return copy.deepcopy(v)
    return None


def all_ids() -> list[str]:
    """所有声线 id（排序），供护栏 set 键比对。"""
    return sorted(v["id"] for v in VOICE_TEMPLATES)


# ────────────────── 运行时路由：LLM timbre 标签 → 具体声线 ──────────────────
# 「TTS 映射」的另一半（文档曾标「待接」）：LLM 在 tts_params 里建议一个 timbre，
# 这里把它路由到一套具体声线模版，拿到 voice_id 交给真实 TTS 引擎。
# 真接引擎时调用 enrich_tts_params(tts_params, gender=...) 即可。

# 全局兜底声线：timbre 未命中且未给性别时使用（甜暖女声，最百搭）。
DEFAULT_VOICE_ID = "xiaoyu"

# 性别首选声线：timbre 未命中但已知伴侣性别时的回退（女→清越、男→沉舟）。
GENDER_DEFAULT = {"female": "qingyue", "male": "chenzhou"}

# 规范 timbre → 声线 id（由清单派生，不手维护，避免与 VOICE_TEMPLATES 脱节）。
TIMBRE_TO_VOICE = {v["timbre"]: v["id"] for v in VOICE_TEMPLATES}

# 宽容别名：LLM 偶尔跑偏的措辞 → 规范 timbre。
# 数据驱动、随版本治理（改动会让 tts_voice_params 指纹变化，需复核）。
TIMBRE_ALIASES = {
    # 女声
    "甜美女声": "甜暖女声", "温柔女声": "甜暖女声", "软萌女声": "甜暖女声",
    "清冷女声": "知性女声", "成熟女声": "知性女声", "御姐音": "知性女声",
    "软糯女声": "慵懒女声", "慵懒音": "慵懒女声", "低语女声": "慵懒女声",
    # 男声
    "温柔男声": "温润男声", "治愈男声": "温润男声", "暖男音": "温润男声",
    "阳光男声": "少年男声", "青年男声": "少年男声", "元气男声": "少年男声",
    "磁性男声": "低沉男声", "大叔音": "低沉男声", "醇厚男声": "低沉男声",
}


def resolve(timbre: str | None, gender: str | None = None,
            preferred_vid: str | None = None) -> dict:
    """把 LLM 返回的 timbre 标签路由到具体声线模版（fail-safe，永不返回 None）。

    匹配顺序（短路）：
      0. preferred_vid 有效 → 直接用（尊重伴侣已选声线）
      1. timbre 精确命中规范标签（TIMBRE_TO_VOICE）
      2. timbre 命中宽容别名（TIMBRE_ALIASES → 规范标签）
      3. 给了 gender → 该性别首选声线（GENDER_DEFAULT）
      4. 全局兜底声线（DEFAULT_VOICE_ID）

    返回 {"voice": 声线副本, "matched": bool, "route": 命中来源}。
    route ∈ {preferred, exact, alias, gender_default, global_default}。
    """
    if preferred_vid:
        v = get_voice(preferred_vid)
        if v is not None:
            return {"voice": v, "matched": True, "route": "preferred"}

    t = (timbre or "").strip()
    vid = TIMBRE_TO_VOICE.get(t)
    route = "exact"
    if vid is None:
        canon = TIMBRE_ALIASES.get(t)
        vid = TIMBRE_TO_VOICE.get(canon) if canon else None
        route = "alias"
    if vid is not None:
        return {"voice": get_voice(vid), "matched": True, "route": route}

    # 回退：性别首选 → 全局兜底
    fb = GENDER_DEFAULT.get(gender or "")
    if fb:
        v = get_voice(fb)
        if v is not None:
            return {"voice": v, "matched": False, "route": "gender_default"}
    return {"voice": get_voice(DEFAULT_VOICE_ID), "matched": False,
            "route": "global_default"}


def resolve_voice_id(timbre: str | None, gender: str | None = None,
                     preferred_vid: str | None = None) -> str:
    """路由后取 voice_id（交给 TTS 引擎的 speaker id）。"""
    return resolve(timbre, gender, preferred_vid)["voice"]["voice_id"]


def enrich_tts_params(tts_params: dict | None, gender: str | None = None,
                      preferred_vid: str | None = None) -> dict:
    """把 LLM 产出的 tts_params 补全成可交 TTS 引擎的结构（fail-safe）。

    - timbre → 解析出具体声线的 voice_id（缺失 / 未知时按回退链兜底，永不 None）；
    - pitch / speed：LLM 显式给了就用，否则补声线模版基线；
    - emotion_tag / timbre 等其余字段原样透传。
    """
    src = dict(tts_params or {})
    voice = resolve(src.get("timbre"), gender, preferred_vid)["voice"]
    out = dict(src)
    out["voice_id"] = voice["voice_id"]
    out["voice_timbre"] = voice["timbre"]
    if out.get("pitch") is None:
        out["pitch"] = voice["pitch"]
    if out.get("speed") is None:
        out["speed"] = voice["speed"]
    return out
