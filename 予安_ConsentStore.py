# -*- coding: utf-8 -*-
"""予安 · §16.1 情境感知授权与隐私开关 —— 持久化层（ConsentStore）

对齐方案 §16.1 的设计要点，逐条落地：

  - 显式授权 / 默认关闭   ：无授权文件、文件损坏、字段缺失 → 一律 fail-closed 全关，
                            绝不静默开启；grant 必须携带 policy_ack（= 首次弹窗说明，
                            用户看过并同意才写入），否则拒绝。
  - 最小必要             ：场景目录 SCENE_CATALOG 白名单（游戏/阅读/工作/出行），
                            目录之外的场景无法授权；只存授权状态本身，
                            不存任何行为数据、内容、画像（数据不出端）。
  - 敏感场景不读         ：SENSITIVE_SCENES（支付/医疗/私密浏览）写死在代码里，
                            用户授权也拒收——不是设置项，是硬边界。
  - 透明告知             ：status() 输出每个场景的感知状态，供常驻小标签渲染；
                            每次变更记录时间戳，用户可查「谁在什么时候改过授权」。
  - 授权说明更新         ：POLICY_VERSION 升级后，旧授权自动失效（needs_reconfirm），
                            重新确认前所有感知请求一律拒绝——条款变了要重新点头。
  - 可随时关闭           ：revoke / set_master / set_dnd 随时可调，立即生效并落盘；
                            勿扰是持久设置（重启后仍生效）。

与 StateEngine（§16.2）的关系：引擎只认 Consent 快照对象（.master / .scopes 读写），
本层 snapshot() 产出兼容快照；运行中撤销授权 → 重新 snapshot + Sig.CONSENT_OFF
→ 引擎按既有规则降级 S1。快照上的勿扰改写不回写授权本体，职责分离。

持久化格式（本地 JSON，原子写入 tmp + os.replace，绝不落半截文件）：
    {"master": bool, "scopes": [..], "dnd": bool,
     "policy_version": str|None, "granted_at": float|None, "updated_at": float}

自测：直接运行本文件看完整授权生命周期演示。
测试：test_予安_ConsentStore.py（pytest）
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field

# ────────────────────────── 授权目录与硬边界 ──────────────────────────

POLICY_VERSION = "2026.10-1"   # 授权说明版本：条款升级 → 旧授权失效，需重新确认

# 场景白名单（最小必要）：只有目录内的场景可被授权，用途说明供首次弹窗展示
SCENE_CATALOG = {
    "game": "感知前台应用是否为游戏及其胜负状态，用于陪你打游戏时的同频反应",
    "read": "感知是否在阅读及阅读进度，用于陪你看书时的安安静静或聊两句",
    "work": "感知是否处于工作状态，用于不打扰与适时递杯'虚拟热水'",
    "nav":  "感知导航是否开启及路况事件，用于出行陪伴（§16.4）",
}

# 敏感场景硬边界：用户授权也拒收（§16.1「敏感场景不读」，非设置项）
SENSITIVE_SCENES = frozenset({"payment", "medical", "private_browsing"})

_REQUIRED = ("master", "scopes", "dnd", "policy_version")   # 授权文件必备字段


# ────────────────────────── 引擎快照（兼容 Consent） ──────────────────────────

@dataclass
class ConsentSnapshot:
    """喂给 §16.2 StateEngine 的授权快照：字段与引擎内置 Consent 完全兼容。
    引擎在勿扰/收权时会改写 .master——改的是快照，不动授权本体。"""
    master: bool = False
    scopes: set = field(default_factory=set)


# ────────────────────────── 持久化层本体 ──────────────────────────

class ConsentStore:
    """情境感知授权的本地持久化存储。所有变更即时原子落盘，重启即恢复。"""

    def __init__(self, path: str | None = None, clock=None):
        self.path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "予安_consent.json")
        self.clock = clock or (lambda: __import__("time").time())
        self._state: dict = {}
        self._load()

    # ── 加载：任何异常一律 fail-closed，绝不静默开启 ──
    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or any(k not in data for k in _REQUIRED):
                raise ValueError("missing fields")
            scopes = [s for s in data["scopes"] if s in SCENE_CATALOG]  # 白名单过滤
            self._state = {
                "master": bool(data["master"]),
                "scopes": set(scopes),
                "dnd": bool(data["dnd"]),
                "policy_version": data["policy_version"],
                "granted_at": data.get("granted_at"),
                "updated_at": data.get("updated_at"),
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._state = {"master": False, "scopes": set(), "dnd": False,
                           "policy_version": None, "granted_at": None,
                           "updated_at": None}
        # 条款有效性由 _policy_ok 在每次查询时判定（唯一失效点）：
        # 文件里的 policy_version 与当前 POLICY_VERSION 不符 → 一律拒绝感知

    def _save(self) -> None:
        self._state["updated_at"] = self.clock()
        payload = {**self._state, "scopes": sorted(self._state["scopes"])}
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)                    # 原子替换，绝不落半截
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    # ── 查询 ──
    def _policy_ok(self) -> bool:
        return self._state["policy_version"] == POLICY_VERSION

    def allows(self, scope: str) -> bool:
        """该场景此刻是否被授权感知（总开关 × 场景白名单 × 条款有效 × 非勿扰）。"""
        return (self._state["master"] and self._policy_ok()
                and scope in self._state["scopes"] and not self._state["dnd"])

    def status(self) -> dict:
        """透明告知：供常驻小标签与设置页渲染。不读任何行为数据。"""
        return {
            "master": self._state["master"] and self._policy_ok(),
            "dnd": self._state["dnd"],
            "needs_reconfirm": (self._state["policy_version"] is not None
                                and not self._policy_ok()),
            "policy_version": self._state["policy_version"],
            "granted_at": self._state["granted_at"],
            "updated_at": self._state["updated_at"],
            "scenes": {s: self.allows(s) for s in SCENE_CATALOG},
        }

    def scene_purpose(self, scope: str) -> str:
        """首次授权弹窗的用途说明文案。"""
        return SCENE_CATALOG.get(scope, "")

    # ── 变更（即时落盘） ──
    def grant(self, scopes, policy_ack: str | None = None) -> None:
        """授权场景。必须携带 policy_ack == POLICY_VERSION（看过授权说明才生效）。"""
        if policy_ack != POLICY_VERSION:
            raise ValueError("需先阅读并同意当前版本的授权说明（policy_ack 不匹配）")
        scopes = set(scopes)
        bad_sensitive = scopes & SENSITIVE_SCENES
        if bad_sensitive:
            raise ValueError(f"敏感场景绝不感知，授权拒收：{sorted(bad_sensitive)}")
        unknown = scopes - set(SCENE_CATALOG)
        if unknown:
            raise ValueError(f"场景不在最小必要目录内：{sorted(unknown)}")
        if not self._state["master"]:
            self._state["master"] = True
            self._state["granted_at"] = self.clock()
        self._state["scopes"] |= scopes
        self._state["policy_version"] = POLICY_VERSION
        self._save()

    def revoke(self, scopes) -> None:
        """关闭部分场景（细粒度开关）。"""
        self._state["scopes"] -= set(scopes)
        self._save()

    def set_master(self, on: bool, policy_ack: str | None = None) -> None:
        """总开关。开启同样要求看过当前版本授权说明；关闭随时可做、无需理由。"""
        if on:
            if policy_ack != POLICY_VERSION:
                raise ValueError("需先阅读并同意当前版本的授权说明（policy_ack 不匹配）")
            if not self._state["master"]:
                self._state["granted_at"] = self.clock()
            self._state["policy_version"] = POLICY_VERSION
        self._state["master"] = bool(on)
        self._save()

    def set_dnd(self, on: bool) -> None:
        """勿扰（持久设置）：开启后 allows() 一律 False，重启仍生效。"""
        self._state["dnd"] = bool(on)
        self._save()

    def reconfirm(self, policy_ack: str | None = None) -> None:
        """条款升级后的重新确认：恢复总开关并刷新版本（场景范围保持不变）。"""
        if policy_ack != POLICY_VERSION:
            raise ValueError("需先阅读新版授权说明（policy_ack 不匹配）")
        self._state["policy_version"] = POLICY_VERSION
        if self._state["scopes"]:
            self._state["master"] = True
        self._save()

    # ── 引擎对接 ──
    def snapshot(self) -> ConsentSnapshot:
        """产出喂给 StateEngine 的授权快照（字段与引擎内置 Consent 兼容）。"""
        ok = self._policy_ok()
        return ConsentSnapshot(master=self._state["master"] and ok and not self._state["dnd"],
                               scopes=set(self._state["scopes"]) if ok else set())


# ────────────────────────── 演示 ──────────────────────────

if __name__ == "__main__":
    lines = []
    say = lines.append

    store = ConsentStore(path=os.path.join(os.getcwd(), "consent_demo.json"))
    if os.path.exists(store.path):
        os.remove(store.path)                          # 演示从零开始
    store._load()

    say("【首次召唤 · 未授权】")
    say(f"  status: {store.status()['master']=}  允许游戏感知: {store.allows('game')}")

    say("\n【首次授权弹窗 · 用途说明】")
    say(f"  游戏场景说明：{store.scene_purpose('game')}")

    say("\n【没看说明就授权 → 拒绝】")
    try:
        store.grant({"game"})
    except ValueError as e:
        say(f"  拒绝：{e}")

    say("\n【试图授权敏感场景 → 硬边界拒收】")
    try:
        store.grant({"game", "payment"}, policy_ack=POLICY_VERSION)
    except ValueError as e:
        say(f"  拒绝：{e}")

    say("\n【看完说明，授权游戏+出行】")
    store.grant({"game", "nav"}, policy_ack=POLICY_VERSION)
    st = store.status()
    say(f"  总开关: {st['master']}  场景: {st['scenes']}")

    say("\n【勿扰持久开启 → 全部拒绝感知】")
    store.set_dnd(True)
    say(f"  允许游戏感知: {store.allows('game')}（勿扰中，重启也生效）")
    store.set_dnd(False)

    say("\n【重启（重新加载文件）→ 授权恢复】")
    store2 = ConsentStore(path=store.path)
    st2 = store2.status()
    say(f"  总开关: {st2['master']}  场景: {st2['scenes']}")

    say("\n【条款升级 → 旧授权失效，需重新确认】")
    # 模拟升级：把授权文件里的条款版本改写为旧版，再重新加载
    with open(store2.path, "r", encoding="utf-8") as f:
        disk = json.load(f)
    disk["policy_version"] = "2026.09-1"
    with open(store2.path, "w", encoding="utf-8") as f:
        json.dump(disk, f, ensure_ascii=False)
    store3 = ConsentStore(path=store2.path)
    say(f"  旧条款加载后允许感知: {store3.allows('game')}"
        f"  needs_reconfirm: {store3.status()['needs_reconfirm']}")
    store3.reconfirm(policy_ack=POLICY_VERSION)        # 用户重新看过新版说明
    say(f"  重新确认后允许感知: {store3.allows('game')}")

    say("\n【一键关闭总开关 → 立即生效并落盘】")
    store2.set_master(False)
    say(f"  允许感知: {store2.allows('game')}  场景范围保留: {sorted(store2._state['scopes'])}")

    os.remove(store.path)

    out = os.path.join(os.getcwd(), "consent_demo_out.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("demo written:", out)
