"""任务追踪：记录当前任务状态、阶段进度、渲染历史。

供 REST 轮询（/api/task/status、/api/renders）与前端展示使用。
任务状态在内存中维护（单进程会话级）；渲染历史可选持久化到 JSON 文件，重启后自动恢复。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from .models import Stage

log = logging.getLogger("task_tracker")

# 默认持久化路径（渲染历史）。可在构造时覆盖。
DEFAULT_HISTORY_FILE = Path.home() / ".ai-daw-conductor" / "render_history.json"


@dataclass
class RenderRecord:
    path: str
    filename: str
    stage: str           # 通常是 "master"
    timestamp: float
    size: int = 0
    task_mode: str = ""  # pipeline | stage
    prompt: str = ""     # 当时使用的创作指令（便于回溯）


@dataclass
class TaskStatus:
    state: str = "idle"          # idle | running | done | error | cancelled
    mode: str = ""               # pipeline | stage
    current_stage: str = ""      # compose | arrange | mix | master
    completed_stages: list = field(default_factory=list)
    total_stages: int = 0
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    prompt: str = ""

    @property
    def progress(self) -> float:
        """0..1 完成度。"""
        if self.total_stages <= 0:
            return 0.0
        done = len(self.completed_stages)
        if self.current_stage and self.current_stage not in self.completed_stages:
            done += 0.5  # 当前阶段进行中算一半
        return min(1.0, done / self.total_stages)

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "mode": self.mode,
            "current_stage": self.current_stage,
            "completed_stages": list(self.completed_stages),
            "total_stages": self.total_stages,
            "progress": round(self.progress, 3),
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1) if self.started_at else 0,
            "prompt": self.prompt,
        }


class TaskTracker:
    """全局任务追踪器（单例，由 server 持有）。

    Args:
        history_file: 渲染历史持久化路径；传 None 则仅内存（不落盘）。
    """

    def __init__(self, history_file: Optional[Path] = None):
        self.status = TaskStatus()
        self.renders: list[RenderRecord] = []
        self.history_file = history_file
        self._load_history()

    # ---------- 任务生命周期 ----------
    def start(self, mode: str, prompt: str = "", total: int = 4):
        self.status = TaskStatus(
            state="running", mode=mode, total_stages=total,
            started_at=time.time(), prompt=prompt,
        )

    def set_stage(self, stage: str):
        self.status.current_stage = stage

    def complete_stage(self, stage: str):
        if stage not in self.status.completed_stages:
            self.status.completed_stages.append(stage)

    def finish(self):
        self.status.state = "done"
        self.status.finished_at = time.time()

    def fail(self, error: str):
        self.status.state = "error"
        self.status.error = error
        self.status.finished_at = time.time()

    def cancel(self):
        self.status.state = "cancelled"
        self.status.finished_at = time.time()

    def reset(self):
        self.status = TaskStatus()

    # ---------- 渲染历史 ----------
    def add_render(self, path: str, filename: str, stage: str = "master",
                   size: int = 0, task_mode: str = "", prompt: str = ""):
        rec = RenderRecord(
            path=path, filename=filename, stage=stage,
            timestamp=time.time(), size=size,
            task_mode=task_mode or self.status.mode,
            prompt=prompt or self.status.prompt,
        )
        self.renders.append(rec)
        self._save_history()
        return rec

    def renders_dict(self) -> list[dict]:
        return [
            {"path": r.path, "filename": r.filename, "stage": r.stage,
             "timestamp": r.timestamp, "size": r.size,
             "task_mode": r.task_mode, "prompt": r.prompt}
            for r in self.renders
        ]

    def clear_history(self) -> int:
        """清空渲染历史记录（不删磁盘文件）。返回被清除的条数。"""
        n = len(self.renders)
        self.renders = []
        self._save_history()
        return n

    # ---------- 持久化 ----------
    def _load_history(self):
        if not self.history_file:
            return
        try:
            if self.history_file.exists():
                data = json.loads(self.history_file.read_text(encoding="utf-8"))
                self.renders = [RenderRecord(**item) for item in data.get("renders", [])]
                log.info("已加载 %d 条渲染历史记录", len(self.renders))
        except Exception as e:
            log.warning("加载渲染历史失败：%s", e)

    def _save_history(self):
        if not self.history_file:
            return
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            data = {"renders": [asdict(r) for r in self.renders]}
            self.history_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            log.warning("保存渲染历史失败：%s", e)
