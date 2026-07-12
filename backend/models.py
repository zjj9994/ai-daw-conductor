"""数据模型：定义 AI 决策、轨道、MIDI 片段、混音参数等结构。

这些模型既是 AI 返回 JSON 的校验 schema，也是 commander 执行动作的输入。
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Stage(str, Enum):
    COMPOSE = "compose"
    ARRANGE = "arrange"
    MIX = "mix"
    MASTER = "master"


class NoteSpec(BaseModel):
    """单个音符。pitch 用 MIDI 数字（0-127）或音名（如 'C4'）。"""
    pitch: str | int
    start: float = Field(description="起始拍（beat），从 0 开始")
    duration: float = Field(default=1.0, description="持续拍数")
    velocity: int = Field(default=90, ge=1, le=127)


class MidiRegionSpec(BaseModel):
    """一段 MIDI 片段，放在某轨道上。"""
    track: str = Field(description="目标轨道名（需已存在或会被创建）")
    start: float = Field(default=0.0, description="片段在轨道上的起始拍")
    notes: list[NoteSpec] = Field(default_factory=list)
    # 程序/音色提示（GM 标准或乐器名），用于创建软件乐器轨道
    instrument: Optional[str] = Field(default=None, description="如 'Acoustic Grand Piano' / 'Synth Bass' / 'Drums'")


class TrackSpec(BaseModel):
    """轨道定义。"""
    name: str
    type: str = Field(default="software", description="software | audio | drummer | external")
    instrument: Optional[str] = None
    color: Optional[int] = Field(default=None, ge=0, le=87)


class PluginSpec(BaseModel):
    """插件实例。"""
    name: str = Field(description="Logic 自带插件名或 Audio Unit 名，如 'Channel EQ' / 'Compressor' / 'Limiter'")
    preset: Optional[str] = None
    bypass: bool = False


class SendSpec(BaseModel):
    target: str = Field(description="总线/辅助通道名，如 'Reverb Bus'")
    amount: float = Field(default=0.0, ge=0.0, le=1.0, description="发送量 0..1")


class MixParams(BaseModel):
    """混音参数。"""
    track: str
    volume_db: Optional[float] = Field(default=None, description="音量 dB，-60..+6")
    pan: Optional[float] = Field(default=None, ge=-1.0, le=1.0, description="声相 -1(左)..+1(右)")
    mute: Optional[bool] = None
    solo: Optional[bool] = None
    eq: Optional[dict] = Field(default=None, description="EQ 频段: {freq, gain, q} 列表")
    plugins: Optional[list[PluginSpec]] = None
    sends: Optional[list[SendSpec]] = None


class TempoSpec(BaseModel):
    bpm: float = Field(default=120.0, ge=20, le=300)
    time_signature: Optional[str] = Field(default=None, description="如 '4/4'")
    key: Optional[str] = Field(default=None, description="如 'C minor'")


class BounceSpec(BaseModel):
    """导出/渲染参数（母带阶段）。"""
    format: str = Field(default="wav", description="wav | aiff | mp3")
    bit_depth: int = Field(default=24)
    sample_rate: int = Field(default=44100)
    normalize: bool = False
    filename: Optional[str] = None


class ProjectPlan(BaseModel):
    """AI 对整首作品的总体规划（compose 阶段输出）。"""
    title: str
    genre: str
    tempo: TempoSpec
    structure: list[str] = Field(description="段落顺序，如 ['intro','verse','chorus','verse','chorus','bridge','outro']")
    key: str
    description: Optional[str] = None


class StageResult(BaseModel):
    """单个阶段的完整动作清单（AI 输出）。"""
    stage: Stage
    summary: str = Field(description="对本阶段决策的中文说明")
    project: Optional[ProjectPlan] = None
    tracks: list[TrackSpec] = Field(default_factory=list)
    regions: list[MidiRegionSpec] = Field(default_factory=list)
    mix: list[MixParams] = Field(default_factory=list)
    master_plugins: list[PluginSpec] = Field(default_factory=list)
    bounce: Optional[BounceSpec] = None
    rationale: Optional[str] = Field(default=None, description="创作思路解释")
