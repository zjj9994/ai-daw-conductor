"""FastAPI 服务器：前端入口 + WebSocket 实时事件流。

路由：
  GET  /                  -> 前端页面
  GET  /api/health        -> 健康检查
  GET  /api/diagnostics   -> 各子系统诊断（playwright/chrome/midi/applescript）
  GET  /api/settings      -> 读取当前配置（网页 AI + 浏览器连接）
  POST /api/settings      -> 更新配置（provider/web_url/browser.*）
  POST /api/stage         -> 单阶段执行（compose/arrange/mix/master）
  POST /api/pipeline      -> 完整四阶段流水线
  GET  /api/task/status   -> 当前任务状态与进度（供轮询）
  POST /api/cancel        -> 取消当前任务
  GET  /api/renders       -> 渲染历史
  POST /api/browser/connect -> 连接已登录的网页 AI 标签页
  WS   /ws                -> 实时事件流（日志/进度/轨道/混音/导出）

运行：uvicorn backend.server:app --host 127.0.0.1 --port 8787 --reload
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .ai_engine import AIEngine
from .autonomous import AutonomousPipeline
from .commander import Commander
from .config_loader import load_config, validate_config
from .daw_controller import DAWController
from .diagnostics import run_diagnostics
from .logging_config import setup_logging
from .models import Stage
from .task_tracker import TaskTracker, DEFAULT_HISTORY_FILE

setup_logging()
log = logging.getLogger("server")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# 全局状态
_state: dict = {"ai": None, "daw": None, "commander": None, "cfg": {}}
_ws_clients: set[WebSocket] = set()
_current_task: Optional[asyncio.Task] = None
_autonomous: Optional[AutonomousPipeline] = None
tracker = TaskTracker(history_file=DEFAULT_HISTORY_FILE)


def _build_engines(cfg: dict):
    ai = AIEngine(cfg)
    daw = DAWController(cfg, event_cb=broadcast, tracker=tracker)
    commander = Commander(ai, daw, tracker=tracker)
    _state.update(ai=ai, daw=daw, commander=commander, cfg=cfg)
    return commander


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    issues = validate_config(cfg)
    for iss in issues:
        log.warning("配置问题：%s", iss)
    _build_engines(cfg)
    log.info("AI 引擎就绪（网页 AI 模式可用=%s）", _state["ai"].online)
    yield
    if _state.get("ai"):
        await _state["ai"].close()
    if _state.get("daw"):
        _state["daw"].close()


app = FastAPI(title="AI-DAW-Conductor", version="0.1.0", lifespan=lifespan)


# ---------- 广播 ----------
async def broadcast(event: dict):
    """把 DAW 事件广播给所有 WebSocket 客户端。"""
    dead = []
    for ws in list(_ws_clients):
        try:
            await ws.send_text(json.dumps(event, ensure_ascii=False, default=str))
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


# ---------- 静态前端 ----------
class SPAStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except Exception:
            return await super().get_response("index.html", scope)


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
async def index():
    index_file = FRONTEND_DIR / "index.html"
    if index_file.exists():
        # 用 HTMLResponse 显式指定 text/html; charset=utf-8
        # 否则返回 str 会被当成 text/plain，浏览器不渲染 HTML 且中文乱码
        return HTMLResponse(index_file.read_text(encoding="utf-8"))
    return JSONResponse({"message": "AI-DAW-Conductor API running. Frontend not found."})


# ---------- REST ----------
@app.get("/api/health")
async def health():
    ai = _state.get("ai")
    browser = _state.get("cfg", {}).get("browser", {})
    return {
        "ok": True,
        "ai_online": ai.online if ai else False,
        "browser_mode": browser.get("mode", "cdp"),
        "browser_connected": bool(ai and ai.driver.connected),
        "daw_platform": "macos" if __import__("platform").system() == "Darwin" else "simulated",
    }


class SettingsIn(BaseModel):
    provider: Optional[str] = None
    web_url: Optional[str] = None
    timeout: Optional[float] = None
    browser_mode: Optional[str] = None      # cdp | persistent
    cdp_url: Optional[str] = None
    user_data_dir: Optional[str] = None
    headless: Optional[bool] = None


@app.get("/api/settings")
async def get_settings():
    ai = _state.get("cfg", {}).get("ai", {})
    browser = _state.get("cfg", {}).get("browser", {})
    engine = _state.get("ai")
    return {
        "provider": ai.get("provider", "doubao"),
        "web_url": ai.get("web_url", ""),
        "timeout": ai.get("timeout", 180),
        "browser_mode": browser.get("mode", "cdp"),
        "cdp_url": browser.get("cdp_url", "http://127.0.0.1:9222"),
        "user_data_dir": browser.get("user_data_dir", "~/.ai-daw-conductor/browser-profile"),
        "ai_online": engine.online if engine else False,
        "browser_connected": bool(engine and engine.driver.connected),
    }


@app.post("/api/settings")
async def update_settings(s: SettingsIn):
    cfg = load_config()
    ai = cfg.setdefault("ai", {})
    browser = cfg.setdefault("browser", {})
    data = s.model_dump(exclude_none=True)
    for k in ("provider", "web_url", "timeout"):
        if k in data:
            ai[k] = data[k]
    for k in ("browser_mode", "cdp_url", "user_data_dir", "headless"):
        if k in data:
            browser[k.replace("browser_", "") if k == "browser_mode" else k] = data[k]
    # 重建引擎以应用新配置（先关闭旧的浏览器/DAW 连接）
    if _state.get("ai"):
        await _state["ai"].close()
    if _state.get("daw"):
        _state["daw"].close()
    _build_engines(cfg)
    return {"ok": True, "ai_online": _state["ai"].online}


class StageIn(BaseModel):
    stage: Stage
    prompt: str = ""
    context: Optional[str] = None
    bpm: float = 100.0


@app.post("/api/stage")
async def api_stage(s: StageIn):
    """异步执行单阶段，事件经 /ws 推送。返回 task 接受标识。"""
    global _current_task
    if _current_task and not _current_task.done():
        return JSONResponse({"ok": False, "error": "已有任务在运行，请先取消"}, status_code=409)
    commander: Commander = _state["commander"]
    tracker.start(mode="stage", prompt=s.prompt, total=1)

    async def _run():
        try:
            await commander.run_stage(s.stage, s.prompt, s.context, s.bpm)
            tracker.finish()
        except asyncio.CancelledError:
            tracker.cancel()
        except Exception as e:
            log.exception("stage 执行失败")
            tracker.fail(str(e))
            await broadcast({"type": "daw_event", "kind": "error", "message": str(e)})
        finally:
            await broadcast({"type": "daw_event", "kind": "task_finished"})

    _current_task = asyncio.create_task(_run())
    return {"ok": True, "stage": s.stage.value}


class PipelineIn(BaseModel):
    prompt: str = ""


@app.post("/api/pipeline")
async def api_pipeline(p: PipelineIn):
    global _current_task
    if _current_task and not _current_task.done():
        return JSONResponse({"ok": False, "error": "已有任务在运行，请先取消"}, status_code=409)
    commander: Commander = _state["commander"]
    tracker.start(mode="pipeline", prompt=p.prompt, total=4)

    async def _run():
        try:
            await commander.run_pipeline(p.prompt)
            tracker.finish()
        except asyncio.CancelledError:
            tracker.cancel()
        except Exception as e:
            log.exception("pipeline 执行失败")
            tracker.fail(str(e))
            await broadcast({"type": "daw_event", "kind": "error", "message": str(e)})
        finally:
            await broadcast({"type": "daw_event", "kind": "task_finished"})

    _current_task = asyncio.create_task(_run())
    return {"ok": True}


class AutonomousIn(BaseModel):
    prompt: str = ""
    enable_self_eval: bool = True
    max_stage_retries: int = 2


@app.post("/api/autonomous")
async def api_autonomous(a: AutonomousIn):
    """启动自主制作：AI 不间断、自主完成一整首作品。

    与 /api/pipeline 的区别：
    - 阶段间累积上下文，保证整首作品连贯（标题/调性/速度/结构/轨道）
    - 每阶段产出后 AI 自评估，不达标带反馈重做
    - 网页 AI 断连自动健康检查 + 重连
    - 单阶段彻底失败降级为 demo，但流水线不中断
    """
    global _current_task, _autonomous
    if _current_task and not _current_task.done():
        return JSONResponse({"ok": False, "error": "已有任务在运行，请先取消"}, status_code=409)
    commander: Commander = _state["commander"]
    ai: AIEngine = _state["ai"]
    _autonomous = AutonomousPipeline(
        ai=ai, commander=commander, tracker=tracker,
        max_stage_retries=max(0, a.max_stage_retries),
        enable_self_eval=a.enable_self_eval,
    )

    async def _run():
        try:
            await _autonomous.run(a.prompt)
        except asyncio.CancelledError:
            tracker.cancel()
        except Exception as e:
            log.exception("autonomous 执行失败")
            tracker.fail(str(e))
            await broadcast({"type": "daw_event", "kind": "error", "message": str(e)})
        finally:
            await broadcast({"type": "daw_event", "kind": "task_finished"})

    _current_task = asyncio.create_task(_run())
    return {"ok": True, "mode": "autonomous", "self_eval": a.enable_self_eval,
            "max_stage_retries": a.max_stage_retries}


@app.post("/api/cancel")
async def api_cancel():
    global _current_task, _autonomous
    # 优先通知自主流水线（它会级联取消 commander）
    if _autonomous:
        _autonomous.cancel()
    if _state.get("commander"):
        _state["commander"].cancel()
    if _current_task and not _current_task.done():
        _current_task.cancel()
    tracker.cancel()
    return {"ok": True}


@app.get("/api/diagnostics")
async def api_diagnostics():
    """运行各子系统诊断检查。"""
    cfg = _state.get("cfg", {})
    engine = _state.get("ai")
    return await run_diagnostics(cfg, engine)


@app.get("/api/task/status")
async def api_task_status():
    """当前任务状态与进度（供轮询）。"""
    return tracker.status.to_dict()


@app.get("/api/renders")
async def api_renders():
    """渲染历史。"""
    return {"renders": tracker.renders_dict(), "count": len(tracker.renders)}


@app.delete("/api/renders")
async def api_renders_clear():
    """清空渲染历史记录（不删磁盘文件）。"""
    n = tracker.clear_history()
    return {"ok": True, "cleared": n}


@app.get("/api/renders/{idx}/download")
async def api_render_download(idx: int):
    """按索引下载渲染历史中的导出文件。"""
    if idx < 0 or idx >= len(tracker.renders):
        return JSONResponse({"error": "索引越界"}, status_code=404)
    rec = tracker.renders[idx]
    p = Path(rec.path)
    if not p.exists():
        return JSONResponse({"error": f"文件不存在：{rec.path}"}, status_code=404)
    return FileResponse(p, filename=f"{rec.filename}.{_guess_ext(p)}")


@app.get("/api/midis")
async def api_midis():
    """列出已生成的 MIDI 文件（扫描 DAW render_dir 与临时目录）。"""
    daw = _state.get("cfg", {}).get("daw", {})
    render_dir = Path(daw.get("render_dir", "~/Music/AI-DAW-Conductor/renders")).expanduser()
    midis: list[dict] = []
    if render_dir.exists():
        for p in sorted(render_dir.glob("*.mid*"), key=lambda x: x.stat().st_mtime, reverse=True):
            st = p.stat()
            midis.append({
                "path": str(p), "filename": p.name,
                "size": st.st_size, "mtime": st.st_mtime,
            })
    return {"midis": midis, "count": len(midis)}


@app.get("/api/midis/download")
async def api_midi_download(path: str):
    """下载指定路径的 MIDI 文件（路径必须在 render_dir 之下，防越权）。"""
    daw = _state.get("cfg", {}).get("daw", {})
    render_dir = Path(daw.get("render_dir", "~/Music/AI-DAW-Conductor/renders")).expanduser()
    target = Path(path).expanduser()
    try:
        target.resolve().relative_to(render_dir.resolve())
    except ValueError:
        return JSONResponse({"error": "路径不在允许的目录内"}, status_code=403)
    if not target.exists() or not target.is_file():
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    return FileResponse(target, filename=target.name)


def _guess_ext(p: Path) -> str:
    return p.suffix.lstrip(".") or "bin"


@app.post("/api/browser/connect")
async def api_browser_connect():
    """连接/复用用户已登录的网页 AI 标签页，返回状态。"""
    engine = _state.get("ai")
    if not engine:
        return {"ok": False, "error": "引擎未初始化"}
    if not engine.online:
        return {"ok": False, "error": "Playwright 未安装，请运行 pip install playwright 后再试（demo 模式无需连接）"}
    try:
        page = await engine.driver.ensure_page()
        url = page.url if page else ""
        await broadcast({"type": "daw_event", "kind": "log", "level": "info",
                         "message": f"已连接网页 AI 标签页：{url}"})
        return {"ok": True, "url": url, "provider": engine.driver.provider}
    except Exception as e:
        log.exception("浏览器连接失败")
        return {"ok": False, "error": str(e)}


# ---------- WebSocket ----------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "daw_event", "kind": "connected",
                                       "ai_online": _state["ai"].online if _state.get("ai") else False},
                                      ensure_ascii=False))
        while True:
            data = await ws.receive_text()
            msg = json.loads(data) if data else {}
            if msg.get("type") == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
            elif msg.get("type") == "produce":
                # 允许从 ws 直接触发流水线
                await api_pipeline(PipelineIn(prompt=msg.get("prompt", "")))
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)


def main():
    import uvicorn
    cfg = load_config()
    server_cfg = cfg.get("server", {})
    uvicorn.run(
        "backend.server:app",
        host=server_cfg.get("host", "127.0.0.1"),
        port=int(server_cfg.get("port", 8787)),
        reload=False,
    )


if __name__ == "__main__":
    main()
