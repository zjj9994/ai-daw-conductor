"""指挥官：把 AI 输出的 StageResult 翻译为 DAWController 的有序执行。

负责：阶段编排、轨道/MIDI/混音/母带的依赖排序、事件汇总、任务追踪。
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
        """执行单个 StageResult。"""
        if self.tracker:
            self.tracker.set_stage(result.stage.value)
        await self.daw.log("info", f"===== 执行阶段：{result.stage.value} =====")
        await self.daw.emit(kind="stage_start", stage=result.stage.value, summary=result.summary)

        if result.project:
            await self.daw.create_project(result.project.tempo, result.project.title)
            bpm = result.project.tempo.bpm

        # 1. 建轨道
        for t in result.tracks:
            await self.daw.ensure_track(t)
            await asyncio.sleep(0.05)

        # 2. 写 MIDI
        for r in result.regions:
            await self.daw.add_region(r, bpm)
            await asyncio.sleep(0.05)

        # 3. 混音
        for m in result.mix:
            await self.daw.apply_mix(m)
            await asyncio.sleep(0.05)

        # 4. 母带
        if result.master_plugins:
            await self.daw.apply_master(result.master_plugins)

        # 5. 导出
        if result.bounce:
            await self.daw.bounce(result.bounce)

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
