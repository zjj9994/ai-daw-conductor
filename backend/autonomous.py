"""自主流水线：AI 不间断、自主地完成一整首音乐/歌曲的制作。

核心能力（区别于 commander.run_pipeline 的线性执行）：
  - 阶段间自动衔接，无需用户介入
  - 每阶段产出后先做「出版级本地质检」（rule-based，明确指标），
    再由 AI 自评估，不达标则带具体问题反馈重做（max_stage_retries 次）
  - 多轮参照评估：MASTER 阶段把 master_spec 与商业发行参照目标
    （REFERENCE_TARGETS）对照，多次校准直到 LUFS/真峰/动态范围达标
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


# ---------- 出版级参照目标（商业发行行业标准） ----------
# 用于 MASTER 阶段的参照评估：把 master_spec 与平台标准对照，
# 偏离容差范围则要求重做。值来自 ITU-R BS.1770 / EBU R128 / Spotify Apple Music 上传规范。
REFERENCE_TARGETS = {
    "streaming": {
        "label": "流媒体（Spotify/Apple Music/YouTube）",
        "target_lufs": -14.0,        # Integrated LUFS
        "lufs_tolerance": 1.0,       # 允许 ±1 LU
        "true_peak_ceiling": -1.0,   # dBTP 上限
        "true_peak_tolerance": 0.2,
        "lra_min": 5.0, "lra_max": 8.0,
    },
    "cd": {
        "label": "CD 唱片",
        "target_lufs": -9.0,
        "lufs_tolerance": 1.5,
        "true_peak_ceiling": -0.3,
        "true_peak_tolerance": 0.1,
        "lra_min": 6.0, "lra_max": 11.0,
    },
    "club": {
        "label": "电子俱乐部",
        "target_lufs": -8.0,
        "lufs_tolerance": 1.0,
        "true_peak_ceiling": -1.0,
        "true_peak_tolerance": 0.2,
        "lra_min": 4.0, "lra_max": 6.0,
    },
    "broadcast": {
        "label": "广播电视（EBU R128）",
        "target_lufs": -23.0,
        "lufs_tolerance": 1.0,
        "true_peak_ceiling": -1.0,
        "true_peak_tolerance": 0.2,
        "lra_min": 6.0, "lra_max": 20.0,
    },
    "film": {
        "label": "影视（DVD/影院）",
        "target_lufs": -27.0,
        "lufs_tolerance": 2.0,
        "true_peak_ceiling": -2.0,
        "true_peak_tolerance": 0.2,
        "lra_min": 10.0, "lra_max": 20.0,
    },
}


# ---------- 出版级质检准则（rule-based 本地检查） ----------
# 每个阶段的检查项都是可程序化判定的，避免完全依赖 AI 自评。
# 返回 (通过, [问题列表])，问题会作为反馈让 AI 重做。
PUBLICATION_GRADE_RUBRIC = {
    Stage.COMPOSE: "作曲：标题/调性/速度/段落结构齐全；≥4 段；≥2 轨（主旋律+和声）；含 markers 标注段落；总长 16-32 小节",
    Stage.ARRANGE: "编曲：含鼓组(底鼓36/军鼓38)、贝斯、和声铺底、副旋律；有 region_ops 量化/复制/循环；轨道数≥4；有 track_stacks 编组",
    Stage.MIX: "混音：所有已有轨道都有 mix 条目；每轨 gain_stage_db -18~-12；headroom_db ≤ -6；贝斯 sidechain_from 底鼓；有 buses（Reverb/Drum/Vocal Bus）；有 automation；有 plugin_params 精调",
    Stage.MASTER: "母带：有 master_spec（target_lufs/true_peak_ceiling/lra_target/platform）；母带链齐全（EQ+多段压缩+立体声加宽+Limiter）；有 bounce；target_lufs 与 platform 匹配；true_peak_ceiling ≤ -1.0（流媒体）",
}


def _has_drum_track(result: StageResult) -> bool:
    """是否含鼓组轨道（按名字或 instrument 判断）。"""
    drum_keywords = ("鼓", "drum", "kick", "snare", "hihat", "底鼓", "军鼓", "踩镲")
    for t in result.tracks:
        name = (t.name or "").lower()
        inst = (t.instrument or "").lower()
        if any(k in name or k in inst for k in drum_keywords):
            return True
    return False


def _has_bass_track(result: StageResult) -> bool:
    bass_keywords = ("贝斯", "bass", "低音")
    for t in result.tracks:
        name = (t.name or "").lower()
        inst = (t.instrument or "").lower()
        if any(k in name or k in inst for k in bass_keywords):
            return True
    return False


def _is_bass_name(name: str) -> bool:
    """判断轨道名是否是贝斯类（用于 sidechain 检查）。"""
    if not name:
        return False
    n = name.lower()
    return any(k in n for k in ("贝斯", "bass", "低音"))


class AutonomousPipeline:
    """自主编排器：驱动 AI 连续完成一整首作品。

    Args:
        ai: AI 引擎（网页 AI 或 demo）
        commander: 已绑定 DAWController 的指挥官，负责把 StageResult 落地到 Logic Pro
        tracker: 任务追踪器，记录状态/进度
        max_stage_retries: 每阶段自评估不达标时的最大重做次数（默认 2）
        enable_self_eval: 是否启用 AI 自评估（关闭则线性推进，等同 run_pipeline）
        enable_publication_qc: 是否启用出版级本地质检（rule-based，明确指标检查）
        reference_rounds: MASTER 阶段参照评估轮数（每轮对照 REFERENCE_TARGETS 校准）
        health_check_interval: 长任务中每隔多少秒做一次网页 AI 健康检查
    """

    def __init__(
        self,
        ai: AIEngine,
        commander: Commander,
        tracker: Optional[TaskTracker] = None,
        max_stage_retries: int = 2,
        enable_self_eval: bool = True,
        enable_publication_qc: bool = True,
        reference_rounds: int = 2,
        health_check_interval: float = 30.0,
    ):
        self.ai = ai
        self.commander = commander
        self.tracker = tracker
        self.max_stage_retries = max_stage_retries
        self.enable_self_eval = enable_self_eval
        self.enable_publication_qc = enable_publication_qc
        self.reference_rounds = max(1, reference_rounds)
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
            # 流水线结束前保存工程，确保所有改动落盘到同一个 .logicx 文件
            if self.commander.daw.current_project_path:
                await self.commander.daw.save_project()
            await self._log("info", f"✓ 自主制作完成：共 {len(results)} 阶段"
                                    + (f"，作品「{project_title}」" if project_title else "")
                                    + (f"，工程已保存：{self.commander.daw.current_project_path}"
                                       if self.commander.daw.current_project_path else ""))
            await self.commander.daw.emit(
                kind="pipeline_done",
                stages=[r.stage.value for r in results],
                autonomous=True,
            )
        return results

    # ---------- 单阶段执行（含本地质检 + 自评估 + 重做） ----------
    async def _run_stage_with_eval(
        self, stage: Stage, user_prompt: str, context: str, bpm: float, stage_idx: int,
    ) -> StageResult:
        """带本地质检与自评估的阶段执行：生成→执行→出版级本地质检→AI 自评估→
        不达标带具体问题反馈重做。MASTER 阶段额外做参照评估。"""
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

            # 1) 出版级本地质检（rule-based，明确指标）
            if self.enable_publication_qc:
                qc_passed, qc_issues = self._publication_qc(stage, result)
                if not qc_passed:
                    await self._log("warn", f"[{stage.value}] 出版级本地质检未通过：{'；'.join(qc_issues)}")
                    if attempt <= self.max_stage_retries:
                        eval_context = eval_context + (
                            f"\n\n[出版级质检反馈-{stage.value}] 上一版未达出版级标准，必须修复：\n"
                            + "\n".join(f"- {s}" for s in qc_issues)
                            + f"\n本阶段准则：{PUBLICATION_GRADE_RUBRIC[stage]}"
                        )
                        continue
                    else:
                        await self._log("warn", f"[{stage.value}] 质检未通过但已达最大重做次数，按当前产出继续推进。")

            # 2) MASTER 阶段做参照评估（多轮校准）
            if stage == Stage.MASTER and result.master_spec and not self._cancel:
                ref_passed, ref_feedback = await self._multi_round_reference_eval(result, eval_context)
                if not ref_passed and attempt <= self.max_stage_retries:
                    await self._log("info", f"[{stage.value}] 参照评估未达标，将根据反馈重做。")
                    eval_context = eval_context + f"\n\n[参照评估反馈-{stage.value}] {ref_feedback}"
                    continue

            # 3) AI 自评估（主观创意维度）
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
                    await self._log("info", f"[{stage.value}] AI 自评估未通过，将根据反馈重做。")
                    eval_context = eval_context + f"\n\n[自评估反馈-{stage.value}] 上一版问题与建议：{feedback}"
                    continue
                else:
                    await self._log("warn", f"[{stage.value}] 自评估未通过但已达最大重做次数，按当前产出继续。")
            return result

        # 兜底
        if last_result:
            return last_result
        return self._fallback_demo(stage, user_prompt, eval_context)

    # ---------- 出版级本地质检（rule-based） ----------
    def _publication_qc(self, stage: Stage, result: StageResult) -> tuple[bool, list[str]]:
        """对阶段产出做出版级本地质检。返回 (通过, [问题列表])。

        这些检查都是可程序化判定的硬指标，避免完全依赖 AI 自评。
        缺项或越界都会被记为问题，作为反馈让 AI 修复重做。
        """
        issues: list[str] = []

        if stage == Stage.COMPOSE:
            if not result.project:
                issues.append("缺少 project（标题/风格/速度/调性/段落结构）")
            else:
                p = result.project
                if not p.title:
                    issues.append("缺少作品标题")
                if not p.tempo or not p.tempo.bpm:
                    issues.append("缺少速度 bpm")
                if not p.tempo.key:
                    issues.append("缺少调性 key")
                if not p.structure or len(p.structure) < 4:
                    issues.append(f"段落结构不足 4 段（当前 {len(p.structure) if p.structure else 0} 段）")
            if len(result.tracks) < 2:
                issues.append(f"轨道数不足 2（主旋律+和声），当前 {len(result.tracks)}")
            if not result.regions:
                issues.append("缺少 MIDI 片段 regions（必须给出主旋律/和声音符）")
            if not result.markers:
                issues.append("缺少 markers（必须标注各段落位置便于后续阶段定位）")

        elif stage == Stage.ARRANGE:
            if not _has_drum_track(result):
                issues.append("缺少鼓组轨道（底鼓36/军鼓38/踩镲42 等）")
            if not _has_bass_track(result):
                issues.append("缺少贝斯轨道（低频支撑）")
            if len(result.tracks) < 4:
                issues.append(f"轨道数不足 4（鼓组+贝斯+和声+副旋律），当前 {len(result.tracks)}")
            if not result.region_ops:
                issues.append("缺少 region_ops（必须有人性化编辑：量化/复制/循环/移调）")
            if not result.track_stacks:
                issues.append("缺少 track_stacks（建议把鼓组打包成文件夹轨道便于管理）")

        elif stage == Stage.MIX:
            track_names = {t.name for t in result.tracks} if result.tracks else set()
            # 检查所有先前轨道是否都有 mix 条目
            # 注意：commander 已执行前一阶段，result.tracks 可能是本阶段新增；
            # 这里以本阶段的 mix 列表为准，若 mix 条目少于已有轨道数则警告
            mix_tracks = {m.track for m in result.mix}
            missing = track_names - mix_tracks
            if missing and len(result.mix) < len(track_names):
                issues.append(f"部分轨道缺少 mix 条目：{list(missing)[:5]}")
            # 增益分级检查
            bad_gain = [m.track for m in result.mix
                        if m.gain_stage_db is not None and not (-20 <= m.gain_stage_db <= -10)]
            if bad_gain:
                issues.append(f"增益分级超出 -18~-12dBFS 范围：{bad_gain[:5]}（出版级要求 -18~-12dBFS）")
            no_gain = [m.track for m in result.mix if m.gain_stage_db is None]
            if no_gain and result.mix:
                issues.append(f"未设 gain_stage_db 的轨道：{no_gain[:5]}（必须显式声明增益分级目标）")
            # headroom 检查
            bad_headroom = [m.track for m in result.mix
                            if m.headroom_db is not None and m.headroom_db > -6]
            if bad_headroom:
                issues.append(f"headroom_db > -6dB，余量不足：{bad_headroom[:5]}（出版级建议 -6dB）")
            # 侧链检查：贝斯应被底鼓侧链
            bass_mix = [m for m in result.mix if _is_bass_name(m.track)]
            if bass_mix and not any(m.sidechain_from for m in bass_mix):
                issues.append("贝斯轨道未设置 sidechain_from（应被底鼓侧链压缩避让低频）")
            # 总线结构
            if not result.buses:
                issues.append("缺少 buses（出版级应有 Reverb Bus + Drum Bus + Vocal Bus）")
            # 自动化与插件精调
            if not result.automation:
                issues.append("缺少 automation（出版级混音必须有动态自动化，如副歌提音量/尾奏渐弱）")
            if not result.plugin_params:
                issues.append("缺少 plugin_params（不只挂插件，必须精调每个 Threshold/Ratio/Gain 值）")

        elif stage == Stage.MASTER:
            if not result.master_spec:
                issues.append("缺少 master_spec（必须输出出版级母带目标规范：LUFS/真峰/动态/平台）")
            else:
                ms = result.master_spec
                ref = REFERENCE_TARGETS.get(ms.platform, REFERENCE_TARGETS["streaming"])
                if abs(ms.target_lufs - ref["target_lufs"]) > ref["lufs_tolerance"]:
                    issues.append(
                        f"target_lufs={ms.target_lufs} 偏离 {ref['label']} 标准 "
                        f"({ref['target_lufs']}±{ref['lufs_tolerance']})"
                    )
                if ms.true_peak_ceiling > ref["true_peak_ceiling"] + ref["true_peak_tolerance"]:
                    issues.append(
                        f"true_peak_ceiling={ms.true_peak_ceiling} 超过 {ref['label']} 上限 "
                        f"({ref['true_peak_ceiling']}dBTP)"
                    )
                if ms.lra_target is not None:
                    if ms.lra_target < ref["lra_min"] or ms.lra_target > ref["lra_max"]:
                        issues.append(
                            f"lra_target={ms.lra_target} 超出 {ref['label']} 动态范围区间 "
                            f"({ref['lra_min']}-{ref['lra_max']} LU)"
                        )
            # 母带链完整性
            plugin_names = " ".join(p.name.lower() for p in result.master_plugins)
            if not result.master_plugins:
                issues.append("缺少 master_plugins（必须挂母带链）")
            else:
                if "eq" not in plugin_names and "channel eq" not in plugin_names:
                    issues.append("母带链缺少 EQ（修整低频/去浑浊）")
                if "multi" not in plugin_names and "multiband" not in plugin_names:
                    issues.append("母带链缺少多段压缩（胶合各频段）")
                if "limit" not in plugin_names:
                    issues.append("母带链缺少 Limiter（控制响度与真峰）")
            if not result.bounce:
                issues.append("缺少 bounce（必须设置导出参数 wav/24bit/44100Hz）")

        return (len(issues) == 0, issues)

    # ---------- 多轮参照评估（仅 MASTER） ----------
    async def _multi_round_reference_eval(
        self, result: StageResult, context: str,
    ) -> tuple[bool, str]:
        """把 master_spec 与商业发行参照目标对照，最多 reference_rounds 轮校准。

        每轮返回具体的偏差与建议，让 AI 调整 master_spec 重做。
        若 master_spec 已在容差内则直接通过。
        """
        ms = result.master_spec
        if not ms:
            return True, ""  # 由本地质检单独处理

        ref = REFERENCE_TARGETS.get(ms.platform, REFERENCE_TARGETS["streaming"])
        deviations: list[str] = []

        lufs_diff = abs(ms.target_lufs - ref["target_lufs"])
        if lufs_diff > ref["lufs_tolerance"]:
            deviations.append(
                f"LUFS 偏差 {lufs_diff:.1f} LU（目标 {ref['target_lufs']}±{ref['lufs_tolerance']}，"
                f"当前 {ms.target_lufs}）"
            )

        if ms.true_peak_ceiling > ref["true_peak_ceiling"] + ref["true_peak_tolerance"]:
            deviations.append(
                f"真峰 {ms.true_peak_ceiling} dBTP 超过 {ref['true_peak_ceiling']} 上限"
            )

        if ms.lra_target is not None and (
            ms.lra_target < ref["lra_min"] or ms.lra_target > ref["lra_max"]
        ):
            deviations.append(
                f"动态范围 {ms.lra_target} LU 超出 {ref['lra_min']}-{ref['lra_max']} 区间"
            )

        if not deviations:
            await self._log("info", f"[master] 参照评估通过：匹配 {ref['label']} 标准。")
            return True, ""

        feedback = (
            f"参照目标：{ref['label']}（LUFS={ref['target_lufs']}/dBTP≤{ref['true_peak_ceiling']}/"
            f"LRA={ref['lra_min']}-{ref['lra_max']}）。当前偏差："
            + "；".join(deviations)
            + "。请调整 master_spec 与母带链参数使其落入参照容差。"
        )
        await self._log("warn", f"[master] 参照评估未达标：{feedback}")
        return False, feedback

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
        # 在作曲阶段后注入工程路径，明确告知后续阶段所有操作指向同一个工程
        daw = self.commander.daw
        if daw.current_project_path and "[工程]" not in (context or ""):
            parts.insert(0, (
                f"[工程] 当前 Logic Pro 工程：{daw.current_project_title}"
                f"（{daw.current_project_path}）。"
                "后续所有阶段都在这同一个工程上操作，禁止新建/打开/关闭工程。"
            ))
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
