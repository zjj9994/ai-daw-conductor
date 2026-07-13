"""FastAPI 服务器：前端入口 + WebSocket 实时事件流。

路由：
  GET  /                  -> 前端页面
  GET  /api/health        -> 健康检查
  GET  /api/diagnostics   -> 各子系统诊断（playwright/chrome/midi/applescript）
  GET  /api/providers     -> 可用网页 AI 目录（13+ 个，含名称/网址/徽章/厂商）
  GET  /api/settings      -> 读取当前配置（网页 AI + 浏览器连接）
  POST /api/settings      -> 更新配置（provider/web_url/browser.*）
  POST /api/provider/switch -> 一键切换网页 AI（仅 provider + 默认网址）
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
from pydantic import BaseModel, Field

from .ai_engine import AIEngine, PROVIDER_CATALOG, list_providers, get_provider_meta
from .autonomous import AutonomousPipeline
from .commander import Commander
from .config_loader import load_config, validate_config
from .daw_controller import DAWController
from .diagnostics import run_diagnostics
from .logging_config import setup_logging
from .models import Stage
from .screenshot import ScreenshotCapture
from .task_tracker import TaskTracker, DEFAULT_HISTORY_FILE
from .visual_loop import VisualLoop

setup_logging()
log = logging.getLogger("server")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# 全局状态
_state: dict = {"ai": None, "daw": None, "commander": None, "screenshot": None, "cfg": {}}
_ws_clients: set[WebSocket] = set()
_current_task: Optional[asyncio.Task] = None
_autonomous: Optional[AutonomousPipeline] = None
_visual: Optional[VisualLoop] = None
tracker = TaskTracker(history_file=DEFAULT_HISTORY_FILE)


def _build_engines(cfg: dict, old_daw: Optional[DAWController] = None):
    ai = AIEngine(cfg)
    daw = DAWController(cfg, event_cb=broadcast, tracker=tracker)
    # 工程一致性：重建引擎（保存设置/切换 provider）时，必须把旧 daw 的工程锚点
    # 迁移到新 daw，否则 project_locked/current_project_path 丢失，后续 AI 会以为
    # 没有工程而再次 create_project，导致「工程在创作流程中被关闭/重建」的 bug。
    # Logic Pro 里的工程文件仍然打开着，只是 Python 侧的引用对象被替换了。
    if old_daw is not None and getattr(old_daw, "project_locked", False):
        daw.project_locked = old_daw.project_locked
        daw.current_project_path = old_daw.current_project_path
        daw.current_project_title = old_daw.current_project_title
        daw._track_index = dict(old_daw._track_index)
        log.info("迁移工程锚点：title=%s path=%s",
                 daw.current_project_title, daw.current_project_path)
    commander = Commander(ai, daw, tracker=tracker)
    daw_cfg = cfg.get("daw", {})
    screenshot = ScreenshotCapture(
        app_name=daw_cfg.get("app_name", "Logic Pro"),
        screenshot_dir=daw_cfg.get("screenshot_dir", "~/.ai-daw-conductor/screenshots"),
    )
    _state.update(ai=ai, daw=daw, commander=commander, screenshot=screenshot, cfg=cfg)
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
    daw = _state.get("daw")
    return {
        "ok": True,
        "ai_online": ai.online if ai else False,
        "browser_mode": browser.get("mode", "cdp"),
        "browser_connected": bool(ai and ai.driver.connected),
        "daw_platform": "macos" if __import__("platform").system() == "Darwin" else "simulated",
        "project_locked": bool(daw and daw.project_locked),
        "current_project_path": (daw.current_project_path if daw else None),
        "current_project_title": (daw.current_project_title if daw else ""),
    }


# ---------- 工程锚点管理：先打开一个工程，之后所有操作都锁定在这个工程里 ----------
class ProjectOpenIn(BaseModel):
    path: str  # .logicx 文件的绝对路径


@app.post("/api/project/open")
async def api_project_open(p: ProjectOpenIn):
    """打开一个已有的 .logicx 工程作为锚点。

    严格规则：一首音乐的所有操作指向同一个工程。
    若已有锁定工程，拒绝切换；若工程文件不存在或非 .logicx，返回错误。
    """
    daw = _state.get("daw")
    if not daw:
        return JSONResponse({"ok": False, "error": "DAW 控制器未就绪"}, status_code=503)
    ok = await daw.open_existing_project(p.path)
    if not ok:
        return JSONResponse(
            {"ok": False, "error": f"无法打开工程：{p.path}（可能已锁定其他工程，或文件不存在/非 .logicx）"},
            status_code=400,
        )
    return {
        "ok": True,
        "project_path": daw.current_project_path,
        "project_title": daw.current_project_title,
        "project_locked": daw.project_locked,
    }


@app.get("/api/project/status")
async def api_project_status():
    """查询当前工程锚点状态（前端据此显示是否已锁定工程）。"""
    daw = _state.get("daw")
    return {
        "locked": bool(daw and daw.project_locked),
        "path": daw.current_project_path if daw else None,
        "title": daw.current_project_title if daw else "",
    }


@app.post("/api/project/reset")
async def api_project_reset():
    """重置工程锚点（取消任务后清空锁定状态，允许开始新一首作品）。

    注意：不会关闭 Logic Pro 里实际打开的工程，只清空后端的锁定状态。
    """
    daw = _state.get("daw")
    if daw:
        daw.unlock_project()
    return {"ok": True, "locked": False}


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
    provider = ai.get("provider", "doubao")
    meta = get_provider_meta(provider)
    return {
        "provider": provider,
        "provider_meta": meta,
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
    # 校验：custom 模式必须提供 web_url
    provider = ai.get("provider", "doubao")
    web_url = ai.get("web_url", "")
    if provider == "custom" and not web_url:
        return JSONResponse(
            {"ok": False, "error": "自定义模式必须填写网页 AI 聊天页地址"},
            status_code=400,
        )
    # 重建引擎以应用新配置（先关闭旧的浏览器/DAW 连接）
    # 注意：daw.close() 只关闭 MIDI 端口，不会关闭 Logic Pro 工程；
    # 工程锚点状态通过 old_daw 迁移到新 daw，保证创作流程中工程不被「关闭」。
    old_daw = _state.get("daw")
    if _state.get("ai"):
        await _state["ai"].close()
    if old_daw:
        old_daw.close()
    # _build_engines 含同步 IO（mkdir/mido.open_output），放线程池并加超时，避免阻塞事件循环
    try:
        await asyncio.wait_for(asyncio.to_thread(_build_engines, cfg, old_daw), timeout=10.0)
    except asyncio.TimeoutError:
        log.error("重建引擎超时（10s），可能 MIDI 子系统或文件系统无响应")
        return JSONResponse(
            {"ok": False, "error": "重建引擎超时（10s），可能 MIDI 子系统或文件系统无响应，请检查后端日志"},
            status_code=504,
        )
    engine = _state["ai"]
    return {
        "ok": True,
        "ai_online": engine.online,
        "provider": provider,
        "provider_meta": get_provider_meta(provider),
        "web_url": engine.driver.url if engine.driver else "",
        "browser_connected": bool(engine and engine.driver.connected),
    }


@app.get("/api/providers")
async def api_providers():
    """返回所有可用网页 AI 的目录（前端渲染卡片网格与快捷切换菜单用）。"""
    return {"ok": True, "providers": list_providers()}


class ProviderSwitchIn(BaseModel):
    provider: str
    web_url: Optional[str] = None  # 留空则用 catalog 默认网址


@app.post("/api/provider/switch")
async def api_provider_switch(s: ProviderSwitchIn):
    """一键切换网页 AI：仅改 provider + 默认网址，浏览器配置不变。

    用于顶栏快捷切换芯片：用户点击某个 AI 卡片即立即切换，
    无需打开完整设置弹层。
    """
    if s.provider not in PROVIDER_CATALOG:
        return JSONResponse(
            {"ok": False, "error": f"未知 provider：{s.provider}"},
            status_code=400,
        )
    meta = PROVIDER_CATALOG[s.provider]
    cfg = load_config()
    ai = cfg.setdefault("ai", {})
    ai["provider"] = s.provider
    # 切换到非 custom 时，若用户未显式给 web_url，则用 catalog 默认网址
    if s.provider == "custom":
        ai["web_url"] = (s.web_url or "").strip() or ai.get("web_url", "")
        if not ai["web_url"]:
            return JSONResponse(
                {"ok": False, "error": "自定义模式必须填写网页 AI 聊天页地址"},
                status_code=400,
            )
    else:
        ai["web_url"] = (s.web_url or "").strip() or meta["url"]
    # 重建引擎应用新 provider（迁移工程锚点，避免创作流程中工程被关闭）
    old_daw = _state.get("daw")
    if _state.get("ai"):
        await _state["ai"].close()
    if old_daw:
        old_daw.close()
    # _build_engines 含同步 IO，放线程池并加超时
    try:
        await asyncio.wait_for(asyncio.to_thread(_build_engines, cfg, old_daw), timeout=10.0)
    except asyncio.TimeoutError:
        log.error("切换 provider 时重建引擎超时（10s）")
        return JSONResponse(
            {"ok": False, "error": "重建引擎超时（10s），可能 MIDI 子系统或文件系统无响应"},
            status_code=504,
        )
    engine = _state["ai"]
    return {
        "ok": True,
        "provider": s.provider,
        "provider_meta": meta,
        "web_url": ai["web_url"],
        "ai_online": engine.online,
        "browser_connected": bool(engine and engine.driver.connected),
    }


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
    global _current_task, _autonomous, _visual
    # 优先通知自主流水线与视觉循环（它们会级联取消 commander）
    if _autonomous:
        _autonomous.cancel()
    if _visual:
        _visual.cancel()
    if _state.get("commander"):
        _state["commander"].cancel()
    if _current_task and not _current_task.done():
        _current_task.cancel()
    tracker.cancel()
    # 注意：取消任务不解锁工程锚点。
    # 取消只代表「停止当前 AI 循环/流水线」，不等于「放弃当前作品」——
    # 用户可能只是想中止当前 AI 步骤，工程仍然保留，可以继续创作或重新跑。
    # 若确实要放弃工程开始新一首作品，应显式调 POST /api/project/reset。
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


# ---------- 视觉驱动模式：AI 根据 Logic Pro 截图自主规划操作 ----------
class VisualIn(BaseModel):
    goal: str = Field(description="视觉规划目标，如「把副歌主旋律复制到第9小节并量化鼓组」")
    max_steps: int = 20
    settle_delay: float = 1.5


@app.post("/api/visual")
async def api_visual(v: VisualIn):
    """启动视觉规划循环：AI 看着 Logic Pro 截图一步步精确操作，直到完成或超步数。

    与 /api/pipeline、/api/autonomous 的区别：
    - pipeline/autonomous 是「盲打」：AI 一次性输出整阶段动作清单；
    - visual 是「看着打」：每步先截图观察 Logic Pro 实际状态，再决定下一步，
      能根据屏幕实际反馈精确调整（如发现片段没对齐就再量化、混音器没开就先打开）。
    需要 macOS（截图）+ 在线网页 AI（多模态视觉理解）。
    """
    global _current_task, _visual
    if _current_task and not _current_task.done():
        return JSONResponse({"ok": False, "error": "已有任务在运行，请先取消"}, status_code=409)
    ai: AIEngine = _state["ai"]
    daw: DAWController = _state["daw"]
    screenshot: ScreenshotCapture = _state["screenshot"]
    _visual = VisualLoop(
        ai=ai, daw=daw, screenshot=screenshot, tracker=tracker,
        max_steps=max(1, v.max_steps), settle_delay=max(0.1, v.settle_delay),
    )

    async def _run():
        try:
            await _visual.run(v.goal)
            tracker.finish()
        except asyncio.CancelledError:
            tracker.cancel()
        except Exception as e:
            log.exception("visual 执行失败")
            tracker.fail(str(e))
            await broadcast({"type": "daw_event", "kind": "error", "message": str(e)})
        finally:
            await broadcast({"type": "daw_event", "kind": "task_finished"})

    _current_task = asyncio.create_task(_run())
    return {"ok": True, "mode": "visual", "goal": v.goal,
            "max_steps": v.max_steps, "screenshot_available": screenshot.available}


@app.post("/api/screenshot")
async def api_screenshot(tag: str = "manual"):
    """手动触发一次 Logic Pro 窗口截图，返回路径。"""
    sc: ScreenshotCapture = _state["screenshot"]
    if not sc:
        return {"ok": False, "error": "截图工具未初始化"}
    path = await sc.capture(tag=tag)
    if not path:
        return {"ok": False, "error": "截图失败（非 macOS 或 Logic Pro 未运行）",
                "available": sc.available}
    await broadcast({"type": "daw_event", "kind": "screenshot_captured",
                     "step": 0, "path": path, "tag": tag})
    return {"ok": True, "path": path, "available": sc.available}


@app.get("/api/screenshot/latest")
async def api_screenshot_latest():
    """获取最近一次截图的路径与可下载文件。"""
    sc: ScreenshotCapture = _state["screenshot"]
    if not sc:
        return {"ok": False, "error": "截图工具未初始化"}
    latest = sc.latest
    if not latest:
        return {"ok": False, "error": "尚无截图", "available": sc.available}
    return {"ok": True, "path": latest, "available": sc.available}


@app.get("/api/screenshot/file")
async def api_screenshot_file(path: str):
    """下载/预览指定路径的截图文件（必须在 screenshot_dir 之下，防越权）。"""
    sc: ScreenshotCapture = _state["screenshot"]
    if not sc:
        return JSONResponse({"error": "截图工具未初始化"}, status_code=500)
    target = Path(path).expanduser()
    try:
        target.resolve().relative_to(sc.screenshot_dir.resolve())
    except ValueError:
        return JSONResponse({"error": "路径不在允许的目录内"}, status_code=403)
    if not target.exists() or not target.is_file():
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    return FileResponse(target, media_type="image/png", filename=target.name)


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
