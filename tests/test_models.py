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
    MasterSpec, MixParams, PluginParamSpec, PluginSpec, ProjectPlan, RecordSpec,
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


# ============ 出版级混音/母带模型扩展测试 ============

# ---------- MixParams 出版级扩展字段 ----------
def test_mix_params_publication_grade_fields():
    """出版级 MixParams 应能容纳频率槽/增益分级/headroom/侧链/立体声宽度/编组总线。"""
    m = MixParams(
        track="贝斯",
        volume_db=-6, pan=0,
        gain_stage_db=-14,
        headroom_db=-6,
        frequency_slot=(60, 250),
        sidechain_from="底鼓",
        stereo_width=0.0,    # 低频居中
        bus_target="Drum Bus",
    )
    assert m.gain_stage_db == -14
    assert m.headroom_db == -6
    assert m.frequency_slot == (60, 250)
    assert m.sidechain_from == "底鼓"
    assert m.stereo_width == 0.0
    assert m.bus_target == "Drum Bus"


def test_mix_params_stereo_width_range():
    """stereo_width 必须在 0-2 范围内。"""
    with pytest.raises(ValidationError):
        MixParams(track="x", stereo_width=3.0)
    with pytest.raises(ValidationError):
        MixParams(track="x", stereo_width=-0.1)


def test_mix_params_optional_fields_default_none():
    """出版级扩展字段默认为 None（向后兼容）。"""
    m = MixParams(track="主旋律", volume_db=-6)
    assert m.gain_stage_db is None
    assert m.headroom_db is None
    assert m.frequency_slot is None
    assert m.sidechain_from is None
    assert m.stereo_width is None
    assert m.bus_target is None


# ---------- MasterSpec 出版级母带规范 ----------
def test_master_spec_defaults_streaming():
    """MasterSpec 默认值应符合流媒体标准（-14 LUFS / -1.0 dBTP）。"""
    ms = MasterSpec()
    assert ms.target_lufs == -14.0
    assert ms.true_peak_ceiling == -1.0
    assert ms.platform == "streaming"


def test_master_spec_full_publication_grade():
    """完整的出版级母带规范（含多段处理参数）。"""
    ms = MasterSpec(
        target_lufs=-14.0,
        true_peak_ceiling=-1.0,
        lra_target=6.0,
        stereo_width=1.2,
        platform="streaming",
        multiband_low_gain=-1.5,
        multiband_mid_gain=0.5,
        multiband_high_gain=1.5,
        notes="副歌提亮、尾奏渐弱",
    )
    assert ms.target_lufs == -14.0
    assert ms.true_peak_ceiling == -1.0
    assert ms.lra_target == 6.0
    assert ms.multiband_low_gain == -1.5
    assert ms.multiband_high_gain == 1.5
    assert "副歌" in ms.notes


def test_master_spec_lufs_range_validation():
    """target_lufs 必须在 -30..0 范围。"""
    with pytest.raises(ValidationError):
        MasterSpec(target_lufs=5.0)
    with pytest.raises(ValidationError):
        MasterSpec(target_lufs=-50)


def test_master_spec_true_peak_range_validation():
    """true_peak_ceiling 必须在 -6..0 范围。"""
    with pytest.raises(ValidationError):
        MasterSpec(true_peak_ceiling=1.0)
    with pytest.raises(ValidationError):
        MasterSpec(true_peak_ceiling=-10)


def test_master_spec_lra_range_validation():
    """lra_target 必须在 0..20 范围。"""
    with pytest.raises(ValidationError):
        MasterSpec(lra_target=25.0)


def test_master_spec_cd_platform():
    """CD 平台标准：-9 LUFS / -0.3 dBTP。"""
    ms = MasterSpec(target_lufs=-9.0, true_peak_ceiling=-0.3, platform="cd")
    assert ms.platform == "cd"
    assert ms.target_lufs == -9.0
    assert ms.true_peak_ceiling == -0.3


# ---------- StageResult 包含 master_spec ----------
def test_stage_result_with_master_spec():
    """MASTER 阶段产出应能携带 master_spec。"""
    r = StageResult(
        stage=Stage.MASTER,
        summary="母带完成",
        master_plugins=[PluginSpec(name="Limiter", preset="Loud")],
        master_spec=MasterSpec(target_lufs=-14.0, true_peak_ceiling=-1.0),
        bounce=BounceSpec(format="wav", bit_depth=24, sample_rate=44100),
    )
    assert r.master_spec is not None
    assert r.master_spec.target_lufs == -14.0
    assert r.bounce.bit_depth == 24


def test_stage_result_master_spec_default_none():
    """非 MASTER 阶段 master_spec 应为 None。"""
    r = StageResult(stage=Stage.COMPOSE, summary="x")
    assert r.master_spec is None
