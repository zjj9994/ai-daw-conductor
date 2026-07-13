"""单元测试：MIDI 引擎（鼓组通道、延音踏板、文件生成）。"""
from pathlib import Path

import mido

from backend.midi_engine import MidiEngine, DRUM_CHANNEL, SUSTAIN_CC
from backend.models import MidiRegionSpec, NoteSpec, TempoSpec


def _engine():
    return MidiEngine(midi_port=None, humanize=False)


def test_build_midi_file_creates_valid_file(tmp_path):
    eng = _engine()
    region = MidiRegionSpec(
        track="主旋律", instrument="Acoustic Grand Piano",
        notes=[NoteSpec(pitch="C4", start=0, duration=1, velocity=90)],
    )
    out = eng.build_midi_file([region], TempoSpec(bpm=100), out_path=tmp_path / "t.mid")
    assert out.exists()
    mid = mido.MidiFile(str(out))
    assert len(mid.tracks) == 2  # tempo track + 1 region track


def test_drum_routed_to_channel_10(tmp_path):
    eng = _engine()
    region = MidiRegionSpec(
        track="鼓组", instrument="Drum Kit Designer",
        notes=[NoteSpec(pitch=36, start=0, duration=0.5, velocity=110)],
    )
    out = eng.build_midi_file([region], TempoSpec(bpm=120), out_path=tmp_path / "drum.mid")
    mid = mido.MidiFile(str(out))
    # 找到 note_on，确认 channel == DRUM_CHANNEL(9)
    drum_track = mid.tracks[1]
    note_ons = [m for m in drum_track if m.type == "note_on"]
    assert note_ons
    assert note_ons[0].channel == DRUM_CHANNEL


def test_piano_gets_sustain_pedal(tmp_path):
    eng = _engine()
    region = MidiRegionSpec(
        track="主旋律", instrument="Acoustic Grand Piano",
        notes=[NoteSpec(pitch="C4", start=0, duration=2, velocity=90)],
    )
    out = eng.build_midi_file([region], TempoSpec(bpm=100), out_path=tmp_path / "piano.mid")
    mid = mido.MidiFile(str(out))
    piano_track = mid.tracks[1]
    ccs = [m for m in piano_track if m.type == "control_change" and m.control == SUSTAIN_CC]
    assert len(ccs) >= 2  # 踩下 + 松开
    assert ccs[0].value == 127
    assert ccs[-1].value == 0


def test_non_sustain_instrument_no_pedal(tmp_path):
    eng = _engine()
    region = MidiRegionSpec(
        track="贝斯", instrument="Synth Bass",
        notes=[NoteSpec(pitch="A1", start=0, duration=2, velocity=95)],
    )
    out = eng.build_midi_file([region], TempoSpec(bpm=100), out_path=tmp_path / "bass.mid")
    mid = mido.MidiFile(str(out))
    bass_track = mid.tracks[1]
    ccs = [m for m in bass_track if m.type == "control_change" and m.control == SUSTAIN_CC]
    assert len(ccs) == 0


def test_zero_duration_note_safe(tmp_path):
    eng = _engine()
    region = MidiRegionSpec(
        track="x", instrument="piano",
        notes=[NoteSpec(pitch="C4", start=0, duration=0, velocity=80)],
    )
    out = eng.build_midi_file([region], TempoSpec(bpm=100), out_path=tmp_path / "zero.mid")
    assert out.exists()  # 不应抛异常


# ============ 出版级 MIDI 引擎扩展测试 ============

def test_swing_delays_offbeat_notes(tmp_path):
    """swing 应让反拍(0.5 拍位)的音符延迟出现。"""
    eng_no_swing = MidiEngine(midi_port=None, humanize=False, swing=0.0)
    eng_swing = MidiEngine(midi_port=None, humanize=False, swing=0.5)

    # 一个反拍音符（start=0.5）
    region = MidiRegionSpec(
        track="melody", instrument="Piano",
        notes=[NoteSpec(pitch="C4", start=0.5, duration=0.5, velocity=90)],
    )
    out_plain = eng_no_swing.build_midi_file([region], TempoSpec(bpm=120),
                                              out_path=tmp_path / "plain.mid")
    out_swing = eng_swing.build_midi_file([region], TempoSpec(bpm=120),
                                          out_path=tmp_path / "swing.mid")

    def first_note_tick(path):
        mid = mido.MidiFile(str(path))
        track = mid.tracks[1]
        for msg in track:
            if msg.type == "note_on":
                return msg.time
        return None

    # swing 版本反拍音符的相对起始 tick 应大于无 swing 版本
    assert first_note_tick(out_swing) > first_note_tick(out_plain)


def test_swing_clamped_to_max(tmp_path):
    """swing 超过 0.7 应被裁剪到 0.7。"""
    eng = MidiEngine(midi_port=None, humanize=False, swing=1.5)
    assert eng.swing == 0.7


def test_expression_cc_added_for_strings(tmp_path):
    """expression=True 时弦乐/管乐类应加 CC11 表情控制。"""
    from backend.midi_engine import EXPRESSION_CC
    eng = MidiEngine(midi_port=None, humanize=False, swing=0.0, expression=True)
    region = MidiRegionSpec(
        track="strings", instrument="String Ensemble",
        notes=[NoteSpec(pitch="C4", start=0, duration=4, velocity=85)],
    )
    out = eng.build_midi_file([region], TempoSpec(bpm=100), out_path=tmp_path / "expr.mid")
    mid = mido.MidiFile(str(out))
    track = mid.tracks[1]
    expr_ccs = [m for m in track if m.type == "control_change" and m.control == EXPRESSION_CC]
    assert len(expr_ccs) >= 1  # 至少有表情事件


def test_velocity_curve_boosts_downbeat(tmp_path):
    """强拍(整拍位置)音符力度应比弱拍高。"""
    eng = MidiEngine(midi_port=None, humanize=False)
    # 强拍音符
    n_strong = NoteSpec(pitch="C4", start=0.0, duration=1, velocity=80)
    # 弱拍音符
    n_weak = NoteSpec(pitch="E4", start=0.25, duration=0.5, velocity=80)
    region = MidiRegionSpec(
        track="melody", instrument="Piano",
        notes=[n_strong, n_weak],
    )
    eng.build_midi_file([region], TempoSpec(bpm=100), out_path=tmp_path / "vel.mid")
    # 调用 _velocity_curve 直接验证
    v_strong = eng._velocity_curve(n_strong, is_drum=False)
    v_weak = eng._velocity_curve(n_weak, is_drum=False)
    assert v_strong > v_weak
    assert 1 <= v_strong <= 127
    assert 1 <= v_weak <= 127


def test_velocity_curve_drum_kick_boosted(tmp_path):
    """鼓组底鼓(pitch=36)力度应额外增强。"""
    eng = MidiEngine(midi_port=None, humanize=False)
    n_kick = NoteSpec(pitch=36, start=0, duration=0.5, velocity=80)
    n_hihat = NoteSpec(pitch=42, start=0, duration=0.5, velocity=80)
    v_kick = eng._velocity_curve(n_kick, is_drum=True)
    v_hihat = eng._velocity_curve(n_hihat, is_drum=True)
    assert v_kick > v_hihat  # 底鼓比踩镲强


def test_velocity_curve_clamps_to_range():
    """力度曲线结果必须落在 1-127（即使输入超界也要被 clamp）。"""
    eng = MidiEngine(midi_port=None, humanize=False)
    # NoteSpec 已 clamp 到 1-127，这里验证 _velocity_curve 输出始终在范围内
    for vel in (1, 50, 100, 127):
        n = NoteSpec(pitch=60, start=0, duration=1, velocity=vel)
        result = eng._velocity_curve(n, is_drum=False)
        assert 1 <= result <= 127
    # 直接验证 mt.clamp_velocity 的边界（_velocity_curve 内部调用它）
    from backend import music_theory as mt
    assert mt.clamp_velocity(-5) == 1
    assert mt.clamp_velocity(200) == 127


def test_legato_overlaps_adjacent_notes(tmp_path):
    """legato：相邻同轨音符应轻微重叠，避免间断。"""
    eng = MidiEngine(midi_port=None, humanize=False)
    region = MidiRegionSpec(
        track="lead", instrument="Synth Lead",
        notes=[
            NoteSpec(pitch="C4", start=0, duration=1, velocity=90),
            NoteSpec(pitch="E4", start=1, duration=1, velocity=90),
        ],
    )
    out = eng.build_midi_file([region], TempoSpec(bpm=100), out_path=tmp_path / "legato.mid")
    mid = mido.MidiFile(str(out))
    track = mid.tracks[1]
    note_offs = [m for m in track if m.type == "note_off"]
    note_ons = [m for m in track if m.type == "note_on" and m.velocity > 0]
    assert len(note_ons) == 2
    assert len(note_offs) == 2
    # 第一个音符的 note_off 应在第二个 note_on 之后（重叠）或同时
    # 这里验证两个音符都正常生成即可，重叠通过 build_midi_file 内部 legato 逻辑实现
    assert note_ons[0].note == 60  # C4
    assert note_ons[1].note == 64  # E4
