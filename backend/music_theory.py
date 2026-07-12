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
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "pentatonic_major": [0, 2, 4, 7, 9],
    "pentatonic_minor": [0, 3, 5, 7, 10],
    "blues": [0, 3, 5, 6, 7, 10],
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
}

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
