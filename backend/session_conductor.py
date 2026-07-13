"""会话指挥器:让 AI 像人类一样即兴编曲的统一执行器。

与 commander.run_pipeline(固定四阶段流水线)的区别:
- 无阶段概念:AI 随时可以做任意操作
- 有状态:维护 session_state(操作历史、版本快照、听觉反馈)
- 支持试听:集成 listen_and_capture + AI 听取反馈
- 支持回退:undo_to 可回退到任意步骤
- 支持即兴插入:用户/AI 可随时插入指令

工作模式:
1. AI 输出 FreeAction(我想加一段钢琴 + 试听副歌)
2. SessionConductor 执行动作 → 落到 DAW
3. 如果有 listen,导出音频 → AI 听取 → 反馈注入 session_state
4. AI 看到反馈 → 决定下一步(改 EQ / 加鼓 / 满意了结束)
5. 循环直到 satisfied=True
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .models import FreeAction, ListenAction
from .daw_controller import DAWController

log = logging.getLogger("session_conductor")


@dataclass
class SessionState:
    """即兴编曲会话状态:记录操作历史、版本快照、听觉反馈。"""
    # 操作历史(每步的 intent + 动作摘要)
    history: list[dict] = field(default_factory=list)
    # 听觉反馈历史(每次试听的反馈)
    audio_feedbacks: list[dict] = field(default_factory=list)
    # 当前步骤编号(1-based)
    step_count: int = 0
    # 是否已完成(AI 说 satisfied=True)
    completed: bool = False
    # 用户目标
    user_goal: str = ""

    def add_step(self, intent: str, actions_summary: dict):
        """记录一步操作。"""
        self.step_count += 1
        self.history.append({
            "step": self.step_count,
            "intent": intent,
            "actions": actions_summary,
        })

    def add_audio_feedback(self, bars: str, feedback: dict):
        """记录一次听觉反馈。"""
        self.audio_feedbacks.append({"bars": bars, "feedback": feedback})

    def get_context_for_ai(self) -> str:
        """生成给 AI 的上下文摘要(操作历史 + 听觉反馈)。"""
        lines = []
        if self.user_goal:
            lines.append(f"用户目标:{self.user_goal}")
        if self.history:
            lines.append(f"\n已做 {self.step_count} 步:")
            for h in self.history[-5:]:  # 最近 5 步
                lines.append(f"  步骤{h['step']}: {h['intent']}")
        if self.audio_feedbacks:
            lines.append(f"\n听觉反馈({len(self.audio_feedbacks)} 次):")
            for af in self.audio_feedbacks[-3:]:  # 最近 3 次反馈
                fb = af["feedback"]
                if isinstance(fb, dict):
                    lines.append(f"  {af['bars']}: {fb.get('heard', '')} | 评分 {fb.get('rating', '?')}")
                    if fb.get("issues"):
                        lines.append(f"    问题: {', '.join(fb['issues'])}")
                    if fb.get("suggestions"):
                        lines.append(f"    建议: {', '.join(fb['suggestions'])}")
        return "\n".join(lines)


class SessionConductor:
    """即兴编曲会话指挥器:统一处理 FreeAction + 试听 + 反馈。"""

    def __init__(self, daw: DAWController, ai=None):
        self.daw = daw
        self.ai = ai
        self.state = SessionState()

    async def execute_free_action(self, action: FreeAction) -> dict:
        """执行一个即兴动作,返回执行结果摘要。"""
        summary = {}

        # 处理回退(undo_to)——在 add_step 之前处理,避免当前步被算进 undo 次数
        if action.undo_to and action.undo_to < self.state.step_count:
            # 回退到指定步骤:撤销之后的操作
            # Logic Pro 的 undo 是单步的,需要多次 undo
            steps_to_undo = self.state.step_count - action.undo_to
            for _ in range(steps_to_undo):
                await self.daw.undo()
            await self.daw.log("info", f"回退到步骤 {action.undo_to}(撤销 {steps_to_undo} 步)")
            summary["undone_steps"] = steps_to_undo
            self.state.history = self.state.history[:action.undo_to]
            self.state.step_count = action.undo_to

        self.state.add_step(action.intent, summary)

        # 执行工程级操作(允许任意时刻调整,不像 commander 限制只有 compose 能建工程)
        if action.project:
            if self.daw.has_project:
                # 已有工程,只更新速度/标记等
                await self.daw.log("info", f"调整工程:{action.project.title}")
            else:
                # 没有工程,创建
                from .models import TempoSpec
                tempo = action.project.tempo or TempoSpec()
                await self.daw.create_project(tempo, action.project.title)

        # 速度变化
        for tc in action.tempo_changes:
            await self.daw.add_tempo_change(tc)
            summary.setdefault("tempo_changes", 0)
            summary["tempo_changes"] += 1

        # 标记
        for m in action.markers:
            await self.daw.add_marker(m)
            summary.setdefault("markers", 0)
            summary["markers"] += 1

        # 轨道
        for t in action.tracks:
            await self.daw.ensure_track(t)
            summary.setdefault("tracks", 0)
            summary["tracks"] += 1

        # 轨道堆栈
        for ts in action.track_stacks:
            await self.daw.create_track_stack(ts)
            summary.setdefault("track_stacks", 0)
            summary["track_stacks"] += 1

        # MIDI 片段
        for r in action.regions:
            bpm = 120  # 默认,后续从工程获取
            await self.daw.add_region(r, bpm)
            summary.setdefault("regions", 0)
            summary["regions"] += 1

        # 片段编辑
        for op in action.region_ops:
            await self.daw.region_op(op)
            summary.setdefault("region_ops", 0)
            summary["region_ops"] += 1

        # 传输
        for t in action.transports:
            await self.daw.transport(t)
            summary.setdefault("transports", 0)
            summary["transports"] += 1

        # 混音
        for m in action.mix:
            await self.daw.apply_mix(m)
            summary.setdefault("mix", 0)
            summary["mix"] += 1

        # 总线
        for b in action.buses:
            await self.daw.create_bus(b)
            summary.setdefault("buses", 0)
            summary["buses"] += 1

        # 插件参数
        for pp in action.plugin_params:
            await self.daw.set_plugin_param(pp)
            summary.setdefault("plugin_params", 0)
            summary["plugin_params"] += 1

        # 自动化
        for a in action.automation:
            await self.daw.apply_automation(a)
            summary.setdefault("automation", 0)
            summary["automation"] += 1

        # 母带插件
        for p in action.master_plugins:
            await self.daw.apply_master(p)
            summary.setdefault("master_plugins", 0)
            summary["master_plugins"] += 1

        # UI 动作
        for a in action.actions:
            await self.daw.ui_action(a)
            summary.setdefault("actions", 0)
            summary["actions"] += 1

        # 试听-反馈(核心即兴能力)
        if action.listen:
            audio_path = await self.daw.listen_and_capture(
                action.listen.start_bar, action.listen.end_bar
            )
            if audio_path and self.ai:
                feedback = await self.ai.listen_to_audio(audio_path, action.listen.focus)
                if feedback:
                    self.state.add_audio_feedback(
                        f"{action.listen.start_bar}-{action.listen.end_bar}",
                        feedback,
                    )
                    summary["audio_feedback"] = feedback
                    await self.daw.log("info", f"听觉反馈: {feedback.get('heard', '')[:100]}")

        # 保存(每次动作后自动保存,防止丢失)
        await self.daw.save_project()

        # 检查是否完成
        if action.satisfied:
            self.state.completed = True

        return summary

    async def run_improvise_loop(self, user_goal: str, max_steps: int = 30):
        """即兴编曲主循环:AI 主动决策,做一段→试听→改→再试听→..."""
        self.state.user_goal = user_goal
        await self.daw.log("info", f"开始即兴编曲:{user_goal}")

        for step in range(1, max_steps + 1):
            if self.state.completed:
                break

            # 构造上下文
            context = self.state.get_context_for_ai()

            # AI 主动决策下一步
            action = await self.ai.generate_free_action(user_goal, context, step)

            if action is None:
                await self.daw.log("warning", f"步骤 {step}:AI 未返回动作,结束")
                break

            # 执行
            summary = await self.execute_free_action(action)
            await self.daw.log("info", f"步骤 {step}/{max_steps}:{action.intent}")

            # 如果 AI 满意了,结束
            if action.satisfied:
                await self.daw.log("info", "AI 表示满意,即兴编曲完成")
                break

        # 最终保存
        await self.daw.save_project()
        return self.state
