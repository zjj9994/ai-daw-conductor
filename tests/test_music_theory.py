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


# ============ 出版级乐理扩展测试 ============

def test_extended_chord_shapes_exist():
    """出版级扩展和弦品质必须可用。"""
    for q in ("maj9", "m9", "9", "maj13", "m13", "13", "11", "m11",
              "maj7#11", "7#9", "7b9", "6add9"):
        assert q in mt.CHORD_SHAPES, f"缺少扩展和弦品质 {q}"
        assert len(mt.CHORD_SHAPES[q]) >= 4, f"{q} 至少应有 4 个音"
    # power/5 和弦特例：只有根音+五音（2 音）
    assert len(mt.CHORD_SHAPES["power"]) == 2


def test_extended_scales_available():
    """出版级音阶必须可用（melodic_minor/phrygian/lydian 等）。"""
    # 7 音音阶
    for s in ("major", "minor", "melodic_minor", "phrygian", "lydian",
              "mixolydian", "locrian", "arabic"):
        assert s in mt.SCALES
        assert len(mt.SCALES[s]) == 7, f"{s} 应为 7 音音阶"
    # 5 音/6 音音阶（五声/蓝调）
    for s in ("pentatonic_major", "pentatonic_minor", "japanese"):
        assert s in mt.SCALES
        assert len(mt.SCALES[s]) == 5, f"{s} 应为 5 音音阶"


def test_chord_progressions_dict():
    """常见和弦进行模板存在且为级数列表。"""
    for name in ("pop_classic", "jazz_ii_V_I", "blues_12bar", "emotional"):
        assert name in mt.CHORD_PROGRESSIONS
        progs = mt.CHORD_PROGRESSIONS[name]
        assert isinstance(progs, list) and len(progs) >= 2
        assert all(1 <= d <= 7 for d in progs)


def test_circle_of_fifths_has_12_keys():
    assert len(mt.CIRCLE_OF_FIFTHS) == 12
    assert mt.CIRCLE_OF_FIFTHS[0] == "C"


def test_chord_inversion_first():
    """第一转位：三音下移八度成为低音。"""
    c_major = [60, 64, 67]  # C E G
    inv1 = mt.chord_inversion(c_major, inversion=1)
    # 第一转位应为 E G C（64 67 72）或其等价八度排布
    assert inv1[0] == 64
    assert inv1[-1] == 72


def test_chord_inversion_zero_returns_original():
    c_major = [60, 64, 67]
    inv0 = mt.chord_inversion(c_major, inversion=0)
    assert inv0 == [60, 64, 67]


def test_diatonic_chord_returns_three_or_four_notes():
    """调内自然三和弦含 3 音；sevenths=True 含 4 音。"""
    triad = mt.diatonic_chord("C", degree=1, scale="major")
    assert len(triad) == 3
    assert triad == [60, 64, 67]  # C E G
    seventh = mt.diatonic_chord("C", degree=5, scale="major", sevenths=True)
    assert len(seventh) == 4  # G B D F (V7)


def test_progression_chords_pop_classic():
    """I-V-vi-IV 在 C 大调应为 C-G-Am-F。"""
    chords = mt.progression_chords("C", "pop_classic")
    assert len(chords) == 4
    # 第一个和弦根音是 C(60)，最后是 F(65)
    assert chords[0][0] == 60
    assert chords[-1][0] == 65


def test_progression_chords_unknown_raises():
    import pytest
    with pytest.raises(ValueError):
        mt.progression_chords("C", "nonexistent_progression")


def test_voice_lead_keeps_common_tones():
    """C-G 之间有共同音 G，声部连接应保持它。"""
    c_major = [60, 64, 67]  # C E G
    g_major = [67, 71, 74]  # G B D
    result = mt.voice_lead(c_major, g_major)
    # G(67) 是共同音，应保留
    assert 67 in result
    # 结果应与原和弦音高类相同（G B D）
    assert sorted(n % 12 for n in result) == [2, 7, 11]


def test_voice_lead_minimizes_movement():
    """相邻和弦声部连接应让总位移最小。"""
    c_major = [60, 64, 67]
    f_major = [65, 69, 72]  # F A C
    result = mt.voice_lead(c_major, f_major)
    # 共同音 C(60/72)，应选离原位最近的
    assert 60 in result or 72 in result


def test_circle_distance_c_to_g_is_one():
    """C 到 G 在五度圈上是 1 步。"""
    assert mt.circle_distance("C", "G") == 1
    assert mt.circle_distance("C", "F") == 1
    assert mt.circle_distance("C", "C") == 0


def test_circle_distance_unknown_returns_large():
    assert mt.circle_distance("C", "H") == 99


def test_passing_tones_stepwise():
    """C 到 G 之间的调内过渡音应为 C D E F G。"""
    result = mt.passing_tones(60, 67, scale_root="C", scale="major")
    assert result[0] == 60
    assert result[-1] == 67
    # 应单调递增
    assert result == sorted(result)
    # 都应在 C 大调内
    assert all(mt.in_scale(n, "C", "major") for n in result)


def test_passing_tones_same_note():
    assert mt.passing_tones(60, 60, "C", "major") == [60]


def test_nearest_scale_tone_in_scale_unchanged():
    assert mt.nearest_scale_tone(60, "C", "major") == 60


def test_nearest_scale_tone_snaps_off_scale():
    """C# 不在 C 大调，应吸附到 C 或 D。"""
    result = mt.nearest_scale_tone(61, "C", "major")
    assert result in (60, 62)


def test_frequency_slot_returns_valid_range():
    """频率槽返回 (low, high) 元组，low < high。"""
    for t in ("kick", "bass", "drums", "vocal", "melody", "pad", "lead"):
        low, high = mt.frequency_slot(t)
        assert 0 < low < high <= 20000


def test_frequency_slot_kick_low_bass_high():
    """底鼓频段低于贝斯，符合出版级频率避让原则。"""
    kick_low, _ = mt.frequency_slot("kick")
    bass_low, bass_high = mt.frequency_slot("bass")
    assert kick_low < bass_low  # 底鼓比贝斯更低
    # 底鼓与贝斯有重叠区（用于侧链避让）
    assert bass_low < 100


def test_frequency_slot_default():
    low, high = mt.frequency_slot("未知乐器")
    assert low < high
