"""系统音频录制：捕获 Logic Pro 的播放输出，供 AI 听取反馈。

实现策略（按优先级）：
1. macOS：用 ffprobe/ffmpeg 录制系统音频（BlackHole/Soundflower 虚拟音频设备，
   或直接用 macOS 的 ScreenCaptureKit 录制系统音频）
2. 兜底：用 Logic Pro 的 bounce 功能导出临时音频文件（最可靠，但慢）

不依赖外部虚拟音频设备（用户不需要额外安装），优先用 bounce 导出临时文件。
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Optional
import logging

log = logging.getLogger("audio_capture")


async def capture_audio_segment(
    daw_controller,
    start_bar: int = 1,
    end_bar: int = 4,
    output_path: Optional[Path] = None,
    timeout: int = 60,
) -> Optional[Path]:
    """录制一段音频（指定小节范围）供 AI 听取。

    实现：用 Logic Pro 的 bounce 功能导出指定小节范围的临时音频文件。
    比"实时播放+录音"更可靠，且不需要虚拟音频设备。

    Args:
        daw_controller: DAWController 实例
        start_bar: 起始小节
        end_bar: 结束小节
        output_path: 输出路径，留空则用临时文件
        timeout: 超时秒数

    Returns:
        音频文件路径，失败返回 None
    """
    import tempfile
    if output_path is None:
        output_path = Path(tempfile.mktemp(suffix=".wav"))
    try:
        # 用 bounce 导出指定小节范围
        from backend.models import BounceSpec
        spec = BounceSpec(
            format="wav",
            filename=str(output_path.stem),
            start_bar=start_bar,
            end_bar=end_bar,
            normalize=False,
        )
        result = await asyncio.wait_for(
            daw_controller.bounce(spec),
            timeout=timeout,
        )
        if result:
            # daw_controller.bounce 返回的是路径或路径列表
            if isinstance(result, list):
                return Path(result[0]) if result else None
            return Path(result)
        # 兜底：直接找 render_dir 下的文件
        if hasattr(daw_controller, 'applescript') and hasattr(daw_controller.applescript, 'render_dir'):
            render_dir = Path(daw_controller.applescript.render_dir)
            wavs = sorted(render_dir.glob(f"{output_path.stem}*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
            if wavs:
                return wavs[0]
        return None
    except asyncio.TimeoutError:
        log.warning("录制音频超时（%ds）", timeout)
        return None
    except Exception as e:
        log.error("录制音频失败: %s", e)
        return None


def check_ffmpeg_available() -> bool:
    """检查 ffmpeg 是否可用（用于音频格式转换，如 wav→mp3 给 AI）。"""
    return shutil.which("ffmpeg") is not None


async def convert_for_ai(input_path: Path, output_path: Optional[Path] = None, fmt: str = "mp3") -> Optional[Path]:
    """把音频转换为 AI 能接受的格式（通常 mp3，采样率 44100）。

    多模态 AI（豆包/Kimi/Claude）通常接受 mp3/wav，但 mp3 文件更小。
    """
    if not check_ffmpeg_available():
        # 没有 ffmpeg，直接返回原文件（AI 可能也能接受 wav）
        return input_path
    if output_path is None:
        output_path = input_path.with_suffix(f".{fmt}")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(input_path),
            "-ar", "44100", "-ac", "2", "-b:a", "128k",
            str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
        return output_path if proc.returncode == 0 else input_path
    except Exception as e:
        log.warning("音频转换失败，用原文件: %s", e)
        return input_path
