# -*- coding: utf-8 -*-
"""aiPartner MoodEngineV2（全谱情感引擎）流转测试 —— 对齐 §16.7。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aiPartner_MoodEngineV2 import MoodEngineV2  # noqa: E402


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, minutes):
        self.t += minutes * 60.0


def make(bond=0.62):
    clock = FakeClock()
    return MoodEngineV2(bond=bond, now=clock), clock


# ── 基色层 ──
def test_appraise_positive_joy():
    eng, _ = make()
    s = eng.appraise(goal=0.8)
    assert s["dominant"] == "joy"


def test_negative_decays_slower_rumination():
    eng, clock = make()
    eng.feel("sorrow", 0.5)
    eng.feel("fun", 0.5)
    clock.advance(5)
    s = eng.observe("tick")
    assert s["vector"]["sorrow"] > s["vector"]["fun"]


def test_feel_reduces_and_clears():
    eng, _ = make()
    eng.feel("fear", 0.5)
    eng.feel("fear", -0.5)
    assert "fear" not in eng.snapshot()["vector"]


def test_unknown_base_rejected():
    eng, _ = make()
    try:
        eng.feel("pride", 0.5)
        assert False, "应拒绝七情之外的基色"
    except ValueError:
        pass


# ── 细色层 ──
def test_fine_wronged_on_cold_treatment():
    eng, _ = make()
    s = eng.observe("cold_treatment")
    assert s["fine"] and s["fine"]["name"] == "wronged"


def test_fine_tender_on_user_hurt():
    eng, _ = make()
    s = eng.observe("user_hurt")
    assert s["fine"] and s["fine"]["name"] == "tender"


def test_fine_moved_needs_deep_bond():
    eng, _ = make(bond=0.6)
    s = eng.observe("user_cares")
    assert s["fine"] and s["fine"]["name"] == "moved"
    shallow, _ = make(bond=0.2)
    s2 = shallow.observe("user_cares")
    assert s2["fine"] is None or s2["fine"]["name"] != "moved"


def test_fine_longing_on_separated():
    eng, _ = make()
    s = eng.observe("separated")
    assert s["fine"] and s["fine"]["name"] == "longing"


def test_fine_guilty_on_self_fault():
    eng, _ = make()
    s = eng.observe("my_fault", {"self_fault": True})
    assert s["fine"] and s["fine"]["name"] == "guilty"


def test_fine_relieved_after_fear_drops():
    eng, _ = make()
    eng.feel("fear", 0.5)
    s = eng.observe("threat_passed")
    assert s["fine"] and s["fine"]["name"] == "relieved"


def test_fine_jealous_needs_comparison_ctx():
    eng, _ = make()
    s = eng.observe("rival", {"comparison": True})
    assert s["fine"] and s["fine"]["name"] == "jealous"
    plain, _ = make()
    s2 = plain.observe("rival")
    assert s2["fine"] is None or s2["fine"]["name"] != "jealous"


def test_fine_expect_on_uncertain():
    eng, _ = make()
    s = eng.observe("uncertain")
    assert s["fine"] and s["fine"]["name"] == "expect"


def test_fine_none_when_neutral():
    eng, _ = make()
    s = eng.observe("tick")
    assert s["fine"] is None


# ── 强度与表达 ──
def test_intensity_word_scales():
    eng, _ = make(bond=0.6)
    s = eng.observe("user_cares")          # moved ≈ 0.45 → 有些
    assert s["fine"]["phrase"].startswith("有些")
    eng.observe("user_cares")              # 再来一次 → joy≈1.0 → 非常
    s2 = eng.snapshot()
    assert s2["fine"]["phrase"].startswith("非常")


def test_expression_switches_with_emotion():
    eng, _ = make()
    s1 = eng.observe("user_cares")
    assert s1["expression"]["pace"] == "话多" and s1["expression"]["jokes"] is True
    eng2, _ = make()
    s2 = eng2.observe("cold_treatment")
    assert s2["expression"]["pace"] == "话短" and s2["expression"]["jokes"] is False


def test_bond_grows_with_positive_attitude():
    eng, _ = make()
    before = eng.snapshot()["bond"]
    eng.appraise(attitude=3.0)
    assert eng.snapshot()["bond"] > before


def test_describe_reads_naturally():
    eng, _ = make()
    eng.observe("cold_treatment")
    text = eng.describe()
    assert text.startswith("此刻她")
