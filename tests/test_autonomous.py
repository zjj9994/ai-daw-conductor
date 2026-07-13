"""单元测试：自主流水线编排逻辑。运行 `pytest tests/`

使用 mock 的 AI 引擎与 commander，验证：
- 四阶段全部执行
- 上下文累积正确
- 自评估不达标触发重做
- 取消信号传播
- 健康检查与重连
"""
import asyncio
from unittest.mock import MagicMock, AsyncMock

from backend.autonomous import AutonomousPipeline, STAGE_ORDER
from backend.models import Stage, StageResult, ProjectPlan, TempoSpec, TrackSpec


def _make_result(stage: Stage, title: str = "测试曲", bpm: int = 100) -> StageResult:
    return StageResult(
        stage=stage,
        summary=f"{stage.value} 产出",
        project=ProjectPlan(
            title=title, genre="Pop",
            tempo=TempoSpec(bpm=bpm, time_signature="4/4", key="A minor"),
            structure=["intro", "verse", "chorus"], key="A minor",
            description="测试",
        ) if stage == Stage.COMPOSE else None,
        tracks=[TrackSpec(name="主旋律", type="software", instrument="Piano")],
        rationale=f"{stage.value} 创作思路",
    )


def _make_ai_mock(evaluations=None, online=True):
    """构造 mock AI 引擎。evaluations: dict[stage.value -> list[bool]] 控制每次评估结果。"""
    ai = MagicMock()
    ai.online = online
    ai.health_check = AsyncMock(return_value=True)
    ai.reconnect = AsyncMock(return_value=True)
    ai._generate_demo = MagicMock(return_value=_make_result(Stage.COMPOSE))

    # evaluate_stage 按 evaluations 配置返回
    eval_state = {k: list(v) for k, v in (evaluations or {}).items()}

    async def _eval(stage, result, context=None, log_cb=None):
        seq = eval_state.get(stage.value, [])
        if not seq:
            return True, ""
        acceptable = seq.pop(0)
        return acceptable, "" if acceptable else "需改进"

    ai.evaluate_stage = _eval
    return ai


def _make_commander_mock():
    """构造 mock commander，run_stage 返回对应阶段的 result。"""
    commander = MagicMock()
    commander._cancel = False
    commander.cancel = MagicMock()
    commander.daw = MagicMock()
    commander.daw.log = AsyncMock()
    commander.daw.emit = AsyncMock()

    async def _run_stage(stage, user_prompt, context, bpm):
        return _make_result(stage)

    commander.run_stage = _run_stage

    async def _execute_stage(result, bpm):
        pass

    commander.execute_stage = _execute_stage
    return commander


def test_runs_all_four_stages():
    ai = _make_ai_mock()
    commander = _make_commander_mock()
    pipe = AutonomousPipeline(ai, commander, tracker=None, enable_self_eval=False)

    results = asyncio.run(pipe.run("测试"))

    assert len(results) == 4
    assert [r.stage for r in results] == STAGE_ORDER


def test_context_accumulates_between_stages():
    ai = _make_ai_mock()
    commander = _make_commander_mock()
    captured_context = []

    async def _run_stage_capturing(stage, user_prompt, context, bpm):
        captured_context.append(context)
        return _make_result(stage)

    commander.run_stage = _run_stage_capturing
    pipe = AutonomousPipeline(ai, commander, tracker=None, enable_self_eval=False)

    asyncio.run(pipe.run("测试"))

    # 第一阶段无上下文，后续阶段应有累积
    assert captured_context[0] == ""
    assert "compose" in captured_context[1]
    assert "主旋律" in captured_context[1]  # 轨道名应出现在上下文


def test_self_eval_rejects_then_accepts():
    """compose 第一次评估不通过，第二次通过 —— 应重做一次。
    关闭出版级本地质检以隔离测试 AI 自评估路径。"""
    ai = _make_ai_mock(evaluations={"compose": [False, True]})
    commander = _make_commander_mock()
    call_count = {"compose": 0}

    async def _run_stage(stage, user_prompt, context, bpm):
        call_count[stage.value] = call_count.get(stage.value, 0) + 1
        return _make_result(stage)

    commander.run_stage = _run_stage
    pipe = AutonomousPipeline(ai, commander, tracker=None,
                              enable_self_eval=True, enable_publication_qc=False,
                              max_stage_retries=2)

    asyncio.run(pipe.run("测试"))

    # compose 应被调用 2 次（初次 + 1 次重做）
    assert call_count["compose"] == 2
    # 其他阶段各 1 次
    assert call_count.get("arrange") == 1
    assert call_count.get("master") == 1


def test_cancel_stops_pipeline():
    ai = _make_ai_mock()
    commander = _make_commander_mock()
    pipe = AutonomousPipeline(ai, commander, tracker=None, enable_self_eval=False)

    # 在第一个阶段执行后取消
    async def _run_stage(stage, user_prompt, context, bpm):
        pipe.cancel()  # 触发取消
        return _make_result(stage)

    commander.run_stage = _run_stage
    results = asyncio.run(pipe.run("测试"))

    # 只应完成 1 个阶段
    assert len(results) == 1


def test_stage_failure_falls_back_to_demo():
    ai = _make_ai_mock()
    commander = _make_commander_mock()

    call_count = {"fail": 0}

    async def _run_stage(stage, user_prompt, context, bpm):
        if stage == Stage.ARRANGE:
            call_count["fail"] += 1
            if call_count["fail"] <= 3:  # 前几次都失败
                raise RuntimeError("模拟网页 AI 故障")
        return _make_result(stage)

    commander.run_stage = _run_stage
    pipe = AutonomousPipeline(ai, commander, tracker=None,
                              enable_self_eval=False, max_stage_retries=2)

    results = asyncio.run(pipe.run("测试"))

    # 流水线不应中断，应完成 4 个阶段
    assert len(results) == 4


def test_tracker_receives_lifecycle_events():
    from backend.task_tracker import TaskTracker
    tracker = TaskTracker(history_file=None)
    ai = _make_ai_mock()
    commander = _make_commander_mock()
    pipe = AutonomousPipeline(ai, commander, tracker=tracker, enable_self_eval=False)

    asyncio.run(pipe.run("测试"))

    assert tracker.status.state == "done"
    assert tracker.status.mode == "autonomous"
    assert len(tracker.status.completed_stages) == 4


def test_health_check_called_when_online():
    ai = _make_ai_mock(online=True)
    commander = _make_commander_mock()
    pipe = AutonomousPipeline(ai, commander, tracker=None, enable_self_eval=False)

    asyncio.run(pipe.run("测试"))

    # 每个阶段前都应做健康检查
    assert ai.health_check.call_count >= 4


def test_health_check_skipped_when_offline():
    ai = _make_ai_mock(online=False)
    commander = _make_commander_mock()
    pipe = AutonomousPipeline(ai, commander, tracker=None, enable_self_eval=False)

    asyncio.run(pipe.run("测试"))

    # 离线模式不应触发健康检查
    assert ai.health_check.call_count == 0


def test_cancel_method_propagates_to_commander():
    ai = _make_ai_mock()
    commander = _make_commander_mock()
    pipe = AutonomousPipeline(ai, commander, tracker=None)

    pipe.cancel()

    assert commander.cancel.called
    assert pipe._cancel is True


def test_stage_order_is_compose_arrange_mix_master():
    assert STAGE_ORDER == [Stage.COMPOSE, Stage.ARRANGE, Stage.MIX, Stage.MASTER]


def test_accumulate_context_includes_key_info():
    ai = _make_ai_mock()
    commander = _make_commander_mock()
    pipe = AutonomousPipeline(ai, commander, tracker=None, enable_self_eval=False)
    result = _make_result(Stage.COMPOSE, title="我的歌", bpm=120)

    ctx = pipe._accumulate_context("", result, Stage.COMPOSE)

    assert "我的歌" in ctx
    assert "120" in ctx
    assert "A minor" in ctx
    assert "主旋律" in ctx


# ============ 出版级本地质检 + 参照评估测试 ============
from backend.autonomous import (
    REFERENCE_TARGETS, PUBLICATION_GRADE_RUBRIC,
    _has_drum_track, _has_bass_track, _is_bass_name,
)
from backend.models import (
    MasterSpec, BounceSpec, PluginSpec, MixParams, BusSpec, RegionOp,
    TrackStackSpec, MarkerSpec, MidiRegionSpec, NoteSpec, AutomationSpec,
    AutomationPoint, PluginParamSpec,
)


def _make_full_compose_result() -> StageResult:
    """构造一个通过出版级本地质检的 COMPOSE 产出。"""
    return StageResult(
        stage=Stage.COMPOSE,
        summary="完整作曲",
        project=ProjectPlan(
            title="测试曲", genre="Pop",
            tempo=TempoSpec(bpm=100, time_signature="4/4", key="A minor"),
            structure=["intro", "verse", "chorus", "outro"], key="A minor",
        ),
        tracks=[
            TrackSpec(name="主旋律", type="software", instrument="Piano"),
            TrackSpec(name="和弦", type="software", instrument="Piano"),
        ],
        regions=[MidiRegionSpec(track="主旋律", notes=[NoteSpec(pitch="C4", start=0, duration=1)])],
        markers=[MarkerSpec(name="Verse", bar=1)],
    )


def _make_full_arrange_result() -> StageResult:
    """构造一个通过出版级本地质检的 ARRANGE 产出。"""
    return StageResult(
        stage=Stage.ARRANGE,
        summary="完整编曲",
        tracks=[
            TrackSpec(name="主旋律", type="software"),
            TrackSpec(name="底鼓", type="software", instrument="Drum Kit"),
            TrackSpec(name="贝斯", type="software", instrument="Synth Bass"),
            TrackSpec(name="和声铺底", type="software"),
        ],
        region_ops=[RegionOp(op="quantize", track="底鼓", grid="1/16")],
        track_stacks=[TrackStackSpec(name="鼓组", members=["底鼓"])],
    )


def _make_full_mix_result() -> StageResult:
    """构造一个通过出版级本地质检的 MIX 产出。"""
    return StageResult(
        stage=Stage.MIX,
        summary="完整混音",
        tracks=[TrackSpec(name="主旋律"), TrackSpec(name="贝斯")],
        mix=[
            MixParams(track="主旋律", volume_db=-6, gain_stage_db=-14, headroom_db=-6),
            MixParams(track="贝斯", volume_db=-6, gain_stage_db=-14, headroom_db=-6,
                      sidechain_from="底鼓"),
        ],
        buses=[BusSpec(name="Reverb Bus")],
        automation=[AutomationSpec(track="主旋律", parameter="Volume",
                                    points=[AutomationPoint(bar=1, value=-6)])],
        plugin_params=[PluginParamSpec(track="主旋律", plugin="Compressor",
                                        parameter="Threshold", value=-20)],
    )


def _make_full_master_result(platform: str = "streaming") -> StageResult:
    """构造一个通过出版级本地质检的 MASTER 产出。"""
    ref = REFERENCE_TARGETS[platform]
    return StageResult(
        stage=Stage.MASTER,
        summary="完整母带",
        master_plugins=[
            PluginSpec(name="Channel EQ"),
            PluginSpec(name="Multiband Compressor"),
            PluginSpec(name="Stereo Width"),
            PluginSpec(name="Limiter"),
        ],
        master_spec=MasterSpec(
            target_lufs=ref["target_lufs"],
            true_peak_ceiling=ref["true_peak_ceiling"],
            lra_target=(ref["lra_min"] + ref["lra_max"]) / 2,
            platform=platform,
        ),
        bounce=BounceSpec(format="wav", bit_depth=24, sample_rate=44100),
    )


# ---------- 辅助函数 ----------
def test_has_drum_track_by_name():
    r = StageResult(stage=Stage.ARRANGE, summary="x",
                    tracks=[TrackSpec(name="底鼓", instrument="Drum Kit")])
    assert _has_drum_track(r)


def test_has_drum_track_by_instrument():
    r = StageResult(stage=Stage.ARRANGE, summary="x",
                    tracks=[TrackSpec(name=" perc", instrument="Drum Kit Designer")])
    assert _has_drum_track(r)


def test_has_drum_track_false():
    r = StageResult(stage=Stage.ARRANGE, summary="x",
                    tracks=[TrackSpec(name="钢琴", instrument="Piano")])
    assert not _has_drum_track(r)


def test_has_bass_track():
    r = StageResult(stage=Stage.ARRANGE, summary="x",
                    tracks=[TrackSpec(name="贝斯", instrument="Synth Bass")])
    assert _has_bass_track(r)


def test_is_bass_name():
    assert _is_bass_name("贝斯")
    assert _is_bass_name("Sub Bass")
    assert _is_bass_name("低音吉他")
    assert not _is_bass_name("主旋律")


# ---------- 本地质检：COMPOSE ----------
def test_qc_compose_pass():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = _make_full_compose_result()
    passed, issues = pipe._publication_qc(Stage.COMPOSE, result)
    assert passed, f"应通过质检，但发现问题：{issues}"


def test_qc_compose_fails_missing_project():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = StageResult(stage=Stage.COMPOSE, summary="缺 project")
    passed, issues = pipe._publication_qc(Stage.COMPOSE, result)
    assert not passed
    assert any("project" in i for i in issues)


def test_qc_compose_fails_short_structure():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = StageResult(
        stage=Stage.COMPOSE, summary="x",
        project=ProjectPlan(
            title="x", genre="x",
            tempo=TempoSpec(bpm=100, key="C"),
            structure=["intro", "verse"],  # 只有 2 段
            key="C",
        ),
        tracks=[TrackSpec(name="a"), TrackSpec(name="b")],
        regions=[MidiRegionSpec(track="a", notes=[NoteSpec(pitch=60, start=0)])],
        markers=[MarkerSpec(name="v", bar=1)],
    )
    passed, issues = pipe._publication_qc(Stage.COMPOSE, result)
    assert not passed
    assert any("段落结构" in i for i in issues)


def test_qc_compose_fails_no_markers():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = StageResult(
        stage=Stage.COMPOSE, summary="x",
        project=ProjectPlan(
            title="x", genre="x",
            tempo=TempoSpec(bpm=100, key="C"),
            structure=["intro", "verse", "chorus", "outro"],
            key="C",
        ),
        tracks=[TrackSpec(name="a"), TrackSpec(name="b")],
        regions=[MidiRegionSpec(track="a", notes=[NoteSpec(pitch=60, start=0)])],
    )
    passed, issues = pipe._publication_qc(Stage.COMPOSE, result)
    assert not passed
    assert any("markers" in i for i in issues)


# ---------- 本地质检：ARRANGE ----------
def test_qc_arrange_pass():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = _make_full_arrange_result()
    passed, issues = pipe._publication_qc(Stage.ARRANGE, result)
    assert passed, f"应通过：{issues}"


def test_qc_arrange_fails_no_drums():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = StageResult(
        stage=Stage.ARRANGE, summary="x",
        tracks=[
            TrackSpec(name="主旋律"),
            TrackSpec(name="贝斯"),
            TrackSpec(name="和声"),
            TrackSpec(name="副旋律"),
        ],
        region_ops=[RegionOp(op="quantize", track="x")],
        track_stacks=[TrackStackSpec(name="x", members=["y"])],
    )
    passed, issues = pipe._publication_qc(Stage.ARRANGE, result)
    assert not passed
    assert any("鼓组" in i for i in issues)


def test_qc_arrange_fails_no_bass():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = StageResult(
        stage=Stage.ARRANGE, summary="x",
        tracks=[
            TrackSpec(name="主旋律"),
            TrackSpec(name="底鼓", instrument="Drum Kit"),
            TrackSpec(name="和声"),
            TrackSpec(name="副旋律"),
        ],
        region_ops=[RegionOp(op="quantize")],
        track_stacks=[TrackStackSpec(name="x", members=["y"])],
    )
    passed, issues = pipe._publication_qc(Stage.ARRANGE, result)
    assert not passed
    assert any("贝斯" in i for i in issues)


def test_qc_arrange_fails_no_region_ops():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = StageResult(
        stage=Stage.ARRANGE, summary="x",
        tracks=[
            TrackSpec(name="主旋律"),
            TrackSpec(name="底鼓", instrument="Drum Kit"),
            TrackSpec(name="贝斯", instrument="Bass"),
            TrackSpec(name="和声"),
        ],
        track_stacks=[TrackStackSpec(name="x", members=["y"])],
    )
    passed, issues = pipe._publication_qc(Stage.ARRANGE, result)
    assert not passed
    assert any("region_ops" in i for i in issues)


# ---------- 本地质检：MIX ----------
def test_qc_mix_pass():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = _make_full_mix_result()
    passed, issues = pipe._publication_qc(Stage.MIX, result)
    assert passed, f"应通过：{issues}"


def test_qc_mix_fails_bad_gain_stage():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = StageResult(
        stage=Stage.MIX, summary="x",
        tracks=[TrackSpec(name="主旋律")],
        mix=[MixParams(track="主旋律", volume_db=-6, gain_stage_db=-5)],  # 超范围
        buses=[BusSpec(name="Reverb Bus")],
        automation=[AutomationSpec(track="主旋律", parameter="Volume",
                                    points=[AutomationPoint(bar=1, value=-6)])],
        plugin_params=[],
    )
    passed, issues = pipe._publication_qc(Stage.MIX, result)
    assert not passed
    assert any("增益分级" in i for i in issues)


def test_qc_mix_fails_no_buses():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = StageResult(
        stage=Stage.MIX, summary="x",
        tracks=[TrackSpec(name="主旋律")],
        mix=[MixParams(track="主旋律", volume_db=-6, gain_stage_db=-14, headroom_db=-6)],
        automation=[AutomationSpec(track="主旋律", parameter="Volume",
                                    points=[AutomationPoint(bar=1, value=-6)])],
    )
    passed, issues = pipe._publication_qc(Stage.MIX, result)
    assert not passed
    assert any("buses" in i for i in issues)


def test_qc_mix_fails_bass_no_sidechain():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = StageResult(
        stage=Stage.MIX, summary="x",
        tracks=[TrackSpec(name="贝斯")],
        mix=[MixParams(track="贝斯", volume_db=-6, gain_stage_db=-14, headroom_db=-6)],
        # 贝斯没有 sidechain_from
        buses=[BusSpec(name="Reverb Bus")],
        automation=[AutomationSpec(track="贝斯", parameter="Volume",
                                    points=[AutomationPoint(bar=1, value=-6)])],
    )
    passed, issues = pipe._publication_qc(Stage.MIX, result)
    assert not passed
    assert any("sidechain" in i for i in issues)


# ---------- 本地质检：MASTER ----------
def test_qc_master_pass_streaming():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = _make_full_master_result("streaming")
    passed, issues = pipe._publication_qc(Stage.MASTER, result)
    assert passed, f"应通过：{issues}"


def test_qc_master_pass_cd():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = _make_full_master_result("cd")
    passed, issues = pipe._publication_qc(Stage.MASTER, result)
    assert passed, f"CD 标准应通过：{issues}"


def test_qc_master_fails_no_master_spec():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = StageResult(
        stage=Stage.MASTER, summary="x",
        master_plugins=[PluginSpec(name="Limiter")],
        bounce=BounceSpec(),
    )
    passed, issues = pipe._publication_qc(Stage.MASTER, result)
    assert not passed
    assert any("master_spec" in i for i in issues)


def test_qc_master_fails_lufs_off_target():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = StageResult(
        stage=Stage.MASTER, summary="x",
        master_plugins=[
            PluginSpec(name="Channel EQ"),
            PluginSpec(name="Multiband Compressor"),
            PluginSpec(name="Limiter"),
        ],
        master_spec=MasterSpec(target_lufs=-5.0, true_peak_ceiling=-1.0, platform="streaming"),
        # -5 LUFS 偏离流媒体 -14 太远
        bounce=BounceSpec(),
    )
    passed, issues = pipe._publication_qc(Stage.MASTER, result)
    assert not passed
    assert any("target_lufs" in i for i in issues)


def test_qc_master_fails_true_peak_too_high():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = StageResult(
        stage=Stage.MASTER, summary="x",
        master_plugins=[PluginSpec(name="Channel EQ"), PluginSpec(name="Limiter")],
        master_spec=MasterSpec(target_lufs=-14.0, true_peak_ceiling=0.0, platform="streaming"),
        # 0 dBTP 超过 -1.0 上限
        bounce=BounceSpec(),
    )
    passed, issues = pipe._publication_qc(Stage.MASTER, result)
    assert not passed
    assert any("true_peak" in i for i in issues)


def test_qc_master_fails_missing_limiter():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = StageResult(
        stage=Stage.MASTER, summary="x",
        master_plugins=[PluginSpec(name="Channel EQ")],  # 缺 Limiter
        master_spec=MasterSpec(target_lufs=-14.0, true_peak_ceiling=-1.0, platform="streaming"),
        bounce=BounceSpec(),
    )
    passed, issues = pipe._publication_qc(Stage.MASTER, result)
    assert not passed
    assert any("Limiter" in i for i in issues)


def test_qc_master_fails_no_bounce():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = StageResult(
        stage=Stage.MASTER, summary="x",
        master_plugins=[
            PluginSpec(name="Channel EQ"),
            PluginSpec(name="Multiband Compressor"),
            PluginSpec(name="Limiter"),
        ],
        master_spec=MasterSpec(target_lufs=-14.0, true_peak_ceiling=-1.0, platform="streaming"),
    )
    passed, issues = pipe._publication_qc(Stage.MASTER, result)
    assert not passed
    assert any("bounce" in i for i in issues)


# ---------- 多轮参照评估 ----------
def test_reference_eval_passes_when_on_target():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = _make_full_master_result("streaming")
    passed, feedback = asyncio.run(pipe._multi_round_reference_eval(result, ""))
    assert passed
    assert feedback == ""


def test_reference_eval_fails_when_lufs_off():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = StageResult(
        stage=Stage.MASTER, summary="x",
        master_spec=MasterSpec(target_lufs=-5.0, true_peak_ceiling=-1.0, platform="streaming"),
    )
    passed, feedback = asyncio.run(pipe._multi_round_reference_eval(result, ""))
    assert not passed
    assert "LUFS" in feedback


def test_reference_eval_fails_when_true_peak_too_high():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = StageResult(
        stage=Stage.MASTER, summary="x",
        master_spec=MasterSpec(target_lufs=-14.0, true_peak_ceiling=0.0, platform="streaming"),
    )
    passed, feedback = asyncio.run(pipe._multi_round_reference_eval(result, ""))
    assert not passed
    assert "真峰" in feedback or "dBTP" in feedback


def test_reference_eval_cd_platform():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = _make_full_master_result("cd")
    passed, _ = asyncio.run(pipe._multi_round_reference_eval(result, ""))
    assert passed  # CD 标准应通过


def test_reference_eval_handles_no_master_spec():
    pipe = AutonomousPipeline(_make_ai_mock(), _make_commander_mock(), enable_self_eval=False)
    result = StageResult(stage=Stage.MASTER, summary="x")
    passed, feedback = asyncio.run(pipe._multi_round_reference_eval(result, ""))
    assert passed  # 无 master_spec 时跳过参照评估（由本地质检处理）


# ---------- 参照目标表 ----------
def test_reference_targets_has_all_platforms():
    for p in ("streaming", "cd", "club", "broadcast", "film"):
        assert p in REFERENCE_TARGETS
        t = REFERENCE_TARGETS[p]
        assert "target_lufs" in t
        assert "true_peak_ceiling" in t
        assert "lufs_tolerance" in t


def test_reference_targets_streaming_is_minus_14():
    assert REFERENCE_TARGETS["streaming"]["target_lufs"] == -14.0
    assert REFERENCE_TARGETS["streaming"]["true_peak_ceiling"] == -1.0


def test_reference_targets_cd_is_minus_9():
    assert REFERENCE_TARGETS["cd"]["target_lufs"] == -9.0


def test_publication_grade_rubric_covers_all_stages():
    for s in (Stage.COMPOSE, Stage.ARRANGE, Stage.MIX, Stage.MASTER):
        assert s in PUBLICATION_GRADE_RUBRIC
        assert isinstance(PUBLICATION_GRADE_RUBRIC[s], str)


# ---------- 集成：本地质检失败触发重做 ----------
def test_qc_failure_triggers_redo():
    """本地质检未通过应触发重做（commander.run_stage 被调用多次）。"""
    ai = _make_ai_mock(online=False)  # 关闭 AI 自评估，只测本地质检
    commander = _make_commander_mock()

    call_count = {"compose": 0}

    async def _run_stage(stage, user_prompt, context, bpm):
        call_count[stage.value] = call_count.get(stage.value, 0) + 1
        if stage == Stage.COMPOSE and call_count["compose"] == 1:
            # 第一次返回缺 markers 的产出（应被本地质检拒）
            return StageResult(
                stage=Stage.COMPOSE, summary="缺 markers",
                project=ProjectPlan(
                    title="x", genre="x",
                    tempo=TempoSpec(bpm=100, key="C"),
                    structure=["intro", "verse", "chorus", "outro"],
                    key="C",
                ),
                tracks=[TrackSpec(name="a"), TrackSpec(name="b")],
                regions=[MidiRegionSpec(track="a", notes=[NoteSpec(pitch=60, start=0)])],
            )
        # 第二次返回完整产出
        return _make_full_compose_result()

    commander.run_stage = _run_stage
    # ARRANGE/MIX/MASTER 直接返回完整产出
    async def _run_stage_full(stage, user_prompt, context, bpm):
        if stage == Stage.COMPOSE:
            return await _run_stage(stage, user_prompt, context, bpm)
        if stage == Stage.ARRANGE:
            return _make_full_arrange_result()
        if stage == Stage.MIX:
            return _make_full_mix_result()
        return _make_full_master_result()
    commander.run_stage = _run_stage_full

    pipe = AutonomousPipeline(ai, commander, tracker=None,
                              enable_self_eval=False, enable_publication_qc=True,
                              max_stage_retries=2)
    results = asyncio.run(pipe.run("测试"))
    assert len(results) == 4
    # compose 应被调用 2 次（初次被本地质检拒，第二次通过）
    assert call_count["compose"] == 2
