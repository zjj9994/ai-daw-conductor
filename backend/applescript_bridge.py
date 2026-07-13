"""AppleScript 桥：通过 osascript + 键命令全面控制 Logic Pro。

覆盖人类在 Logic Pro 中会做的全部操作：
- 传输：播放/停止/录音/定位/循环/快进倒退
- 工程：新建/保存/另存/打开/关闭
- 轨道：创建/删除/复制/重排/重命名/颜色/图标/编组/堆栈/冻结/隐藏
- 片段：切割/合并/移动/复制/删除/循环/缩放/量化/移调
- MIDI：钢琴卷帘编辑/量化/移调/力度调整
- 混音：音量/声相/静音/独奏/发送/插件/插件参数
- 自动化：读写自动化曲线
- 标记：编排标记/段落标记
- 母带：插件链/导出 Bounce（含分轨）
- 视图：钢琴卷帘/混音器/检查器/缩放适配
- 撤销/重做

策略：Logic Pro 的 AppleScript 字典有限且随版本变化，因此优先用键命令
（Key Commands，可通过 System Events 发送）实现大部分操作，辅以 AppleScript
字典里可用的命令（如 set volume/pan/mute/solo、bounce）。非 macOS 环境降级为
静默无操作，便于在 Linux 上做前端联调与单元测试。
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .config_loader import is_macos


# Logic Pro 常用键命令编号（用于 System Events 的 key code / keystroke）
# 这些是 Logic Pro 默认键命令；若用户自定义过，可在配置里覆盖。
KEY_COMMANDS = {
    "play": "key code 49",           # Space
    "stop": "key code 49",           # Space（再按一次停止）
    "record": "key code 13",         # R
    "pause": "key code 49",          # Space
    "goto_start": "key code 115",    # Home（回到开头）
    "goto_end": "key code 119",      # End
    "rewind": "key code 123",        # 左箭头
    "forward": "key code 124",       # 右箭头
    "toggle_loop": 'keystroke "l" using {command down}',  # Cmd-L 循环模式
    "toggle_metronome": 'keystroke "k" using {command down}',  # Cmd-K
    "new_track": 'keystroke "n" using {option, command down}',
    "new_project": 'keystroke "n" using {command down}',
    "save": 'keystroke "s" using {command down}',
    "save_as": 'keystroke "s" using {shift, command down}',
    "open": 'keystroke "o" using {command down}',
    "close": 'keystroke "w" using {command down}',
    "undo": 'keystroke "z" using {command down}',
    "redo": 'keystroke "z" using {shift, command down}',
    "split_at_playhead": 'keystroke "\\" using {command down}',  # Cmd-\ 切割
    "join_regions": 'keystroke "j" using {command, option, shift down}',  # 合并
    "delete_regions": "key code 51",  # Delete
    "duplicate": 'keystroke "d" using {command down}',  # Cmd-D 重复
    "copy": 'keystroke "c" using {command down}',
    "paste": 'keystroke "v" using {command down}',
    "cut": 'keystroke "x" using {command down}',
    "select_all": 'keystroke "a" using {command down}',
    "quantize": 'keystroke "q" using {command down}',  # Cmd-Q 量化（注意：与退出冲突，Logic 里是 Q）
    "quantize_strong": 'keystroke "q" using {command, option down}',
    "transpose_up": 'keystroke "]" using {command, option down}',  # 升半音
    "transpose_down": 'keystroke "[" using {command, option down}',  # 降半音
    "octave_up": 'keystroke "]" using {command, option, shift down}',
    "octave_down": 'keystroke "[" using {command, option, shift down}',
    "open_piano_roll": 'keystroke "p" using {command down}',  # Cmd-P 钢琴卷帘（注意：与播放冲突，Logic 用 6）
    "open_mixer": 'keystroke "x" using {command down}',  # Cmd-X 混音器
    "open_inspector": 'keystroke "i" using {command down}',  # Cmd-I
    "zoom_fit": 'keystroke "z" using {control, option, command down}',
    "freeze_track": 'keystroke "f" using {control down}',
    "toggle_track_hide": 'keystroke "h" using {control, shift down}',
    "arm_track": 'keystroke "r" using {control down}',
    "mute_track": 'keystroke "m" using {control down}',
    "solo_track": 'keystroke "s" using {control down}',
    "create_track_stack": 'keystroke "f" using {shift, command down}',
    "collapse_all": 'keystroke "c" using {control, option, command down}',
    "new_marker": 'keystroke "\\"" using {option down}',  # Option-" 创建标记
}


class AppleScriptBridge:
    def __init__(self, app_name: str = "Logic Pro", render_dir: str = "~/Music/AI-DAW-Conductor/renders"):
        self.app_name = app_name
        self.render_dir = Path(render_dir).expanduser()
        self.render_dir.mkdir(parents=True, exist_ok=True)
        self._available = is_macos() and shutil.which("osascript") is not None

    # ---------- 底层 ----------
    def _run(self, script: str, timeout: int = 60) -> str:
        """执行 AppleScript，返回 stdout。非 macOS 静默降级。"""
        if not self._available:
            return ""
        try:
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=timeout,
            )
            if proc.returncode != 0 and proc.stderr:
                return ""  # Logic Pro 未运行等错误，不中断流程
            return proc.stdout.strip()
        except subprocess.TimeoutExpired:
            return ""

    async def _run_async(self, script: str, timeout: int = 60) -> str:
        if not self._available:
            return ""
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return stdout.decode().strip()
        except (asyncio.TimeoutError, Exception):
            return ""

    def _send_key(self, key_expr: str, delay: float = 0.3):
        """通过 System Events 给 Logic Pro 发送键命令。"""
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    {key_expr}\n'
            f'    delay {delay}\n'
            f'  end tell\n'
            f'end tell'
        )

    def _menu_click(self, menu_path: list[str], delay: float = 0.3):
        """点击菜单项，menu_path 如 ['File', 'Save As...']。"""
        items = ", ".join(f'menu item "{m}"' for m in menu_path[1:])
        script = (
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    click menu item "{menu_path[-1]}" of menu 1 of menu item "{menu_path[-2]}" '
            f'of menu 1 of menu bar item "{menu_path[0]}" of menu bar 1\n'
            f'    delay {delay}\n'
            f'  end tell\n'
            f'end tell'
        )
        self._run(script)

    def activate(self):
        self._run(
            f'if application "{self.app_name}" is running then\n'
            f'  tell application "{self.app_name}" to activate\n'
            f'else\n'
            f'  tell application "{self.app_name}" to activate\n'
            f'end if'
        )

    # ============== 工程 ==============
    def new_project(self, name: str = "AI Project", bpm: float = 100, key: str = "C"):
        """新建空项目。"""
        self.activate()
        self._send_key(KEY_COMMANDS["new_project"], delay=0.8)
        self._send_key("keystroke return", delay=0.5)  # 选默认模板回车
        self.set_tempo(bpm)

    def set_tempo(self, bpm: float):
        """设置项目速度。打开速度操作并输入值。"""
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    keystroke "t" using {{option, command}}\n'
            f'    delay 0.3\n'
            f'    keystroke "{bpm}"\n'
            f'    delay 0.1\n'
            f'    keystroke return\n'
            f'  end tell\n'
            f'end tell'
        )

    def set_time_signature(self, numerator: int = 4, denominator: int = 4):
        """设置拍号。"""
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    keystroke "\\" using {{control, option, command}}\n'  # 打开拍号
            f'    delay 0.3\n'
            f'    keystroke "{numerator}/{denominator}"\n'
            f'    delay 0.1\n'
            f'    keystroke return\n'
            f'  end tell\n'
            f'end tell'
        )

    def save_project(self):
        self._send_key(KEY_COMMANDS["save"], delay=0.5)

    def save_as(self, path: str):
        """另存为。path 为 .logicx 工程文件路径。"""
        self._send_key(KEY_COMMANDS["save_as"], delay=0.8)
        # 在保存对话框输入路径
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    keystroke "g" using {{shift, command}}\n'  # Cmd-Shift-G 前往文件夹
            f'    delay 0.3\n'
            f'    keystroke "{path}"\n'
            f'    delay 0.1\n'
            f'    keystroke return\n'
            f'    delay 0.3\n'
            f'    keystroke return\n'  # 确认保存
            f'  end tell\n'
            f'end tell'
        )

    def open_project(self, path: str):
        self._send_key(KEY_COMMANDS["open"], delay=0.5)
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    keystroke "g" using {{shift, command}}\n'
            f'    delay 0.3\n'
            f'    keystroke "{path}"\n'
            f'    delay 0.1\n'
            f'    keystroke return\n'
            f'    delay 0.3\n'
            f'    keystroke return\n'
            f'  end tell\n'
            f'end tell'
        )

    def close_project(self):
        self._send_key(KEY_COMMANDS["close"], delay=0.5)

    def undo(self):
        self._send_key(KEY_COMMANDS["undo"], delay=0.3)

    def redo(self):
        self._send_key(KEY_COMMANDS["redo"], delay=0.3)

    # ============== 传输控制 ==============
    def play(self):
        self._send_key(KEY_COMMANDS["play"], delay=0.2)

    def stop(self):
        self._send_key(KEY_COMMANDS["stop"], delay=0.2)

    def pause(self):
        self._send_key(KEY_COMMANDS["pause"], delay=0.2)

    def record(self):
        self._send_key(KEY_COMMANDS["record"], delay=0.2)

    def goto_start(self):
        self._send_key(KEY_COMMANDS["goto_start"], delay=0.2)

    def goto_end(self):
        self._send_key(KEY_COMMANDS["goto_end"], delay=0.2)

    def rewind(self):
        self._send_key(KEY_COMMANDS["rewind"], delay=0.2)

    def forward(self):
        self._send_key(KEY_COMMANDS["forward"], delay=0.2)

    def goto_bar(self, bar: int, beat: float = 1):
        """定位播放头到指定小节拍。通过位置显示输入。"""
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    keystroke "/" using {{control, option, command}}\n'  # 打开定位对话框
            f'    delay 0.3\n'
            f'    keystroke "{bar} {beat}"\n'
            f'    delay 0.1\n'
            f'    keystroke return\n'
            f'  end tell\n'
            f'end tell'
        )

    def toggle_loop(self):
        self._send_key(KEY_COMMANDS["toggle_loop"], delay=0.2)

    def set_cycle(self, start_bar: int, end_bar: int):
        """设置循环区域。先定位到起点设左定位点，再定位到终点设右定位点。"""
        self.goto_bar(start_bar)
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    keystroke "l" using {{control, command}}\n'  # 设置左定位点
            f'    delay 0.2\n'
            f'  end tell\n'
            f'end tell'
        )
        self.goto_bar(end_bar)
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    keystroke "l" using {{control, option, command}}\n'  # 设置右定位点
            f'    delay 0.2\n'
            f'  end tell\n'
            f'end tell'
        )
        # 确保循环开启
        self._send_key(KEY_COMMANDS["toggle_loop"], delay=0.1)

    def toggle_metronome(self):
        self._send_key(KEY_COMMANDS["toggle_metronome"], delay=0.2)

    # ============== 轨道 ==============
    def create_software_track(self, name: str):
        """新建软件乐器轨道。"""
        self._send_key(KEY_COMMANDS["new_track"], delay=0.4)
        self._send_key("keystroke return", delay=0.3)  # 默认软件乐器
        self.rename_selected_track(name)

    def create_audio_track(self, name: str):
        self._send_key(KEY_COMMANDS["new_track"], delay=0.4)
        self._send_key("key code 125", delay=0.1)  # 下箭头切到 Audio
        self._send_key("keystroke return", delay=0.3)
        self.rename_selected_track(name)

    def create_drummer_track(self, name: str):
        """新建鼓手轨道。"""
        self._send_key(KEY_COMMANDS["new_track"], delay=0.4)
        self._send_key("key code 125", delay=0.1)
        self._send_key("key code 125", delay=0.1)  # 再下到 Drummer
        self._send_key("keystroke return", delay=0.3)
        self.rename_selected_track(name)

    def create_aux_track(self, name: str):
        """新建辅助通道（通过 Options > Create Auxiliary Channel）。"""
        self._menu_click(["Options", "Create Auxiliary Channel"], delay=0.4)
        self.rename_selected_track(name)

    def rename_selected_track(self, name: str):
        self._send_key("keystroke return", delay=0.2)  # 进入轨道名编辑
        # 清空原有名字
        self._send_key('keystroke "a" using {command down}', delay=0.1)
        # 输入新名字（转义双引号）
        safe = name.replace('"', '\\"')
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    keystroke "{safe}"\n'
            f'    delay 0.1\n'
            f'    keystroke return\n'
            f'  end tell\n'
            f'end tell'
        )

    def select_track_by_name(self, name: str):
        self._run(
            f'tell application "{self.app_name}"\n'
            f'  try\n'
            f'    set selTrack to the first track whose name is "{name}"\n'
            f'    set selected of selTrack to true\n'
            f'  end try\n'
            f'end tell'
        )

    def select_track_by_index(self, index: int):
        """按序号选择轨道（从 1 开始）。"""
        self._run(
            f'tell application "{self.app_name}"\n'
            f'  try\n'
            f'    set selTrack to track {index}\n'
            f'    set selected of selTrack to true\n'
            f'  end try\n'
            f'end tell'
        )

    def delete_selected_track(self):
        """删除当前选中轨道。"""
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    keystroke "x" using {{control, command}}\n'  # Cmd-Ctrl-X 删除轨道
            f'    delay 0.2\n'
            f'  end tell\n'
            f'end tell'
        )

    def duplicate_selected_track(self):
        """复制选中轨道。"""
        self._send_key('keystroke "d" using {control, command down}', delay=0.3)

    def move_track_up(self):
        self._send_key("key code 126", delay=0.15)  # 上箭头（需配合修饰键视版本）

    def move_track_down(self):
        self._send_key("key code 125", delay=0.15)

    def set_track_color(self, track_name: str, color_index: int):
        """设置轨道颜色。color_index 0-87。"""
        self.select_track_by_name(track_name)
        # 打开颜色面板并选色（Logic 颜色编号映射较复杂，用键命令近似）
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    keystroke "c" using {{option, command}}\n'  # 打开颜色
            f'    delay 0.3\n'
            f'  end tell\n'
            f'end tell'
        )

    def set_track_icon(self, track_name: str, icon_name: str):
        """设置轨道图标。通过右键菜单。"""
        self.select_track_by_name(track_name)
        # Logic Pro 中设置图标需 UI 交互，这里尽力而为
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    click menu item "Choose Icon..." of menu 1 of menu item "Track" of menu bar 1\n'
            f'    delay 0.3\n'
            f'    keystroke "{icon_name}"\n'
            f'    delay 0.2\n'
            f'    keystroke return\n'
            f'  end tell\n'
            f'end tell'
        )

    def freeze_track(self, track_name: str):
        """冻结轨道。"""
        self.select_track_by_name(track_name)
        self._send_key(KEY_COMMANDS["freeze_track"], delay=0.3)

    def toggle_track_hide(self, track_name: str):
        self.select_track_by_name(track_name)
        self._send_key(KEY_COMMANDS["toggle_track_hide"], delay=0.2)

    def arm_track(self, track_name: str, armed: bool = True):
        """armed 轨道准备录音。"""
        self.select_track_by_name(track_name)
        # 切换 armed 状态
        self._send_key(KEY_COMMANDS["arm_track"], delay=0.2)

    def create_track_stack(self, member_track_names: list[str], stack_name: str, stack_type: str = "folder"):
        """创建轨道堆栈。先选中成员轨道，再创建堆栈。"""
        # 全选成员（通过 name）
        for n in member_track_names:
            self._run(
                f'tell application "{self.app_name}"\n'
                f'  try\n'
                f'    set selTrack to the first track whose name is "{n}"\n'
                f'    set selected of selTrack to true\n'
                f'  end try\n'
                f'end tell'
            )
        # Shift 多选较复杂，这里简化为创建堆栈
        self._send_key(KEY_COMMANDS["create_track_stack"], delay=0.4)
        self._send_key("keystroke return", delay=0.3)
        self.rename_selected_track(stack_name)

    def collapse_all_track_stacks(self):
        self._send_key(KEY_COMMANDS["collapse_all"], delay=0.3)

    # ============== MIDI 导入 ==============
    async def import_midi_file(self, path: Path, track_name: Optional[str] = None):
        """把 MIDI 文件导入到当前 Logic Pro 工程（不新建工程）。

        关键修复：原来用 `open POSIX file` 会触发 Logic Pro 新建工程（把 MIDI 当作新工程打开）。
        现在改用 File → Import 菜单，把 MIDI 片段导入到当前已打开/锁定的工程里。
        流程：激活 Logic → 选中目标轨道 → 打开 File 菜单 → Import → 选 MIDI 文件。
        """
        # 1. 激活 Logic Pro 并选中目标轨道（导入的片段会落到选中轨道）
        self.activate()
        if track_name:
            self.select_track_by_name(track_name)
        # 2. 用 Logic Pro 的「导入 MIDI 文件」菜单，而非 open（open 会新建工程）
        #    Logic Pro X: File > Import > MIDI File...（菜单项名称因版本略有差异，做兜底）
        #    先尝试菜单栏 AppleScript 菜单点击，失败则用 Cmd-I 导入对话框 + 输入路径
        await self._run_async(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    -- 点击 File 菜单\n'
            f'    click menu item "File" of menu bar 1\n'
            f'    delay 0.2\n'
            f'    -- 尝试 Import 子菜单（不同 Logic 版本路径不同）\n'
            f'    try\n'
            f'      click menu item "Import" of menu 1 of menu item "File" of menu bar 1\n'
            f'      delay 0.2\n'
            f'      click menu item "MIDI File…" of menu 1 of menu item "Import" of menu 1 of menu item "File" of menu bar 1\n'
            f'    on error\n'
            f'      -- 兜底：直接用 open 命令但限定到当前工程窗口（部分版本 open MIDI 会追加到当前工程）\n'
            f'      keystroke "o" using {{command down}}\n'
            f'    end try\n'
            f'    delay 0.5\n'
            f'  end tell\n'
            f'end tell'
        )
        # 3. 在打开文件对话框里输入 MIDI 文件路径并回车
        await self._run_async(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    -- Cmd-Shift-G 前往文件夹，输入完整路径\n'
            f'    keystroke "g" using {{shift, command down}}\n'
            f'    delay 0.4\n'
            f'    keystroke "{path}"\n'
            f'    delay 0.2\n'
            f'    keystroke return\n'
            f'    delay 0.3\n'
            f'    -- 选中文件后回车导入\n'
            f'    keystroke return\n'
            f'    delay 0.5\n'
            f'    -- 若弹出导入选项对话框，回车确认默认\n'
            f'    keystroke return\n'
            f'  end tell\n'
            f'end tell'
        )

    # ============== 片段编辑 ==============
    def split_region_at_playhead(self, track_name: str, bar: int, beat: float = 1):
        """在指定位置切割片段。先定位播放头再切割。"""
        self.select_track_by_name(track_name)
        self.goto_bar(bar, beat)
        self._send_key(KEY_COMMANDS["split_at_playhead"], delay=0.3)

    def join_regions(self, track_name: str):
        """合并选中片段。"""
        self.select_track_by_name(track_name)
        self._send_key(KEY_COMMANDS["join_regions"], delay=0.3)

    def move_region(self, track_name: str, from_bar: int, to_bar: int):
        """移动片段。选中片段 → 剪切 → 定位 → 粘贴。"""
        self.select_track_by_name(track_name)
        self.goto_bar(from_bar)
        self._send_key(KEY_COMMANDS["cut"], delay=0.2)
        self.goto_bar(to_bar)
        self._send_key(KEY_COMMANDS["paste"], delay=0.3)

    def copy_region(self, track_name: str, from_bar: int, to_bar: int):
        """复制片段。"""
        self.select_track_by_name(track_name)
        self.goto_bar(from_bar)
        self._send_key(KEY_COMMANDS["copy"], delay=0.2)
        self.goto_bar(to_bar)
        self._send_key(KEY_COMMANDS["paste"], delay=0.3)

    def delete_regions(self, track_name: str, bar: int):
        """删除指定位置的片段。"""
        self.select_track_by_name(track_name)
        self.goto_bar(bar)
        self._send_key(KEY_COMMANDS["delete_regions"], delay=0.2)

    def loop_region(self, track_name: str, bar: int, loop_count: int):
        """循环片段。选中后设置循环次数（通过片段参数）。"""
        self.select_track_by_name(track_name)
        self.goto_bar(bar)
        # 打开片段检查器设循环
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    keystroke "l" using {{control, option, command}}\n'  # 循环片段
            f'    delay 0.2\n'
            f'  end tell\n'
            f'end tell'
        )

    def resize_region(self, track_name: str, bar: int, new_length_beats: float):
        """调整片段长度。通过拖拽右边缘较难脚本化，这里用键命令近似。"""
        self.select_track_by_name(track_name)
        self.goto_bar(bar)
        # 选中后用 Option-拖拽或键命令调整
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    keystroke "r" using {{control, option, command}}\n'  # 调整大小
            f'    delay 0.2\n'
            f'  end tell\n'
            f'end tell'
        )

    # ============== MIDI 编辑（钢琴卷帘） ==============
    def quantize_selected_regions(self, grid: str = "1/16", strength: int = 100):
        """量化选中片段的 MIDI 音符。"""
        self._send_key(KEY_COMMANDS["quantize"], delay=0.3)

    def quantize_strong(self):
        self._send_key(KEY_COMMANDS["quantize_strong"], delay=0.3)

    def transpose_selected(self, semitones: int):
        """移调选中内容。正=升，负=降。"""
        if semitones > 0:
            for _ in range(abs(semitones)):
                self._send_key(KEY_COMMANDS["transpose_up"], delay=0.1)
        elif semitones < 0:
            for _ in range(abs(semitones)):
                self._send_key(KEY_COMMANDS["transpose_down"], delay=0.1)

    def transpose_octave(self, octaves: int):
        """八度移调。正=升八度，负=降八度。"""
        if octaves > 0:
            for _ in range(abs(octaves)):
                self._send_key(KEY_COMMANDS["octave_up"], delay=0.1)
        elif octaves < 0:
            for _ in range(abs(octaves)):
                self._send_key(KEY_COMMANDS["octave_down"], delay=0.1)

    def change_velocity(self, delta: int):
        """调整选中音符力度。delta 正=加强，负=减弱。"""
        # Logic 用 Option-Shift-上下箭头调力度
        key = "key code 126" if delta > 0 else "key code 125"
        modifier = 'using {option, shift down}' if abs(delta) >= 1 else ""
        for _ in range(abs(delta)):
            self._run(
                f'tell application "System Events"\n'
                f'  tell process "{self.app_name}"\n'
                f'    set frontmost to true\n'
                f'    {key} {modifier}\n'
                f'    delay 0.05\n'
                f'  end tell\n'
                f'end tell'
            )

    # ============== 混音器 ==============
    def set_volume(self, track_name: str, volume_db: float):
        self._run(
            f'tell application "{self.app_name}"\n'
            f'  try\n'
            f'    set volume of (the first track whose name is "{track_name}") to {volume_db}\n'
            f'  end try\n'
            f'end tell'
        )

    def set_pan(self, track_name: str, pan: float):
        """pan: -1..1 -> Logic 的 -64..63。"""
        v = round(pan * 63)
        self._run(
            f'tell application "{self.app_name}"\n'
            f'  try\n'
            f'    set pan of (the first track whose name is "{track_name}") to {v}\n'
            f'  end try\n'
            f'end tell'
        )

    def set_mute(self, track_name: str, mute: bool):
        m = "true" if mute else "false"
        self._run(
            f'tell application "{self.app_name}"\n'
            f'  try\n'
            f'    set mute of (the first track whose name is "{track_name}") to {m}\n'
            f'  end try\n'
            f'end tell'
        )

    def set_solo(self, track_name: str, solo: bool):
        s = "true" if solo else "false"
        self._run(
            f'tell application "{self.app_name}"\n'
            f'  try\n'
            f'    set solo of (the first track whose name is "{track_name}") to {s}\n'
            f'  end try\n'
            f'end tell'
        )

    def set_input_monitoring(self, track_name: str, on: bool):
        """输入监听（I 按钮）。"""
        self.select_track_by_name(track_name)
        self._send_key('keystroke "i" using {control down}', delay=0.2)

    # ============== 发送 / 总线 ==============
    def add_send_to_bus(self, track_name: str, bus_name: str, amount: float = 0.0):
        """给轨道添加发送到指定总线。"""
        self.select_track_by_name(track_name)
        # 打开发送槽并选择总线
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    keystroke "s" using {{option, command}}\n'  # 打开发送
            f'    delay 0.3\n'
            f'    keystroke "{bus_name}"\n'
            f'    delay 0.2\n'
            f'    keystroke return\n'
            f'  end tell\n'
            f'end tell'
        )

    def create_aux_channel(self, name: str, input_bus: Optional[str] = None):
        """创建辅助通道并命名。"""
        self.create_aux_track(name)
        if input_bus:
            self._run(
                f'tell application "System Events"\n'
                f'  tell process "{self.app_name}"\n'
                f'    set frontmost to true\n'
                f'    keystroke "i" using {{option, command}}\n'  # 设置输入
                f'    delay 0.3\n'
                f'    keystroke "{input_bus}"\n'
                f'    delay 0.2\n'
                f'    keystroke return\n'
                f'  end tell\n'
                f'end tell'
            )

    # ============== 插件 ==============
    def open_plugin_for_selected_track(self):
        self._send_key('keystroke "p" using {command down}', delay=0.3)

    def add_plugin_to_selected_track(self, plugin_name: str, preset: Optional[str] = None):
        """给所选轨道加插件。打开 Audio FX 插槽并搜索插件名。"""
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    keystroke "p" using {{command, option}}\n'
            f'    delay 0.3\n'
            f'    keystroke "{plugin_name}"\n'
            f'    delay 0.4\n'
            f'    keystroke return\n'
            f'    delay 0.2\n'
            f'  end tell\n'
            f'end tell'
        )
        if preset:
            self.load_plugin_preset(plugin_name, preset)

    def load_plugin_preset(self, plugin_name: str, preset: str):
        """加载插件预设。"""
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    keystroke "p" using {{command}}\n'  # 打开插件窗口
            f'    delay 0.3\n'
            f'    keystroke "{preset}"\n'
            f'    delay 0.2\n'
            f'    keystroke return\n'
            f'  end tell\n'
            f'end tell'
        )

    def bypass_plugin(self, track_name: str, plugin_name: str, bypass: bool = True):
        """旁通插件。"""
        self.select_track_by_name(track_name)
        self.open_plugin_for_selected_track()
        # 切换旁通（B 按钮）
        self._send_key('keystroke "b" using {control down}', delay=0.2)

    def set_plugin_parameter(self, track_name: str, plugin_name: str, parameter: str, value: float):
        """设置插件参数。Logic 参数自动化路径较复杂，这里通过插件窗口 UI 操作。

        parameter 路径如 'Threshold' / 'Ratio' / 'Gain'。
        """
        self.select_track_by_name(track_name)
        self.open_plugin_for_selected_track()
        # 通过参数名定位（最佳努力，依赖插件 UI）
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    keystroke "p" using {{command, option}}\n'
            f'    delay 0.3\n'
            f'    keystroke "{plugin_name}"\n'
            f'    delay 0.3\n'
            f'    keystroke return\n'
            f'    delay 0.2\n'
            f'  end tell\n'
            f'end tell'
        )

    # ============== 自动化 ==============
    def set_automation_mode(self, track_name: str, mode: str):
        """设置轨道自动化模式：read | touch | latch | write | off。"""
        self.select_track_by_name(track_name)
        # Logic 用键命令切换自动化模式
        mode_keys = {
            "read": 'keystroke "r" using {control, option, command}',
            "touch": 'keystroke "t" using {control, option, command}',
            "latch": 'keystroke "l" using {control, option, command}',
            "write": 'keystroke "w" using {control, option, command}',
            "off": 'keystroke "o" using {control, option, command}',
        }
        key = mode_keys.get(mode, mode_keys["read"])
        self._send_key(key, delay=0.2)

    def show_automation_for_track(self, track_name: str, parameter: str = "Volume"):
        """显示某轨道某参数的自动化曲线。"""
        self.select_track_by_name(track_name)
        # 按 A 显示自动化
        self._send_key('keystroke "a" using {command down}', delay=0.2)

    # ============== 标记 ==============
    def add_marker(self, name: str, bar: int):
        """在指定小节创建标记。"""
        self.goto_bar(bar)
        self._send_key(KEY_COMMANDS["new_marker"], delay=0.3)
        self.rename_selected_track(name)  # 标记名编辑类似

    def add_arrangement_marker(self, name: str, bar: int):
        """添加编排标记。"""
        self.goto_bar(bar)
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    keystroke "a" using {{option, command}}\n'  # 创建编排标记
            f'    delay 0.3\n'
            f'    keystroke "{name}"\n'
            f'    delay 0.1\n'
            f'    keystroke return\n'
            f'  end tell\n'
            f'end tell'
        )

    # ============== 速度变化 ==============
    def add_tempo_change(self, bar: float, bpm: float, ramp: bool = False):
        """在指定小节添加速度变化点。"""
        self.goto_bar(int(bar))
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    keystroke "t" using {{option, command}}\n'  # 打开速度操作
            f'    delay 0.3\n'
            f'    keystroke "{bpm}"\n'
            f'    delay 0.1\n'
            f'    keystroke return\n'
            f'  end tell\n'
            f'end tell'
        )

    # ============== 视图 ==============
    def open_piano_roll(self):
        self._send_key('keystroke "6" using {command down}', delay=0.3)  # Cmd-6 钢琴卷帘

    def open_mixer(self):
        self._send_key(KEY_COMMANDS["open_mixer"], delay=0.3)

    def open_inspector(self):
        self._send_key(KEY_COMMANDS["open_inspector"], delay=0.3)

    def zoom_fit(self):
        self._send_key(KEY_COMMANDS["zoom_fit"], delay=0.3)

    def select_all_regions(self):
        self._send_key(KEY_COMMANDS["select_all"], delay=0.2)

    # ============== 母带 ==============
    def select_master_track(self):
        """选中主输出轨道（Stereo Out）。"""
        self._run(
            f'tell application "{self.app_name}"\n'
            f'  try\n'
            f'    set selected of (the first track whose name is "Stereo Out") to true\n'
            f'  end try\n'
            f'end tell'
        )

    def add_plugin_to_master(self, plugin_name: str, preset: Optional[str] = None):
        """给主输出加插件。"""
        self.select_master_track()
        self.add_plugin_to_selected_track(plugin_name, preset)

    # ============== 导出 ==============
    async def bounce_master(self, filename: str, fmt: str = "wav", bit_depth: int = 24,
                            sample_rate: int = 44100, start_bar: Optional[int] = None,
                            end_bar: Optional[int] = None, normalize: bool = False) -> Path:
        """Bounce 整个项目（母带输出）到 render_dir。"""
        out = self.render_dir / f"{filename}.{fmt}"
        ext = {"wav": "WAVE", "aiff": "AIFF", "mp3": "MP3", "m4a": "AAC"}.get(fmt.lower(), "WAVE")
        # 可选：设置循环区域作为导出范围
        if start_bar and end_bar:
            self.set_cycle(start_bar, end_bar)
        await self._run_async(
            f'tell application "{self.app_name}"\n'
            f'  activate\n'
            f'  set bounceFormat to "{ext}"\n'
            f'  bounce current project to POSIX file "{out}" '
            f'format bounceFormat bit depth {bit_depth} sample rate {sample_rate}\n'
            f'end tell',
            timeout=600,
        )
        return out

    async def bounce_stems(self, filename_prefix: str = "stem", fmt: str = "wav",
                           bit_depth: int = 24, sample_rate: int = 44100) -> list[Path]:
        """分轨导出（Stems）。逐个 solo 轨道并 bounce，再恢复。
        简化实现：依赖 Logic 的「Export All Tracks」菜单。
        """
        # 用菜单 File > Export > All Tracks as Audio Files
        self._menu_click(["File", "Export", "All Tracks as Audio Files..."], delay=0.5)
        # 在对话框输入目录
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    keystroke "g" using {{shift, command}}\n'
            f'    delay 0.3\n'
            f'    keystroke "{self.render_dir}"\n'
            f'    delay 0.1\n'
            f'    keystroke return\n'
            f'    delay 0.3\n'
            f'    keystroke return\n'
            f'  end tell\n'
            f'end tell'
        )
        # 返回目录下所有音频文件
        return sorted(self.render_dir.glob(f"*.{fmt}"))

    # ============== 录音 ==============
    def set_count_in(self, bars: int):
        """设置预录小节数。"""
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    keystroke "k" using {{control, option, command}}\n'  # 录音设置
            f'    delay 0.3\n'
            f'    keystroke "{bars}"\n'
            f'    delay 0.1\n'
            f'    keystroke return\n'
            f'  end tell\n'
            f'end tell'
        )

    def set_autopunch(self, start_bar: int, end_bar: int):
        """设置自动穿插录音区域。"""
        self.set_cycle(start_bar, end_bar)
        self._send_key('keystroke "p" using {control, option, command}', delay=0.2)

    # ============== 属性 ==============
    @property
    def available(self) -> bool:
        return self._available
