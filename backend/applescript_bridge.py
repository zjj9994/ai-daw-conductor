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
# 这些是 Logic Pro X 默认键命令；若用户自定义过，可在配置里覆盖。
# 注意：Logic Pro 的修饰键语法是 {command down, option down} 等，每个都要带 down。
KEY_COMMANDS = {
    "play": "key code 49",           # Space
    "stop": "key code 49",           # Space（再按一次停止）
    "record": "key code 13",         # R（Logic 默认 R 录音）
    "pause": "key code 49",          # Space
    "goto_start": "key code 115",    # Home（回到开头）
    "goto_end": "key code 119",      # End
    "rewind": "key code 123",        # 左箭头
    "forward": "key code 124",       # 右箭头
    "toggle_loop": "key code 8",     # C（Logic 默认 C 切换 Cycle/循环区域，Cmd-L 是 Loop Browser！）
    "toggle_metronome": 'keystroke "p" using {control down}',  # Ctrl-P 节拍器（Cmd-K 是 Key Commands 编辑器！）
    "new_track": 'keystroke "n" using {option down, command down}',  # Opt-Cmd-N 新建轨道
    "new_project": 'keystroke "n" using {shift down, control down, option down, command down}',  # Shift-Ctrl-Opt-Cmd-N 新建工程（避免与 new_track 冲突）
    "save": 'keystroke "s" using {command down}',
    "save_as": 'keystroke "s" using {shift down, command down}',
    "open": 'keystroke "o" using {command down}',
    "close": 'keystroke "w" using {command down}',
    "undo": 'keystroke "z" using {command down}',
    "redo": 'keystroke "z" using {shift down, command down}',
    "split_at_playhead": 'keystroke "\\" using {command down}',  # Cmd-\ 切割
    "join_regions": 'keystroke "j" using {command down}',  # Cmd-J 合并（不是 Cmd-Opt-Shift-J）
    "delete_regions": "key code 51",  # Delete
    "duplicate": 'keystroke "d" using {command down}',  # Cmd-D 重复
    "copy": 'keystroke "c" using {command down}',
    "paste": 'keystroke "v" using {command down}',
    "cut": 'keystroke "x" using {command down}',
    "select_all": 'keystroke "a" using {command down}',
    "quantize": 'keystroke "q" using {command down}',  # Q 量化（Logic 里 Q 是量化，Cmd-Q 才是退出）
    "quantize_strong": 'keystroke "q" using {command down, option down}',
    "transpose_up": 'keystroke "]" using {command down, option down}',  # 升半音
    "transpose_down": 'keystroke "[" using {command down, option down}',  # 降半音
    "octave_up": 'keystroke "]" using {command down, option down, shift down}',
    "octave_down": 'keystroke "[" using {command down, option down, shift down}',
    "open_piano_roll": "key code 22",  # 数字键 6（Logic 默认用 6 打开钢琴卷帘，不是 Cmd-P）
    "open_mixer": 'keystroke "x"',  # X（无修饰！Cmd-X 是剪切，会删片段！）
    "open_inspector": 'keystroke "i"',  # I（无修饰！Cmd-I 是 Import）
    "zoom_fit": 'keystroke "z" using {control down, option down, command down}',
    "freeze_track": 'keystroke "f" using {control down}',
    "toggle_track_hide": 'keystroke "h" using {control down}',  # Ctrl-H（不是 Ctrl-Shift-H）
    "arm_track": 'keystroke "r" using {control down}',  # Ctrl-R arm
    "mute_track": 'keystroke "m" using {control down}',  # Ctrl-M mute（Logic 里 M 也能 mute 选中轨道）
    "solo_track": 'keystroke "s" using {control down}',  # Ctrl-S solo（Logic 里 S 也能 solo）
    "create_track_stack": "",  # 无默认快捷键，走菜单 Track > Create Track Stack...
    "collapse_all": 'keystroke "c" using {control down, option down, command down}',
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
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return stdout.decode().strip()
        except asyncio.TimeoutError:
            # 超时时显式 kill 子进程，避免僵尸 osascript 继续在后台跑
            if proc:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            return ""
        except asyncio.CancelledError:
            # 取消时也要 kill 子进程
            if proc:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            raise
        except Exception:
            return ""

    # ---------- 弹窗处理 ----------
    # Logic Pro 在多个关键路径会弹模态对话框（保存/打开/导入/导出/录音/插件缺失等），
    # 一旦弹窗挡住主窗口，后续键命令会落到弹窗上而非 Logic Pro 主窗口，导致 AI 完全失控。
    # 修复策略：每次发键命令前自动 dismiss 残留弹窗；关键路径(save_as/bounce)主动点确认。
    def has_dialog(self) -> bool:
        """检测 Logic Pro 当前是否有任何模态弹窗挡住主窗口。"""
        if not self._available:
            return False
        # 检测两类弹窗：1)挂在 front window 的 sheet；2)独立的 AXDialog 窗口
        out = self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set sheetCount to 0\n'
            f'    set dlgCount to 0\n'
            f'    try\n'
            f'      set sheetCount to count of sheets of front window\n'
            f'    end try\n'
            f'    try\n'
            f'      set dlgCount to count of (every window whose subrole is "AXDialog")\n'
            f'    end try\n'
            f'    return (sheetCount + dlgCount) > 0\n'
            f'  end tell\n'
            f'end tell'
        )
        return out.strip().lower() == "true"

    def dismiss_dialogs(self, action: str = "cancel", max_count: int = 5) -> int:
        """关闭 Logic Pro 当前所有弹窗，返回成功关闭的弹窗数量。

        action:
          - "cancel"  : 优先点 Cancel/Don't Save（保守，避免误覆盖；用于键命令前置清场）
          - "confirm" : 优先点 OK/Replace/Save 默认按钮（用于 save_as/bounce 等需确认覆盖的场景）
          - "ok"      : 只点 OK 按钮（用于启动时的插件缺失等警告）
        max_count: 最多关闭多少个弹窗，防止误关用户后续主动打开的对话框。
        """
        if not self._available:
            return 0
        # 按优先级尝试点击的按钮文本（action 决定顺序）
        if action == "confirm":
            btn_prefs = ["Replace", "OK", "Save", "Yes", "确认", "替换", "保存", "是"]
        elif action == "ok":
            btn_prefs = ["OK", "确定", "Continue", "继续"]
        else:  # cancel
            btn_prefs = ["Cancel", "Don't Save", "取消", "不保存", "No", "否"]
        btn_list = "{" + ", ".join(f'"{b}"' for b in btn_prefs) + "}"
        closed = 0
        for _ in range(max_count):
            # 每轮关一个弹窗（先 sheet 后 dialog），直到没有弹窗或达到上限
            out = self._run(
                f'tell application "System Events"\n'
                f'  tell process "{self.app_name}"\n'
                f'    set acted to false\n'
                f'    -- 1. 先处理 front window 的 sheet\n'
                f'    try\n'
                f'      if (count of sheets of front window) > 0 then\n'
                f'        set theSheet to sheet 1 of front window\n'
                f'        repeat with btnName in {btn_list}\n'
                f'          try\n'
                f'            click button btnName of theSheet\n'
                f'            set acted to true\n'
                f'            exit repeat\n'
                f'          end try\n'
                f'        end repeat\n'
                f'        -- 兜底：点 sheet 第 1 个按钮（通常是默认/确认）\n'
                f'        if not acted then\n'
                f'          try\n'
                f'            click button 1 of theSheet\n'
                f'            set acted to true\n'
                f'          end try\n'
                f'        end if\n'
                f'      end if\n'
                f'    end try\n'
                f'    -- 2. 处理独立 AXDialog 窗口\n'
                f'    if not acted then\n'
                f'      try\n'
                f'        set dlgWindows to (every window whose subrole is "AXDialog")\n'
                f'        if (count of dlgWindows) > 0 then\n'
                f'          set theDlg to item 1 of dlgWindows\n'
                f'          repeat with btnName in {btn_list}\n'
                f'            try\n'
                f'              click button btnName of theDlg\n'
                f'              set acted to true\n'
                f'              exit repeat\n'
                f'            end try\n'
                f'          end repeat\n'
                f'          if not acted then\n'
                f'            try\n'
                f'              click button 1 of theDlg\n'
                f'              set acted to true\n'
                f'            end try\n'
                f'          end if\n'
                f'        end if\n'
                f'      end try\n'
                f'    end if\n'
                f'    if acted then\n'
                f'      delay 0.3\n'
                f'      return "1"\n'
                f'    else\n'
                f'      return "0"\n'
                f'    end if\n'
                f'  end tell\n'
                f'end tell'
            )
            if out.strip() == "1":
                closed += 1
            else:
                break
        return closed

    def _send_key(self, key_expr: str, delay: float = 0.3, auto_dismiss: bool = False):
        """通过 System Events 给 Logic Pro 发送键命令。

        auto_dismiss 默认 False——因为很多操作是多步序列（如 new_track→return），
        第二步 return 之前不能 dismiss，否则会把刚打开的 New Tracks 对话框关掉。
        只在明确的「入口」（activate、save_as 前、bounce 前）才显式调 dismiss。
        """
        if auto_dismiss:
            self.dismiss_dialogs(action="cancel", max_count=3)
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    {key_expr}\n'
            f'    delay {delay}\n'
            f'  end tell\n'
            f'end tell'
        )

    def _menu_click(self, menu_path: list[str], delay: float = 0.3, auto_dismiss: bool = False):
        """点击菜单项，menu_path 如 ['File', 'Save As...']。

        auto_dismiss 默认 False，原因同 _send_key。
        """
        if auto_dismiss:
            self.dismiss_dialogs(action="cancel", max_count=3)
        # 递归构造菜单路径，支持任意层级（File > Export > All Tracks as Audio Files…）
        # 从最内层往外拼：click menu item "X" of menu 1 of menu item "Y" of menu 1 of menu bar item "Z"
        # menu_path = [menu_bar_item, level1, level2, ..., leaf]
        if len(menu_path) < 2:
            return
        path_expr = f'menu bar item "{menu_path[0]}" of menu bar 1'
        for item in menu_path[1:-1]:
            path_expr = f'menu item "{item}" of menu 1 of {path_expr}'
        leaf = menu_path[-1]
        # 省略号兼容：Logic Pro 用 Unicode …，但用户/AI 可能传 ASCII ...
        # AppleScript 精确匹配字符串，这里尝试精确名 + 模糊匹配（包含 leaf 去掉省略号后的关键词）
        leaf_plain = leaf.rstrip('.').rstrip('…').strip()
        script = (
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    try\n'
            f'      click menu item "{leaf}" of menu 1 of {path_expr}\n'
            f'    on error\n'
            f'      -- 兜底：遍历菜单项找包含关键词的（兼容 …/... 和本地化文案）\n'
            f'      set targetMenu to menu 1 of {path_expr}\n'
            f'      repeat with mi in menu items of targetMenu\n'
            f'        set miName to name of mi\n'
            f'        if miName contains "{leaf_plain}" then\n'
            f'          click mi\n'
            f'          exit repeat\n'
            f'        end if\n'
            f'      end repeat\n'
            f'    end try\n'
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
        # 启动/激活时清场：关掉 Logic Pro 启动时常弹的「Missing Audio Units」
        # 「无音频设备」「Core Audio 未就绪」等警告，用 ok 模式点确定。
        self.dismiss_dialogs(action="ok", max_count=5)

    # ============== 工程 ==============
    def new_project(self, name: str = "AI Project", bpm: float = 100, key: str = "C"):
        """新建空项目。"""
        self.activate()
        self._send_key(KEY_COMMANDS["new_project"], delay=0.8)
        self._send_key("keystroke return", delay=0.5)  # 选默认模板回车
        self.set_tempo(bpm)

    def set_tempo(self, bpm: float):
        """设置项目速度。

        优先用 AppleScript 字典直接设 tempo（最可靠），失败则走菜单
        Edit > Tempo > Set Tempo... 打开对话框输入。
        """
        # 方案1：直接用 AppleScript 字典（Logic Pro X 支持 set tempo）
        out = self._run(
            f'tell application "{self.app_name}"\n'
            f'  try\n'
            f'    set tempo to {bpm}\n'
            f'    return "ok"\n'
            f'  on error\n'
            f'    return "fallback"\n'
            f'  end try\n'
            f'end tell'
        )
        if out.strip() == "ok":
            return
        # 方案2：走菜单 Edit > Tempo > Set Tempo...
        self._menu_click(["Edit", "Tempo", "Set Tempo..."], delay=0.4)
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    delay 0.2\n'
            f'    keystroke "{bpm}"\n'
            f'    delay 0.1\n'
            f'    keystroke return\n'
            f'  end tell\n'
            f'end tell'
        )

    def set_time_signature(self, numerator: int = 4, denominator: int = 4):
        """设置拍号。

        用 AppleScript 字典直接设 time signature（Logic Pro X 支持）。
        """
        self._run(
            f'tell application "{self.app_name}"\n'
            f'  try\n'
            f'    set time signature of tempo track to {numerator}/{denominator}\n'
            f'  end try\n'
            f'end tell'
        )

    def save_project(self):
        self._send_key(KEY_COMMANDS["save"], delay=0.5)

    def save_as(self, path: str):
        """另存为。path 为 .logicx 工程文件路径。

        若文件已存在，Logic Pro 会弹「是否替换」对话框——这里主动点 Replace 确认覆盖。
        """
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
        # 若弹出「文件已存在，是否替换」对话框，主动点 Replace 确认覆盖
        self.dismiss_dialogs(action="confirm", max_count=2)

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
        """定位播放头到指定小节拍。

        优先用 AppleScript 字典直接 set playhead（最可靠），失败则走菜单
        Edit > Go To > Position... 打开定位对话框输入 "bar beat"。
        """
        # 方案1：AppleScript 字典直接定位（Logic Pro X 支持）
        out = self._run(
            f'tell application "{self.app_name}"\n'
            f'  try\n'
            f'    set playhead to {bar} {beat}\n'
            f'    return "ok"\n'
            f'  on error\n'
            f'    return "fallback"\n'
            f'  end try\n'
            f'end tell'
        )
        if out.strip() == "ok":
            return
        # 方案2：走菜单 Edit > Go To > Position...
        self._menu_click(["Edit", "Go To", "Position..."], delay=0.4)
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    delay 0.2\n'
            f'    keystroke "{bar} {beat}"\n'
            f'    delay 0.1\n'
            f'    keystroke return\n'
            f'  end tell\n'
            f'end tell'
        )

    def toggle_loop(self):
        self._send_key(KEY_COMMANDS["toggle_loop"], delay=0.2)

    def set_cycle(self, start_bar: int, end_bar: int):
        """设置循环区域。

        优先用 AppleScript 字典设 cycle mode + left/right locator（最可靠），
        失败则走菜单和键命令兜底。
        """
        # 方案1：AppleScript 字典直接设左右定位点
        out = self._run(
            f'tell application "{self.app_name}"\n'
            f'  try\n'
            f'    set left locator to {start_bar} 1\n'
            f'    set right locator to {end_bar} 1\n'
            f'    set cycle mode to true\n'
            f'    return "ok"\n'
            f'  on error\n'
            f'    return "fallback"\n'
            f'  end try\n'
            f'end tell'
        )
        if out.strip() == "ok":
            return
        # 方案2：定位到起点，键命令设左定位点；定位到终点，设右定位点
        self.goto_bar(start_bar)
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    keystroke "[" using {{control, command}}\n'  # Ctrl-Cmd-[ 设左定位点（Logic 默认）
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
        """设置片段循环次数。

        用 Region Inspector 的 Loops 数值框设置循环次数。
        """
        self.select_track_by_name(track_name)
        # 先选中该位置的片段
        self.goto_bar(bar)
        # 用 Region Inspector 设 Loops
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    delay 0.2\n'
            f'    try\n'
            f'      -- Region Inspector 的 Loops 文本框\n'
            f'      set loopsField to first text field of window 1 whose description contains "Loops"\n'
            f'      set focused of loopsField to true\n'
            f'      delay 0.1\n'
            f'      keystroke "{loop_count}"\n'
            f'      delay 0.1\n'
            f'      keystroke return\n'
            f'    end try\n'
            f'  end tell\n'
            f'end tell'
        )

    def resize_region(self, track_name: str, bar: int, new_length_beats: float):
        """调整片段长度。

        用 Region Inspector 的 Length 数值框设置长度（拍）。
        """
        self.select_track_by_name(track_name)
        self.goto_bar(bar)
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    delay 0.2\n'
            f'    try\n'
            f'      -- Region Inspector 的 Length 文本框\n'
            f'      set lenField to first text field of window 1 whose description contains "Length"\n'
            f'      set focused of lenField to true\n'
            f'      delay 0.1\n'
            f'      keystroke "{new_length_beats}"\n'
            f'      delay 0.1\n'
            f'      keystroke return\n'
            f'    end try\n'
            f'  end tell\n'
            f'end tell'
        )

    def crop_region(self, track_name: str, bar: int, end_bar: int):
        """裁剪片段到指定范围（删除范围外的部分）。

        用 Region Inspector 的 Crop 数值框或菜单 Edit > Trim > Crop。
        """
        self.select_track_by_name(track_name)
        self.goto_bar(bar)
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    delay 0.2\n'
            f'    try\n'
            f'      set cropField to first text field of window 1 whose description contains "Crop"\n'
            f'      set focused of cropField to true\n'
            f'      delay 0.1\n'
            f'      keystroke "{end_bar - bar}"\n'
            f'      delay 0.1\n'
            f'      keystroke return\n'
            f'    end try\n'
            f'  end tell\n'
            f'end tell'
        )

    def fade_in_region(self, track_name: str, bar: int, length_beats: float = 2.0):
        """给片段加淡入。用 Region Inspector 的 Fade In 数值框。"""
        self.select_track_by_name(track_name)
        self.goto_bar(bar)
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    delay 0.2\n'
            f'    try\n'
            f'      set fadeField to first text field of window 1 whose description contains "Fade In"\n'
            f'      set focused of fadeField to true\n'
            f'      delay 0.1\n'
            f'      keystroke "{length_beats}"\n'
            f'      delay 0.1\n'
            f'      keystroke return\n'
            f'    end try\n'
            f'  end tell\n'
            f'end tell'
        )

    def fade_out_region(self, track_name: str, bar: int, length_beats: float = 2.0):
        """给片段加淡出。用 Region Inspector 的 Fade Out 数值框。"""
        self.select_track_by_name(track_name)
        self.goto_bar(bar)
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    delay 0.2\n'
            f'    try\n'
            f'      set fadeField to first text field of window 1 whose description contains "Fade Out"\n'
            f'      set focused of fadeField to true\n'
            f'      delay 0.1\n'
            f'      keystroke "{length_beats}"\n'
            f'      delay 0.1\n'
            f'      keystroke return\n'
            f'    end try\n'
            f'  end tell\n'
            f'end tell'
        )

    def crossfade_regions(self, track_name: str, bar: int, length_beats: float = 1.0):
        """交叉淡化两个相邻片段。选中两个片段后用菜单 Edit > Fade > Crossfade。"""
        self.select_track_by_name(track_name)
        self.goto_bar(bar)
        # 选中两个相邻片段（Shift+点击）
        self._send_key("key code 125 using {shift down}", delay=0.2)  # Shift+下箭头扩展选区
        # 菜单 Edit > Fade > Crossfade
        self._menu_click(["Edit", "Fade", "Crossfade"], delay=0.3)

    # ============== MIDI 编辑（钢琴卷帘） ==============
    def quantize_selected_regions(self, grid: str = "1/16", strength: int = 100):
        """量化选中片段，指定网格和强度。

        grid: 1/16 | 1/8 | 1/4 | 1/16T | 1/8T 等
        strength: 0-100，100=完全量化，<100=部分量化（人性化）
        """
        # 方案1：用 Region Inspector 的 Quantize 下拉框和 Q-Strength
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    delay 0.2\n'
            f'    try\n'
            f'      -- Quantize 下拉框\n'
            f'      set qMenu to first pop up button of window 1 whose description contains "Quantize"\n'
            f'      click qMenu\n'
            f'      delay 0.2\n'
            f'      -- 尝试精确匹配\n'
            f'      try\n'
            f'        click menu item "{grid}" of menu 1 of qMenu\n'
            f'      on error\n'
            f'        -- 模糊匹配\n'
            f'        repeat with mi in menu items of menu 1 of qMenu\n'
            f'          if name of mi contains "{grid}" then\n'
            f'            click mi\n'
            f'            exit repeat\n'
            f'          end if\n'
            f'        end repeat\n'
            f'      end try\n'
            f'      delay 0.1\n'
            f'      -- Q-Strength 滑块（如果 strength < 100）\n'
            f'      if {strength} < 100 then\n'
            f'        try\n'
            f'          set qStrSlider to first slider of window 1 whose description contains "Strength"\n'
            f'          set value of qStrSlider to {strength}\n'
            f'        end try\n'
            f'      end if\n'
            f'    on error\n'
            f'      -- 兜底：键命令 Cmd-Q 量化（用上次设的网格）\n'
            f'      keystroke "q" using {{command down}}\n'
            f'    end try\n'
            f'  end tell\n'
            f'end tell'
        )

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
        """设置插件参数（像人类拧 Threshold/Ratio/Gain 旋钮）。

        通过 AX accessibility 遍历插件窗口 UI 元素，按参数名匹配并设置值。
        """
        self.select_track_by_name(track_name)
        # 转义 parameter 中的特殊字符（双引号、反斜杠）
        param_esc = parameter.replace('\\', '\\\\').replace('"', '\\"')
        # 方案1：AppleScript 字典直接打开插件窗口
        opened = self._run(
            f'tell application "{self.app_name}"\n'
            f'  try\n'
            f'    open plugin editor of (first track whose name is "{track_name}") plugin 1\n'
            f'    return "ok"\n'
            f'  on error\n'
            f'    return "fallback"\n'
            f'  end try\n'
            f'end tell'
        )
        if opened.strip() != "ok":
            # 兜底：键命令打开插件编辑器
            self._send_key('keystroke "e" using {command down}', delay=0.5)
        # 用 AX 遍历找参数并设置
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    delay 0.3\n'
            f'    -- 遍历插件窗口的 UI 元素找参数\n'
            f'    set pluginWindow to window 1\n'
            f'    set paramSet to false\n'
            f'    try\n'
            f'      -- 优先找 AXSlider（旋钮通常是 slider）\n'
            f'      repeat with elem in UI elements of pluginWindow\n'
            f'        try\n'
            f'          set elemDesc to description of elem\n'
            f'          set elemTitle to title of elem\n'
            f'          if elemDesc contains "{param_esc}" or elemTitle contains "{param_esc}" then\n'
            f'            set value of elem to {value}\n'
            f'            set paramSet to true\n'
            f'            exit repeat\n'
            f'          end if\n'
            f'        end try\n'
            f'      end repeat\n'
            f'    end try\n'
            f'    -- 兜底：找 AXTextField（数值输入框）\n'
            f'    if not paramSet then\n'
            f'      try\n'
            f'        repeat with elem in UI elements of pluginWindow\n'
            f'          try\n'
            f'            set elemDesc to description of elem\n'
            f'            if elemDesc contains "{param_esc}" then\n'
            f'              set focused of elem to true\n'
            f'              delay 0.1\n'
            f'              keystroke "{value}"\n'
            f'              delay 0.1\n'
            f'              keystroke return\n'
            f'              set paramSet to true\n'
            f'              exit repeat\n'
            f'            end if\n'
            f'          end try\n'
            f'        end repeat\n'
            f'      end try\n'
            f'    end if\n'
            f'    -- 关闭插件窗口\n'
            f'    delay 0.2\n'
            f'    keystroke "w" using {{command down}}\n'
            f'  end tell\n'
            f'end tell'
        )

    def set_channel_eq_params(self, track_name: str, eq_bands: list):
        """设置 Channel EQ 多段参数。

        eq_bands: [{band: 1, freq: 200, gain: -3, q: 1.0}, ...]
        band 1-8 对应 Logic Channel EQ 的 8 个频段。
        """
        self.select_track_by_name(track_name)
        # 打开 Channel EQ（假设已挂载，打开插件窗口）
        self._run(
            f'tell application "{self.app_name}"\n'
            f'  try\n'
            f'    open plugin editor of (first track whose name is "{track_name}") plugin 1\n'
            f'  end try\n'
            f'end tell'
        )
        # 对每个频段设置参数
        for band in eq_bands:
            band_num = band.get("band", 1)
            freq = band.get("freq", 1000)
            gain = band.get("gain", 0)
            q = band.get("q", 1.0)
            # 用 AX 遍历找频段的 Freq/Gain/Q 文本框并设置
            self._run(
                f'tell application "System Events"\n'
                f'  tell process "{self.app_name}"\n'
                f'    set frontmost to true\n'
                f'    delay 0.2\n'
                f'    try\n'
                f'      -- 频段 {band_num} 的 Freq/Gain/Q 通常有对应的 AXTextField\n'
                f'      -- 用 description 或 title 匹配 "Band {band_num} Frequency" 等\n'
                f'      set bandElems to every UI element of window 1 whose description contains "Band {band_num}"\n'
                f'      repeat with elem in bandElems\n'
                f'        try\n'
                f'          set elemDesc to description of elem\n'
                f'          if elemDesc contains "Frequency" then\n'
                f'            set focused of elem to true\n'
                f'            keystroke "{freq}"\n'
                f'            keystroke return\n'
                f'          else if elemDesc contains "Gain" then\n'
                f'            set focused of elem to true\n'
                f'            keystroke "{gain}"\n'
                f'            keystroke return\n'
                f'          else if elemDesc contains "Q" then\n'
                f'            set focused of elem to true\n'
                f'            keystroke "{q}"\n'
                f'            keystroke return\n'
                f'          end if\n'
                f'        end try\n'
                f'      end repeat\n'
                f'    end try\n'
                f'  end tell\n'
                f'end tell'
            )
        # 关闭插件窗口
        self._send_key('keystroke "w" using {command down}', delay=0.2)

    def set_sidechain(self, track_name: str, plugin_name: str, source_track: str):
        """设置压缩器的侧链源（如底鼓侧链压缩贝斯）。

        在 Compressor 插件窗口的 Side Chain 下拉选源轨道。
        """
        self.select_track_by_name(track_name)
        # 打开插件窗口
        self._run(
            f'tell application "{self.app_name}"\n'
            f'  try\n'
            f'    open plugin editor of (first track whose name is "{track_name}") plugin 1\n'
            f'  end try\n'
            f'end tell'
        )
        # 在插件窗口找 Side Chain 下拉菜单并选源轨道
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    delay 0.3\n'
            f'    try\n'
            f'      -- 找 Side Chain 弹出菜单\n'
            f'      set sideChainMenu to first pop up button of window 1 whose description contains "Side Chain"\n'
            f'      click sideChainMenu\n'
            f'      delay 0.2\n'
            f'      -- 选源轨道\n'
            f'      click menu item "{source_track}" of menu 1 of sideChainMenu\n'
            f'    end try\n'
            f'    delay 0.2\n'
            f'    keystroke "w" using {{command down}}\n'
            f'  end tell\n'
            f'end tell'
        )

    def set_stereo_width(self, track_name: str, width: float):
        """设置立体声宽度（0=单声道, 1=原宽, 2=超宽）。

        通过挂载 Direction Mixer 插件并设 Width 参数。
        """
        self.select_track_by_name(track_name)
        # Direction Mixer 是 Logic 自带插件，直接挂载并设参数
        # 假设已挂载，打开插件窗口设 Width
        self._run(
            f'tell application "{self.app_name}"\n'
            f'  try\n'
            f'    open plugin editor of (first track whose name is "{track_name}") plugin 1\n'
            f'  end try\n'
            f'end tell'
        )
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    delay 0.3\n'
            f'    try\n'
            f'      repeat with elem in UI elements of window 1\n'
            f'        try\n'
            f'          set elemDesc to description of elem\n'
            f'          if elemDesc contains "Width" then\n'
            f'            set value of elem to {width}\n'
            f'            exit repeat\n'
            f'          end if\n'
            f'        end try\n'
            f'      end repeat\n'
            f'    end try\n'
            f'    delay 0.2\n'
            f'    keystroke "w" using {{command down}}\n'
            f'  end tell\n'
            f'end tell'
        )

    # ============== 自动化 ==============
    def set_automation_mode(self, track_name: str, mode: str):
        """设置轨道自动化模式：read | touch | latch | write | off。

        用 AX accessibility 点击轨道头的 Automation Mode 按钮并选模式。
        """
        self.select_track_by_name(track_name)
        mode_label = {
            "read": "Read", "touch": "Touch", "latch": "Latch",
            "write": "Write", "off": "Off",
        }.get(mode.lower(), "Read")
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    delay 0.2\n'
            f'    try\n'
            f'      -- 找轨道头的 Automation Mode 按钮（通常是 pop up button）\n'
            f'      set trackHeader to first track whose name is "{track_name}"\n'
            f'      set autoBtn to first pop up button of trackHeader whose description contains "Automation"\n'
            f'      click autoBtn\n'
            f'      delay 0.2\n'
            f'      click menu item "{mode_label}" of menu 1 of autoBtn\n'
            f'    end try\n'
            f'  end tell\n'
            f'end tell'
        )

    def show_automation_for_track(self, track_name: str, parameter: str = "Volume"):
        """显示某轨道某参数的自动化曲线。

        parameter: Volume | Pan | Send1 | Send2 | Plugin:插件名:参数名 等。
        """
        self.select_track_by_name(track_name)
        # 按 A 显示自动化 lane
        self._send_key('keystroke "a" using {command down}', delay=0.3)
        # 用 AX 在自动化 lane 的参数下拉菜单选具体参数
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    delay 0.2\n'
            f'    try\n'
            f'      -- 自动化 lane 的参数选择菜单\n'
            f'      set paramMenu to first pop up button of window 1 whose description contains "Automation Parameter"\n'
            f'      click paramMenu\n'
            f'      delay 0.2\n'
            f'      -- 尝试精确匹配菜单项\n'
            f'      try\n'
            f'        click menu item "{parameter}" of menu 1 of paramMenu\n'
            f'      on error\n'
            f'        -- 模糊匹配：遍历菜单项找包含关键词的\n'
            f'        set paramPlain to "{parameter}"\n'
            f'        repeat with mi in menu items of menu 1 of paramMenu\n'
            f'          if name of mi contains paramPlain then\n'
            f'            click mi\n'
            f'            exit repeat\n'
            f'          end if\n'
            f'        end repeat\n'
            f'      end try\n'
            f'    end try\n'
            f'  end tell\n'
            f'end tell'
        )

    def add_automation_points(self, track_name: str, parameter: str, points: list):
        """在自动化曲线上添加节点（像人类用铅笔在自动化 lane 上点节点）。

        points: [{bar: 1.0, value: 0.0, shape: "linear"}, ...]
        bar 是小节位置（可带小数），value 是参数值，shape 是曲线形状。

        实现策略：
        1. 选中轨道 + 显示该参数的自动化 lane
        2. 定位播放头到每个 bar 位置（goto_bar）
        3. 用键命令 Cmd-Ctrl-Opt-A 创建自动化节点（Logic 默认）
        4. 用上下箭头调整节点值（最佳努力，精确值需要 AX 操作）
        """
        self.show_automation_for_track(track_name, parameter)
        for pt in points:
            bar = pt.get("bar", 1.0)
            value = pt.get("value", 0.0)
            # 定位播放头到节点位置
            self.goto_bar(int(bar), (bar - int(bar)) * 4 + 1)
            # 创建自动化节点（键命令，Logic 默认无单键，用菜单 Edit > Create Automation Point）
            self._menu_click(["Edit", "Create Automation Point"], delay=0.2)
            # 最佳努力：用 AX 设置节点值
            self._run(
                f'tell application "System Events"\n'
                f'  tell process "{self.app_name}"\n'
                f'    set frontmost to true\n'
                f'    delay 0.1\n'
                f'    try\n'
                f'      -- 选中的自动化节点通常有 AXSlider 可调值\n'
                f'      set autoPt to first slider of window 1 whose selected is true\n'
                f'      set value of autoPt to {value}\n'
                f'    end try\n'
                f'  end tell\n'
                f'end tell'
            )

    def set_automation_curve(self, track_name: str, parameter: str, points: list):
        """批量写入自动化曲线（像人类用铅笔连续点节点画曲线）。

        比 add_automation_points 更高效：先清空已有节点，再一次性写入所有节点。
        """
        self.show_automation_for_track(track_name, parameter)
        # 先选中所有已有节点并删除
        self._send_key('keystroke "a" using {command down}', delay=0.1)
        self._send_key("key code 51", delay=0.1)  # Delete
        # 逐个创建节点
        self.add_automation_points(track_name, parameter, points)

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

    def open_smart_controls(self):
        """打开智能控制窗口（Cmd-7）。

        Smart Controls 是 Logic X 推荐的快速调音色路径，可映射多个插件参数到旋钮。
        """
        self._send_key('keystroke "7" using {command down}', delay=0.3)

    def open_score_editor(self):
        """打开乐谱编辑器（Cmd-8）。"""
        self._send_key('keystroke "8" using {command down}', delay=0.3)

    def open_step_editor(self):
        """打开步进编辑器（Cmd-9 或菜单 View > Show Step Editor）。"""
        # 先尝试键命令
        opened = self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    try\n'
            f'      keystroke "9" using {{command down}}\n'
            f'      delay 0.3\n'
            f'      return "ok"\n'
            f'    on error\n'
            f'      return "fallback"\n'
            f'    end try\n'
            f'  end tell\n'
            f'end tell'
        )
        if opened.strip() != "ok":
            # 兜底：菜单 View > Show Step Editor
            self._menu_click(["View", "Show Step Editor"], delay=0.3)

    def select_tool(self, tool_name: str):
        """切换工具（像人类按 Esc 后选工具）。

        tool_name: pencil | scissors | eraser | text | zoom | solo | mute | fade
        Logic 工具栏：按 Esc 打开，然后按对应字母。
        """
        tool_keys = {
            "pencil": "p",       # 画笔
            "scissors": "x",     # 剪刀（Logic 默认 X 是混音器，需先 Esc）
            "eraser": "e",       # 橡皮
            "text": "t",         # 文字
            "zoom": "z",         # 放大镜
            "solo": "s",         # 独奏
            "mute": "m",         # 静音
            "fade": "f",         # 渐变
        }
        key = tool_keys.get(tool_name.lower(), "p")
        # 先按 Esc 打开工具栏（如果当前不在工具选择模式）
        self._send_key("key code 53", delay=0.1)  # Esc
        # 然后按字母选工具
        self._send_key(f'keystroke "{key}"', delay=0.2)

    def set_project_audio_settings(self, sample_rate: int = 44100, bit_depth: int = 24):
        """设置工程音频参数（采样率/位深）。

        通过 File > Project Settings > Audio 菜单打开设置对话框。
        """
        self._menu_click(["File", "Project Settings", "Audio..."], delay=0.5)
        # 在设置对话框设采样率
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    delay 0.3\n'
            f'    try\n'
            f'      -- 找采样率下拉框\n'
            f'      set srMenu to first pop up button of window 1 whose description contains "Sample Rate"\n'
            f'      click srMenu\n'
            f'      delay 0.2\n'
            f'      click menu item "{sample_rate}" of menu 1 of srMenu\n'
            f'    end try\n'
            f'    try\n'
            f'      -- 找位深下拉框\n'
            f'      set bdMenu to first pop up button of window 1 whose description contains "Bit Depth"\n'
            f'      click bdMenu\n'
            f'      delay 0.2\n'
            f'      click menu item "{bit_depth}" of menu 1 of bdMenu\n'
            f'    end try\n'
            f'    delay 0.1\n'
            f'    keystroke return\n'  # 确认
            f'  end tell\n'
            f'end tell'
        )

    def set_master_target(self, target_lufs: float = -14.0, true_peak_ceiling: float = -1.0,
                          lra_target: Optional[float] = None, stereo_width: Optional[float] = None):
        """设置母带目标参数（LUFS/真峰/动态范围/立体声宽度）。

        假设已挂载 Limiter，打开插件窗口设 Ceiling = true_peak_ceiling。
        target_lufs 用于 AI 反馈迭代（通过响度表读数），这里只设 Ceiling。
        """
        self.select_master_track()
        # 打开 master 轨道的第一个插件（通常是 Limiter）
        self._run(
            f'tell application "{self.app_name}"\n'
            f'  try\n'
            f'    open plugin editor of (first track whose name is "Stereo Out") plugin 1\n'
            f'  end try\n'
            f'end tell'
        )
        # 用 AX 找 Ceiling 参数并设值
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    delay 0.3\n'
            f'    try\n'
            f'      repeat with elem in UI elements of window 1\n'
            f'        try\n'
            f'          set elemDesc to description of elem\n'
            f'          if elemDesc contains "Ceiling" then\n'
            f'            set value of elem to {true_peak_ceiling}\n'
            f'            exit repeat\n'
            f'          end if\n'
            f'        end try\n'
            f'      end repeat\n'
            f'    end try\n'
            f'    delay 0.2\n'
            f'    keystroke "w" using {{command down}}\n'  # 关闭插件窗口
            f'  end tell\n'
            f'end tell'
        )

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
        """Bounce 整个项目（母带输出）到 render_dir。

        若输出文件已存在，Logic Pro 会弹「是否替换」对话框——这里主动点 Replace 确认覆盖。
        """
        out = self.render_dir / f"{filename}.{fmt}"
        ext = {"wav": "WAVE", "aiff": "AIFF", "mp3": "MP3", "m4a": "AAC"}.get(fmt.lower(), "WAVE")
        # 可选：设置循环区域作为导出范围
        if start_bar and end_bar:
            self.set_cycle(start_bar, end_bar)
        # 导出前先清残留弹窗，防止 bounce 命令落到弹窗上
        self.dismiss_dialogs(action="cancel", max_count=3)
        await self._run_async(
            f'tell application "{self.app_name}"\n'
            f'  activate\n'
            f'  set bounceFormat to "{ext}"\n'
            f'  bounce current project to POSIX file "{out}" '
            f'format bounceFormat bit depth {bit_depth} sample rate {sample_rate}\n'
            f'end tell',
            timeout=600,
        )
        # 若弹出「文件已存在，是否替换」对话框，主动点 Replace 确认覆盖
        self.dismiss_dialogs(action="confirm", max_count=2)
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
        # 若弹「文件已存在/是否替换」对话框，主动点 Replace 确认覆盖
        self.dismiss_dialogs(action="confirm", max_count=5)
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
