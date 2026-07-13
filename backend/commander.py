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

        # 1. 工程 —— 只有作曲阶段才允许新建工程，确保一首音乐的所有操作指向同一个工程
        if result.project:
            if result.stage == Stage.COMPOSE:
                # create_project 已加锁：若工程已锁定（如自主模式重做 compose），拒绝重建并返回 False
                created = await self.daw.create_project(result.project.tempo, result.project.title)
                if created:
                    bpm = result.project.tempo.bpm
                # created=False 时工程已锁定，沿用原工程，不动 bpm
            else:
                # 非 compose 阶段若 AI 误输出 project 字段，忽略以防意外新建/切换工程
                await self.daw.log(
                    "warn",
                    f"[{result.stage.value}] 阶段忽略了 project 字段："
                    f"工程已在作曲阶段创建为「{self.daw.current_project_title}」，"
                    "一首音乐的所有操作必须指向同一个 Logic Pro 工程。",
                )

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
        """完整四阶段流水线：作曲→编曲→混音→母带，所有操作指向同一个 Logic Pro 工程。"""
        self._cancel = False
        results: list[StageResult] = []
        context = ""
        bpm = self.daw.cfg.get("default_bpm", 100)
        async for result in self.ai.generate_full(user_prompt, context, log_cb=self.daw.log):
            if self._cancel:
                await self.daw.log("warn", "流水线已被取消。")
                break
            await self.execute_stage(result, bpm)
            if result.project and result.stage == Stage.COMPOSE:
                bpm = result.project.tempo.bpm
            context += f"\n[{result.stage.value}] {result.summary}"
            # 把工程路径注入上下文，让后续阶段明确知道所有操作指向同一个工程
            if self.daw.current_project_path and not context.startswith("[工程]"):
                context = (f"[工程] 当前 Logic Pro 工程：{self.daw.current_project_title}"
                           f"（{self.daw.current_project_path}）。"
                           "后续所有阶段都在这同一个工程上操作，禁止新建/打开/关闭工程。"
                           + context)
            results.append(result)
            await self.daw.emit(kind="pipeline_progress", completed=result.stage.value,
                                done=len(results), total=4)
        # 流水线结束前保存工程，确保所有改动落盘
        if self.daw.current_project_path:
            await self.daw.save_project()
        await self.daw.emit(kind="pipeline_done", stages=[r.stage.value for r in results])
        return results
