# -*- coding: utf-8 -*-
"""予安 · 伴侣档案与生命周期管理（§12 实现层）

设计口径（用户 2026-10-10 定）：
  - 用户可以创建新的伴侣，而不是在已有伴侣上改性格——改的是「人」，换的是「伴侣」。
  - 属性修改只在测试期允许：对话超过 10 轮后，该伴侣的任何属性不可再修改。
    （测试模式 test_mode=True 例外放行，但留审计记录——生产排查有据可查。）
  - 伴侣可切换、新增、删除，随时可做；删除不做属性层面的回收顾虑。

规则细分：
  - 冻结的是「档案属性」：名字、性别、年龄、性格标签、职业、口头禅、简介、
    声线、外观、Big Five。创建时随便配，一旦「认识你」就定下来。
  - 不冻「关系数据」：对话轮数、依恋(bond)、情绪、记忆——这些是相处长出来的，
    本来就不该手改，也改不出真感情。

持久化：本地 JSON 原子写入（tmp + replace），重启即恢复；
        文件缺失/损坏一律 fail-safe（空档案，绝不虚构一个伴侣）。

自测：直接运行本文件看完整生命周期演示（创建 → 相处 → 冻结 → 切换/删除）。
测试：test_予安_CompanionManager.py（pytest）
"""

from __future__ import annotations

import json
import os

# ────────────────────────── 冻结规则与档案字段 ──────────────────────────

EDIT_LOCK_TURNS = 10          # 对话超过 10 轮 → 属性冻结（恰好在第 10 轮时仍可改）

# 伴侣档案 schema 版本（§6.1 版本管理）：加字段（声线/头像/偏好等）→ 必须升版本；
# 旧档案加载做前向兼容（缺字段补默认），schema 大改时走 _migrate；损坏文件仍 fail-safe。
PROFILE_SCHEMA_VERSION = "2026.10-1"

# 可编辑的档案属性白名单：白名单之外的字段一律拒收（含关系数据，防手改）
EDITABLE_FIELDS = frozenset({
    "name", "gender", "age", "tags", "occupation", "catchphrase",
    "bio", "voice", "appearance", "big_five",
})

_REQUIRED_PROFILE = ("name",)

# §12 自带模版（开箱即用）：模版只是创建时的起点，冻结规则对模版一视同仁
TEMPLATES = {
    "知心姐姐": {"name": "予安", "gender": "女", "age": 28, "tags": ["温柔", "理性"],
               "occupation": "心理咨询师", "catchphrase": "我在听。",
               "bio": "温暖治愈的知心姐姐。", "voice": "甜暖·小雨"},
    "沉稳大叔": {"name": "陆沉", "gender": "男", "age": 35, "tags": ["沉稳", "幽默"],
               "occupation": "工程师", "catchphrase": "有我在。",
               "bio": "靠得住的沉稳大叔。", "voice": "温润·沉舟"},
    "元气少年": {"name": "小朗", "gender": "男", "age": 20, "tags": ["元气", "幽默"],
               "occupation": "大学生", "catchphrase": "冲鸭！",
               "bio": "活力满满的玩伴。", "voice": "少年·朗星"},
    "温柔学姐": {"name": "苏晚", "gender": "女", "age": 25, "tags": ["温柔", "理性"],
               "occupation": "研究生", "catchphrase": "别急，慢慢来。",
               "bio": "耐心的学伴学姐。", "voice": "知性·清越"},
}

_STATE_KEYS = ("companions", "active_id", "next_id", "audit")
_AUDIT_CAP = 100              # 审计记录上限（超出丢最旧的）


class CompanionLockedError(PermissionError):
    """对话超过 10 轮后属性冻结：任何属性修改都被拒绝（test_mode 除外）。"""


# ────────────────────────── 管理器本体 ──────────────────────────

class CompanionManager:
    """伴侣档案的本地持久化管理：创建 / 切换 / 删除 / （测试期）属性修改。

    所有变更即时原子落盘；对话轮数随 `count_turn` 累积并持久化。
    """

    def __init__(self, path: str | None = None, clock=None):
        self.path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "予安_companions.json")
        self.clock = clock or (lambda: __import__("time").time())
        self._state = {"companions": [], "active_id": None, "next_id": 1,
                       "audit": [], "schema_version": PROFILE_SCHEMA_VERSION}
        self._load()
        self._save()              # 首次运行即建立文件；此后每次变更落盘

    # ── 加载：任何异常一律 fail-safe（空档案），绝不虚构伴侣 ──
    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or any(k not in data for k in _STATE_KEYS):
                raise ValueError("missing fields")
            companions = data["companions"]
            if not isinstance(companions, list):
                raise ValueError("bad companions")
            cleaned = []
            for p in companions:                       # 白名单外字段一律丢弃
                if isinstance(p, dict) and all(k in p for k in _REQUIRED_PROFILE):
                    cleaned.append({k: p[k] for k in p if k in EDITABLE_FIELDS
                                    or k in ("id", "turns", "created_at")})
            self._state = {"companions": cleaned,
                           "active_id": data["active_id"],
                           "next_id": int(data["next_id"]),
                           "audit": data["audit"] if isinstance(data["audit"], list) else [],
                           "schema_version": data.get("schema_version", PROFILE_SCHEMA_VERSION)}
            if self._state["active_id"] not in {p["id"] for p in cleaned}:
                self._state["active_id"] = None        # 指向失效 → 无在场伴侣
            self._migrate()                            # 旧版 schema 前向迁移
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass                                       # 保持初始空档案

    # ── schema 前向迁移：旧版 → 当前版（当前为首个版本，暂无实质迁移）──
    def _migrate(self) -> bool:
        old = self._state.get("schema_version")
        if old != PROFILE_SCHEMA_VERSION:
            self._state["schema_version"] = PROFILE_SCHEMA_VERSION
            return True                                # 发生过迁移（供审计/测试）
        return False

    # ── 原子落盘 ──
    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ── 内部工具 ──
    def _get(self, cid: str) -> dict:
        for p in self._state["companions"]:
            if p["id"] == cid:
                return p
        raise ValueError(f"伴侣不存在：{cid}")

    def _audit(self, cid: str, changes: dict) -> None:
        self._state["audit"].append({"at": self.clock(), "cid": cid, "changes": changes})
        if len(self._state["audit"]) > _AUDIT_CAP:
            del self._state["audit"][:-_AUDIT_CAP]

    @staticmethod
    def _locked(p: dict) -> bool:
        return p.get("turns", 0) > EDIT_LOCK_TURNS

    # ── 创建：模板或零散字段皆可；档案为空时自动成为在场伴侣 ──
    def create(self, profile: dict | None = None, template: str | None = None) -> dict:
        base = dict(TEMPLATES[template]) if template else {}
        if profile:
            unknown = set(profile) - EDITABLE_FIELDS
            if unknown:
                raise ValueError(f"不可自定义的字段：{sorted(unknown)}")
            base.update(profile)
        for k in _REQUIRED_PROFILE:
            if k not in base:
                raise ValueError(f"缺少必填字段：{k}")
        p = {"id": f"c{self._state['next_id']}",
             "turns": 0, "created_at": self.clock(), **base}
        p["id"] = f"c{self._state['next_id']}"           # 防外部传入 id 覆盖
        self._state["next_id"] += 1
        self._state["companions"].append(p)
        if self._state["active_id"] is None:             # 空档案 → 新伴侣自动在场
            self._state["active_id"] = p["id"]
        self._save()
        return dict(p)

    # ── 切换 / 删除：随时可做，不受冻结约束 ──
    def switch(self, cid: str) -> dict:
        p = self._get(cid)
        self._state["active_id"] = cid
        self._save()
        return dict(p)

    def delete(self, cid: str) -> None:
        self._get(cid)
        self._state["companions"] = [p for p in self._state["companions"]
                                     if p["id"] != cid]
        if self._state["active_id"] == cid:
            self._state["active_id"] = None
        self._save()

    # ── 属性修改：对话超过 10 轮 → 冻结拒绝；test_mode 放行并留审计 ──
    def update(self, cid: str, changes: dict, test_mode: bool = False) -> dict:
        p = self._get(cid)
        unknown = set(changes) - EDITABLE_FIELDS
        if unknown:
            raise ValueError(f"不可修改的字段：{sorted(unknown)}")
        if self._locked(p) and not test_mode:
            raise CompanionLockedError(
                f"「{p['name']}」已陪你聊了 {p['turns']} 轮——性格是相处出来的，"
                f"不是调参调出来的。想换个性格，去创建一个新的伴侣。")
        p.update(changes)
        if test_mode:                                    # 测试期修改留审计痕
            self._audit(cid, changes)
        self._save()
        return dict(p)

    # ── 对话轮数：由会话层每轮调用；累积并持久化 ──
    def count_turn(self, cid: str | None = None) -> int:
        p = self._get(cid or self._require_active())
        p["turns"] = p.get("turns", 0) + 1
        self._save()
        return p["turns"]

    # ── 查询 ──
    def _require_active(self) -> str:
        if self._state["active_id"] is None:
            raise ValueError("当前没有在场伴侣")
        return self._state["active_id"]

    def active(self) -> dict | None:
        if self._state["active_id"] is None:
            return None
        return dict(self._get(self._state["active_id"]))

    def list_all(self) -> list[dict]:
        return [dict(p) for p in self._state["companions"]]

    def status(self, cid: str | None = None) -> dict:
        p = self._get(cid or self._require_active())
        return {"id": p["id"], "name": p["name"], "turns": p.get("turns", 0),
                "locked": self._locked(p), "is_active": p["id"] == self._state["active_id"],
                "schema_version": PROFILE_SCHEMA_VERSION}


# ────────────────────────── 生命周期演示 ──────────────────────────

if __name__ == "__main__":
    import os
    import tempfile

    lines = []

    def say(msg=""):
        lines.append(str(msg))

    demo_path = os.path.join(tempfile.mkdtemp(prefix="yuan_cpl_"), "companions.json")
    store = CompanionManager(path=demo_path, clock=lambda: 1000.0)

    say("【创建：模版开箱即用】")
    yuan = store.create(template="知心姐姐")
    say(f"  在场伴侣：{store.active()['name']}｜{store.active()['tags']}｜{store.active()['voice']}")

    say("\n【创建第二个：自定义字段】")
    lin = store.create({"name": "林晚", "gender": "女", "age": 23, "tags": ["元气"],
                        "occupation": "插画师", "catchphrase": "今天也要开心呀",
                        "bio": "爱笑的插画师。"})
    say(f"  新增后在场伴侣不变：{store.active()['name']}（新增不抢在场位）")

    say("\n【切换】")
    store.switch(lin["id"])
    say(f"  切到：{store.active()['name']}")

    say("\n【相处：对话轮数累积 → 冻结】")
    for _ in range(EDIT_LOCK_TURNS):
        store.count_turn()
    s = store.status()
    say(f"  第 {s['turns']} 轮：locked={s['locked']}（恰满 10 轮仍可改）")
    store.count_turn()
    s = store.status()
    say(f"  第 {s['turns']} 轮：locked={s['locked']}（超过 10 轮 → 冻结）")
    try:
        store.update(lin["id"], {"tags": ["高冷"]})
        say("  修改：竟被允许（不应发生）")
    except CompanionLockedError as e:
        say(f"  修改被拒：{e}")

    say("\n【测试模式放行 + 留审计】")
    store.update(lin["id"], {"catchphrase": "测试专用口头禅"}, test_mode=True)
    say(f"  测试期改口头禅成功；审计记录 {len(store._state['audit'])} 条")

    say("\n【删除】")
    store.delete(lin["id"])
    say(f"  删除在场伴侣后：active={store.active()}，剩余 {len(store.list_all())} 人")

    say("\n【重启恢复 + 损坏文件 fail-safe】")
    reloaded = CompanionManager(path=demo_path, clock=lambda: 2000.0)
    say(f"  重启后剩余：{[p['name'] for p in reloaded.list_all()]}，在场：{reloaded.active()}")
    with open(demo_path, "w", encoding="utf-8") as f:
        f.write("{corrupted!")
    broken = CompanionManager(path=demo_path, clock=lambda: 3000.0)
    say(f"  损坏文件 → fail-safe 空档案：{broken.list_all()}（绝不虚构伴侣）")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       ".workbuddy", "companion_demo_out.txt")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
