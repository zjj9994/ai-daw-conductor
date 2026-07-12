"""AppleScript 桥：通过 osascript 控制 Logic Pro。

负责轨道创建、软件乐器分派、混音器参数、插件、Bounce 等无法用纯 MIDI 完成的操作。
仅在 macOS 可用；非 macOS 环境下所有方法会返回降级信息而不抛异常，便于在 Linux 上做前端联调。

Logic Pro 的脚本能力有限且随版本变化，这里采用「键命令 + MIDI 导入 + AppleScript UI」组合策略，
兼容 Logic Pro 10.7+。关键命令编号可在 Logic Pro > Key Commands 里导出查询。
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .config_loader import is_macos


class AppleScriptBridge:
    def __init__(self, app_name: str = "Logic Pro", render_dir: str = "~/Music/AI-DAW-Conductor/renders"):
        self.app_name = app_name
        self.render_dir = Path(render_dir).expanduser()
        self.render_dir.mkdir(parents=True, exist_ok=True)
        self._available = is_macos() and shutil.which("osascript") is not None

    # ---------- 底层 ----------
    def _run(self, script: str, timeout: int = 60) -> str:
        """执行 AppleScript，返回 stdout。"""
        if not self._available:
            return ""  # 静默降级
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0 and proc.stderr:
            # Logic Pro 未运行等错误，记录但不中断流程
            return ""
        return proc.stdout.strip()

    async def _run_async(self, script: str, timeout: int = 60) -> str:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return stdout.decode().strip()
        except asyncio.TimeoutError:
            proc.kill()
            return ""

    def activate(self):
        self._run(
            f'if application "{self.app_name}" is running then\n'
            f'  tell application "{self.app_name}" to activate\n'
            f'else\n'
            f'  tell application "{self.app_name}" to activate\n'
            f'end if'
        )

    # ---------- 项目 ----------
    def new_project(self, name: str = "AI Project", bpm: float = 100, key: str = "C"):
        """新建空项目。Logic Pro 无直接 AppleScript 建项目命令，使用键命令 New。"""
        self.activate()
        # 触发 File > New (Cmd-N 之后回车选模板)
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    keystroke "n" using {{command down}}\n'
            f'    delay 0.8\n'
            f'    keystroke return\n'
            f'    delay 0.5\n'
            f'  end tell\n'
            f'end tell'
        )
        self.set_tempo(bpm)

    def set_tempo(self, bpm: float):
        # 打开 Tempo 操作并设值
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    keystroke "t" using {{option, command}}\n'  # 打开 Tempo 操作（视版本）
            f'    delay 0.3\n'
            f'    keystroke "{bpm}"\n'
            f'    delay 0.1\n'
            f'    keystroke return\n'
            f'  end tell\n'
            f'end tell'
        )

    # ---------- 轨道 ----------
    def create_software_track(self, name: str):
        """新建一条软件乐器轨道。"""
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    keystroke "n" using {{option, command}}\n'  # 新建轨道
            f'    delay 0.4\n'
            f'    keystroke return\n'
            f'    delay 0.3\n'
            f'  end tell\n'
            f'end tell'
        )
        self.rename_selected_track(name)

    def create_audio_track(self, name: str):
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    keystroke "n" using {{option, command}}\n'
            f'    delay 0.4\n'
            f'    key code 125\n'  # 下箭头切到 Audio
            f'    delay 0.1\n'
            f'    keystroke return\n'
            f'    delay 0.3\n'
            f'  end tell\n'
            f'end tell'
        )
        self.rename_selected_track(name)

    def rename_selected_track(self, name: str):
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    keystroke return\n'  # 进入轨道名编辑
            f'    delay 0.2\n'
            f'    keystroke "{name}"\n'
            f'    delay 0.1\n'
            f'    keystroke return\n'
            f'  end tell\n'
            f'end tell'
        )

    def select_track_by_name(self, name: str):
        self._run(
            f'tell application "{self.app_name}"\n'
            f'  set selTrack to the first track whose name is "{name}"\n'
            f'  set selected of selTrack to true\n'
            f'end tell'
        )

    # ---------- MIDI 导入 ----------
    async def import_midi_file(self, path: Path, track_name: Optional[str] = None):
        """把 MIDI 文件导入到 Logic Pro。"""
        if track_name:
            self.select_track_by_name(track_name)
        await self._run_async(
            f'tell application "{self.app_name}"\n'
            f'  activate\n'
            f'  open POSIX file "{path}"\n'
            f'end tell'
        )

    # ---------- 混音器 ----------
    def set_volume(self, track_name: str, volume_db: float):
        """设置轨道音量（dB）。"""
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

    # ---------- 插件 ----------
    def open_plugin_for_selected_track(self):
        # 打开所选轨道的 Audio FX 插槽
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    keystroke "p" using {{command}}\n'  # 打开插件窗口
            f'    delay 0.3\n'
            f'  end tell\n'
            f'end tell'
        )

    def add_plugin_to_selected_track(self, plugin_name: str, preset: Optional[str] = None):
        """通过键命令与菜单给所选轨道加插件。逻辑较脆，依赖插件名菜单匹配。"""
        # 这是个尽力而为的实现：打开 Audio FX 插槽并输入插件名搜索
        self._run(
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    keystroke "p" using {{command, option}}\n'
            f'    delay 0.3\n'
            f'    keystroke "{plugin_name}"\n'
            f'    delay 0.4\n'
            f'    keystroke return\n'
            f'    delay 0.2\n'
            f'  end tell\n'
            f'end tell'
        )

    # ---------- 导出 ----------
    async def bounce_master(self, filename: str, fmt: str = "wav", bit_depth: int = 24, sample_rate: int = 44100) -> Path:
        """Bounce 整个项目（母带输出）到 render_dir。"""
        out = self.render_dir / f"{filename}.{fmt}"
        ext = {"wav": "WAVE", "aiff": "AIFF", "mp3": "MP3"}.get(fmt.lower(), "WAVE")
        await self._run_async(
            f'tell application "{self.app_name}"\n'
            f'  activate\n'
            f'  set bounceFormat to "{ext}"\n'
            f'  bounce current project to POSIX file "{out}" '
            f'format bounceFormat bit depth {bit_depth} sample rate {sample_rate}\n'
            f'end tell',
            timeout=300,
        )
        return out

    @property
    def available(self) -> bool:
        return self._available
