"""诊断：检查各子系统可用性，帮助用户快速定位问题。"""
from __future__ import annotations

import asyncio
import logging
import platform
import shutil
import socket
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("diagnostics")


def _check_playwright() -> dict:
    try:
        import playwright  # noqa: F401
        return {"ok": True, "detail": "playwright 已安装"}
    except Exception as e:
        return {"ok": False, "detail": f"未安装：{e}（pip install playwright）"}


def _check_mido() -> dict:
    try:
        import mido  # noqa: F401
        return {"ok": True, "detail": "mido 已安装"}
    except Exception as e:
        return {"ok": False, "detail": f"未安装：{e}"}


def _check_rtmidi() -> dict:
    try:
        import rtmidi  # noqa: F401
        return {"ok": True, "detail": "python-rtmidi 已安装（实时 MIDI 可用）"}
    except Exception:
        return {"ok": False, "detail": "未安装（仅文件模式可用，无虚拟端口实时输出）"}


def _check_applescript() -> dict:
    if platform.system() != "Darwin":
        return {"ok": False, "detail": f"非 macOS（{platform.system()}），AppleScript 不可用，DAW 控制为模拟模式"}
    if not shutil.which("osascript"):
        return {"ok": False, "detail": "osascript 未找到"}
    return {"ok": True, "detail": "macOS + osascript 可用，Logic Pro 实控就绪"}


def _check_cdp(cdp_url: str) -> dict:
    """检查 CDP 调试端口是否可达。"""
    if not cdp_url:
        return {"ok": False, "detail": "未配置 cdp_url"}
    try:
        parsed = urlparse(cdp_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 9222
        with socket.create_connection((host, port), timeout=2):
            pass
        return {"ok": True, "detail": f"CDP 端口可达（{host}:{port}）"}
    except Exception as e:
        return {"ok": False, "detail": f"无法连接 {cdp_url}：{e}（请先用 scripts/launch_chrome.sh 启动 Chrome）"}


async def run_diagnostics(cfg: dict, ai_engine=None) -> dict[str, Any]:
    """运行全部诊断检查，返回结构化结果。"""
    browser = cfg.get("browser", {})
    ai = cfg.get("ai", {})

    result = {
        "platform": platform.system(),
        "python": platform.python_version(),
        "playwright": _check_playwright(),
        "mido": _check_mido(),
        "rtmidi": _check_rtmidi(),
        "applescript": _check_applescript(),
        "cdp": _check_cdp(browser.get("cdp_url", "http://127.0.0.1:9222")),
        "ai_provider": ai.get("provider", "doubao"),
        "ai_online": bool(ai_engine and ai_engine.online),
        "browser_connected": bool(ai_engine and ai_engine.driver.connected),
    }

    # 综合建议
    suggestions = []
    if not result["playwright"]["ok"]:
        suggestions.append("运行 pip install playwright 安装浏览器自动化库")
    if result["playwright"]["ok"] and not result["cdp"]["ok"]:
        suggestions.append("CDP 端口不可达：用 ./scripts/launch_chrome.sh 启动调试 Chrome 并登录网页 AI")
    if not result["applescript"]["ok"] and platform.system() == "Darwin":
        suggestions.append("macOS 上 osascript 不可用，检查系统权限")
    if not result["rtmidi"]["ok"]:
        suggestions.append("（可选）安装 python-rtmidi 启用实时 MIDI 输出；文件模式不受影响")
    if not suggestions:
        suggestions.append("所有核心组件就绪，可以开始制作。")
    result["suggestions"] = suggestions
    result["ready"] = result["playwright"]["ok"] and result["mido"]["ok"]
    return result
