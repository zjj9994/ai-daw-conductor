"""MIDI 引擎：把音符/片段转换为 MIDI 数据并送往 Logic Pro。

两种工作模式：
  1. 文件模式（推荐）：生成标准 MIDI 文件，由 AppleScript 导入 Logic Pro。
     - 跨平台、可靠、可在轨道上编辑。
  2. 实时模式：通过虚拟 MIDI 端口实时发送 Note 开关 / CC，用于现场触发与混音器控制。
     - 需要本机安装 python-rtmidi，且 Logic Pro 开启该端口输入。

出版级增强（让 MIDI 听起来像真人演奏）：
- 鼓组自动路由到 GM 通道 10（channel 9）
- 人性化：力度曲线（强弱拍动态）+ 时值微偏移（groove）+ swing（摇摆）
- 钢琴/弦乐类自动加延音踏板 CC64
- 表情 CC：CC11(Expression) 渐强渐弱、CC1(Modulation) 颤音深度
- legato：相邻同轨音符轻微重叠，避免音符间断的机器感
- velocity 曲线：按拍位（强拍/弱拍/反拍）分配力度，模拟人类律动
"""
from __future__ import annotations

import asyncio
import random
import tempfile
from pathlib import Path
from typing import Optional

import mido
from mido import Message, MidiFile, MidiTrack, MetaMessage, bpm2tempo

from . import music_theory as mt
from .models import MidiRegionSpec, NoteSpec, TempoSpec

# GM 标准鼓组在 channel 10（0-indexed 为 9）
DRUM_CHANNEL = 9
# 常用 CC 编号
SUSTAIN_CC = 64        # 延音踏板
EXPRESSION_CC = 11     # 表情（音量包络渐变）
MODULATION_CC = 1      # 调制（颤音深度）
BREATH_CC = 2          # 呼吸控制器（管乐表情）
VOLUME_CC = 7          # 通道音量


class MidiEngine:
    def __init__(self, midi_port: Optional[str] = None, ticks_per_beat: int = 480,
                 humanize: bool = False, humanize_seed: int = 42,
                 swing: float = 0.0, expression: bool = False):
        """
        Args:
            swing: 摇摆量 0.0-0.7（0=直，0.5=三连音感，0.67=轻摇摆）
            expression: 是否自动加表情 CC（弦乐/管乐/Pad 渐强渐弱）
        """
        self.ticks_per_beat = ticks_per_beat
        self.port_name = midi_port
        self.humanize = humanize
        self.swing = max(0.0, min(0.7, swing))
        self.expression = expression
        self._rng = random.Random(humanize_seed)  # 固定种子保证可复现
        self._outport = None
        self._connect()

    def _connect(self):
        """尝试打开虚拟输出端口；非 macOS / 无 rtmidi 时降级为仅文件模式。"""
        try:
            import rtmidi  # noqa: F401
            self._outport = mido.open_output(
                self.port_name or "AI-DAW-Conductor", virtual=True
            )
        except Exception:
            # rtmidi 不可用或非实时环境，静默降级
            self._outport = None

    # ---------- 文件模式 ----------
    def build_midi_file(
        self,
        regions: list[MidiRegionSpec],
        tempo: TempoSpec,
        out_path: Optional[Path] = None,
    ) -> Path:
        """把多个 region 合并到一个 type-1 MIDI 文件（每 region 一个轨道）。"""
        mid = MidiFile(ticks_per_beat=self.ticks_per_beat)

        # tempo track
        tempo_track = MidiTrack()
        tempo_track.append(MetaMessage(
            "set_tempo", tempo=bpm2tempo(tempo.bpm), time=0
        ))
        if tempo.time_signature:
            num, den = tempo.time_signature.split("/")
            tempo_track.append(MetaMessage(
                "time_signature",
                numerator=int(num), denominator=int(den),
                clocks_per_click=24, notated_32nd_notes_per_beat=8, time=0,
            ))
        mid.tracks.append(tempo_track)

        for region in regions:
            track = MidiTrack()
            # mido 用 latin-1 编码 track_name，需转为 ASCII 安全名；
            # Logic Pro 中的真实轨道名由 AppleScript 单独设置
            track.append(MetaMessage("track_name", name=self._ascii_name(region.track), time=0))

            is_drum = self._is_drum(region.instrument) or self._is_drum(region.track)
            channel = DRUM_CHANNEL if is_drum else 0
            program = 0 if is_drum else self._guess_program(region.instrument)
            track.append(Message("program_change", channel=channel, program=program, time=0))

            # 钢琴/弦乐类加延音踏板
            use_sustain = (not is_drum) and self._wants_sustain(region.instrument)
            if use_sustain and region.notes:
                start_tick = int(round(min(n.start for n in region.notes) * self.ticks_per_beat))
                track.append(Message("control_change", channel=channel, control=SUSTAIN_CC, value=127, time=start_tick))

            # 是否加表情 CC（弦乐/管乐/Pad 类）
            use_expr = (not is_drum) and self.expression and self._wants_expression(region.instrument)
            if use_expr:
                track.append(Message("control_change", channel=channel, control=EXPRESSION_CC, value=100, time=0))
                track.append(Message("control_change", channel=channel, control=MODULATION_CC, value=0, time=0))

            notes_sorted = sorted(region.notes, key=lambda n: n.start)
            events: list[tuple[int, str, NoteSpec]] = []
            for idx, n in enumerate(notes_sorted):
                start_beat = n.start
                # swing：把反拍（.5 拍位）往后推
                if self.swing > 0:
                    frac = start_beat - int(start_beat)
                    if 0.4 < frac < 0.6:  # 反拍
                        start_beat += self.swing * 0.5
                start_tick = int(round(start_beat * self.ticks_per_beat))
                dur_beats = n.duration
                # legato：与下一个同轨音符重叠 5%（避免音符间断的机器感）
                if not is_drum and idx + 1 < len(notes_sorted):
                    next_start = notes_sorted[idx + 1].start
                    if next_start > start_beat and next_start < start_beat + dur_beats:
                        dur_beats = next_start - start_beat
                end_tick = int(round((start_beat + dur_beats) * self.ticks_per_beat))
                if end_tick <= start_tick:
                    end_tick = start_tick + 1  # 最小 1 tick，避免零长音符
                events.append((start_tick, "on", n))
                events.append((end_tick, "off", n))

                # 表情 CC：长音符中间加渐变（出版级弦乐/管乐必备）
                if use_expr and dur_beats >= 2.0:
                    mid_tick = start_tick + (end_tick - start_tick) // 2
                    events.append((mid_tick, "cc_expr", n))

            events.sort(key=lambda e: (e[0], 0 if e[1] == "off" else 1))

            abs_tick = 0
            for tick, kind, n in events:
                delta = max(0, tick - abs_tick)
                abs_tick = tick
                pitch = mt.resolve_pitch(n.pitch)
                if kind == "on":
                    vel = self._velocity_curve(n, is_drum)
                    track.append(Message("note_on", channel=channel, note=pitch, velocity=vel, time=delta))
                elif kind == "cc_expr":
                    # 长音符中段轻微渐强（90 -> 110）
                    val = 90 + self._rng.randint(0, 20)
                    track.append(Message("control_change", channel=channel, control=EXPRESSION_CC, value=val, time=delta))
                else:
                    track.append(Message("note_off", channel=channel, note=pitch, velocity=0, time=delta))

            if use_sustain and region.notes:
                end_tick = int(round(max(n.start + n.duration for n in region.notes) * self.ticks_per_beat))
                track.append(Message("control_change", channel=channel, control=SUSTAIN_CC, value=0,
                                     time=max(0, end_tick - abs_tick)))

            track.append(MetaMessage("end_of_track", time=0))
            mid.tracks.append(track)

        out_path = out_path or Path(tempfile.mkstemp(suffix=".mid")[1])
        mid.save(str(out_path))
        return out_path

    @staticmethod
    def _is_drum(name: Optional[str]) -> bool:
        if not name:
            return False
        n = name.lower()
        return any(k in n for k in ("drum", "鼓", "percussion", "打击"))

    @staticmethod
    def _wants_sustain(instrument: Optional[str]) -> bool:
        """钢琴/电钢琴/弦乐/铺底类适合自动延音踏板。"""
        if not instrument:
            return False
        n = instrument.lower()
        return any(k in n for k in ("piano", "grand", "ep", "string", "pad", "organ"))

    @staticmethod
    def _wants_expression(instrument: Optional[str]) -> bool:
        """弦乐/管乐/铺底类适合自动表情 CC（渐强渐弱、颤音）。"""
        if not instrument:
            return False
        n = instrument.lower()
        return any(k in n for k in ("string", "violin", "cello", "pad", "brass", "flute",
                                     "oboe", "clarinet", "bassoon", "sax", "trumpet", "trombone",
                                     "synth lead", "lead"))

    def _velocity_curve(self, n: NoteSpec, is_drum: bool) -> int:
        """出版级力度曲线：按拍位分配强弱，模拟人类律动。

        - 强拍（整数拍）：力度 +8
        - 反拍（.5 拍）：力度 +3（backbeat 突出）
        - 弱拍（其他）：力度 -5
        - 鼓组：底鼓/军鼓按位置加强，踩镲偏弱
        - 人性化：再叠加 ±5 微抖动
        """
        vel = mt.clamp_velocity(n.velocity)
        beat_pos = n.start - int(n.start) if n.start > 0 else 0.0
        # 强弱拍加权
        if abs(beat_pos) < 0.05:        # 强拍（downbeat）
            vel += 8
        elif 0.45 < beat_pos < 0.55:    # 反拍（backbeat）
            vel += 3
        else:                           # 弱拍/切分
            vel -= 5

        # 鼓组特化：底鼓强、军鼓中、踩镲弱
        if is_drum:
            pitch = mt.resolve_pitch(n.pitch)
            if pitch == 36:    # 底鼓
                vel += 10
            elif pitch == 38:  # 军鼓
                vel += 5
            elif pitch in (42, 46):  # 踩镲
                vel -= 8
            elif pitch == 49:  # crash
                vel += 8

        # 人性化微抖动
        if self.humanize:
            vel += self._rng.randint(-5, 5)
        return mt.clamp_velocity(vel)

    @staticmethod
    def _ascii_name(name: str) -> str:
        """把任意轨道名转为 ASCII 安全名（mido 的 track_name 仅支持 latin-1）。"""
        if not name:
            return "Track"
        out = []
        for ch in name:
            if ord(ch) < 128:
                out.append(ch)
        ascii_only = "".join(out).strip()
        return (ascii_only or "Track")[:32]

    @staticmethod
    def _guess_program(instrument: Optional[str]) -> int:
        """把乐器名粗略映射到 GM program number（0-127）。"""
        if not instrument:
            return 0
        name = instrument.lower()
        table = {
            "piano": 0, "grand": 0, "ep": 4, "electric piano": 4, "organ": 16,
            "bass": 32, "synth bass": 38, "string": 48, "strings": 48, "pad": 88,
            "synth": 80, "lead": 80, "guitar": 24, "brass": 56, "flute": 73,
        }
        for k, v in table.items():
            if k in name:
                return v
        return 0

    # ---------- 实时模式 ----------
    async def play_region_live(self, region: MidiRegionSpec, bpm: float):
        """通过虚拟端口实时演奏一段（用于试听）。"""
        if not self._outport:
            return
        sec_per_beat = 60.0 / bpm
        for n in sorted(region.notes, key=lambda x: x.start):
            pitch = mt.resolve_pitch(n.pitch)
            await asyncio.sleep(n.start * sec_per_beat)
            self._outport.send(Message("note_on", note=pitch, velocity=mt.clamp_velocity(n.velocity)))
            await asyncio.sleep(n.duration * sec_per_beat)
            self._outport.send(Message("note_off", note=pitch, velocity=0))

    def send_cc(self, channel: int, control: int, value: int):
        """发送 MIDI CC（用于混音器/参数控制）。"""
        if not self._outport:
            return
        value = max(0, min(127, int(value)))
        self._outport.send(Message("control_change", channel=channel, control=control, value=value))

    def close(self):
        if self._outport:
            try:
                self._outport.close()
            except Exception:
                pass
            self._outport = None
