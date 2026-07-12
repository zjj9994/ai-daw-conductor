"""MIDI 引擎：把音符/片段转换为 MIDI 数据并送往 Logic Pro。

两种工作模式：
  1. 文件模式（推荐）：生成标准 MIDI 文件，由 AppleScript 导入 Logic Pro。
     - 跨平台、可靠、可在轨道上编辑。
  2. 实时模式：通过虚拟 MIDI 端口实时发送 Note 开关 / CC，用于现场触发与混音器控制。
     - 需要本机安装 python-rtmidi，且 Logic Pro 开启该端口输入。
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Optional

import mido
from mido import Message, MidiFile, MidiTrack, MetaMessage, bpm2tempo, second2tick

from . import music_theory as mt
from .models import MidiRegionSpec, NoteSpec, TempoSpec


class MidiEngine:
    def __init__(self, midi_port: Optional[str] = None, ticks_per_beat: int = 480):
        self.ticks_per_beat = ticks_per_beat
        self.port_name = midi_port
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
            program = self._guess_program(region.instrument)
            track.append(Message("program_change", program=program, time=0))

            notes_sorted = sorted(region.notes, key=lambda n: n.start)
            abs_tick = 0
            # 记录每个音符的结束事件，按时间排序后输出
            events: list[tuple[int, str, NoteSpec]] = []
            for n in notes_sorted:
                start_tick = int(round(n.start * self.ticks_per_beat))
                end_tick = int(round((n.start + n.duration) * self.ticks_per_beat))
                events.append((start_tick, "on", n))
                events.append((end_tick, "off", n))
            events.sort(key=lambda e: (e[0], 0 if e[1] == "off" else 1))

            for tick, kind, n in events:
                delta = max(0, tick - abs_tick)
                abs_tick = tick
                pitch = mt.resolve_pitch(n.pitch)
                vel = mt.clamp_velocity(n.velocity)
                if kind == "on":
                    track.append(Message("note_on", note=pitch, velocity=vel, time=delta))
                else:
                    track.append(Message("note_off", note=pitch, velocity=0, time=delta))
            track.append(MetaMessage("end_of_track", time=0))
            mid.tracks.append(track)

        out_path = out_path or Path(tempfile.mkstemp(suffix=".mid")[1])
        mid.save(str(out_path))
        return out_path

    @staticmethod
    def _ascii_name(name: str) -> str:
        """把任意轨道名转为 ASCII 安全名（mido 的 track_name 仅支持 latin-1）。"""
        if not name:
            return "Track"
        out = []
        for ch in name:
            if ord(ch) < 128:
                out.append(ch)
            # 保留常见中文轨道的英文提示
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
