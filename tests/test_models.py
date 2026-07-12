"""单元测试：新数据模型校验。运行 `pytest tests/`

覆盖「像人类一样全面操作 Logic Pro」新增的全部 Pydantic 模型：
TransportAction / RegionOp / AutomationSpec / AutomationPoint /
MarkerSpec / TempoChangeSpec / PluginParamSpec / BusSpec /
TrackStackSpec / RecordSpec / UIAction，以及 StageResult 的全字段聚合。
"""
import pytest
from pydantic import ValidationError

from backend.models import (
    AutomationPoint, AutomationSpec, BounceSpec, BusSpec, MarkerSpec,
    MixParams, PluginParamSpec, PluginSpec, ProjectPlan, RecordSpec,
    RegionOp, Stage, StageResult, TempoChangeSpec, TempoSpec, TrackSpec,
    TrackStackSpec, TransportAction, UIAction,
)


# ---------- TransportAction ----------
def test_transport_goto_bar():
    a = TransportAction(op="goto", bar=17)
    assert a.op == "goto"
    assert a.bar == 17


def test_transport_set_cycle_range():
    a = TransportAction(op="set_cycle", start_bar=1, end_bar=32)
    assert a.start_bar == 1
    assert a.end_bar == 32


def test_transport_bar_must_be_positive():
    with pytest.raises(ValidationError):
        TransportAction(op="goto", bar=0)


# ---------- RegionOp ----------
def test_region_op_quantize():
    r = RegionOp(op="quantize", track="鼓组", grid="1/16", strength=80)
    assert r.op == "quantize"
    assert r.grid == "1/16"
    assert r.strength == 80


def test_region_op_transpose_semitones():
    r = RegionOp(op="transpose", track="副旋律", semitones=-12)
    assert r.semitones == -12


def test_region_op_strength_range():
    with pytest.raises(ValidationError):
        RegionOp(op="quantize", strength=150)


# ---------- Automation ----------
def test_automation_point_defaults():
    p = AutomationPoint(bar=1.0, value=-6.0)
    assert p.shape == "linear"


def test_automation_spec_with_points():
    a = AutomationSpec(
        track="主旋律", parameter="Volume", mode="latch",
        points=[AutomationPoint(bar=1, value=-6), AutomationPoint(bar=9, value=-3)],
    )
    assert a.track == "主旋律"
    assert len(a.points) == 2
    assert a.mode == "latch"


def test_automation_default_mode_is_write():
    a = AutomationSpec(track="x", parameter="Pan")
    assert a.mode == "write"


# ---------- Marker ----------
def test_marker_basic():
    m = MarkerSpec(name="Chorus", bar=9)
    assert m.name == "Chorus"
    assert m.bar == 9


def test_marker_bar_must_be_positive():
    with pytest.raises(ValidationError):
        MarkerSpec(name="x", bar=0)


def test_marker_color_range():
    with pytest.raises(ValidationError):
        MarkerSpec(name="x", bar=1, color=100)


# ---------- TempoChange ----------
def test_tempo_change_ramp():
    t = TempoChangeSpec(bar=17, bpm=104, ramp=True)
    assert t.ramp is True


def test_tempo_change_bpm_range():
    with pytest.raises(ValidationError):
        TempoChangeSpec(bar=1, bpm=10)


# ---------- PluginParamSpec ----------
def test_plugin_param():
    p = PluginParamSpec(track="鼓组", plugin="Compressor", parameter="Threshold", value=-20)
    assert p.value == -20


# ---------- BusSpec ----------
def test_bus_with_plugins():
    b = BusSpec(
        name="Reverb Bus", input="Bus 1",
        plugins=[PluginSpec(name="Space Designer", preset="Large Hall")],
    )
    assert b.name == "Reverb Bus"
    assert len(b.plugins) == 1


# ---------- TrackStackSpec ----------
def test_track_stack():
    ts = TrackStackSpec(name="鼓组", members=["底鼓", "军鼓", "踩镲"], stack_type="folder")
    assert ts.stack_type == "folder"
    assert len(ts.members) == 3


# ---------- RecordSpec ----------
def test_record_spec_defaults():
    r = RecordSpec(track="人声")
    assert r.armed is True
    assert r.count_in == 0


def test_record_with_autopunch():
    r = RecordSpec(track="人声", count_in=1, autopunch={"start_bar": 9, "end_bar": 16})
    assert r.autopunch["start_bar"] == 9


# ---------- UIAction ----------
def test_ui_action_save():
    a = UIAction(op="save")
    assert a.op == "save"


def test_ui_action_save_as_with_path():
    a = UIAction(op="save_as", path="/tmp/x.logicx")
    assert a.path == "/tmp/x.logicx"


# ---------- TrackSpec 扩展字段 ----------
def test_track_spec_color_group_freeze():
    t = TrackSpec(name="主旋律", type="software", color=5, group="弦乐", freeze=True)
    assert t.color == 5
    assert t.group == "弦乐"
    assert t.freeze is True


def test_track_spec_color_range_invalid():
    with pytest.raises(ValidationError):
        TrackSpec(name="x", color=100)


# ---------- BounceSpec 扩展 ----------
def test_bounce_stems_and_bar_range():
    b = BounceSpec(format="wav", stems=True, start_bar=1, end_bar=32)
    assert b.stems is True
    assert b.start_bar == 1
    assert b.end_bar == 32


# ---------- StageResult 聚合全部新字段 ----------
def test_stage_result_full_aggregation():
    """一个 StageResult 同时包含全部新动作类型字段。"""
    r = StageResult(
        stage=Stage.MIX,
        summary="测试混音",
        tempo_changes=[TempoChangeSpec(bar=17, bpm=104)],
        markers=[MarkerSpec(name="Chorus", bar=9)],
        track_stacks=[TrackStackSpec(name="鼓组", members=["底鼓"])],
        region_ops=[RegionOp(op="quantize", track="鼓组", grid="1/16")],
        transports=[TransportAction(op="goto", bar=1)],
        buses=[BusSpec(name="Reverb Bus")],
        mix=[MixParams(track="主旋律", volume_db=-6, pan=0)],
        plugin_params=[PluginParamSpec(track="鼓组", plugin="Compressor",
                                        parameter="Threshold", value=-20)],
        automation=[AutomationSpec(track="主旋律", parameter="Volume",
                                    points=[AutomationPoint(bar=1, value=-6)])],
        record=RecordSpec(track="人声"),
        master_plugins=[PluginSpec(name="Limiter")],
        bounce=BounceSpec(format="wav"),
        actions=[UIAction(op="save")],
    )
    assert r.stage == Stage.MIX
    assert len(r.tempo_changes) == 1
    assert len(r.markers) == 1
    assert len(r.track_stacks) == 1
    assert len(r.region_ops) == 1
    assert len(r.transports) == 1
    assert len(r.buses) == 1
    assert len(r.mix) == 1
    assert len(r.plugin_params) == 1
    assert len(r.automation) == 1
    assert r.record is not None
    assert len(r.master_plugins) == 1
    assert r.bounce is not None
    assert len(r.actions) == 1


def test_stage_result_default_empty_lists():
    """新字段默认都应该是空列表，不会是 None。"""
    r = StageResult(stage=Stage.COMPOSE, summary="x")
    assert r.tempo_changes == []
    assert r.markers == []
    assert r.track_stacks == []
    assert r.region_ops == []
    assert r.transports == []
    assert r.buses == []
    assert r.plugin_params == []
    assert r.automation == []
    assert r.actions == []
    assert r.record is None
    assert r.bounce is None
