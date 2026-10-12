# -*- coding: utf-8 -*-
"""
予安 · 导航陪伴插件 NavCompanionPlugin（§16.4，按 §16.6 契约接入）
==================================================================

「陪你走这段路」——一个标准的外置插件：InteractionPlugin 子类，
register_plugin() 注册即接入，unregister_plugin() 卸载即失效。

§16.4 规格落点：
- 确认才进入：检测到导航先问「要不要我陪你走这段？」，接受才开播报；
  婉拒后本段行程不再问（到站/关导航后重置）。
- 播报克制，只说三类话：① 路况突变提醒；② 长直路段解闷（接 §16.3 话题卡）；
  ③ 抵达道贺。
- 转向、路口绝不插话，高速/复杂路况自动静默（导航信息永远是第一优先级）。
  配套引擎规则：插件静默不消耗节流窗口——安静 ≠ 互动。
- 困意/疲惫（接 §8.1 护栏）：不接话聊天、不递话题，只温和提醒休息。
- 未确认前（offered）一切播报静默——确认才进入，不抢跑。

信号：Sig.NAV_EVENT（scope="nav"，需 §16.1 授权）；
事件词汇：nav_started / nav_accept / nav_decline / traffic / long_road /
         arrival / turn / highway / fatigue / nav_end。
生产版上层负责把「检测到导航前台」转成 nav_started 事件（需授权 + 场景在「出行」内）。
"""

from 予安_StateEngine import Action, InteractionPlugin, Sig


class NavCompanionPlugin(InteractionPlugin):
    """导航陪伴：确认才进入、只说三类话、转向高速绝不插话。"""

    def __init__(self, topics=None):
        super().__init__(
            name="nav_companion",
            signals=(Sig.NAV_EVENT,),
            scope="nav",
            actions=("sync_react",),
            replies={},                          # 播报逻辑在 react 里，不走静态资源包
            anims={},
        )
        self.topics = topics                     # 可选：§16.3 TopicEngine，长路段递话题
        self.mode = "off"                        # off | offered | on | declined
        self._fatigued = False                   # 困意标记：之后不再接话聊天

    # ── 内部：统一动作出口 ──
    @staticmethod
    def _say(text, event, anim="lean_in"):
        return Action("sync_react", text=text, animation=anim,
                      detail={"event": event})

    # ── 插件反应逻辑（§16.4 三类话 + 硬静默）──
    def react(self, event, ctx=None):
        if event == "nav_started":
            if self.mode != "off":               # 已问过 / 已在陪 / 已婉拒：不重复
                return None
            self.mode = "offered"
            return self._say("检测到你在导航，要不要我陪你走这段？不吵你，路上解解闷。",
                             event)
        if event == "nav_accept":
            if self.mode != "offered":
                return None
            self.mode = "on"
            return self._say("好，我陪着。转向和路口我不插话，导航说了算。", event)
        if event == "nav_decline":
            if self.mode != "offered":
                return None
            self.mode = "declined"               # 本段行程不再问
            return self._say("好，那你专心开车，需要我随时叫我。", event)
        if event == "turn":
            return None                          # 转向路口绝不插话（导航第一优先级）
        if event == "highway":
            return None                          # 高速/复杂路况：自动静默
        if event == "traffic":
            if self.mode != "on":
                return None                      # 确认前不插话
            return self._say("前面有点堵，别急，安全第一，我陪着。", event)
        if event == "long_road":
            if self.mode != "on" or self._fatigued:
                return None                      # 未确认 / 困意后不接话聊天
            if self.topics is not None:
                t = self.topics.suggest()
                if t is not None:
                    return self._say(f"这段路挺长，给你说个事：{t['share']}"
                                     f"（{t['source']}刷到的）", event)
            return self._say("前面路还长，想听点歌还是聊聊天？", event)
        if event == "fatigue":
            self._fatigued = True                # 接 §8.1：不接话，只提醒休息
            return self._say("有点困了？前面找地方歇会儿，不急这一时，我等你。",
                             event, anim="stay_close")
        if event == "arrival":
            if self.mode != "on":
                return None
            self.mode = "off"                    # 行程结束，复位等下一段
            self._fatigued = False
            return self._say("到啦！一路辛苦，慢走。", event, anim="cheer")
        if event == "nav_end":
            self.mode = "off"
            self._fatigued = False
            return None                          # 用户取消路线：不追问
        return None


# ────────────────────────── 演示 ──────────────────────────

if __name__ == "__main__":
    from 予安_StateEngine import Consent, StateEngine
    from 予安_TopicEngine import TopicEngine

    t = [1000.0]
    topics = TopicEngine()
    eng = StateEngine(consent=Consent(master=True,
                                      scopes={"game", "read", "work", "nav"}),
                      topics=topics)
    eng.clock = lambda: t[0]
    eng.register_plugin(NavCompanionPlugin(topics=topics))   # 注册即接入
    eng.handle(Sig.SUMMON)

    def fire(event):
        t[0] += 181                                          # 跨过同频节流窗口
        a = eng.handle(Sig.NAV_EVENT, {"event": event})
        if a:
            print(f"[{eng.state}] nav:{event} → {a.kind}｜{a.text}｜动画:{a.animation}")
        else:
            print(f"[{eng.state}] nav:{event} → （静默）")

    print("── 导航陪伴演示（§16.4）──")
    fire("nav_started")       # 提议：要不要我陪你走这段？
    fire("turn")              # 转向 → 静默（且不耗节流窗口）
    fire("traffic")           # 确认前不插话 → 静默
    fire("nav_accept")        # 确认 → 进入陪伴
    fire("turn")              # 转向 → 静默
    fire("long_road")         # 长路段 → 递话题卡解闷
    fire("fatigue")           # 困意 → 温和提醒休息
    fire("long_road")         # 困意后 → 不接话聊天，静默
    fire("highway")           # 高速 → 自动静默
    fire("arrival")           # 抵达 → 道贺并复位
