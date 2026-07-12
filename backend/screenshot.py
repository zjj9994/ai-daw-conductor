"""截图工具：截取 Logic Pro 应用窗口的实时截图，供网页 AI 视觉规划使用。

策略：
- macOS：用 AppleScript 激活 Logic Pro 并获取其窗口 bounds，再用 `screencapture`
  按窗口区域截图（`screencapture -R x,y,w,h`）；若取不到窗口 bounds 则退化为
  全屏截图。截图保存为 PNG 到 screenshot_dir。
- 非 macOS：返回 None（模拟模式），视觉循环会跳过截图步骤。

所有方法都做成异步友好（_run_async 内部用 asyncio.create_subprocess_exec），
且失败静默降级，不中断主流程。
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from .config_loader import is_macos

log = logging.getLogger("screenshot")


class ScreenshotCapture:
    """截取 Logic Pro 窗口的实时截图。"""

    def __init__(
        self,
        app_name: str = "Logic Pro",
        screenshot_dir: str = "~/.ai-daw-conductor/screenshots",
    ):
        self.app_name = app_name
        self.screenshot_dir = Path(screenshot_dir).expanduser()
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._available = is_macos() and shutil.which("screencapture") is not None
        # 最近一次截图路径（供前端预览/API 取用）
        self._latest: Optional[str] = None

    @property
    def available(self) -> bool:
        return self._available

    @property
    def latest(self) -> Optional[str]:
        """最近一次截图的绝对路径。"""
        return self._latest

    async def activate_app(self) -> bool:
        """激活 Logic Pro 窗口到最前。返回是否成功。"""
        if not self._available:
            return False
        script = (
            f'if application "{self.app_name}" is running then\n'
            f'  tell application "{self.app_name}" to activate\n'
            f'  delay 0.4\n'
            f'  return "ok"\n'
            f'else\n'
            f'  return "not_running"\n'
            f'end if'
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            out = stdout.decode().strip()
            if out == "ok":
                return True
            log.warning("Logic Pro 未运行，无法激活。")
            return False
        except Exception as e:
            log.warning("激活 Logic Pro 失败：%s", e)
            return False

    async def get_window_bounds(self) -> Optional[Tuple[int, int, int, int]]:
        """获取 Logic Pro 最前窗口的 {x, y, w, h}。失败返回 None。"""
        if not self._available:
            return None
        # 通过 System Events 取窗口的 position 和 size
        script = (
            f'tell application "System Events"\n'
            f'  tell process "{self.app_name}"\n'
            f'    set frontmost to true\n'
            f'    try\n'
            f'      set wPos to position of front window\n'
            f'      set wSize to size of front window\n'
            f'      return (item 1 of wPos as text) & "," & (item 2 of wPos as text) & "," & '
            f'            (item 1 of wSize as text) & "," & (item 2 of wSize as text)\n'
            f'    on error\n'
            f'      return ""\n'
            f'    end try\n'
            f'  end tell\n'
            f'end tell'
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
            out = stdout.decode().strip()
            if not out:
                return None
            parts = [int(p.strip()) for p in out.split(",")]
            if len(parts) != 4:
                return None
            x, y, w, h = parts
            if w <= 0 or h <= 0:
                return None
            return x, y, w, h
        except Exception as e:
            log.debug("取窗口 bounds 失败：%s", e)
            return None

    async def capture(self, tag: str = "frame") -> Optional[str]:
        """截取 Logic Pro 窗口，返回 PNG 绝对路径。失败/非 macOS 返回 None。

        Args:
            tag: 文件名标签，用于区分场景（如 frame / verify / step3）。
        """
        if not self._available:
            log.debug("非 macOS，截图跳过（模拟模式）。")
            return None

        # 先激活窗口，确保截到的是 Logic Pro 而非其他应用
        activated = await self.activate_app()
        if not activated:
            return None

        bounds = await self.get_window_bounds()
        name = f"{datetime.now():%Y%m%d_%H%M%S}_{tag}.png"
        path = self.screenshot_dir / name

        try:
            if bounds:
                x, y, w, h = bounds
                # screencapture -R x,y,w,h 按区域截图（无阴影更快）
                cmd = ["screencapture", "-R", f"{x},{y},{w},{h}", "-x", str(path)]
            else:
                # 退化：全屏截图
                cmd = ["screencapture", "-x", str(path)]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
            if path.exists() and path.stat().st_size > 0:
                self._latest = str(path)
                log.info("截图已保存：%s", path)
                return str(path)
            log.warning("截图文件未生成或为空。")
            return None
        except asyncio.TimeoutExpired:
            log.warning("截图超时。")
            return None
        except Exception as e:
            log.warning("截图失败：%s", e)
            return None

    def list_recent(self, limit: int = 20) -> list[dict]:
        """列出最近的截图文件（按修改时间倒序）。"""
        files = sorted(
            self.screenshot_dir.glob("*.png"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
        return [
            {"path": str(p), "filename": p.name,
             "size": p.stat().st_size, "mtime": p.stat().st_mtime}
            for p in files
        ]
