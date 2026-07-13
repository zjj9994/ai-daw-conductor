"""单元测试：commander 按人类工作流顺序执行新动作类型。运行 `pytest tests/`

用 AsyncMock 替身 DAWController，验证：
- execute_stage 按顺序调用全部 DAW 方法（tempo/markers/tracks/regions/region_ops/
  transports/buses/mix/plugin_params/automation/record/master/bounce/actions）
- 取消信号能中断循环
- 空字段不会触发对应方法
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from backend.commander import Commander
from backend.models import (
    AutomationPoint, AutomationSpec, BounceSpec, BusSpec, MarkerSpec,
    MixParams, PluginParamSpec, PluginSpec, ProjectPlan, RecordSpec,
    RegionOp, Stage, StageResult, TempoChangeSpec, TempoSpec, TrackSpec,
    TrackStackSpec, TransportAction, UIAction,
)


def _make_daw_mock():
    """构造一个全方法 AsyncMock 的 DAWController 替身。"""
    daw = MagicMock()
    daw.cfg = {"default_bpm": 100}
    for name in [
        "emit", "log", "create_project", "add_tempo_change", "add_marker",
        "ensure_track", "create_track_stack", "add_region", "region_op",
        "transport", "create_bus", "apply_mix", "set_plugin_param",
        "apply_automation", "setup_record", "apply_master", "bounce", "ui_action",
    ]:
        setattr(daw, name, AsyncMock())
    return daw


def _full_stage_result() -> StageResult:
    """构造一个包含全部新动作类型的 StageResult。

    stage 用 COMPOSE，因为 project 字段（新建工程）只允许在作曲阶段输出；
    其他动作类型（tempo/markers/tracks/regions/mix/master/bounce/actions）
    在任何阶段都可执行，commander 不按 stage 过滤。
    """
    return StageResult(
        stage=Stage.COMPOSE,
        summary="全功能测试",
        project=ProjectPlan(
            title="测试曲", genre="Pop",
            tempo=TempoSpec(bpm=120, time_signature="4/4", key="C minor"),
            structure=["intro", "chorus"], key="C minor",
        ),
        tempo_changes=[TempoChangeSpec(bar=9, bpm=104)],
        markers=[MarkerSpec(name="Chorus", bar=9)],
        tracks=[TrackSpec(name="主旋律", type="software")],
        track_stacks=[TrackStackSpec(name="鼓组", members=["底鼓"])],
        region_ops=[RegionOp(op="quantize", track="鼓组", grid="1/16")],
        transports=[TransportAction(op="goto", bar=1)],
        buses=[BusSpec(name="Reverb Bus")],
        mix=[MixParams(track="主旋律", volume_db=-6)],
        plugin_params=[PluginParamSpec(track="鼓组", plugin="Compressor",
                                        parameter="Threshold", value=-20)],
        automation=[AutomationSpec(track="主旋律", parameter="Volume",
                                    points=[AutomationPoint(bar=1, value=-6)])],
        record=RecordSpec(track="人声"),
        master_plugins=[PluginSpec(name="Limiter")],
        bounce=BounceSpec(format="wav"),
        actions=[UIAction(op="save")],
    )


# ---------- 顺序执行 ----------
def test_execute_stage_calls_all_methods_in_order():
    """execute_stage 应按人类工作流顺序调用全部 DAW 方法。"""
    daw = _make_daw_mock()
    cmd = Commander(ai=MagicMock(), daw=daw)
    result = _full_stage_result()

    asyncio.run(cmd.execute_stage(result, bpm=120))

    daw.create_project.assert_awaited_once()
    daw.add_tempo_change.assert_awaited_once()
    daw.add_marker.assert_awaited_once()
    daw.ensure_track.assert_awaited_once()
    daw.create_track_stack.assert_awaited_once()
    daw.region_op.assert_awaited_once()
    daw.transport.assert_awaited_once()
    daw.create_bus.assert_awaited_once()
    daw.apply_mix.assert_awaited_once()
    daw.set_plugin_param.assert_awaited_once()
    daw.apply_automation.assert_awaited_once()
    daw.setup_record.assert_awaited_once()
    daw.apply_master.assert_awaited_once()
    daw.bounce.assert_awaited_once()
    daw.ui_action.assert_awaited_once()


def test_execute_stage_human_workflow_order():
    """验证调用顺序符合人类工作流：tempo → markers → tracks → regions →
    region_ops → transports → buses → mix → plugin_params → automation →
    record → master → bounce → actions。"""
    daw = _make_daw_mock()
    cmd = Commander(ai=MagicMock(), daw=daw)
    result = _full_stage_result()

    manager = MagicMock()
    manager.attach_mock(daw.add_tempo_change, "add_tempo_change")
    manager.attach_mock(daw.add_marker, "add_marker")
    manager.attach_mock(daw.ensure_track, "ensure_track")
    manager.attach_mock(daw.create_track_stack, "create_track_stack")
    manager.attach_mock(daw.region_op, "region_op")
    manager.attach_mock(daw.transport, "transport")
    manager.attach_mock(daw.create_bus, "create_bus")
    manager.attach_mock(daw.apply_mix, "apply_mix")
    manager.attach_mock(daw.set_plugin_param, "set_plugin_param")
    manager.attach_mock(daw.apply_automation, "apply_automation")
    manager.attach_mock(daw.setup_record, "setup_record")
    manager.attach_mock(daw.apply_master, "apply_master")
    manager.attach_mock(daw.bounce, "bounce")
    manager.attach_mock(daw.ui_action, "ui_action")

    asyncio.run(cmd.execute_stage(result, bpm=120))

    order = [c[0] for c in manager.mock_calls if c[0] in (
        "add_tempo_change", "add_marker", "ensure_track", "create_track_stack",
        "region_op", "transport", "create_bus", "apply_mix", "set_plugin_param",
        "apply_automation", "setup_record", "apply_master", "bounce", "ui_action",
    )]
    expected = [
        "add_tempo_change", "add_marker", "ensure_track", "create_track_stack",
        "region_op", "transport", "create_bus", "apply_mix", "set_plugin_param",
        "apply_automation", "setup_record", "apply_master", "bounce", "ui_action",
    ]
    assert order == expected, f"调用顺序错误: {order}"


# ---------- 空字段不触发 ----------
def test_execute_stage_empty_result_calls_nothing():
    """空 StageResult（除 summary 外全空）不应触发任何 DAW 动作方法。"""
    daw = _make_daw_mock()
    cmd = Commander(ai=MagicMock(), daw=daw)
    result = StageResult(stage=Stage.COMPOSE, summary="空测试")

    asyncio.run(cmd.execute_stage(result, bpm=100))

    daw.create_project.assert_not_awaited()
    daw.add_tempo_change.assert_not_awaited()
    daw.add_marker.assert_not_awaited()
    daw.ensure_track.assert_not_awaited()
    daw.region_op.assert_not_awaited()
    daw.transport.assert_not_awaited()
    daw.create_bus.assert_not_awaited()
    daw.apply_mix.assert_not_awaited()
    daw.apply_automation.assert_not_awaited()
    daw.setup_record.assert_not_awaited()
    daw.apply_master.assert_not_awaited()
    daw.bounce.assert_not_awaited()
    daw.ui_action.assert_not_awaited()


# ---------- 取消信号 ----------
def test_cancel_stops_iteration():
    """取消信号应在下一动作前中断循环。"""
    daw = _make_daw_mock()
    cmd = Commander(ai=MagicMock(), daw=daw)
    result = StageResult(
        stage=Stage.COMPOSE, summary="取消测试",
        tempo_changes=[
            TempoChangeSpec(bar=1, bpm=100),
            TempoChangeSpec(bar=9, bpm=104),
            TempoChangeSpec(bar=17, bpm=108),
        ],
    )
    cmd._cancel = True
    asyncio.run(cmd.execute_stage(result, bpm=100))
    daw.add_tempo_change.assert_not_awaited()


def test_cancel_midway_stops_remaining():
    """在第一个动作调用后置 cancel=True，后续动作不应执行。"""
    daw = _make_daw_mock()
    cmd = Commander(ai=MagicMock(), daw=daw)
    result = StageResult(
        stage=Stage.COMPOSE, summary="中途取消",
        markers=[
            MarkerSpec(name="A", bar=1),
            MarkerSpec(name="B", bar=9),
            MarkerSpec(name="C", bar=17),
        ],
    )

    async def _set_cancel_after_first(*a, **kw):
        cmd._cancel = True
    daw.add_marker.side_effect = _set_cancel_after_first

    asyncio.run(cmd.execute_stage(result, bpm=100))
    assert daw.add_marker.await_count == 1


# ---------- stage 事件 ----------
def test_execute_stage_emits_stage_start_and_done():
    """应发出 stage_start 与 stage_done 事件。"""
    daw = _make_daw_mock()
    cmd = Commander(ai=MagicMock(), daw=daw)
    result = StageResult(stage=Stage.MIX, summary="事件测试", rationale="思路")

    asyncio.run(cmd.execute_stage(result, bpm=100))

    emit_kinds = [c.kwargs.get("kind") for c in daw.emit.await_args_list]
    assert "stage_start" in emit_kinds
    assert "stage_done" in emit_kinds
    done_call = next(c for c in daw.emit.await_args_list if c.kwargs.get("kind") == "stage_done")
    assert done_call.kwargs.get("rationale") == "思路"


# ---------- 多项目批量 ----------
def test_multiple_items_in_each_category():
    """每类动作有多个项目时全部应执行。"""
    daw = _make_daw_mock()
    cmd = Commander(ai=MagicMock(), daw=daw)
    result = StageResult(
        stage=Stage.ARRANGE, summary="多项目",
        tracks=[
            TrackSpec(name="底鼓"), TrackSpec(name="军鼓"), TrackSpec(name="踩镲"),
        ],
        region_ops=[
            RegionOp(op="quantize", track="底鼓", grid="1/16"),
            RegionOp(op="copy", track="主旋律", at_bar=1, to_bar=9),
            RegionOp(op="transpose", track="副旋律", semitones=12),
        ],
    )

    asyncio.run(cmd.execute_stage(result, bpm=100))
    assert daw.ensure_track.await_count == 3
    assert daw.region_op.await_count == 3
