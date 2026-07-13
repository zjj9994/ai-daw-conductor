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
        # 工程锁：一旦 create_project/open_existing_project 把工程设为锚点，
        # 后续 create_project/new_project 全部被拒绝，所有修改操作都必须在锁定的工程内进行。
        # 这是「先打开一个工程，之后所有操作都在这个工程里」的硬性保证。
        self.project_locked: bool = False

    async def emit(self, **payload: Any):
        if self.event_cb:
            try:
                await self.event_cb({"type": "daw_event", **payload})
            except Exception as e:
                log.debug("event_cb 失败: %s", e)

    async def log(self, level: str, message: str):
        await self.emit(kind="log", level=level, message=message)

    async def _call_applescript(self, method_name: str, *args, **kwargs):
        """把同步的 applescript 方法调用移到线程池执行，避免阻塞 asyncio 事件循环。

        applescript_bridge 的所有方法都用 subprocess.run（同步阻塞），如果直接在
        async def 里调用，会让整个事件循环停转——WebSocket 收不到消息、截图任务
        无法调度、cancel 信号传不进来。这里用 asyncio.to_thread 把同步调用移到
        线程池，让事件循环保持响应。

        错误透传：applescript_bridge._run 把所有失败静默成空字符串，调用方无法
        区分成功/失败。这里捕获异常并 emit daw_error 事件，让 AI/前端能看到失败
        并自我纠正。注意：_run 返回空字符串不一定是错误（命令成功但无输出），
        所以只在抛异常时 emit error，空字符串仍返回。
        """
        if not self._real:
            return None
        method = getattr(self.applescript, method_name)
        try:
            return await asyncio.to_thread(method, *args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self.log("error", f"AppleScript 调用失败 {method_name}({args}, {kwargs}): {e}")
            await self.emit(kind="daw_error", method=method_name, error=str(e))
            return None

    @property
    def _real(self) -> bool:
        """是否真正驱动 Logic Pro（而非模拟）。"""
        return self.use_applescript and self.applescript.available

    @property
    def has_project(self) -> bool:
        """是否已锁定一个活动工程（所有修改操作的前提）。"""
        return self.project_locked and bool(self.current_project_path)

    def _require_project(self, op_desc: str) -> bool:
        """修改操作前的守卫：必须已锁定一个工程才能执行。

        Returns True 表示可以继续；False 表示无活动工程，调用方应跳过并告警。
        """
        return self.has_project

    def _lock_project(self, path: str, title: str):
        """记录工程锚点并加锁。已锁定时拒绝（防意外创建其他工程）。"""
        if self.project_locked:
            # 已有锁定工程，不允许切换——这是「防止意外创建其他工程」的硬保证
            return False
        self.current_project_path = path
        self.current_project_title = title
        self.project_locked = True
        return True

    def unlock_project(self):
        """显式解锁（仅用于重置/取消任务后清空状态，正常制作流程不调用）。"""
        self.project_locked = False
        self.current_project_path = None
        self.current_project_title = ""
        self._track_index.clear()

    # ============== 工程 ==============
    async def create_project(self, tempo: TempoSpec, title: str = "AI Project"):
        """新建 Logic Pro 工程，并立即另存为到 project_dir 下的 .logicx 文件。

        严格规则：一首音乐的所有操作指向同一个工程文件。
        - 若已有锁定工程（project_locked=True），拒绝新建并告警，防止意外创建其他工程。
        - 新建成功后立即加锁，后续所有修改操作都必须在这个工程内进行。
        工程路径记录在 self.current_project_path，供阶段间引用与校验。
        """
        # 文件名安全化：去掉路径分隔符与非法字符
        safe_title = "".join(c for c in title if c not in '/\\:*?"<>|') or "AI Project"
        project_path = str(self.project_dir / f"{safe_title}.logicx")
        # 锁校验：已有活动工程时，禁止再建——防止意外创建其他工程
        if not self._lock_project(project_path, title):
            await self.log(
                "warn",
                f"已拒绝新建工程「{title}」：当前已锁定工程「{self.current_project_title}」"
                f"（{self.current_project_path}）。一首音乐的所有操作必须指向同一个工程，"
                "禁止创建其他工程。如需新建，请先取消当前任务并重置工程。",
            )
            return False

        await self.log("info", f"新建 Logic Pro 工程：{title}（{tempo.bpm}BPM）→ {project_path}（已锁定）")
        if self._real:
            await self._call_applescript("new_project", name=title, bpm=tempo.bpm, key=tempo.key or "C")
            if tempo.time_signature:
                num, den = (tempo.time_signature.split("/") + [4, 4])[:2]
                await self._call_applescript("set_time_signature", int(num), int(den))
            # 立即另存为到磁盘，确保工程有确定路径，后续所有阶段都指向它
            await self._call_applescript("save_as", project_path)
            await self.log("info", f"工程已保存并锁定：{project_path}")
        else:
            await self.log("warn", "非 macOS 或未启用 AppleScript，跳过实际建项目（模拟模式，工程锁仍生效）。")
        await self.emit(kind="project_created", title=title, bpm=tempo.bpm,
                        key=tempo.key, time_signature=tempo.time_signature,
                        project_path=project_path, locked=True)
        return True

    async def open_existing_project(self, path: str) -> bool:
        """打开一个已有的 .logicx 工程作为锚点（用户先打开工程后再开始制作）。

        严格规则：若已有锁定工程，拒绝切换，防止意外切换到其他工程。
        """
        from pathlib import Path as _Path
        p = _Path(path).expanduser()
        if not p.exists() or p.suffix != ".logicx":
            await self.log("error", f"工程文件不存在或非 .logicx：{path}")
            return False
        title = p.stem
        if not self._lock_project(str(p), title):
            await self.log(
                "warn",
                f"已拒绝打开工程「{path}」：当前已锁定工程「{self.current_project_title}」。"
                "禁止在制作过程中切换工程。",
            )
            return False
        await self.log("info", f"打开已有工程：{title}（{path}，已锁定）")
        if self._real:
            await self._call_applescript("open_project", str(p))
        await self.emit(kind="project_opened", title=title, project_path=str(p), locked=True)
        return True

    async def save_project(self):
        if not self._require_project("保存"):
            await self.log("warn", "无活动工程，跳过保存。")
            return
        await self.log("info", "保存工程")
        if self._real:
            await self._call_applescript("save_project")
        await self.emit(kind="project_saved")

    async def save_as(self, path: str):
        await self.log("info", f"另存为：{path}")
        if self._real:
            await self._call_applescript("save_as", path)
        await self.emit(kind="project_saved_as", path=path)

    async def undo(self):
        await self.log("info", "撤销")
        if self._real:
            await self._call_applescript("undo")
        await self.emit(kind="undo")

    async def redo(self):
        await self.log("info", "重做")
        if self._real:
            await self._call_applescript("redo")
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
                await self._call_applescript("play")
            elif op == "stop":
                await self._call_applescript("stop")
            elif op == "pause":
                await self._call_applescript("pause")
            elif op == "record":
                await self._call_applescript("record")
            elif op == "goto":
                await self._call_applescript("goto_bar", action.bar or 1, action.beat or 1)
            elif op == "set_cycle" or op == "set_loop":
                if action.start_bar and action.end_bar:
                    await self._call_applescript("set_cycle", action.start_bar, action.end_bar)
            elif op == "toggle_loop":
                await self._call_applescript("toggle_loop")
            elif op == "rewind":
                await self._call_applescript("rewind")
            elif op == "forward":
                await self._call_applescript("forward")
        await self.emit(kind="transport", op=op, bar=action.bar,
                        start_bar=action.start_bar, end_bar=action.end_bar)

    # ============== 轨道 ==============
    async def ensure_track(self, track: TrackSpec):
        if not self._require_project("创建轨道"):
            await self.log("warn", f"已跳过创建轨道「{track.name}」：无活动工程。请先在作曲阶段创建或打开一个工程。")
            return
        if track.name in self._track_index:
            return
        self._track_index[track.name] = track
        await self.log("info", f"创建轨道：{track.name}（{track.type} / {track.instrument or '-'}）")
        if self._real:
            if track.type == "audio":
                await self._call_applescript("create_audio_track", track.name)
            elif track.type == "drummer":
                await self._call_applescript("create_drummer_track", track.name)
            elif track.type == "aux":
                await self._call_applescript("create_aux_track", track.name)
            else:
                await self._call_applescript("create_software_track", track.name)
            if track.color is not None:
                await self._call_applescript("set_track_color", track.name, track.color)
            if track.icon:
                await self._call_applescript("set_track_icon", track.name, track.icon)
            if track.freeze:
                await self._call_applescript("freeze_track", track.name)
            if track.hidden:
                await self._call_applescript("toggle_track_hide", track.name)
        await self.emit(kind="track_added", track=track.model_dump())

    async def delete_track(self, name: str):
        await self.log("info", f"删除轨道：{name}")
        self._track_index.pop(name, None)
        if self._real:
            await self._call_applescript("select_track_by_name", name)
            await self._call_applescript("delete_selected_track")
        await self.emit(kind="track_deleted", track=name)

    async def duplicate_track(self, name: str):
        await self.log("info", f"复制轨道：{name}")
        if self._real:
            await self._call_applescript("select_track_by_name", name)
            await self._call_applescript("duplicate_selected_track")
        await self.emit(kind="track_duplicated", track=name)

    async def create_track_stack(self, stack: TrackStackSpec):
        await self.log("info", f"创建轨道堆栈「{stack.name}」：{stack.members}")
        if self._real:
            await self._call_applescript("create_track_stack", stack.members, stack.name, stack.stack_type)
        await self.emit(kind="track_stack_created", name=stack.name, members=stack.members)

    # ============== MIDI 区域 ==============
    async def add_region(self, region: MidiRegionSpec, bpm: float):
        if not self._require_project("写入 MIDI 片段"):
            await self.log("warn", f"已跳过写入 MIDI 片段到「{region.track}」：无活动工程。")
            return
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
        if not self._require_project("片段操作"):
            await self.log("warn", f"已跳过片段操作「{op.op}」：无活动工程。")
            return
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
                await self._call_applescript("split_region_at_playhead", op.track, op.at_bar or 1, op.at_beat or 1)
            elif op.op == "join":
                await self._call_applescript("join_regions", op.track)
            elif op.op == "move":
                await self._call_applescript("move_region", op.track, op.at_bar or 1, op.to_bar or 1)
            elif op.op == "copy":
                await self._call_applescript("copy_region", op.track, op.at_bar or 1, op.to_bar or 1)
            elif op.op == "delete":
                await self._call_applescript("delete_regions", op.track, op.at_bar or 1)
            elif op.op == "loop":
                await self._call_applescript("loop_region", op.track, op.at_bar or 1, op.loop_count or 2)
            elif op.op == "resize":
                await self._call_applescript("resize_region", op.track, op.at_bar or 1, op.new_length_beats or 4)
            elif op.op == "quantize":
                await self._call_applescript("select_track_by_name", op.track)
                await self._call_applescript("quantize_selected_regions", op.grid or "1/16", op.strength or 100)
            elif op.op == "transpose":
                await self._call_applescript("select_track_by_name", op.track)
                await self._call_applescript("transpose_selected", op.semitones or 0)
        await self.emit(kind="region_op", op=op.op, track=op.track,
                        at_bar=op.at_bar, to_bar=op.to_bar)

    # ============== 混音 ==============
    async def apply_mix(self, params: MixParams):
        if not self._require_project("混音"):
            await self.log("warn", f"已跳过混音「{params.track}」：无活动工程。")
            return
        await self.log("info", f"混音：{params.track}（vol={params.volume_db} pan={params.pan}）")
        if self._real:
            if params.volume_db is not None:
                await self._call_applescript("set_volume", params.track, params.volume_db)
            if params.pan is not None:
                await self._call_applescript("set_pan", params.track, params.pan)
            if params.mute is not None:
                await self._call_applescript("set_mute", params.track, params.mute)
            if params.solo is not None:
                await self._call_applescript("set_solo", params.track, params.solo)
            if params.input_monitoring is not None:
                await self._call_applescript("set_input_monitoring", params.track, params.input_monitoring)
            if params.plugins:
                await self._call_applescript("select_track_by_name", params.track)
                for p in params.plugins:
                    if p.bypass:
                        await self._call_applescript("bypass_plugin", params.track, p.name, True)
                    else:
                        await self._call_applescript("add_plugin_to_selected_track", p.name, p.preset)
            if params.sends:
                for s in params.sends:
                    await self._call_applescript("add_send_to_bus", params.track, s.target, s.amount)
        else:
            await self.log("warn", "模拟模式：跳过混音器实际操作。")
        await self.emit(kind="mix_applied", track=params.track,
                        params=params.model_dump(exclude_none=True))

    async def create_bus(self, bus: BusSpec):
        """创建辅助通道/总线并挂插件。"""
        if not self._require_project("创建总线"):
            await self.log("warn", f"已跳过创建总线「{bus.name}」：无活动工程。")
            return
        await self.log("info", f"创建总线「{bus.name}」（输入={bus.input or '-'}，{len(bus.plugins)} 个插件）")
        if self._real:
            await self._call_applescript("create_aux_channel", bus.name, bus.input)
            for p in bus.plugins:
                await self._call_applescript("select_track_by_name", bus.name)
                await self._call_applescript("add_plugin_to_selected_track", p.name, p.preset)
        await self.emit(kind="bus_created", name=bus.name, input=bus.input,
                        plugins=[p.name for p in bus.plugins])

    async def set_plugin_param(self, p: PluginParamSpec):
        """设置插件参数。"""
        await self.log("info", f"插件参数：{p.track}/{p.plugin}/{p.parameter} = {p.value}")
        if self._real:
            await self._call_applescript("set_plugin_parameter", p.track, p.plugin, p.parameter, p.value)
        await self.emit(kind="plugin_param", track=p.track, plugin=p.plugin,
                        parameter=p.parameter, value=p.value)

    # ============== 自动化 ==============
    async def apply_automation(self, auto: AutomationSpec):
        """写自动化曲线。"""
        await self.log("info", f"自动化：{auto.track}/{auto.parameter}（{len(auto.points)} 个点，模式={auto.mode}）")
        if self._real:
            await self._call_applescript("set_automation_mode", auto.track, auto.mode)
            await self._call_applescript("show_automation_for_track", auto.track, auto.parameter)
        await self.emit(kind="automation", track=auto.track, parameter=auto.parameter,
                        mode=auto.mode, point_count=len(auto.points))

    # ============== 标记 ==============
    async def add_marker(self, marker: MarkerSpec):
        """添加编排标记。"""
        await self.log("info", f"标记：{marker.name} @ 小节 {marker.bar}")
        if self._real:
            await self._call_applescript("add_arrangement_marker", marker.name, marker.bar)
        await self.emit(kind="marker_added", name=marker.name, bar=marker.bar)

    # ============== 速度变化 ==============
    async def add_tempo_change(self, tc: TempoChangeSpec):
        """添加速度变化点。"""
        ramp_desc = "（渐变）" if tc.ramp else "（跳变）"
        await self.log("info", f"速度变化：{tc.bpm}BPM @ 小节 {tc.bar}{ramp_desc}")
        if self._real:
            await self._call_applescript("add_tempo_change", tc.bar, tc.bpm, tc.ramp)
        await self.emit(kind="tempo_change", bar=tc.bar, bpm=tc.bpm, ramp=tc.ramp)

    # ============== 母带 ==============
    async def apply_master(self, plugins: list[PluginSpec]):
        if not self._require_project("母带"):
            await self.log("warn", "已跳过母带处理：无活动工程。")
            return
        await self.log("info", f"母带链：{[p.name for p in plugins]}")
        if self._real:
            await self._call_applescript("select_master_track")
            for p in plugins:
                if p.bypass:
                    await self._call_applescript("bypass_plugin", "Stereo Out", p.name, True)
                else:
                    await self._call_applescript("add_plugin_to_master", p.name, p.preset)
        await self.emit(kind="master_applied", plugins=[p.model_dump() for p in plugins])

    # ============== 录音 ==============
    async def setup_record(self, rec: RecordSpec):
        """设置录音参数并 armed 轨道。"""
        await self.log("info", f"录音准备：{rec.track}（armed={rec.armed}，count_in={rec.count_in}）")
        if self._real:
            if rec.count_in:
                await self._call_applescript("set_count_in", rec.count_in)
            if rec.autopunch:
                await self._call_applescript("set_autopunch", rec.autopunch.get("start_bar", 1),
                                             rec.autopunch.get("end_bar", 2))
            if rec.armed:
                await self._call_applescript("arm_track", rec.track, True)
        await self.emit(kind="record_setup", track=rec.track, armed=rec.armed,
                        count_in=rec.count_in)

    # ============== 导出 ==============
    async def bounce(self, bounce: BounceSpec) -> Path:
        if not self._require_project("导出"):
            await self.log("warn", "已跳过导出：无活动工程，无法 bounce。")
            return None
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
            "collapse_all": "折叠所有堆栈", "dismiss_dialog": "关闭弹窗",
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
                    await self._call_applescript("save_project")
            else:
                await self.log("warn", "save_as 被忽略：当前无活动工程。")
            await self.emit(kind="ui_action", op="save_as", ignored=bool(not self.current_project_path))
            return
        await self.log("info", f"UI：{labels.get(action.op, action.op)}")
        if self._real:
            if action.op == "save":
                await self._call_applescript("save_project")
            elif action.op == "undo":
                await self._call_applescript("undo")
            elif action.op == "redo":
                await self._call_applescript("redo")
            elif action.op == "open_piano_roll":
                await self._call_applescript("open_piano_roll")
            elif action.op == "open_mixer":
                await self._call_applescript("open_mixer")
            elif action.op == "open_inspector":
                await self._call_applescript("open_inspector")
            elif action.op == "zoom_fit":
                await self._call_applescript("zoom_fit")
            elif action.op == "select_all":
                await self._call_applescript("select_all_regions")
            elif action.op == "collapse_all":
                await self._call_applescript("collapse_all_track_stacks")
            elif action.op == "dismiss_dialog":
                # AI 主动清弹窗（截图看到弹窗时输出此动作）
                closed = await self._call_applescript("dismiss_dialogs", action="cancel", max_count=5)
                await self.log("info", f"关闭弹窗：{closed} 个")
            elif action.op == "toggle_track" and action.track:
                await self._call_applescript("toggle_track_hide", action.track)
        await self.emit(kind="ui_action", op=action.op)

    def dismiss_dialogs(self, action: str = "cancel", max_count: int = 5) -> int:
        """主动关闭 Logic Pro 弹窗。

        action: "cancel" | "confirm" | "ok"（详见 applescript_bridge.dismiss_dialogs）。
        供 commander/AI 在截图发现弹窗时主动调用，避免弹窗挡住后续操作。
        """
        if not self._real:
            return 0
        return self.applescript.dismiss_dialogs(action=action, max_count=max_count)

    def close(self):
        self.midi.close()
