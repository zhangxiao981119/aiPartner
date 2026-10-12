# -*- coding: utf-8 -*-
"""aiPartner · TTS 声线模版 测试（pytest）"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import aiPartner_VoiceTemplate as VT


def test_version_constant_exists():
    assert isinstance(VT.TTS_VOICE_VERSION, str) and VT.TTS_VOICE_VERSION
    assert VT.TTS_VOICE_VERSION == "2026.10-2"


def test_template_count_is_six():
    assert len(VT.list_voices()) == 6


def test_gender_filter():
    female = VT.list_voices("female")
    male = VT.list_voices("male")
    assert len(female) == 3 and len(male) == 3
    assert all(v["gender"] == "female" for v in female)
    assert all(v["gender"] == "male" for v in male)
    # 默认（None）返回全部
    assert len(VT.list_voices()) == len(female) + len(male)


def test_get_voice_returns_full_params():
    v = VT.get_voice("xiaoyu")
    assert v is not None
    for field in ("id", "name", "gender", "timbre", "pitch", "speed",
                  "preview_text", "voice_id"):
        assert field in v


def test_unknown_voice_returns_none():
    assert VT.get_voice("not_exist") is None


def test_template_fields_complete_and_valid():
    ids = set()
    for v in VT.VOICE_TEMPLATES:
        assert v["gender"] in ("female", "male")
        assert isinstance(v["pitch"], (int, float))
        assert isinstance(v["speed"], (int, float))
        assert v["voice_id"].startswith("reserved:")
        assert isinstance(v["preview_text"], str) and v["preview_text"]
        assert v["id"] not in ids, "声线 id 必须唯一"
        ids.add(v["id"])
    assert VT.all_ids() == sorted(ids)


# ────────────────────────── 运行时路由：timbre → 声线 ──────────────────────────

def test_resolve_exact_timbre():
    r = VT.resolve("甜暖女声")
    assert r["matched"] and r["route"] == "exact"
    assert r["voice"]["id"] == "xiaoyu"


def test_resolve_alias_timbre():
    r = VT.resolve("温柔女声")          # 宽容别名 → 甜暖女声
    assert r["matched"] and r["route"] == "alias"
    assert r["voice"]["id"] == "xiaoyu"
    # 别名表覆盖到男声
    assert VT.resolve("大叔音")["voice"]["id"] == "shanlan"


def test_resolve_gender_default_fallback():
    r = VT.resolve("不存在的音色", gender="male")
    assert (not r["matched"]) and r["route"] == "gender_default"
    assert r["voice"]["id"] == VT.GENDER_DEFAULT["male"]


def test_resolve_global_default_fallback():
    r = VT.resolve(None)                # 无 timbre、无性别 → 全局兜底
    assert (not r["matched"]) and r["route"] == "global_default"
    assert r["voice"]["id"] == VT.DEFAULT_VOICE_ID


def test_resolve_preferred_vid_wins_and_invalid_ignored():
    r = VT.resolve("甜暖女声", preferred_vid="shanlan")
    assert r["route"] == "preferred" and r["voice"]["id"] == "shanlan"
    # 无效 preferred_vid → 退回正常路由（不是崩、不是 None）
    r2 = VT.resolve("知性女声", preferred_vid="not_exist")
    assert r2["voice"]["id"] == "qingyue"


def test_resolve_voice_id_returns_speaker_id():
    assert VT.resolve_voice_id("知性女声") == VT.get_voice("qingyue")["voice_id"]
    # 未知 timbre 也能拿到一个 voice_id（fail-safe）
    assert VT.resolve_voice_id("乱写的") == VT.get_voice(VT.DEFAULT_VOICE_ID)["voice_id"]


def test_enrich_tts_params_fills_voice_id_and_baseline():
    out = VT.enrich_tts_params({"timbre": "温润男声", "emotion_tag": "tender_mixed"})
    v = VT.get_voice("chenzhou")
    assert out["voice_id"] == v["voice_id"]
    assert out["pitch"] == v["pitch"] and out["speed"] == v["speed"]
    assert out["emotion_tag"] == "tender_mixed"
    # LLM 显式给的 pitch/speed 不被覆盖
    out2 = VT.enrich_tts_params({"timbre": "温润男声", "pitch": 0.5, "speed": 1.5})
    assert out2["pitch"] == 0.5 and out2["speed"] == 1.5
    # 空输入 fail-safe 不崩，仍给 voice_id
    out3 = VT.enrich_tts_params(None)
    assert out3["voice_id"] and out3["voice_timbre"]
