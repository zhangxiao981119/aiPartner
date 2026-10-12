# -*- coding: utf-8 -*-
"""aiPartner · 长期记忆持久化（§2.5 / §17 实现层）
====================================================

承接预留的 MEMORY_SCHEMA_VERSION，把"情感记忆 + 关系史 + 学到的用户事实"
做成按伴侣分桶的本地持久化记忆库。

设计口径（用户 2026-10-12 定）：
  - 只做"持久化 + 喂给 System Prompt"这一层；**不做 LLM 自动抽取**——
    记忆由显式 add() 写入，避免幻觉与虚构。
  - 输出直接对齐 System Prompt 已有的两个占位：
      【情感记忆】{affective_memory}  ←  type=affective 的叙事
      【关系史】  {relationship_note} ←  type=relationship / fact 折叠进来
  - 记录类型白名单 MEMORY_FIELDS（schema 的一部分）：affective / relationship / fact。
    删类型 = 破坏性（旧数据/代码断引用），由版本护栏设为 set 键（删即 BREAKING）。
  - **不重复** aiPartner_TopicEngine 的「口味记忆」（类别偏好已落盘）；**不存** bond
    （bond 属情绪引擎态，走 Prompt 的 bond= 入参）；**不联动** 伴侣删除（按需按 cid 清桶）。

持久化：复用 aiPartner_CompanionManager 范式——本地 JSON 原子写入（tmp + replace），
        重启即恢复；文件缺失/损坏一律 fail-safe（空记忆，绝不虚构一段关系）。

自测：直接运行本文件看完整生命周期演示（写入 → 读取 → 重启恢复 → 损坏 fail-safe）。
测试：test_aiPartner_MemoryStore.py（pytest）
"""

from __future__ import annotations

import json
import os

# ────────────────────────── schema 版本与记录类型 ──────────────────────────

# 长期记忆 schema 版本（§6.1 / §17 版本管理）：增删记录类型（MEMORY_FIELDS）或
# 改记录结构 → 必须升版本；旧记忆加载做前向兼容（缺字段补默认），损坏文件仍 fail-safe。
MEMORY_SCHEMA_VERSION = "2026.10-2"

# 记忆记录类型白名单：记忆 schema 的结构面。增删类型 = 破坏性变更，必须带迁移说明。
#   affective    : 情感记忆——"什么事件让谁产生什么情绪"（§2.5）
#   relationship : 关系史锚点——关系里发生过的关键节点 / 约定
#   fact         : 学到的用户事实——用户教过的操作 / 口味 / 梗（§16.5「慢慢学习」）
MEMORY_FIELDS = frozenset({"affective", "relationship", "fact"})

# 单条记忆记录的允许字段（白名单外的字段加载时丢弃，防手改/防脏数据）
_RECORD_FIELDS = ("type", "text", "emotion", "at", "weight")
_STATE_KEYS = ("schema_version", "memories", "audit")
_AUDIT_CAP = 200
_MAX_AFFECTIVE_DEFAULT = 8          # summarize_for_prompt 默认取最近 N 条情感记忆

# 占位文本：与 aiPartner_Prompt.build_system_prompt 的"暂无"占位保持一致
_PLACEHOLDER = "（暂无）"


class MemoryKindError(ValueError):
    """记忆类型不在 MEMORY_FIELDS 白名单内。"""


# ────────────────────────── 记忆库本体 ──────────────────────────

class MemoryStore:
    """按伴侣分桶的长期记忆本地持久化：写入 / 读取 / 喂 Prompt / 迁移 / fail-safe。"""

    def __init__(self, path: str | None = None, clock=None):
        self.path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "aiPartner_memory.json")
        self.clock = clock or (lambda: __import__("time").time())
        self._state = {"schema_version": MEMORY_SCHEMA_VERSION, "memories": {}, "audit": []}
        self._load()
        self._save()                  # 首次运行即建立文件

    # ── 加载：任何异常一律 fail-safe（空记忆），绝不虚构 ──
    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or any(k not in data for k in _STATE_KEYS):
                raise ValueError("missing fields")
            memories = data["memories"]
            if not isinstance(memories, dict):
                raise ValueError("bad memories")
            cleaned: dict[str, list] = {}
            for cid, recs in memories.items():
                if not isinstance(recs, list):
                    continue
                kept = []
                for r in recs:
                    if isinstance(r, dict) and r.get("type") in MEMORY_FIELDS:
                        # 仅保留白名单字段；缺 weight/at 补默认，保证旧记录可前向读
                        rec = {k: r.get(k) for k in _RECORD_FIELDS}
                        rec["weight"] = float(rec.get("weight") or 1.0)
                        rec["at"] = rec.get("at") or 0.0
                        kept.append(rec)
                cleaned[cid] = kept
            self._state = {
                "schema_version": data.get("schema_version", MEMORY_SCHEMA_VERSION),
                "memories": cleaned,
                "audit": data["audit"] if isinstance(data.get("audit"), list) else [],
            }
            self._migrate()            # 旧版 schema 前向迁移
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass                       # 保持初始空记忆

    # ── schema 前向迁移：旧版 → 当前版（当前为首個真实版，预留占位 2026.10-1 也归一到此）──
    def _migrate(self) -> bool:
        old = self._state.get("schema_version")
        if old != MEMORY_SCHEMA_VERSION:
            self._state["schema_version"] = MEMORY_SCHEMA_VERSION
            return True                # 发生过迁移（供审计/测试）
        return False

    # ── 原子落盘 ──
    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ── 内部工具 ──
    def _audit(self, cid: str, change: dict) -> None:
        self._state["audit"].append({"at": self.clock(), "cid": cid, "change": change})
        if len(self._state["audit"]) > _AUDIT_CAP:
            del self._state["audit"][:-_AUDIT_CAP]

    # ── 写入：显式落一条记忆（不做自动抽取） ──
    def add(self, cid: str, kind: str, text: str, *, emotion=None, weight: float = 1.0,
            at: float | None = None) -> dict:
        if kind not in MEMORY_FIELDS:
            raise MemoryKindError(f"未知记忆类型：{kind}（允许：{sorted(MEMORY_FIELDS)}）")
        rec = {"type": kind, "text": text, "emotion": emotion,
               "at": self.clock() if at is None else float(at), "weight": float(weight)}
        self._state["memories"].setdefault(cid, []).append(rec)
        self._audit(cid, {"add": kind, "text": text})
        self._save()
        return dict(rec)

    # ── 读取：按类型召回 ──
    def recall(self, cid: str, kind: str | None = None) -> list[dict]:
        recs = self._state["memories"].get(cid, [])
        if kind is not None:
            recs = [r for r in recs if r.get("type") == kind]
        return [dict(r) for r in recs]

    def count(self, cid: str) -> int:
        return len(self._state["memories"].get(cid, []))

    # ── 喂 Prompt：返回 (affective_memory, relationship_note) ──
    def summarize_for_prompt(self, cid: str, max_affective: int = _MAX_AFFECTIVE_DEFAULT):
        recs = self._state["memories"].get(cid, [])
        aff = sorted((r for r in recs if r.get("type") == "affective"),
                     key=lambda r: r.get("at", 0), reverse=True)[:max_affective]
        aff_text = "；".join(r.get("text", "") for r in aff) if aff else _PLACEHOLDER
        rel = sorted((r for r in recs if r.get("type") in ("relationship", "fact")),
                     key=lambda r: r.get("at", 0), reverse=True)
        rel_text = "；".join(r.get("text", "") for r in rel) if rel else _PLACEHOLDER
        return aff_text, rel_text

    # ── 状态总览 ──
    def status(self, cid: str) -> dict:
        recs = self._state["memories"].get(cid, [])
        kinds = sorted({r.get("type") for r in recs if r.get("type") in MEMORY_FIELDS})
        return {"schema_version": MEMORY_SCHEMA_VERSION,
                "count": len(recs), "kinds": kinds}


# ────────────────────────── 生命周期演示 ──────────────────────────

if __name__ == "__main__":
    import tempfile

    lines = []
    say = lines.append

    demo_path = os.path.join(tempfile.mkdtemp(prefix="yuan_mem_"), "memory.json")
    store = MemoryStore(path=demo_path, clock=lambda: 1000.0)
    cid = "c1"

    say("【写入：情感记忆 + 关系史 + 学到的用户事实】")
    store.add(cid, "affective", "用户提过加班到凌晨会焦虑", emotion="worry")
    store.add(cid, "relationship", "上周她陪用户熬过一次项目deadline，关系更近")
    store.add(cid, "fact", "用户教过：五子棋要堵活三", weight=1.2)
    aff, rel = store.summarize_for_prompt(cid)
    say(f"  情感记忆 → {aff}")
    say(f"  关系史   → {rel}")

    say("\n【喂给 System Prompt 的两个占位已对齐】")
    say(f"  【情感记忆】{aff} 【关系史】{rel}")

    say("\n【按类型召回 / 计数 / 状态】")
    say(f"  recall(affective)={store.recall(cid, 'affective')}")
    say(f"  count={store.count(cid)} status={store.status(cid)}")

    say("\n【重启恢复 + 损坏文件 fail-safe】")
    reloaded = MemoryStore(path=demo_path, clock=lambda: 2000.0)
    say(f"  重启后 count={reloaded.count(cid)}，关系史仍在：{reloaded.summarize_for_prompt(cid)[1]}")
    with open(demo_path, "w", encoding="utf-8") as f:
        f.write("{corrupted!")
    broken = MemoryStore(path=demo_path, clock=lambda: 3000.0)
    say(f"  损坏文件 → fail-safe 空记忆：count={broken.count(cid)}（绝不虚构）")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       ".workbuddy", "memory_demo_out.txt")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
