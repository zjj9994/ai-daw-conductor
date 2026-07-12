"""指挥官：把 AI 输出的 StageResult 翻译为 DAWController 的有序执行。

负责：阶段编排、按人类工作流顺序执行全部动作类型、事件汇总、任务追踪。

执行顺序（模拟人类制作流程）：
1. 工程（create_project）
2. 速度变化（tempo_changes）
3. 标记（markers）
4. 轨道（tracks）+ 轨道堆栈（track_stacks）
5. 片段写入（regions）
6. 片段编辑操作（region_ops：切割/移动/复制/量化/移调）
7. 传输控制（transports：定位/循环）
8. 总线/辅助通道（buses）
9. 混音（mix）+ 插件参数（plugin_params）
10. 自动化（automation）
11. 录音准备（record）
12. 母带（master_plugins）
13. 导出（bounce）
14. UI 动作（actions：保存/撤销/视图）
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .ai_engine import AIEngine
from .daw_controller import DAWController
from .models import Stage, StageResult
from .task_tracker import TaskTracker

log = logging.getLogger("commander")


class Commander:
    def __init__(self, ai: AIEngine, daw: DAWController, tracker: Optional[TaskTracker] = None):
        self.ai = ai
        self.daw = daw
        self.tracker = tracker
        self._cancel = False

    def cancel(self):
        self._cancel = True

    async def execute_stage(self, result: StageResult, bpm: float):
        """执行单个 StageResult 的全部动作，按人类工作流顺序。"""
        if self.tracker:
            self.tracker.set_stage(result.stage.value)
        await self.daw.log("info", f"===== 执行阶段：{result.stage.value} =====")
        await self.daw.emit(kind="stage_start", stage=result.stage.value, summary=result.summary)

        # 1. 工程
        if result.project:
            await self.daw.create_project(result.project.tempo, result.project.title)
            bpm = result.project.tempo.bpm

        # 2. 速度变化（在轨道/片段之前，像人类先定速度曲线）
        for tc in result.tempo_changes:
            if self._cancel: break
            await self.daw.add_tempo_change(tc)
            await asyncio.sleep(0.05)

        # 3. 标记（编排标记，在编排区定结构）
        for m in result.markers:
            if self._cancel: break
            await self.daw.add_marker(m)
            await asyncio.sleep(0.05)

        # 4. 轨道
        for t in result.tracks:
            if self._cancel: break
            await self.daw.ensure_track(t)
            await asyncio.sleep(0.05)

        # 4.1 轨道堆栈
        for ts in result.track_stacks:
            if self._cancel: break
            await self.daw.create_track_stack(ts)
            await asyncio.sleep(0.05)

        # 5. 写 MIDI 片段
        for r in result.regions:
            if self._cancel: break
            await self.daw.add_region(r, bpm)
            await asyncio.sleep(0.05)

        # 6. 片段编辑操作（像人类在编排区拖拽编辑）
        for ro in result.region_ops:
            if self._cancel: break
            await self.daw.region_op(ro)
            await asyncio.sleep(0.05)

        # 7. 传输控制（定位/循环，混音前先定位到要处理的位置）
        for tr in result.transports:
            if self._cancel: break
            await self.daw.transport(tr)
            await asyncio.sleep(0.05)

        # 8. 总线/辅助通道（混音前先建总线，才能发送）
        for b in result.buses:
            if self._cancel: break
            await self.daw.create_bus(b)
            await asyncio.sleep(0.05)

        # 9. 混音
        for m in result.mix:
            if self._cancel: break
            await self.daw.apply_mix(m)
            await asyncio.sleep(0.05)

        # 9.1 插件参数微调（像人类拧旋钮）
        for pp in result.plugin_params:
            if self._cancel: break
            await self.daw.set_plugin_param(pp)
            await asyncio.sleep(0.05)

        # 10. 自动化
        for a in result.automation:
            if self._cancel: break
            await self.daw.apply_automation(a)
            await asyncio.sleep(0.05)

        # 11. 录音准备
        if result.record:
            if not self._cancel:
                await self.daw.setup_record(result.record)

        # 12. 母带
        if result.master_plugins:
            if not self._cancel:
                await self.daw.apply_master(result.master_plugins)

        # 13. 导出
        if result.bounce:
            if not self._cancel:
                await self.daw.bounce(result.bounce)

        # 14. UI 动作（保存/撤销/视图切换）
        for a in result.actions:
            if self._cancel: break
            await self.daw.ui_action(a)
            await asyncio.sleep(0.05)

        if self.tracker:
            self.tracker.complete_stage(result.stage.value)
        await self.daw.emit(kind="stage_done", stage=result.stage.value, rationale=result.rationale or "")

    async def run_stage(
        self,
        stage: Stage,
        user_prompt: str,
        context: Optional[str] = None,
        bpm: float = 100.0,
    ) -> StageResult:
        await self.daw.log("info", f"AI 生成阶段 [{stage.value}] ...")
        result = await self.ai.generate_stage(stage, user_prompt, context, log_cb=self.daw.log)
        await self.daw.emit(kind="ai_result", stage=result.stage.value, summary=result.summary,
                            rationale=result.rationale or "")
        await self.execute_stage(result, bpm)
        return result

    async def run_pipeline(self, user_prompt: str) -> list[StageResult]:
        """完整四阶段流水线。"""
        self._cancel = False
        results: list[StageResult] = []
        context = ""
        bpm = self.daw.cfg.get("default_bpm", 100)
        async for result in self.ai.generate_full(user_prompt, context, log_cb=self.daw.log):
            if self._cancel:
                await self.daw.log("warn", "流水线已被取消。")
                break
            await self.execute_stage(result, bpm)
            if result.project:
                bpm = result.project.tempo.bpm
            context += f"\n[{result.stage.value}] {result.summary}"
            results.append(result)
            await self.daw.emit(kind="pipeline_progress", completed=result.stage.value,
                                done=len(results), total=4)
        await self.daw.emit(kind="pipeline_done", stages=[r.stage.value for r in results])
        return results
