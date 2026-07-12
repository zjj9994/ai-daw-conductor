"""自主流水线：AI 不间断、自主地完成一整首音乐/歌曲的制作。

核心能力（区别于 commander.run_pipeline 的线性执行）：
  - 阶段间自动衔接，无需用户介入
  - 每阶段产出后由 AI 自评估，不达标则带反馈重做（max_stage_retries 次）
  - 网页 AI 断连/卡死时自动健康检查 + 重连，长任务不中断
  - 上下文在阶段间累积传递（含结构、调性、轨道清单、自评估反馈），保证整首作品连贯
  - 随时可取消，取消信号立即传播
  - 单阶段彻底失败时降级为 demo，但流水线继续推进（不让整首作品因一阶段卡死而废弃）

调用入口：`AutonomousPipeline.run(user_prompt)`，由 server 的 /api/autonomous 触发。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .ai_engine import AIEngine
from .commander import Commander
from .models import Stage, StageResult
from .task_tracker import TaskTracker

log = logging.getLogger("autonomous")

# 阶段顺序（整首作品的制作流程）
STAGE_ORDER = [Stage.COMPOSE, Stage.ARRANGE, Stage.MIX, Stage.MASTER]


class AutonomousPipeline:
    """自主编排器：驱动 AI 连续完成一整首作品。

    Args:
        ai: AI 引擎（网页 AI 或 demo）
        commander: 已绑定 DAWController 的指挥官，负责把 StageResult 落地到 Logic Pro
        tracker: 任务追踪器，记录状态/进度
        max_stage_retries: 每阶段自评估不达标时的最大重做次数（默认 2）
        enable_self_eval: 是否启用 AI 自评估（关闭则线性推进，等同 run_pipeline）
        health_check_interval: 长任务中每隔多少秒做一次网页 AI 健康检查
    """

    def __init__(
        self,
        ai: AIEngine,
        commander: Commander,
        tracker: Optional[TaskTracker] = None,
        max_stage_retries: int = 2,
        enable_self_eval: bool = True,
        health_check_interval: float = 30.0,
    ):
        self.ai = ai
        self.commander = commander
        self.tracker = tracker
        self.max_stage_retries = max_stage_retries
        self.enable_self_eval = enable_self_eval
        self.health_check_interval = health_check_interval
        self._cancel = False

    def cancel(self):
        """请求取消（异步生效，下一个检查点会中断）。"""
        self._cancel = True
        self.commander.cancel()

    async def run(self, user_prompt: str) -> list[StageResult]:
        """自主执行完整四阶段流水线，返回各阶段产出。"""
        self._cancel = False
        self.commander._cancel = False
        if self.tracker:
            self.tracker.start(mode="autonomous", prompt=user_prompt, total=len(STAGE_ORDER))

        await self._log("info", f"自主制作启动：目标「{user_prompt[:40]}{'…' if len(user_prompt) > 40 else ''}」")
        await self._log("info", f"策略：连续 {len(STAGE_ORDER)} 阶段，自评估={'开' if self.enable_self_eval else '关'}，每阶段最多重做 {self.max_stage_retries} 次")

        results: list[StageResult] = []
        context = ""          # 累积上下文：各阶段 summary + rationale + 结构信息
        bpm = 100.0
        project_title = ""

        for idx, stage in enumerate(STAGE_ORDER, 1):
            if self._cancel:
                await self._log("warn", "自主制作已被用户取消。")
                if self.tracker:
                    self.tracker.cancel()
                break

            await self._log("info", f"━━━ [{idx}/{len(STAGE_ORDER)}] 阶段：{stage.value} ━━━")
            if self.tracker:
                self.tracker.set_stage(stage.value)

            # 确保网页 AI 仍在线（长任务保护）
            await self._ensure_alive()

            result = await self._run_stage_with_eval(stage, user_prompt, context, bpm, idx)

            # 记录 bpm / 标题供后续阶段延续
            if result.project:
                bpm = result.project.tempo.bpm
                if result.project.title:
                    project_title = result.project.title

            # 累积上下文（让下一阶段知道前面做了什么）
            context = self._accumulate_context(context, result, stage)
            results.append(result)

            if self.tracker:
                self.tracker.complete_stage(stage.value)
            await self.commander.daw.emit(
                kind="pipeline_progress",
                completed=stage.value, done=idx, total=len(STAGE_ORDER),
            )

            if self._cancel:
                break

        if self.tracker and not self._cancel:
            self.tracker.finish()

        if results and not self._cancel:
            await self._log("info", f"✓ 自主制作完成：共 {len(results)} 阶段"
                                    + (f"，作品「{project_title}」" if project_title else ""))
            await self.commander.daw.emit(
                kind="pipeline_done",
                stages=[r.stage.value for r in results],
                autonomous=True,
            )
        return results

    # ---------- 单阶段执行（含自评估与重做） ----------
    async def _run_stage_with_eval(
        self, stage: Stage, user_prompt: str, context: str, bpm: float, stage_idx: int,
    ) -> StageResult:
        """带自评估的阶段执行：生成→执行→评估→不达标带反馈重做。"""
        last_result: Optional[StageResult] = None
        eval_context = context  # 评估反馈会追加到这里，供重做时参考

        for attempt in range(1, self.max_stage_retries + 2):  # 初次 + retries 次重做
            if self._cancel:
                break
            if attempt > 1:
                await self._log("info", f"[{stage.value}] 第 {attempt} 次尝试（带评估反馈重做）...")
                await self._ensure_alive()

            try:
                result = await self.commander.run_stage(stage, user_prompt, eval_context, bpm)
                last_result = result
            except Exception as e:
                log.warning("阶段 %s 第 %d 次执行失败：%s", stage.value, attempt, e)
                await self._log("warn", f"[{stage.value}] 第 {attempt} 次失败：{e}")
                # 执行失败也尝试健康检查+重连
                await self._ensure_alive(force=True)
                if attempt > self.max_stage_retries:
                    await self._log("error", f"[{stage.value}] 已达最大重试次数，降级为 demo 继续推进。")
                    return self._fallback_demo(stage, user_prompt, eval_context)
                await asyncio.sleep(min(2 * attempt, 8))
                continue

            # 自评估
            if self.enable_self_eval and self.ai.online and not self._cancel:
                try:
                    acceptable, feedback = await self.ai.evaluate_stage(
                        stage, result, eval_context, log_cb=self._log,
                    )
                except Exception as e:
                    log.warning("自评估异常：%s，按通过处理", e)
                    acceptable, feedback = True, ""

                if acceptable:
                    return result
                # 不达标：带反馈重做
                if attempt <= self.max_stage_retries:
                    await self._log("info", f"[{stage.value}] 自评估未通过，将根据反馈重做。")
                    eval_context = eval_context + f"\n\n[自评估反馈-{stage.value}] 上一版问题与建议：{feedback}"
                    continue
                else:
                    await self._log("warn", f"[{stage.value}] 自评估未通过但已达最大重做次数，按当前产出继续。")
            return result

        # 兜底
        if last_result:
            return last_result
        return self._fallback_demo(stage, user_prompt, eval_context)

    def _fallback_demo(self, stage: Stage, user_prompt: str, context: str) -> StageResult:
        """彻底失败时用 demo 生成器兜底，保证流水线不中断。"""
        result = self.ai._generate_demo(stage, user_prompt, context)
        # 同步给 commander 落地
        asyncio.ensure_future(self.commander.execute_stage(result, 100.0))
        return result

    # ---------- 上下文累积 ----------
    def _accumulate_context(self, context: str, result: StageResult, stage: Stage) -> str:
        """把本阶段产出追加到上下文，供下一阶段延续。"""
        parts = [context] if context else []
        parts.append(f"\n[{stage.value}] 摘要：{result.summary}")
        if result.project:
            p = result.project
            parts.append(
                f"作品：{p.title} | 风格：{p.genre} | 调性：{p.tempo.key} | "
                f"BPM：{p.tempo.bpm} | 段落结构：{p.structure}"
            )
        if result.tracks:
            names = [t.name for t in result.tracks]
            parts.append(f"已有轨道：{names}")
        if result.rationale:
            parts.append(f"创作思路：{result.rationale[:200]}")
        return "\n".join(parts).strip()

    # ---------- 网页 AI 健康检查与重连 ----------
    async def _ensure_alive(self, force: bool = False):
        """长任务中检查网页 AI 是否仍可响应，断开则重连。"""
        if not self.ai.online:
            return
        try:
            healthy = await self.ai.health_check() if not force else False
            if healthy and not force:
                return
            await self._log("warn", "网页 AI 连接异常，尝试重连...")
            ok = await self.ai.reconnect(log_cb=self._log)
            if not ok:
                await self._log("error", "重连失败，后续将降级为 demo 模式。")
        except Exception as e:
            log.warning("健康检查异常：%s", e)

    # ---------- 日志 ----------
    async def _log(self, level: str, message: str):
        log.log(getattr(logging, level.upper(), logging.INFO), message)
        await self.commander.daw.log(level, message)
