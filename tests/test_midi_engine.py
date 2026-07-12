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
