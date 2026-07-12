"""数据模型：定义 AI 决策、轨道、MIDI 片段、混音参数等结构。

这些模型既是 AI 返回 JSON 的校验 schema，也是 commander 执行动作的输入。
设计目标：覆盖人类在 Logic Pro 中会做的全部操作——传输、轨道、片段、MIDI、
混音、自动化、标记、插件参数、发送、母带、保存等，让 AI 能像人类一样全面操作。
"""
from __future__ import annotations

from enum import Enum
from typing import Optional, Union

from pydantic import BaseModel, Field


class Stage(str, Enum):
    COMPOSE = "compose"
    ARRANGE = "arrange"
    MIX = "mix"
    MASTER = "master"


# ---------- 音符 / MIDI ----------
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
    length: Optional[float] = Field(default=None, description="片段长度（拍），留空则按音符推断")


# ---------- 轨道 ----------
class TrackSpec(BaseModel):
    """轨道定义。"""
    name: str
    type: str = Field(default="software", description="software | audio | drummer | external | aux")
    instrument: Optional[str] = None
    color: Optional[int] = Field(default=None, ge=0, le=87, description="轨道颜色 0-87")
    group: Optional[str] = Field(default=None, description="编组名，同名的轨道归为一组")
    icon: Optional[str] = Field(default=None, description="轨道图标名（如 'Piano'）")
    hidden: bool = False
    freeze: bool = Field(default=False, description="冻结轨道以节省 CPU")


class TrackStackSpec(BaseModel):
    """轨道堆栈（文件夹/总和轨道）。"""
    name: str
    members: list[str] = Field(description="成员轨道名列表")
    stack_type: str = Field(default="folder", description="folder | summing")


# ---------- 传输控制 ----------
class TransportAction(BaseModel):
    """传输控制动作（播放/停止/录音/定位/循环）。"""
    op: str = Field(description="play | stop | pause | record | goto | set_cycle | toggle_loop | set_loop | rewind | forward")
    bar: Optional[int] = Field(default=None, ge=1, description="目标小节（goto/set_cycle_start/end 用）")
    beat: Optional[float] = Field(default=None, description="目标拍")
    start_bar: Optional[int] = Field(default=None, ge=1, description="循环起始小节")
    end_bar: Optional[int] = Field(default=None, ge=1, description="循环结束小节")


# ---------- 片段操作 ----------
class RegionOp(BaseModel):
    """对已有片段的操作（像人类在编排区拖拽编辑）。"""
    op: str = Field(description="split | join | move | copy | delete | loop | resize | quantize | transpose | crop")
    track: Optional[str] = Field(default=None, description="目标轨道名")
    # split/join/move/copy 用拍或小节定位
    at_bar: Optional[int] = Field(default=None, ge=1, description="操作位置小节")
    at_beat: Optional[float] = Field(default=None, description="操作位置拍")
    to_bar: Optional[int] = Field(default=None, ge=1, description="move/copy 目标小节")
    to_beat: Optional[float] = Field(default=None, description="move/copy 目标拍")
    # quantize
    grid: Optional[str] = Field(default=None, description="量化网格：1/16 | 1/8 | 1/4 | 1/8T | 1/16T")
    strength: Optional[int] = Field(default=None, ge=0, le=100, description="量化强度 0-100")
    # transpose
    semitones: Optional[int] = Field(default=None, description="移调半音数（正=升，负=降）")
    # loop
    loop_count: Optional[int] = Field(default=None, ge=1, description="循环次数")
    # resize
    new_length_beats: Optional[float] = Field(default=None, description="resize 后长度（拍）")


# ---------- 自动化 ----------
class AutomationPoint(BaseModel):
    """自动化曲线上的一个点。"""
    bar: float = Field(description="位置（小节，可带小数）")
    value: float = Field(description="参数值（具体含义由参数决定）")
    shape: str = Field(default="linear", description="linear | curve | step")


class AutomationSpec(BaseModel):
    """在某轨道写自动化。"""
    track: str
    parameter: str = Field(description="Volume | Pan | Send1 | Plugin:Compressor:Threshold 等参数路径")
    points: list[AutomationPoint] = Field(default_factory=list)
    mode: str = Field(default="write", description="read | touch | latch | write | off")


# ---------- 标记 ----------
class MarkerSpec(BaseModel):
    """编排标记/段落标记。"""
    name: str = Field(description="标记名，如 'Verse 1' / 'Chorus' / 'Outro'")
    bar: int = Field(ge=1, description="起始小节")
    length_bars: Optional[int] = Field(default=None, ge=1, description="长度（小节），留空则到下一标记")
    color: Optional[int] = Field(default=None, ge=0, le=87)


# ---------- 速度 / 拍号 ----------
class TempoChangeSpec(BaseModel):
    """速度变化点（支持渐变，像人类画速度曲线）。"""
    bar: float = Field(description="位置（小节）")
    bpm: float = Field(ge=20, le=300)
    ramp: bool = Field(default=False, description="True=从上一速度渐变到本速度；False=立即跳变")


# ---------- 插件 ----------
class PluginSpec(BaseModel):
    """插件实例。"""
    name: str = Field(description="Logic 自带插件名或 Audio Unit 名，如 'Channel EQ' / 'Compressor' / 'Limiter'")
    preset: Optional[str] = None
    bypass: bool = False
    slot: Optional[int] = Field(default=None, ge=0, description="Audio FX 插槽序号（0=EQAux等），留空追加")


class PluginParamSpec(BaseModel):
    """设置插件某个参数的值（像人类拧旋钮）。"""
    track: str
    plugin: str = Field(description="插件名")
    parameter: str = Field(description="参数名，如 'Threshold' / 'Ratio' / 'Gain'")
    value: float = Field(description="参数值")


# ---------- 发送 / 总线 ----------
class SendSpec(BaseModel):
    """发送到总线/辅助通道。"""
    target: str = Field(description="总线/辅助通道名，如 'Reverb Bus'")
    amount: float = Field(default=0.0, ge=0.0, le=1.0, description="发送量 0..1")


class BusSpec(BaseModel):
    """创建辅助通道/总线。"""
    name: str
    input: Optional[str] = Field(default=None, description="输入总线，如 'Bus 1'")
    plugins: list[PluginSpec] = Field(default_factory=list, description="该总线上挂的插件（如混响/延迟）")


# ---------- 混音 ----------
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
    input_monitoring: Optional[bool] = Field(default=None, description="输入监听")


# ---------- 录音 ----------
class RecordSpec(BaseModel):
    """录音设置（像人类 armed 轨道并录音）。"""
    track: str
    armed: bool = True
    count_in: int = Field(default=0, ge=0, description="预录小节数")
    autopunch: Optional[dict] = Field(default=None, description="自动穿插：{start_bar, end_bar}")


# ---------- 速度 / 拍号基础 ----------
class TempoSpec(BaseModel):
    bpm: float = Field(default=120.0, ge=20, le=300)
    time_signature: Optional[str] = Field(default=None, description="如 '4/4'")
    key: Optional[str] = Field(default=None, description="如 'C minor'")


# ---------- 导出 ----------
class BounceSpec(BaseModel):
    """导出/渲染参数。"""
    format: str = Field(default="wav", description="wav | aiff | mp3 | m4a")
    bit_depth: int = Field(default=24)
    sample_rate: int = Field(default=44100)
    normalize: bool = False
    filename: Optional[str] = None
    # 可选：仅导出某小节范围
    start_bar: Optional[int] = Field(default=None, ge=1)
    end_bar: Optional[int] = Field(default=None, ge=1)
    # 导出轨道分轨（stems）
    stems: bool = Field(default=False, description="True=分轨导出各轨道")


# ---------- 项目计划 ----------
class ProjectPlan(BaseModel):
    """AI 对整首作品的总体规划（compose 阶段输出）。"""
    title: str
    genre: str
    tempo: TempoSpec
    structure: list[str] = Field(description="段落顺序，如 ['intro','verse','chorus','verse','chorus','bridge','outro']")
    key: str
    description: Optional[str] = None


# ---------- 通用动作（保存/撤销/视图） ----------
class UIAction(BaseModel):
    """UI/工程级动作（像人类点菜单）。"""
    op: str = Field(description="save | save_as | open | close | undo | redo | open_piano_roll | open_mixer | open_inspector | zoom_fit | toggle_track | select_all | collapse_all")
    path: Optional[str] = Field(default=None, description="save_as/open 用的文件路径")
    track: Optional[str] = Field(default=None, description="toggle_track/select 用")


# ---------- 阶段结果 ----------
class StageResult(BaseModel):
    """单个阶段的完整动作清单（AI 输出）。

    AI 可输出任意子集的字段；commander 会按人类工作流顺序执行：
    工程 → 速度/标记 → 轨道/堆栈 → 片段 → MIDI编辑 → 传输 → 混音/发送/插件参数 →
    自动化 → 母带 → 录音 → 导出 → 保存。
    """
    stage: Stage
    summary: str = Field(description="对本阶段决策的中文说明")
    project: Optional[ProjectPlan] = None
    # 速度
    tempo_changes: list[TempoChangeSpec] = Field(default_factory=list, description="速度变化点")
    # 标记
    markers: list[MarkerSpec] = Field(default_factory=list, description="编排标记")
    # 轨道
    tracks: list[TrackSpec] = Field(default_factory=list)
    track_stacks: list[TrackStackSpec] = Field(default_factory=list, description="轨道堆栈")
    # 片段
    regions: list[MidiRegionSpec] = Field(default_factory=list)
    region_ops: list[RegionOp] = Field(default_factory=list, description="对已有片段的编辑操作")
    # 传输
    transports: list[TransportAction] = Field(default_factory=list, description="播放/停止/录音/定位/循环")
    # 混音
    mix: list[MixParams] = Field(default_factory=list)
    buses: list[BusSpec] = Field(default_factory=list, description="新建辅助通道/总线")
    plugin_params: list[PluginParamSpec] = Field(default_factory=list, description="插件参数调整")
    # 自动化
    automation: list[AutomationSpec] = Field(default_factory=list)
    # 录音
    record: Optional[RecordSpec] = Field(default=None)
    # 母带
    master_plugins: list[PluginSpec] = Field(default_factory=list)
    # 导出
    bounce: Optional[BounceSpec] = None
    # UI/工程动作
    actions: list[UIAction] = Field(default_factory=list, description="保存/撤销/视图切换等")
    rationale: Optional[str] = Field(default=None, description="创作思路解释")


# ---------- 视觉驱动规划（基于 Logic Pro 截图） ----------
class VisualStep(BaseModel):
    """AI 看完 Logic Pro 截图后输出的「单步操作」。

    视觉循环每次截图后让 AI 输出一个 VisualStep：
    - 若 done=True 表示 AI 判定目标已达成，循环结束；
    - 否则执行 actions 列表里的动作（复用 StageResult 的字段子集），
      然后回到「截图 → 规划」进入下一轮，直到 done 或超步数。
    AI 可按需输出任意动作字段子集，与 StageResult 一致。
    """
    observation: str = Field(description="AI 对当前截图的观察（中文）：看到了什么、状态如何")
    plan: str = Field(description="这一步打算做什么（中文，简短）")
    done: bool = Field(default=False, description="是否已达成最终目标（True 则结束视觉循环）")
    # 复用 StageResult 的动作字段（按需输出子集）
    transports: list[TransportAction] = Field(default_factory=list)
    tracks: list[TrackSpec] = Field(default_factory=list)
    track_stacks: list[TrackStackSpec] = Field(default_factory=list)
    region_ops: list[RegionOp] = Field(default_factory=list)
    mix: list[MixParams] = Field(default_factory=list)
    buses: list[BusSpec] = Field(default_factory=list)
    plugin_params: list[PluginParamSpec] = Field(default_factory=list)
    automation: list[AutomationSpec] = Field(default_factory=list)
    markers: list[MarkerSpec] = Field(default_factory=list)
    tempo_changes: list[TempoChangeSpec] = Field(default_factory=list)
    master_plugins: list[PluginSpec] = Field(default_factory=list)
    record: Optional[RecordSpec] = Field(default=None)
    bounce: Optional[BounceSpec] = Field(default=None)
    actions: list[UIAction] = Field(default_factory=list)
    rationale: Optional[str] = Field(default=None, description="为什么这么做（中文）")
