"""DAW 控制器：把高层动作翻译为对 Logic Pro 的 MIDI + AppleScript 操作。

所有动作通过 event_cb 回调上报事件，供 WebSocket 实时推送给前端。
非 macOS 环境下 AppleScript 部分降级为「模拟」（仅记录事件），仍会生成 MIDI 文件。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .applescript_bridge import AppleScriptBridge
from .config_loader import is_macos
from .midi_engine import MidiEngine
from .models import (
    BounceSpec, MixParams, MidiRegionSpec, PluginSpec, TempoSpec, TrackSpec,
)

log = logging.getLogger("daw_controller")

EventCB = Callable[[dict], Awaitable[None]]


class DAWController:
    def __init__(self, cfg: dict, event_cb: Optional[EventCB] = None):
        self.cfg = cfg.get("daw", {})
        self.event_cb = event_cb
        self.midi = MidiEngine(
            midi_port=self.cfg.get("midi_port"),
            humanize=bool(self.cfg.get("humanize", False)),
        )
        self.applescript = AppleScriptBridge(
            app_name=self.cfg.get("app_name", "Logic Pro"),
            render_dir=self.cfg.get("render_dir", "~/Music/AI-DAW-Conductor/renders"),
        )
        self.use_applescript = bool(self.cfg.get("use_applescript", True))
        self._track_index: dict[str, TrackSpec] = {}

    async def emit(self, **payload: Any):
        if self.event_cb:
            try:
                await self.event_cb({"type": "daw_event", **payload})
            except Exception as e:
                log.debug("event_cb 失败: %s", e)

    async def log(self, level: str, message: str):
        await self.emit(kind="log", level=level, message=message)

    # ---------- 项目 ----------
    async def create_project(self, tempo: TempoSpec, title: str = "AI Project"):
        await self.log("info", f"新建 Logic Pro 项目：{title}（{tempo.bpm}BPM）")
        if self.use_applescript and self.applescript.available:
            self.applescript.new_project(name=title, bpm=tempo.bpm, key=tempo.key or "C")
        else:
            await self.log("warn", "非 macOS 或未启用 AppleScript，跳过实际建项目（模拟模式）。")
        await self.emit(kind="project_created", title=title, bpm=tempo.bpm,
                        key=tempo.key, time_signature=tempo.time_signature)

    # ---------- 轨道 ----------
    async def ensure_track(self, track: TrackSpec):
        if track.name in self._track_index:
            return
        self._track_index[track.name] = track
        await self.log("info", f"创建轨道：{track.name}（{track.type} / {track.instrument or '-'}）")
        if self.use_applescript and self.applescript.available:
            if track.type == "audio":
                self.applescript.create_audio_track(track.name)
            else:
                self.applescript.create_software_track(track.name)
        await self.emit(kind="track_added", track=track.model_dump())

    # ---------- MIDI 区域 ----------
    async def add_region(self, region: MidiRegionSpec, bpm: float):
        await self.log("info", f"写入 MIDI 片段到轨道「{region.track}」，{len(region.notes)} 个音符")
        # 生成 MIDI 文件
        mid_path = self.midi.build_midi_file([region], TempoSpec(bpm=bpm))
        await self.emit(kind="midi_generated", track=region.track,
                        path=str(mid_path), note_count=len(region.notes))
        if self.use_applescript and self.applescript.available:
            await self.applescript.import_midi_file(mid_path, region.track)
        else:
            await self.log("warn", f"模拟模式：MIDI 已生成于 {mid_path}（未自动导入 Logic Pro）")
        await self.emit(kind="region_added", track=region.track,
                        start=region.start, note_count=len(region.notes))

    # ---------- 混音 ----------
    async def apply_mix(self, params: MixParams):
        await self.log("info", f"混音：{params.track}（vol={params.volume_db} pan={params.pan}）")
        if self.use_applescript and self.applescript.available:
            if params.volume_db is not None:
                self.applescript.set_volume(params.track, params.volume_db)
            if params.pan is not None:
                self.applescript.set_pan(params.track, params.pan)
            if params.mute is not None:
                self.applescript.set_mute(params.track, params.mute)
            if params.solo is not None:
                self.applescript.set_solo(params.track, params.solo)
            if params.plugins:
                self.applescript.select_track_by_name(params.track)
                for p in params.plugins:
                    self.applescript.add_plugin_to_selected_track(p.name, p.preset)
        else:
            await self.log("warn", "模拟模式：跳过混音器实际操作。")
        await self.emit(kind="mix_applied", track=params.track,
                        params=params.model_dump(exclude_none=True))

    # ---------- 母带 ----------
    async def apply_master(self, plugins: list[PluginSpec]):
        await self.log("info", f"母带链：{[p.name for p in plugins]}")
        if self.use_applescript and self.applescript.available:
            # 选中主输出轨道（Stereo Out）
            self.applescript._run(
                f'tell application "{self.applescript.app_name}"\n'
                f'  set selected of (the first track whose name is "Stereo Out") to true\n'
                f'end tell'
            )
            for p in plugins:
                self.applescript.add_plugin_to_selected_track(p.name, p.preset)
        await self.emit(kind="master_applied", plugins=[p.model_dump() for p in plugins])

    async def bounce(self, bounce: BounceSpec) -> Path:
        await self.log("info", f"开始导出（Bounce）：{bounce.filename}.{bounce.format}")
        out = None
        if self.use_applescript and self.applescript.available:
            out = await self.applescript.bounce_master(
                filename=bounce.filename or "master",
                fmt=bounce.format, bit_depth=bounce.bit_depth,
                sample_rate=bounce.sample_rate,
            )
        else:
            out = self.applescript.render_dir / f"{bounce.filename or 'master'}.{bounce.format}"
            out.write_bytes(b"")  # 模拟占位
            await self.log("warn", f"模拟模式：已生成占位导出文件 {out}")
        await self.emit(kind="bounce_done", path=str(out))
        return out

    def close(self):
        self.midi.close()
