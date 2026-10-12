# -*- coding: utf-8 -*-
"""予安 · 版本升级护栏（§6.1 / §17 版本管理的"长牙"机制）
====================================================================

为什么需要它：
  上一轮我们给每个子系统都打了版本常量（policy / prompt_template / output_schema /
  profile_schema / safety_rules / state_machine / plugin_contract / model_config /
  memory_schema / app），但「升了版本」和「内容真的变了」之间是脱节的——
  全靠人肉 review。本护栏把两者钉死：

    1. 内容变了却没升版本        → BREAKING（下游/审计失明，必须升）
    2. 升了版本但删了字段/词/契约项 → BREAKING（破坏性变更，需迁移说明 + 复核）
    3. 升了版本只新增（向后兼容）  → OK（加危机词、加输出字段都安全）
    4. 升了版本但内容无变化       → WARN（无意义 bump）
    5. 升了版本且文案/配置变了     → WARN（已升版本，请人工复核安全性）

受约束内容的指纹：
  - 结构化（删项=BREAKING）：output_schema（JSON 字段）/ safety_rules（危机词）/
    profile_schema（可编辑字段白名单）/ plugin_contract（契约字段）/
    memory_schema（长期记忆记录类型）/ tts_voice（声线模版增删）
  - 不透明（变了=需复核）：policy（场景目录+敏感硬边界）/ prompt_template（固定段）/
    state_machine（状态机/信号面）/ model_config（调用配置默认值 + json 响应模式）/
    tts_voice_params（声线参数 + 路由别名：pitch/speed/timbre/aliases）
  - 聚合：app（发布版本，仅跟踪）

用法：
  python 予安_version_guard.py                 # 比对基线，有 BREAKING 则 exit 1
  python 予安_version_guard.py --update-baseline  # 把当前快照记为新基线
  python 予安_version_guard.py --strict          # WARN 也视为失败（CI 卡更严）
  python 予安_version_guard.py --manifest 路径    # 指定基线文件位置

CI 接入：在「提交前 / 构建流水线」跑一次 `python 予安_version_guard.py`，
  exit 1 = 拦下。基线文件 予安_version_manifest.json 需随代码一起提交，
  版本升级时先 --update-baseline 再提交，护栏即成为可回溯的"版本牙齿"。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys


# ────────────────────────── 指纹工具 ──────────────────────────

def _h(*parts: str) -> str:
    """稳定的内容指纹：多段拼接后取 sha256 前 24 位。"""
    sep = "\u241f"  # 单元分隔符，避免段间串味
    return hashlib.sha256(sep.join(parts).encode("utf-8")).hexdigest()[:24]


def _parse_output_keys(block: str) -> list:
    """从 `【输出】JSON：{a, b, c}` 解析契约字段名（顺序无关）。"""
    import re
    m = re.search(r"\{([^}]*)\}", block or "")
    if not m:
        return []
    return sorted(k.strip() for k in m.group(1).split(",") if k.strip())


# ────────────────────────── 各子系统指纹采集 ──────────────────────────
# 每个 governor 返回 (version:str, content, summary:str)。
#   content 形态：结构化键 = 排序后的 list（可算增删）；不透明键 = 指纹 str；预留 = None。

# 注意：所有 governor 均「动态读模块属性」（import 模块后取属性），
# 不使用 `from module import name`——后者会在导入时刻把值冻结，
# 导致 monkeypatch 改不掉、变异测试探不出牙齿。

def _gov_policy():
    import 予安_ConsentStore as CS
    content = _h(
        json.dumps(CS.SCENE_CATALOG, ensure_ascii=False, sort_keys=True),
        repr(sorted(CS.SENSITIVE_SCENES)),
    )
    return CS.POLICY_VERSION, content, "授权说明（场景目录 + 敏感场景硬边界）"


def _gov_prompt_template():
    import 予安_Prompt as PR
    content = _h(PR._RULES_BLOCK, PR._REDLINE_BLOCK, PR._OUTPUT_BLOCK)
    return PR.PROMPT_VERSION, content, "Prompt 固定段（反应规则 + 红线 + 输出块）"


def _gov_output_schema():
    import 予安_Prompt as PR
    return PR.OUTPUT_SCHEMA_VERSION, _parse_output_keys(PR._OUTPUT_BLOCK), \
        "LLM 输出 JSON 契约字段"


def _gov_profile_schema():
    import 予安_CompanionManager as CM
    return CM.PROFILE_SCHEMA_VERSION, sorted(CM.EDITABLE_FIELDS), \
        "伴侣档案可编辑字段白名单"


def _gov_safety_rules():
    import 予安_StateEngine as SE
    return SE.SAFETY_RULES_VERSION, sorted(SE.CRISIS_WORDS), "危机词库"


def _gov_state_machine():
    import 予安_StateEngine as SE
    surface = []
    for ns in (SE.State, SE.Sig):
        for v in vars(ns).values():
            if isinstance(v, str):
                surface.append(v)
    return SE.STATE_VERSION, _h(json.dumps(sorted(surface), ensure_ascii=False)), \
        "状态机状态 / 信号面"


def _gov_plugin_contract():
    import 予安_StateEngine as SE
    return SE.PLUGIN_CONTRACT_VERSION, \
        sorted(SE.InteractionPlugin.__dataclass_fields__.keys()), "插件契约字段"


def _gov_model_config():
    import 予安_LLM as LLM
    chat_def = LLM.LLMClient.chat.__defaults__ or ()
    init_def = LLM.LLMClient.__init__.__defaults__ or ()
    temperature = chat_def[0] if chat_def else None
    timeout = init_def[3] if len(init_def) > 3 else None
    retries = init_def[4] if len(init_def) > 4 else None
    content = _h(json.dumps(
        {"temperature": temperature, "timeout": timeout, "retries": retries,
         "json_mode": getattr(LLM, "JSON_RESPONSE_MODE", None)},
        sort_keys=True))
    return LLM.MODEL_CONFIG_VERSION, content, "LLM 调用配置默认值（含 json 响应模式）"


def _gov_memory_schema():
    import 予安_MemoryStore as MS
    return MS.MEMORY_SCHEMA_VERSION, sorted(MS.MEMORY_FIELDS), "长期记忆记录类型"


def _gov_tts_voice():
    import 予安_VoiceTemplate as VT
    return VT.TTS_VOICE_VERSION, VT.all_ids(), \
        "声线模版（声线 ID 集合：删=阻断 / 加=兼容）"


def _gov_tts_voice_params():
    import 予安_VoiceTemplate as VT
    import hashlib, json
    # 排除 id（由 tts_voice 的 set 键管增删），其余字段归一化哈希：
    # 改 name/pitch/speed/timbre/gender/preview_text/voice_id 都会触发指纹变化。
    # 另纳入运行时路由的宽容别名表（TIMBRE_ALIASES）——路由配置变更同样需复核。
    payload = json.dumps(
        {
            "templates": [{k: v for k, v in t.items() if k != "id"}
                          for t in VT.VOICE_TEMPLATES],
            "aliases": VT.TIMBRE_ALIASES,
        },
        ensure_ascii=False, sort_keys=True)
    fp = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return VT.TTS_VOICE_VERSION, fp, \
        "声线参数 + 路由别名（pitch/speed/timbre/aliases，变了需复核）"


def _gov_app():
    import 予安_Version as V
    return V.APP_VERSION, V.APP_VERSION, "整体发布版本"


# 键 → governor
GOVERNORS = {
    "policy": _gov_policy,
    "prompt_template": _gov_prompt_template,
    "output_schema": _gov_output_schema,
    "profile_schema": _gov_profile_schema,
    "safety_rules": _gov_safety_rules,
    "state_machine": _gov_state_machine,
    "plugin_contract": _gov_plugin_contract,
    "model_config": _gov_model_config,
    "memory_schema": _gov_memory_schema,
    "tts_voice": _gov_tts_voice,
    "tts_voice_params": _gov_tts_voice_params,
    "app": _gov_app,
}

# 内容形态分类：决定"删项"是否判破坏
SET_KEYS = {"output_schema", "safety_rules", "profile_schema", "plugin_contract",
            "memory_schema", "tts_voice"}
REVIEW_KEYS = {"policy", "prompt_template", "state_machine", "model_config",
              "tts_voice_params"}
RESERVED_KEYS = set()


# ────────────────────────── 数据结构 ──────────────────────────

class Change:
    """一条比对结论。level ∈ {BREAKING, WARN, OK, INFO, NEW}。"""

    __slots__ = ("key", "level", "message", "removed", "added")

    def __init__(self, key, level, message, removed=None, added=None):
        self.key = key
        self.level = level
        self.message = message
        self.removed = removed or []
        self.added = added or []


class Entry:
    """基线/当前快照中的一条记录。"""

    def __init__(self, version, content, summary):
        self.version = version
        self.content = content          # list（结构化）| str（指纹）| None（预留）
        self.summary = summary

    @property
    def is_set(self):
        return isinstance(self.content, list)


# ────────────────────────── 快照 / 基线 ──────────────────────────

def snapshot_current() -> dict[str, Entry]:
    """采集当前代码里所有受约束子系统的 (版本, 内容指纹, 说明)。"""
    out: dict[str, Entry] = {}
    for key, gov in GOVERNORS.items():
        version, content, summary = gov()
        out[key] = Entry(version, content, summary)
    return out


def snapshot_to_dict(snap: dict[str, Entry]) -> dict:
    return {
        "generated_by": "予安_version_guard",
        "entries": {
            k: {"version": e.version, "content": e.content, "summary": e.summary}
            for k, e in snap.items()
        },
    }


def load_baseline(path: str) -> dict[str, Entry] | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("entries", {})
        return {k: Entry(e["version"], e.get("content"), e.get("summary", ""))
                for k, e in entries.items()}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def save_baseline(path: str, snap: dict[str, Entry]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot_to_dict(snap), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ────────────────────────── 比对 / 分类 ──────────────────────────

_LABELS = {
    "policy": "授权说明", "prompt_template": "Prompt 文案",
    "output_schema": "输出契约", "profile_schema": "伴侣档案",
    "memory_schema": "长期记忆", "state_machine": "状态机/行为",
    "safety_rules": "危机词库", "plugin_contract": "插件契约",
    "model_config": "模型配置", "tts_voice": "声线模版",
    "tts_voice_params": "声线参数", "app": "发布版本",
}


def classify(key: str, base: Entry, cur: Entry) -> Change | None:
    """核心分类逻辑。返回 Change 或 None（无变化）。"""
    label = _LABELS.get(key, key)

    # 聚合 / 预留：仅跟踪版本，不判破坏
    if key == "app":
        if base.version != cur.version:
            return Change(key, "INFO", f"{label} 升级 {base.version} → {cur.version}")
        return None
    if key in RESERVED_KEYS:
        if base.version != cur.version:
            return Change(key, "INFO", f"{label} 版本变更（内容尚未接入，仅记录）")
        return None

    # 结构化键：内容按集合比对，可算增删
    if key in SET_KEYS:
        base_set = frozenset(base.content or [])
        cur_set = frozenset(cur.content or [])
        if cur.version == base.version:
            if cur_set != base_set:
                removed = sorted(base_set - cur_set)
                added = sorted(cur_set - base_set)
                return Change(key, "BREAKING",
                              f"{label} 内容变了却没升版本（移除 {removed} / 新增 {added}）",
                              removed, added)
        else:
            removed = sorted(base_set - cur_set)
            if removed:
                return Change(key, "BREAKING",
                              f"{label} 移除项（即便升了版本也需迁移说明 + 复核）：{removed}",
                              removed)
            added = sorted(cur_set - base_set)
            if added:
                return Change(key, "OK",
                              f"{label} 向后兼容（新增 {added}）", added=added)
            # 版本变了但集合相同（如重排）→ 视为无实质变化
            return None
        return None

    # 不透明键：内容按指纹比对
    if key in REVIEW_KEYS:
        if cur.version == base.version:
            if base.content != cur.content:
                return Change(key, "BREAKING",
                              f"{label} 文案/配置变了却没升版本（需升版本以便溯源）")
        else:
            if base.content != cur.content:
                return Change(key, "WARN",
                              f"{label} 文案/配置变更，已升版本，请人工复核安全性")
            return Change(key, "WARN", f"{label} 升了版本但内容无变化（无意义 bump）")
        return None

    return None


def diff(baseline: dict[str, Entry], current: dict[str, Entry]) -> list[Change]:
    changes: list[Change] = []
    for key, cur in current.items():
        if key not in baseline:
            changes.append(Change(key, "NEW",
                                 f"{_LABELS.get(key, key)} 首次纳入护栏（新增受约束项）"))
            continue
        c = classify(key, baseline[key], cur)
        if c is not None:
            changes.append(c)
    # 基线有、当前没有的键（被移除治理）→ 提示
    for key in baseline:
        if key not in current:
            changes.append(Change(key, "WARN",
                                 f"{_LABELS.get(key, key)} 从护栏治理中消失（请确认是否故意）"))
    return changes


# ────────────────────────── 报告 / CLI ──────────────────────────

_LEVEL_ORDER = {"BREAKING": 0, "WARN": 1, "NEW": 2, "OK": 3, "INFO": 4}
_LEVEL_MARK = {"BREAKING": "✗ 阻断", "WARN": "! 复核", "NEW": "+ 新增",
               "OK": "✓ 兼容", "INFO": "· 提示"}


def render_report(changes: list[Change]) -> str:
    if not changes:
        return "版本护栏：当前代码与基线一致，无阻断 / 复核项。"
    lines = ["版本升级护栏比对结果："]
    for c in sorted(changes, key=lambda x: (_LEVEL_ORDER.get(x.level, 9), x.key)):
        mark = _LEVEL_MARK.get(c.level, c.level)
        lines.append(f"  [{mark}] {_LABELS.get(c.key, c.key)} v→ {c.message}")
    return "\n".join(lines)


def run_check(manifest: str, strict: bool) -> int:
    baseline = load_baseline(manifest)
    if baseline is None:
        # 无基线：以当前为基线，视为首次建立
        save_baseline(manifest, snapshot_current())
        print(f"无基线文件，已建立初始基线：{manifest}")
        print("（请将其提交到仓库，作为版本升级比对的参考点）")
        return 0

    current = snapshot_current()
    changes = diff(baseline, current)
    print(render_report(changes))

    breaking = [c for c in changes if c.level == "BREAKING"]
    warn = [c for c in changes if c.level == "WARN"]
    if breaking:
        print(f"\n→ 存在 {len(breaking)} 项阻断性变更，已拦下（exit 1）。")
        return 1
    if warn and strict:
        print(f"\n→ --strict：存在 {len(warn)} 项需复核项，视为失败（exit 1）。")
        return 1
    if warn:
        print(f"\n→ 存在 {len(warn)} 项需复核项（非阻断）。")
    print("\n→ 通过（exit 0）。")
    return 0


def main(argv=None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    ap = argparse.ArgumentParser(description="予安 版本升级护栏")
    ap.add_argument("--manifest", default=os.path.join(here, "予安_version_manifest.json"),
                    help="基线 manifest 路径")
    ap.add_argument("--update-baseline", action="store_true",
                    help="把当前快照记为新基线")
    ap.add_argument("--strict", action="store_true",
                    help="WARN 也视为失败")
    args = ap.parse_args(argv)

    if args.update_baseline:
        save_baseline(args.manifest, snapshot_current())
        print(f"已更新基线：{args.manifest}")
        return 0
    return run_check(args.manifest, args.strict)


if __name__ == "__main__":
    sys.exit(main())
