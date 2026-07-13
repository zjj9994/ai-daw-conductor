"""乐理工具：音名 <-> MIDI 互转、音阶、和弦、量化等。

供 commander 校验/修补 AI 生成的 MIDI 数据使用。
"""
from __future__ import annotations

import re
from typing import Iterable

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
NAME_TO_SEMI = {n: i for i, n in enumerate(NOTE_NAMES)}
# 允许的变体
NAME_ALIASES = {"B#": "C", "Db": "C#", "Eb": "D#", "Fb": "E", "Gb": "F#",
                "Ab": "G#", "Bb": "A#", "Cb": "B", "♮": ""}

SCALES = {
    "major": [0, 2, 4, 5, 7, 9, 11],
    "minor": [0, 2, 3, 5, 7, 8, 10],
    "harmonic_minor": [0, 2, 3, 5, 7, 8, 11],
    "melodic_minor": [0, 2, 3, 5, 7, 9, 11],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "phrygian": [0, 1, 3, 5, 7, 8, 10],
    "lydian": [0, 2, 4, 6, 7, 9, 11],
    "mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "locrian": [0, 1, 3, 5, 6, 8, 10],
    "pentatonic_major": [0, 2, 4, 7, 9],
    "pentatonic_minor": [0, 3, 5, 7, 10],
    "blues": [0, 3, 5, 6, 7, 10],
    "japanese": [0, 1, 5, 7, 8],   # 和风音阶（平调子）
    "arabic": [0, 1, 4, 5, 7, 8, 11],
}

# 和弦品质 -> 相对根音的半音集合
CHORD_SHAPES = {
    "": [0, 4, 7],            # major
    "maj": [0, 4, 7],
    "m": [0, 3, 7],           # minor
    "min": [0, 3, 7],
    "dim": [0, 3, 6],
    "aug": [0, 4, 8],
    "sus2": [0, 2, 7],
    "sus4": [0, 5, 7],
    "maj7": [0, 4, 7, 11],
    "m7": [0, 3, 7, 10],
    "7": [0, 4, 7, 10],
    "dim7": [0, 3, 6, 9],
    "m7b5": [0, 3, 6, 10],
    "add9": [0, 4, 7, 14],
    "6": [0, 4, 7, 9],
    "m6": [0, 3, 7, 9],
    # 出版级常用扩展和弦（爵士/neo-soul/R&B）
    "maj9": [0, 4, 7, 11, 14],
    "m9": [0, 3, 7, 10, 14],
    "9": [0, 4, 7, 10, 14],
    "maj13": [0, 4, 7, 11, 14, 21],
    "m13": [0, 3, 7, 10, 14, 21],
    "13": [0, 4, 7, 10, 14, 21],
    "11": [0, 4, 7, 10, 14, 17],
    "m11": [0, 3, 7, 10, 14, 17],
    "maj7#11": [0, 4, 7, 11, 18],   # lydian 主和弦
    "7#9": [0, 4, 7, 10, 15],      # Hendrix 和弦
    "7b9": [0, 4, 7, 10, 13],
    "7#5": [0, 4, 8, 10],
    "7b5": [0, 4, 6, 10],
    "m7b5": [0, 3, 6, 10],
    "maj7b5": [0, 4, 6, 11],
    "6add9": [0, 4, 7, 9, 14],
    "m6add9": [0, 3, 7, 9, 14],
    "power": [0, 7],                # 5和弦
    "5": [0, 7],
}

# 常见和弦进行（罗马数字级数，按调式索引）。
# 出版级编曲必备——人类作曲家最常用的进行模板。
# 每个进行是一组级数（1=I主，2=ii，等），可代入任意调。
CHORD_PROGRESSIONS = {
    # 流行/摇滚
    "pop_classic": [1, 5, 6, 4],            # I-V-vi-IV（无数流行金曲）
    "pop_alt": [1, 4, 5, 4],                # I-IV-V-IV
    "pop_verse": [1, 6, 4, 5],              # I-vi-IV-V
    "pop_chorus": [4, 5, 3, 6],             # IV-V-iii-vi（副歌对比）
    # 爵士
    "jazz_ii_V_I": [2, 5, 1],               # ii-V-I（爵士骨架）
    "jazz_ii_V_I_vi": [2, 5, 1, 6],         # ii-V-I-vi（turnaround）
    "jazz_turnaround": [1, 6, 2, 5],        # I-vi-ii-V
    "jazz_coltrane": [1, 2, 3, 4, 4],       # Coltrane 替换雏形
    # 民谣/影视
    "folk_classic": [1, 4, 1, 5],           # I-IV-I-V
    "ballad": [1, 6, 3, 7],                 # I-vi-iii-VII（小调抒情）
    "cinematic": [1, 7, 6, 7],              # I-VII-vi-VII（史诗感）
    # 蓝调
    "blues_12bar": [1, 1, 1, 1, 4, 4, 1, 1, 5, 4, 1, 5],   # 12小节蓝调
    "blues_quick": [1, 4, 1, 5],            # 快速蓝调
    # R&B/Neo-soul
    "neo_soul": [1, 7, 3, 4],               # I-VII-iii-IV（带扩展和弦更佳）
    "rnb_love": [2, 5, 3, 6],               # ii-V-iii-vi
    # 情绪/影视
    "emotional": [6, 4, 1, 5],              # vi-IV-I-V（伤感）
    "uplifting": [1, 5, 4, 4],              # I-V-IV-IV
}

# 五度圈：C → G → D → A → E → B → F# → C# → G# → D# → A# → F → C
CIRCLE_OF_FIFTHS = ["C", "G", "D", "A", "E", "B", "F#", "C#", "G#", "D#", "A#", "F"]

_NOTE_RE = re.compile(r"^([A-Ga-g])([#b]?)(\d)?$", re.IGNORECASE)


def name_to_midi(name: str) -> int:
    """'C4' -> 60, 'A#3' -> 58。中央 C 为 C4=60。"""
    name = name.strip()
    if name in NAME_ALIASES:
        name = NAME_ALIASES[name]
    m = _NOTE_RE.match(name)
    if not m:
        raise ValueError(f"无法解析音名: {name!r}")
    letter, accidental, octave = m.groups()
    base = NAME_TO_SEMI[letter.upper()]
    if accidental == "#":
        base += 1
    elif accidental == "b":
        base -= 1
    oct_num = int(octave) if octave else 4
    return (oct_num + 1) * 12 + base


def midi_to_name(midi: int) -> str:
    midi = int(midi)
    name = NOTE_NAMES[midi % 12]
    octave = midi // 12 - 1
    return f"{name}{octave}"


def resolve_pitch(pitch: str | int) -> int:
    if isinstance(pitch, int):
        return int(pitch)
    try:
        return int(pitch)
    except ValueError:
        return name_to_midi(str(pitch))


def scale_notes(root: str | int, scale: str = "major", octaves: int = 2) -> list[int]:
    root_midi = resolve_pitch(root)
    intervals = SCALES.get(scale, SCALES["major"])
    notes: list[int] = []
    for o in range(octaves):
        for iv in intervals:
            notes.append(root_midi + o * 12 + iv)
    return notes


def chord_notes(root: str | int, quality: str = "", octave: int = 4) -> list[int]:
    root_midi = resolve_pitch(f"{root}{octave}") if isinstance(root, str) and not root[-1].isdigit() else resolve_pitch(root)
    shape = CHORD_SHAPES.get(quality, CHORD_SHAPES[""])
    return [root_midi + iv for iv in shape]


def quantize(value: float, grid: float = 0.25) -> float:
    """量化到网格（默认 1/16 拍）。"""
    return round(value / grid) * grid


def clamp_velocity(v: int) -> int:
    return max(1, min(127, int(v)))


def in_scale(midi: int, root: str | int, scale: str = "major") -> bool:
    root_pc = resolve_pitch(root) % 12
    intervals = SCALES.get(scale, SCALES["major"])
    return (midi - root_pc) % 12 in intervals


# ============ 出版级乐理扩展 ============

def chord_inversion(notes: list[int], inversion: int = 0) -> list[int]:
    """对和弦做转位。

    inversion=0 原位；1 第一转位（三音为低音）；2 第二转位（五音为低音）；
    3 第三转位（七音为低音，七和弦用）。转位把最低的音上移八度（旋转），
    让对应序数的音成为低音。转位让低声部线条更流畅，是声部连接的基础。
    """
    if not notes or inversion <= 0:
        return list(notes)
    sorted_notes = sorted(notes)
    inv = min(inversion, len(sorted_notes) - 1)
    result = list(sorted_notes)
    for i in range(inv):
        result[i] = result[i] + 12  # 最低音上移八度（旋转到顶部）
    return sorted(result)


def diatonic_chord(root: str | int, degree: int, scale: str = "major",
                   octave: int = 4, sevenths: bool = False) -> list[int]:
    """取调内自然和弦（按级数）。

    degree: 1=I, 2=ii, 3=iii, 4=IV, 5=V, 6=vi, 7=vii°
    sevenths: True 加七音（爵士/neo-soul 用）
    返回和弦音列表（MIDI 数字）。
    """
    root_midi = resolve_pitch(root) if isinstance(root, int) else name_to_midi(f"{root}{octave}")
    intervals = SCALES.get(scale, SCALES["major"])
    # 级数 1-based -> 0-based
    deg_idx = (degree - 1) % 7
    # 三和弦：根音 + 三度（叠三度）+ 五度
    root_pc = intervals[deg_idx]
    third_pc = intervals[(deg_idx + 2) % 7]
    # 处理跨八度
    third_offset = 12 if (deg_idx + 2) >= 7 else 0
    fifth_offset = 12 if (deg_idx + 4) >= 7 else 0
    fifth_pc = intervals[(deg_idx + 4) % 7]
    notes = [
        root_midi + root_pc,
        root_midi + third_pc + third_offset,
        root_midi + fifth_pc + fifth_offset,
    ]
    if sevenths:
        seventh_pc = intervals[(deg_idx + 6) % 7]
        seventh_offset = 12 if (deg_idx + 6) >= 7 else 0
        notes.append(root_midi + seventh_pc + seventh_offset)
    return notes


def progression_chords(key_root: str | int, progression_name: str,
                        scale: str = "major", octave: int = 4,
                        sevenths: bool = False) -> list[list[int]]:
    """按命名进行生成一组和弦。

    例：progression_chords("C", "pop_classic") 返回 C-G-Am-F 四个和弦的音列表。
    """
    degrees = CHORD_PROGRESSIONS.get(progression_name)
    if not degrees:
        raise ValueError(f"未知进行名：{progression_name}，可选：{list(CHORD_PROGRESSIONS)}")
    return [diatonic_chord(key_root, d, scale, octave, sevenths) for d in degrees]


def voice_lead(prev_chord: list[int], next_chord: list[int]) -> list[int]:
    """对 next_chord 做声部连接：每个声部移到最近的同名音，减少跳动。

    声部连接（voice leading）是出版级和声的核心：相邻和弦的共同音保持，
    其余声部做最小半音移动，让和声进行平滑、不突兀。
    """
    if not prev_chord or not next_chord:
        return list(next_chord)
    result: list[int] = []
    used = set()
    for n in next_chord:
        pc = n % 12
        # 在 prev_chord 找同音高类（pitch class）且未被占用的音
        candidates = [p for p in prev_chord if p % 12 == pc and p not in used]
        if candidates:
            # 选离 n 最近的八度
            best = min(candidates, key=lambda p: abs(p - n))
            result.append(best)
            used.add(best)
        else:
            # 没有共同音：选离 n 最近的可用音（任意八度同 pc）
            octave_opts = [n + 12 * k for k in range(-2, 3)]
            best = min(octave_opts, key=lambda x: min(abs(x - p) for p in prev_chord))
            result.append(best)
    return sorted(result)


def circle_distance(a: str, b: str) -> int:
    """五度圈上两个调之间的步数（最短距离）。用于判断转调远近。"""
    try:
        ia = CIRCLE_OF_FIFTHS.index(a)
        ib = CIRCLE_OF_FIFTHS.index(b)
    except ValueError:
        return 99
    diff = abs(ia - ib)
    return min(diff, 12 - diff)


def passing_tones(start: int, end: int, scale_root: str | int,
                  scale: str = "major") -> list[int]:
    """在两个目标音之间插入过渡音（调内），让旋律线条更流畅。

    用于出版级主旋律：长跳进之间填阶进音，避免空洞。
    返回包含首尾的完整音序列。
    """
    if start == end:
        return [start]
    intervals = SCALES.get(scale, SCALES["major"])
    root_pc = resolve_pitch(scale_root) % 12
    # 调内音高类集合
    scale_pcs = {(root_pc + iv) % 12 for iv in intervals}
    direction = 1 if end > start else -1
    result = [start]
    cur = start
    while cur != end:
        cur += direction
        if (cur % 12) in scale_pcs:
            result.append(cur)
    return result


def nearest_scale_tone(midi: int, root: str | int, scale: str = "major") -> int:
    """把任意音吸附到最近的调内音（用于修正离调音）。"""
    if in_scale(midi, root, scale):
        return midi
    for delta in [1, -1, 2, -2]:
        if in_scale(midi + delta, root, scale):
            return midi + delta
    return midi


def frequency_slot(track_type: str) -> tuple[int, int]:
    """返回某类轨道的推荐频率占用区间（Hz），用于出版级频率避让编排。

    像人类混音师一样给每个声部预留频段，避免频率打架：
    - 底鼓：30-80Hz（低频基础）
    - 贝斯：60-250Hz（低频支撑）
    - 鼓组其余：200-8kHz（中频冲击+高频空气）
    - 主旋律/人声：200Hz-5kHz（中频核心，最重要）
    - 和弦/铺底：250Hz-4kHz（中频填充）
    - 高频装饰：5kHz-16kHz（空气感）
    """
    slots = {
        "kick": (30, 80),
        "bass": (60, 250),
        "drums": (200, 8000),
        "vocal": (200, 5000),
        "melody": (200, 5000),
        "pad": (250, 4000),
        "chord": (250, 4000),
        "lead": (2000, 12000),
        "fx": (5000, 16000),
    }
    t = (track_type or "").lower()
    for k, v in slots.items():
        if k in t:
            return v
    return (100, 8000)  # 默认中频全频段
