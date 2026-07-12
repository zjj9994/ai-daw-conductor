"""日志配置：控制台 + 文件双输出，便于排查网页 AI 与 DAW 控制问题。

日志文件写入 ~/.ai-daw-conductor/logs/ai-daw-conductor.log，按天轮转。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

LOG_DIR = Path("~/.ai-daw-conductor/logs").expanduser()
_LOG_CONFIGURED = False


def setup_logging(level: int = logging.INFO) -> None:
    """配置根 logger（仅生效一次）。"""
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return
    _LOG_CONFIGURED = True

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                            datefmt="%H:%M:%S")

    root = logging.getLogger()
    root.setLevel(level)
    # 清除可能存在的默认 handler，避免重复
    root.handlers.clear()

    # 控制台
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(level)
    root.addHandler(console)

    # 文件（按天轮转，保留 7 天）
    try:
        file_h = logging.handlers.TimedRotatingFileHandler(
            LOG_DIR / "ai-daw-conductor.log",
            when="midnight", backupCount=7, encoding="utf-8",
        )
        file_h.setFormatter(fmt)
        file_h.setLevel(level)
        root.addHandler(file_h)
    except Exception as e:  # 权限等问题不阻断启动
        logging.getLogger("logging_config").warning("文件日志初始化失败：%s", e)

    logging.getLogger("ai_engine").info("日志已就绪，文件：%s", LOG_DIR / "ai-daw-conductor.log")
