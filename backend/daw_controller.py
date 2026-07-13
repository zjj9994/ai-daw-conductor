"""DAW 控制器：把高层动作翻译为对 Logic Pro 的 MIDI + AppleScript 操作。

所有动作通过 event_cb 回调上报事件，供 WebSocket 实时推送给前端。
非 macOS 环境下 AppleScript 部分降级为「模拟」（仅记录事件），仍会生成 MIDI 文件。

覆盖人类在 Logic Pro 中的全部操作：
工程、传输、轨道、片段、MIDI、混音、自动化、标记、插件、发送、母带、录音、导出、视图、撤销。
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
    AutomationSpec, BounceSpec, BusSpec, MarkerSpec, MixParams, MidiRegionSpec,
    PluginParamSpec, PluginSpec, RecordSpec, RegionOp, TempoChangeSpec,
    TempoSpec, TrackSpec, TrackStackSpec, TransportAction, UIAction,
)

log = logging.getLogger("daw_controller")

EventCB = Callable[[dict], Awaitable[None]]


class DAWController:
    def __init__(self, cfg: dict, event_cb: Optional[EventCB] = None, tracker=None):
        self.cfg = cfg.get("daw", {})
        self.event_cb = event_cb
        self.tracker = tracker
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
        # 工程 lifecycle：一首音乐的所有操作都指向同一个工程文件
        self.project_dir = Path(self.cfg.get(
            "project_dir", "~/Music/AI-DAW-Conductor/projects"
        )).expanduser()
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.current_project_path: Optional[str] = None  # .logicx 文件绝对路径
        self.current_project_title: str = ""

    async def emit(self, **payload: Any):
        if self.event_cb:
            try:
                await self.event_cb({"type": "daw_event", **payload})
            except Exception as e:
                log.debug("event_cb 失败: %s", e)

    async def log(self, level: str, message: str):
        await self.emit(kind="log", level=level, message=message)

    @property
    def _real(self) -> bool:
        """是否真正驱动 Logic Pro（而非模拟）。"""
        return self.use_applescript and self.applescript.available

    # ============== 工程 ==============
    async def create_project(self, tempo: TempoSpec, title: str = "AI Project"):
        """新建 Logic Pro 工程，并立即另存为到 project_dir 下的 .logicx 文件。

        一首音乐的所有操作（作曲/编曲/混音/母带/导出）都指向这同一个工程文件。
        工程路径记录在 self.current_project_path，供阶段间引用与校验。
        """
        # 文件名安全化：去掉路径分隔符与非法字符
        safe_title = "".join(c for c in title if c not in '/\\:*?"<>|') or "AI Project"
        project_path = str(self.project_dir / f"{safe_title}.logicx")
        self.current_project_path = project_path
        self.current_project_title = title

        await self.log("info", f"新建 Logic Pro 工程：{title}（{tempo.bpm}BPM）→ {project_path}")
        if self._real:
            self.applescript.new_project(name=title, bpm=tempo.bpm, key=tempo.key or "C")
            if tempo.time_signature:
                num, den = (tempo.time_signature.split("/") + [4, 4])[:2]
                self.applescript.set_time_signature(int(num), int(den))
            # 立即另存为到磁盘，确保工程有确定路径，后续所有阶段都指向它
            self.applescript.save_as(project_path)
            await self.log("info", f"工程已保存：{project_path}")
        else:
            await self.log("warn", "非 macOS 或未启用 AppleScript，跳过实际建项目（模拟模式）。")
        await self.emit(kind="project_created", title=title, bpm=tempo.bpm,
                        key=tempo.key, time_signature=tempo.time_signature,
                        project_path=project_path)

    async def save_project(self):
        await self.log("info", "保存工程")
        if self._real:
            self.applescript.save_project()
        await self.emit(kind="project_saved")

    async def save_as(self, path: str):
        await self.log("info", f"另存为：{path}")
        if self._real:
            self.applescript.save_as(path)
        await self.emit(kind="project_saved_as", path=path)

    async def undo(self):
        await self.log("info", "撤销")
        if self._real:
            self.applescript.undo()
        await self.emit(kind="undo")

    async def redo(self):
        await self.log("info", "重做")
        if self._real:
            self.applescript.redo()
        await self.emit(kind="redo")

    # ============== 传输 ==============
    async def transport(self, action: TransportAction):
        """执行传输控制动作（播放/停止/录音/定位/循环）。"""
        op = action.op
        labels = {
            "play": "播放", "stop": "停止", "pause": "暂停", "record": "录音",
            "goto": f"定位到 {action.bar or 1} 小节", "set_cycle": f"循环区 {action.start_bar}-{action.end_bar}",
            "toggle_loop": "切换循环", "set_loop": "设置循环", "rewind": "倒退", "forward": "前进",
        }
        await self.log("info", f"传输：{labels.get(op, op)}")
        if self._real:
            if op == "play":
                self.applescript.play()
            elif op == "stop":
                self.applescript.stop()
            elif op == "pause":
                self.applescript.pause()
            elif op == "record":
                self.applescript.record()
            elif op == "goto":
                self.applescript.goto_bar(action.bar or 1, action.beat or 1)
            elif op == "set_cycle" or op == "set_loop":
                if action.start_bar and action.end_bar:
                    self.applescript.set_cycle(action.start_bar, action.end_bar)
            elif op == "toggle_loop":
                self.applescript.toggle_loop()
            elif op == "rewind":
                self.applescript.rewind()
            elif op == "forward":
                self.applescript.forward()
        await self.emit(kind="transport", op=op, bar=action.bar,
                        start_bar=action.start_bar, end_bar=action.end_bar)

    # ============== 轨道 ==============
    async def ensure_track(self, track: TrackSpec):
        if track.name in self._track_index:
            return
        self._track_index[track.name] = track
        await self.log("info", f"创建轨道：{track.name}（{track.type} / {track.instrument or '-'}）")
        if self._real:
            if track.type == "audio":
                self.applescript.create_audio_track(track.name)
            elif track.type == "drummer":
                self.applescript.create_drummer_track(track.name)
            elif track.type == "aux":
                self.applescript.create_aux_track(track.name)
            else:
                self.applescript.create_software_track(track.name)
            if track.color is not None:
                self.applescript.set_track_color(track.name, track.color)
            if track.icon:
                self.applescript.set_track_icon(track.name, track.icon)
            if track.freeze:
                self.applescript.freeze_track(track.name)
            if track.hidden:
                self.applescript.toggle_track_hide(track.name)
        await self.emit(kind="track_added", track=track.model_dump())

    async def delete_track(self, name: str):
        await self.log("info", f"删除轨道：{name}")
        self._track_index.pop(name, None)
        if self._real:
            self.applescript.select_track_by_name(name)
            self.applescript.delete_selected_track()
        await self.emit(kind="track_deleted", track=name)

    async def duplicate_track(self, name: str):
        await self.log("info", f"复制轨道：{name}")
        if self._real:
            self.applescript.select_track_by_name(name)
            self.applescript.duplicate_selected_track()
        await self.emit(kind="track_duplicated", track=name)

    async def create_track_stack(self, stack: TrackStackSpec):
        await self.log("info", f"创建轨道堆栈「{stack.name}」：{stack.members}")
        if self._real:
            self.applescript.create_track_stack(stack.members, stack.name, stack.stack_type)
        await self.emit(kind="track_stack_created", name=stack.name, members=stack.members)

    # ============== MIDI 区域 ==============
    async def add_region(self, region: MidiRegionSpec, bpm: float):
        await self.log("info", f"写入 MIDI 片段到轨道「{region.track}」，{len(region.notes)} 个音符")
        mid_path = self.midi.build_midi_file([region], TempoSpec(bpm=bpm))
        await self.emit(kind="midi_generated", track=region.track,
                        path=str(mid_path), note_count=len(region.notes))
        if self._real:
            await self.applescript.import_midi_file(mid_path, region.track)
        else:
            await self.log("warn", f"模拟模式：MIDI 已生成于 {mid_path}（未自动导入 Logic Pro）")
        await self.emit(kind="region_added", track=region.track,
                        start=region.start, note_count=len(region.notes))

    async def region_op(self, op: RegionOp):
        """执行片段编辑操作（切割/合并/移动/复制/删除/循环/量化/移调）。"""
        op_labels = {
            "split": "切割", "join": "合并", "move": "移动", "copy": "复制",
            "delete": "删除", "loop": "循环", "resize": "调整长度",
            "quantize": "量化", "transpose": "移调", "crop": "裁剪",
        }
        detail = f"「{op.track}」{op_labels.get(op.op, op.op)}"
        if op.op == "quantize" and op.grid:
            detail += f" 网格={op.grid}"
        if op.op == "transpose" and op.semitones is not None:
            detail += f" {op.semitones}半音"
        if op.op in ("move", "copy") and op.to_bar:
            detail += f" → {op.to_bar}小节"
        await self.log("info", f"片段操作：{detail}")
        if self._real and op.track:
            if op.op == "split":
                self.applescript.split_region_at_playhead(op.track, op.at_bar or 1, op.at_beat or 1)
            elif op.op == "join":
                self.applescript.join_regions(op.track)
            elif op.op == "move":
                self.applescript.move_region(op.track, op.at_bar or 1, op.to_bar or 1)
            elif op.op == "copy":
                self.applescript.copy_region(op.track, op.at_bar or 1, op.to_bar or 1)
            elif op.op == "delete":
                self.applescript.delete_regions(op.track, op.at_bar or 1)
            elif op.op == "loop":
                self.applescript.loop_region(op.track, op.at_bar or 1, op.loop_count or 2)
            elif op.op == "resize":
                self.applescript.resize_region(op.track, op.at_bar or 1, op.new_length_beats or 4)
            elif op.op == "quantize":
                self.applescript.select_track_by_name(op.track)
                self.applescript.quantize_selected_regions(op.grid or "1/16", op.strength or 100)
            elif op.op == "transpose":
                self.applescript.select_track_by_name(op.track)
                self.applescript.transpose_selected(op.semitones or 0)
        await self.emit(kind="region_op", op=op.op, track=op.track,
                        at_bar=op.at_bar, to_bar=op.to_bar)

    # ============== 混音 ==============
    async def apply_mix(self, params: MixParams):
        await self.log("info", f"混音：{params.track}（vol={params.volume_db} pan={params.pan}）")
        if self._real:
            if params.volume_db is not None:
                self.applescript.set_volume(params.track, params.volume_db)
            if params.pan is not None:
                self.applescript.set_pan(params.track, params.pan)
            if params.mute is not None:
                self.applescript.set_mute(params.track, params.mute)
            if params.solo is not None:
                self.applescript.set_solo(params.track, params.solo)
            if params.input_monitoring is not None:
                self.applescript.set_input_monitoring(params.track, params.input_monitoring)
            if params.plugins:
                self.applescript.select_track_by_name(params.track)
                for p in params.plugins:
                    if p.bypass:
                        self.applescript.bypass_plugin(params.track, p.name, True)
                    else:
                        self.applescript.add_plugin_to_selected_track(p.name, p.preset)
            if params.sends:
                for s in params.sends:
                    self.applescript.add_send_to_bus(params.track, s.target, s.amount)
        else:
            await self.log("warn", "模拟模式：跳过混音器实际操作。")
        await self.emit(kind="mix_applied", track=params.track,
                        params=params.model_dump(exclude_none=True))

    async def create_bus(self, bus: BusSpec):
        """创建辅助通道/总线并挂插件。"""
        await self.log("info", f"创建总线「{bus.name}」（输入={bus.input or '-'}，{len(bus.plugins)} 个插件）")
        if self._real:
            self.applescript.create_aux_channel(bus.name, bus.input)
            for p in bus.plugins:
                self.applescript.select_track_by_name(bus.name)
                self.applescript.add_plugin_to_selected_track(p.name, p.preset)
        await self.emit(kind="bus_created", name=bus.name, input=bus.input,
                        plugins=[p.name for p in bus.plugins])

    async def set_plugin_param(self, p: PluginParamSpec):
        """设置插件参数。"""
        await self.log("info", f"插件参数：{p.track}/{p.plugin}/{p.parameter} = {p.value}")
        if self._real:
            self.applescript.set_plugin_parameter(p.track, p.plugin, p.parameter, p.value)
        await self.emit(kind="plugin_param", track=p.track, plugin=p.plugin,
                        parameter=p.parameter, value=p.value)

    # ============== 自动化 ==============
    async def apply_automation(self, auto: AutomationSpec):
        """写自动化曲线。"""
        await self.log("info", f"自动化：{auto.track}/{auto.parameter}（{len(auto.points)} 个点，模式={auto.mode}）")
        if self._real:
            self.applescript.set_automation_mode(auto.track, auto.mode)
            self.applescript.show_automation_for_track(auto.track, auto.parameter)
        await self.emit(kind="automation", track=auto.track, parameter=auto.parameter,
                        mode=auto.mode, point_count=len(auto.points))

    # ============== 标记 ==============
    async def add_marker(self, marker: MarkerSpec):
        """添加编排标记。"""
        await self.log("info", f"标记：{marker.name} @ 小节 {marker.bar}")
        if self._real:
            self.applescript.add_arrangement_marker(marker.name, marker.bar)
        await self.emit(kind="marker_added", name=marker.name, bar=marker.bar)

    # ============== 速度变化 ==============
    async def add_tempo_change(self, tc: TempoChangeSpec):
        """添加速度变化点。"""
        ramp_desc = "（渐变）" if tc.ramp else "（跳变）"
        await self.log("info", f"速度变化：{tc.bpm}BPM @ 小节 {tc.bar}{ramp_desc}")
        if self._real:
            self.applescript.add_tempo_change(tc.bar, tc.bpm, tc.ramp)
        await self.emit(kind="tempo_change", bar=tc.bar, bpm=tc.bpm, ramp=tc.ramp)

    # ============== 母带 ==============
    async def apply_master(self, plugins: list[PluginSpec]):
        await self.log("info", f"母带链：{[p.name for p in plugins]}")
        if self._real:
            self.applescript.select_master_track()
            for p in plugins:
                if p.bypass:
                    self.applescript.bypass_plugin("Stereo Out", p.name, True)
                else:
                    self.applescript.add_plugin_to_master(p.name, p.preset)
        await self.emit(kind="master_applied", plugins=[p.model_dump() for p in plugins])

    # ============== 录音 ==============
    async def setup_record(self, rec: RecordSpec):
        """设置录音参数并 armed 轨道。"""
        await self.log("info", f"录音准备：{rec.track}（armed={rec.armed}，count_in={rec.count_in}）")
        if self._real:
            if rec.count_in:
                self.applescript.set_count_in(rec.count_in)
            if rec.autopunch:
                self.applescript.set_autopunch(rec.autopunch.get("start_bar", 1),
                                                rec.autopunch.get("end_bar", 2))
            if rec.armed:
                self.applescript.arm_track(rec.track, True)
        await self.emit(kind="record_setup", track=rec.track, armed=rec.armed,
                        count_in=rec.count_in)

    # ============== 导出 ==============
    async def bounce(self, bounce: BounceSpec) -> Path:
        await self.log("info", f"开始导出（Bounce）：{bounce.filename}.{bounce.format}"
                               + ("（分轨）" if bounce.stems else ""))
        out = None
        if self._real:
            if bounce.stems:
                # 分轨导出
                outs = await self.applescript.bounce_stems(
                    filename_prefix=bounce.filename or "stem",
                    fmt=bounce.format, bit_depth=bounce.bit_depth,
                    sample_rate=bounce.sample_rate,
                )
                out = outs[0] if outs else (self.applescript.render_dir / f"{bounce.filename or 'master'}.{bounce.format}")
            else:
                out = await self.applescript.bounce_master(
                    filename=bounce.filename or "master",
                    fmt=bounce.format, bit_depth=bounce.bit_depth,
                    sample_rate=bounce.sample_rate,
                    start_bar=bounce.start_bar, end_bar=bounce.end_bar,
                    normalize=bounce.normalize,
                )
        else:
            out = self.applescript.render_dir / f"{bounce.filename or 'master'}.{bounce.format}"
            out.write_bytes(b"")  # 模拟占位
            await self.log("warn", f"模拟模式：已生成占位导出文件 {out}")
        # 记录到渲染历史
        size = out.stat().st_size if out.exists() else 0
        if self.tracker:
            self.tracker.add_render(
                path=str(out),
                filename=bounce.filename or "master",
                stage="master", size=size,
            )
        await self.emit(kind="bounce_done", path=str(out), filename=bounce.filename or "master",
                        stems=bounce.stems)
        return out

    # ============== UI 动作 ==============
    async def ui_action(self, action: UIAction):
        """执行 UI/工程级动作（保存/撤销/视图切换等）。

        安全策略：禁止 open/close 动作——一首音乐的所有操作必须指向同一个工程，
        AI 不得在制作过程中打开或关闭工程，否则后续阶段会操作错工程。
        save/save_as 由系统在 create_project 时统一管理，AI 输出 save 会被接受
        （仅发 Cmd-S 保存到已确定的路径），但 save_as 的 path 会被忽略以避免切换工程。
        """
        labels = {
            "save": "保存", "save_as": "另存为", "open": "打开", "close": "关闭",
            "undo": "撤销", "redo": "重做", "open_piano_roll": "打开钢琴卷帘",
            "open_mixer": "打开混音器", "open_inspector": "打开检查器",
            "zoom_fit": "缩放适配", "toggle_track": "切换轨道", "select_all": "全选",
            "collapse_all": "折叠所有堆栈",
        }
        # 守卫：禁止切换/关闭工程，保证所有操作指向同一个工程
        if action.op in ("open", "close"):
            await self.log(
                "warn",
                f"已忽略 UI 动作「{action.op}」：制作过程中不得打开/关闭工程，"
                "一首音乐的所有操作必须指向同一个 Logic Pro 工程。",
            )
            await self.emit(kind="ui_action", op=action.op, ignored=True)
            return
        if action.op == "save_as":
            # 强制使用系统管理的工程路径，忽略 AI 给的 path
            if self.current_project_path:
                await self.log("info", f"保存工程（使用系统路径）：{self.current_project_path}")
                if self._real:
                    self.applescript.save_project()
            else:
                await self.log("warn", "save_as 被忽略：当前无活动工程。")
            await self.emit(kind="ui_action", op="save_as", ignored=bool(not self.current_project_path))
            return
        await self.log("info", f"UI：{labels.get(action.op, action.op)}")
        if self._real:
            if action.op == "save":
                self.applescript.save_project()
            elif action.op == "undo":
                self.applescript.undo()
            elif action.op == "redo":
                self.applescript.redo()
            elif action.op == "open_piano_roll":
                self.applescript.open_piano_roll()
            elif action.op == "open_mixer":
                self.applescript.open_mixer()
            elif action.op == "open_inspector":
                self.applescript.open_inspector()
            elif action.op == "zoom_fit":
                self.applescript.zoom_fit()
            elif action.op == "select_all":
                self.applescript.select_all_regions()
            elif action.op == "collapse_all":
                self.applescript.collapse_all_track_stacks()
            elif action.op == "toggle_track" and action.track:
                self.applescript.toggle_track_hide(action.track)
        await self.emit(kind="ui_action", op=action.op)

    def close(self):
        self.midi.close()
