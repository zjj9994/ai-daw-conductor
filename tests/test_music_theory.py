"""单元测试：乐理工具。运行 `pytest tests/`"""
from backend import music_theory as mt


def test_name_to_midi():
    assert mt.name_to_midi("C4") == 60
    assert mt.name_to_midi("A4") == 69
    assert mt.name_to_midi("A#3") == 58
    assert mt.name_to_midi("Bb3") == 58


def test_midi_to_name_roundtrip():
    assert mt.midi_to_name(60) == "C4"
    assert mt.midi_to_name(69) == "A4"
    for m in range(48, 84):
        assert mt.name_to_midi(mt.midi_to_name(m)) == m


def test_resolve_pitch_accepts_int_and_str():
    assert mt.resolve_pitch(60) == 60
    assert mt.resolve_pitch("60") == 60
    assert mt.resolve_pitch("C4") == 60


def test_scale_notes_in_key():
    notes = mt.scale_notes("C4", "major", 1)
    assert notes == [60, 62, 64, 65, 67, 69, 71]


def test_chord_notes():
    assert mt.chord_notes("C", "", octave=4) == [60, 64, 67]
    assert mt.chord_notes("A", "m", octave=3) == [57, 60, 64]


def test_quantize():
    assert mt.quantize(1.1, 0.25) == 1.0
    assert mt.quantize(1.3, 0.25) == 1.25
    assert mt.quantize(0.0) == 0.0


def test_clamp_velocity():
    assert mt.clamp_velocity(0) == 1
    assert mt.clamp_velocity(200) == 127
    assert mt.clamp_velocity(64) == 64


def test_in_scale():
    assert mt.in_scale(60, "C", "major")
    assert not mt.in_scale(61, "C", "major")
    assert mt.in_scale(57, "A", "minor")
