"""视觉规划循环：让网页 AI 根据 Logic Pro 实时截图自主规划并执行操作。

核心闭环（直到 AI 判定完成或超步数）：
  截图 Logic Pro → 上传截图给网页 AI → AI 输出 VisualStep（观察/计划/动作）
  → 执行动作 → 把操作记录追加到 history → 回到截图

与 commander.run_pipeline 的区别：
- commander 是「盲打」：AI 一次性输出整阶段动作清单，按顺序执行；
- visual_loop 是「看着打」：每步先截图观察再决定下一步，像人类盯着屏幕操作，
  能根据 Logic Pro 实际状态精确调整（如发现片段没对齐就再量化、发现混音器没开就先打开）。

调用入口：`VisualLoop.run(goal)`，由 server 的 /api/visual 触发。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .ai_engine import AIEngine
from .daw_controller import DAWController
from .models import VisualStep
from .screenshot import ScreenshotCapture
from .task_tracker import TaskTracker

log = logging.getLogger("visual_loop")


class VisualLoop:
    """视觉反馈闭环驱动器。

    Args:
        ai: AI 引擎（需在线才能做视觉规划，否则直接结束）
        daw: DAW 控制器，负责把 VisualStep 的动作落地到 Logic Pro
        screenshot: 截图工具
        tracker: 任务追踪器
        max_steps: 最大循环步数（防止 AI 永远不说 done 导致死循环）
        settle_delay: 每步动作执行后等待 Logic Pro 刷新的秒数，再截图
    """

    def __init__(
        self,
        ai: AIEngine,
        daw: DAWController,
        screenshot: ScreenshotCapture,
        tracker: Optional[TaskTracker] = None,
        max_steps: int = 20,
        settle_delay: float = 1.5,
    ):
        self.ai = ai
        self.daw = daw
        self.screenshot = screenshot
        self.tracker = tracker
        self.max_steps = max(1, max_steps)
        self.settle_delay = max(0.1, settle_delay)
        self._cancel = False
        # 最近一次截图路径（供前端预览）
        self._latest_screenshot: Optional[str] = None

    def cancel(self):
        """请求取消（异步生效，下一个检查点会中断）。"""
        self._cancel = True

    @property
    def latest_screenshot(self) -> Optional[str]:
        return self._latest_screenshot or self.screenshot.latest

    async def run(self, goal: str) -> list[VisualStep]:
        """视觉规划循环主入口。返回各步的 VisualStep 列表。"""
        self._cancel = False
        if self.tracker:
            self.tracker.start(mode="visual", prompt=goal, total=self.max_steps)

        await self._log("info", f"━━━ 视觉规划启动：目标「{goal}」 最多 {self.max_steps} 步 ━━━")
        await self.daw.emit(kind="visual_start", goal=goal, max_steps=self.max_steps)

        # demo 模式 / 非 macOS 无截图能力：直接结束
        if not self.ai.online:
            await self._log("warn", "未连接网页 AI，视觉规划无法进行（需先在设置里连接网页 AI）。")
            await self.daw.emit(kind="visual_done", reason="no_ai", steps=0, goal=goal)
            if self.tracker:
                self.tracker.finish()
            return []

        steps: list[VisualStep] = []
        history = ""  # 累积操作记录，让 AI 知道已做了什么

        for i in range(1, self.max_steps + 1):
            if self._cancel:
                await self._log("warn", "视觉规划已被用户取消。")
                break

            if self.tracker:
                self.tracker.set_stage(f"visual_{i}")

            await self._log("info", f"━━━ 视觉步骤 {i}/{self.max_steps} ━━━")

            # 1. 截图 Logic Pro 当前状态
            shot = await self.screenshot.capture(tag=f"step{i}")
            self._latest_screenshot = shot
            if not shot:
                await self._log("warn", "截图失败（非 macOS 或 Logic Pro 未运行），视觉循环终止。")
                await self.daw.emit(kind="visual_done", reason="no_screenshot",
                                    steps=len(steps), goal=goal)
                break
            await self.daw.emit(kind="screenshot_captured",
                                step=i, path=shot, tag=f"step{i}")

            # 2. 让 AI 看截图规划下一步
            step = await self.ai.plan_from_screenshot(goal, shot, history, log_cb=self._log)
            steps.append(step)
            await self.daw.emit(
                kind="visual_step",
                step=i,
                observation=step.observation,
                plan=step.plan,
                done=step.done,
                rationale=step.rationale or "",
            )
            await self._log("info", f"[步骤{i} 观察] {step.observation}")
            await self._log("info", f"[步骤{i} 计划] {step.plan}")

            # 3. 若 AI 判定完成，结束
            if step.done:
                await self._log("info", f"✓ 视觉规划完成：AI 判定目标已达成（共 {len(steps)} 步）。")
                break

            # 4. 执行本步动作
            await self._execute_step_actions(step, i)

            # 5. 累积操作记录
            history = self._accumulate_history(history, step, i)

            # 6. 等 Logic Pro 刷新后再截图
            await asyncio.sleep(self.settle_delay)

        else:
            await self._log("warn", f"已达最大步数 {self.max_steps}，视觉循环停止（AI 未判定完成）。")

        if self.tracker and not self._cancel:
            self.tracker.finish()
        await self.daw.emit(kind="visual_done", reason="completed" if (steps and steps[-1].done) else "max_steps",
                            steps=len(steps), goal=goal)
        return steps

    # ---------- 执行 VisualStep 的动作 ----------
    async def _execute_step_actions(self, step: VisualStep, step_idx: int):
        """把 VisualStep 的动作字段分发到 DAWController 对应方法。

        顺序与 commander.execute_stage 一致（人类工作流），
        但 VisualStep 没有 project/regions（视觉模式不直接写 MIDI，靠 region_ops 编辑已有片段）。
        """
        if self._cancel:
            return

        # transports（先定位，像人类先点播放头位置）
        for tr in step.transports:
            if self._cancel: break
            await self.daw.transport(tr)
            await asyncio.sleep(0.1)

        # 轨道
        for t in step.tracks:
            if self._cancel: break
            await self.daw.ensure_track(t)
            await asyncio.sleep(0.1)

        # 轨道堆栈
        for ts in step.track_stacks:
            if self._cancel: break
            await self.daw.create_track_stack(ts)
            await asyncio.sleep(0.1)

        # 片段编辑
        for ro in step.region_ops:
            if self._cancel: break
            await self.daw.region_op(ro)
            await asyncio.sleep(0.1)

        # 总线（混音前先建总线）
        for b in step.buses:
            if self._cancel: break
            await self.daw.create_bus(b)
            await asyncio.sleep(0.1)

        # 混音
        for m in step.mix:
            if self._cancel: break
            await self.daw.apply_mix(m)
            await asyncio.sleep(0.1)

        # 插件参数微调
        for pp in step.plugin_params:
            if self._cancel: break
            await self.daw.set_plugin_param(pp)
            await asyncio.sleep(0.1)

        # 自动化
        for a in step.automation:
            if self._cancel: break
            await self.daw.apply_automation(a)
            await asyncio.sleep(0.1)

        # 标记
        for m in step.markers:
            if self._cancel: break
            await self.daw.add_marker(m)
            await asyncio.sleep(0.1)

        # 速度变化
        for tc in step.tempo_changes:
            if self._cancel: break
            await self.daw.add_tempo_change(tc)
            await asyncio.sleep(0.1)

        # 录音准备
        if step.record and not self._cancel:
            await self.daw.setup_record(step.record)

        # 母带
        if step.master_plugins and not self._cancel:
            await self.daw.apply_master(step.master_plugins)

        # 导出
        if step.bounce and not self._cancel:
            await self.daw.bounce(step.bounce)

        # UI 动作（保存/视图切换，放最后）
        for a in step.actions:
            if self._cancel: break
            await self.daw.ui_action(a)
            await asyncio.sleep(0.1)

    # ---------- 历史记录累积 ----------
    def _accumulate_history(self, history: str, step: VisualStep, step_idx: int) -> str:
        """把本步操作追加到历史记录，供下一步 AI 参考。"""
        parts = [history] if history else []
        actions_desc = []
        if step.transports: actions_desc.append(f"传输{[t.op for t in step.transports]}")
        if step.tracks: actions_desc.append(f"建轨{[t.name for t in step.tracks]}")
        if step.region_ops: actions_desc.append(f"片段{[r.op for r in step.region_ops]}")
        if step.mix: actions_desc.append(f"混音{[m.track for m in step.mix]}")
        if step.buses: actions_desc.append(f"总线{[b.name for b in step.buses]}")
        if step.plugin_params: actions_desc.append(f"插件参数{len(step.plugin_params)}项")
        if step.automation: actions_desc.append(f"自动化{len(step.automation)}条")
        if step.markers: actions_desc.append(f"标记{[m.name for m in step.markers]}")
        if step.master_plugins: actions_desc.append(f"母带{[p.name for p in step.master_plugins]}")
        if step.actions: actions_desc.append(f"UI{[a.op for a in step.actions]}")
        if step.bounce: actions_desc.append("导出")
        if step.record: actions_desc.append(f"录音{step.record.track}")
        desc = "；".join(actions_desc) if actions_desc else "无动作"
        parts.append(f"[步骤{step_idx}] 计划：{step.plan}；执行：{desc}")
        return "\n".join(parts)

    # ---------- 日志 ----------
    async def _log(self, level: str, message: str):
        log.log(getattr(logging, level.upper(), logging.INFO), message)
        await self.daw.log(level, message)
