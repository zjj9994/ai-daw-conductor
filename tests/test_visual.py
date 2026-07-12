"""单元测试：截图工具与视觉规划循环。运行 `pytest tests/`

覆盖：
- ScreenshotCapture 在非 macOS 下的降级行为（available=False, capture 返回 None）
- VisualLoop 在 demo 模式（AI 离线）直接结束、不进入循环
- VisualLoop 完整闭环（mock AI 在第 N 步返回 done=True）：截图→规划→执行→done
- VisualLoop 取消信号传播
- VisualLoop._execute_step_actions 按序调用 DAW 方法
- VisualLoop._accumulate_history 累积操作记录
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from backend.daw_controller import DAWController
from backend.models import (
    AutomationPoint, AutomationSpec, BusSpec, MarkerSpec, MixParams,
    PluginParamSpec, PluginSpec, RegionOp, TrackSpec, TransportAction,
    UIAction, VisualStep,
)
from backend.screenshot import ScreenshotCapture
from backend.visual_loop import VisualLoop


# ---------- ScreenshotCapture ----------
def test_screenshot_unavailable_on_non_macos(monkeypatch):
    """非 macOS 环境下截图工具不可用，capture 返回 None。"""
    sc = ScreenshotCapture(app_name="Logic Pro", screenshot_dir="/tmp/test_sc_shots")
    if not sc.available:
        # 非 macOS 测试环境
        result = asyncio.run(sc.capture(tag="test"))
        assert result is None
        assert sc.latest is None
    else:
        # macOS 环境：available 应为 True（不实际触发截图，只验证属性）
        assert sc.available is True


def test_screenshot_latest_property():
    sc = ScreenshotCapture(screenshot_dir="/tmp/test_sc_shots2")
    assert sc.latest is None
    sc._latest = "/tmp/x.png"
    assert sc.latest == "/tmp/x.png"


def test_screenshot_list_recent_empty():
    sc = ScreenshotCapture(screenshot_dir="/tmp/test_sc_shots3")
    # 空目录应返回空列表
    assert sc.list_recent() == []


# ---------- VisualLoop ----------
def _make_daw_mock():
    """构造全方法 AsyncMock 的 DAWController 替身。"""
    daw = MagicMock(spec=DAWController)
    daw.cfg = {}
    for name in [
        "emit", "log", "transport", "ensure_track", "create_track_stack",
        "region_op", "create_bus", "apply_mix", "set_plugin_param",
        "apply_automation", "add_marker", "add_tempo_change",
        "setup_record", "apply_master", "bounce", "ui_action",
    ]:
        setattr(daw, name, AsyncMock())
    return daw


def _make_screenshot_mock(paths):
    """构造按顺序返回 paths 的截图 mock。"""
    sc = MagicMock(spec=ScreenshotCapture)
    sc.available = True
    sc.latest = paths[-1] if paths else None
    sc.capture = AsyncMock(side_effect=paths)
    return sc


def _make_ai_mock(steps):
    """构造按顺序返回 VisualStep 的 AI mock。steps: list[VisualStep]。"""
    ai = MagicMock()
    ai.online = True
    ai.plan_from_screenshot = AsyncMock(side_effect=steps)
    return ai


def test_visual_loop_no_ai_ends_immediately():
    """AI 离线时视觉循环应直接结束，不进入循环。"""
    daw = _make_daw_mock()
    sc = _make_screenshot_mock(["/tmp/s1.png"])
    ai = MagicMock()
    ai.online = False
    loop = VisualLoop(ai=ai, daw=daw, screenshot=sc, max_steps=5)

    results = asyncio.run(loop.run("目标"))

    assert results == []
    ai.plan_from_screenshot.assert_not_called()
    sc.capture.assert_not_called()


def test_visual_loop_no_screenshot_ends():
    """截图失败时视觉循环应立即终止。"""
    daw = _make_daw_mock()
    sc = MagicMock(spec=ScreenshotCapture)
    sc.available = True
    sc.latest = None
    sc.capture = AsyncMock(return_value=None)  # 截图失败
    ai = _make_ai_mock([])
    loop = VisualLoop(ai=ai, daw=daw, screenshot=sc, max_steps=5)

    results = asyncio.run(loop.run("目标"))

    assert results == []
    ai.plan_from_screenshot.assert_not_called()
    sc.capture.assert_awaited_once()


def test_visual_loop_completes_when_ai_says_done():
    """AI 第一步就返回 done=True，循环应执行 1 步后结束。"""
    daw = _make_daw_mock()
    sc = _make_screenshot_mock(["/tmp/s1.png"])
    step = VisualStep(
        observation="看到 Logic Pro 已打开",
        plan="目标已达成",
        done=True,
        rationale="已是期望状态",
    )
    ai = _make_ai_mock([step])
    loop = VisualLoop(ai=ai, daw=daw, screenshot=sc, max_steps=5)

    results = asyncio.run(loop.run("打开 Logic Pro"))

    assert len(results) == 1
    assert results[0].done is True
    ai.plan_from_screenshot.assert_awaited_once()
    sc.capture.assert_awaited_once()


def test_visual_loop_runs_multiple_steps_until_done():
    """AI 前 2 步 done=False，第 3 步 done=True，应执行 3 步。"""
    daw = _make_daw_mock()
    sc = _make_screenshot_mock([f"/tmp/s{i}.png" for i in range(1, 4)])
    steps = [
        VisualStep(observation="o1", plan="p1", done=False,
                   transports=[TransportAction(op="goto", bar=1)]),
        VisualStep(observation="o2", plan="p2", done=False,
                   region_ops=[RegionOp(op="quantize", track="鼓组", grid="1/16")]),
        VisualStep(observation="o3", plan="p3", done=True, rationale="完成"),
    ]
    ai = _make_ai_mock(steps)
    loop = VisualLoop(ai=ai, daw=daw, screenshot=sc, max_steps=10, settle_delay=0)

    results = asyncio.run(loop.run("量化鼓组"))

    assert len(results) == 3
    assert results[-1].done is True
    assert sc.capture.await_count == 3
    assert ai.plan_from_screenshot.await_count == 3
    # 第 1 步的 transport 应被执行
    daw.transport.assert_awaited_once()
    # 第 2 步的 region_op 应被执行
    daw.region_op.assert_awaited_once()


def test_visual_loop_max_steps_stop():
    """AI 一直不说 done，达到 max_steps 应停止。"""
    daw = _make_daw_mock()
    sc = _make_screenshot_mock([f"/tmp/s{i}.png" for i in range(1, 10)])
    # 所有步都 done=False
    steps = [VisualStep(observation="o", plan="p", done=False) for _ in range(10)]
    ai = _make_ai_mock(steps)
    loop = VisualLoop(ai=ai, daw=daw, screenshot=sc, max_steps=3, settle_delay=0)

    results = asyncio.run(loop.run("目标"))

    assert len(results) == 3
    assert all(not r.done for r in results)


def test_visual_loop_cancel_stops():
    """取消信号应在下一个迭代检查点中断视觉循环。"""
    daw = _make_daw_mock()
    sc = _make_screenshot_mock([f"/tmp/s{i}.png" for i in range(1, 10)])
    ai = MagicMock()
    ai.online = True

    async def _plan_then_cancel(*a, **kw):
        # 第一次规划完成后触发取消，下一迭代开头检查点会 break
        loop.cancel()
        return VisualStep(observation="o", plan="p", done=False)
    ai.plan_from_screenshot = _plan_then_cancel
    loop = VisualLoop(ai=ai, daw=daw, screenshot=sc, max_steps=10, settle_delay=0)

    results = asyncio.run(loop.run("目标"))

    # 第 1 步规划后 cancel=True，第 2 步迭代开头 break，故只 1 步
    assert len(results) == 1


def test_visual_loop_executes_all_action_types():
    """VisualStep 含全部动作字段时，_execute_step_actions 应调用对应 DAW 方法。"""
    daw = _make_daw_mock()
    step = VisualStep(
        observation="o", plan="p", done=False,
        transports=[TransportAction(op="goto", bar=1)],
        tracks=[TrackSpec(name="主旋律")],
        track_stacks=[],
        region_ops=[RegionOp(op="quantize", track="鼓组")],
        mix=[MixParams(track="主旋律", volume_db=-6)],
        buses=[BusSpec(name="Reverb Bus")],
        plugin_params=[PluginParamSpec(track="鼓组", plugin="Compressor",
                                        parameter="Threshold", value=-20)],
        automation=[AutomationSpec(track="主旋律", parameter="Volume",
                                    points=[AutomationPoint(bar=1, value=-6)])],
        markers=[MarkerSpec(name="Chorus", bar=9)],
        master_plugins=[PluginSpec(name="Limiter")],
        actions=[UIAction(op="save")],
    )
    sc = _make_screenshot_mock(["/tmp/s1.png", "/tmp/s2.png"])
    ai = _make_ai_mock([step, VisualStep(observation="done", plan="done", done=True)])
    loop = VisualLoop(ai=ai, daw=daw, screenshot=sc, max_steps=5, settle_delay=0)

    asyncio.run(loop.run("全动作测试"))

    daw.transport.assert_awaited_once()
    daw.ensure_track.assert_awaited_once()
    daw.region_op.assert_awaited_once()
    daw.create_bus.assert_awaited_once()
    daw.apply_mix.assert_awaited_once()
    daw.set_plugin_param.assert_awaited_once()
    daw.apply_automation.assert_awaited_once()
    daw.add_marker.assert_awaited_once()
    daw.apply_master.assert_awaited_once()
    daw.ui_action.assert_awaited_once()


# ---------- VisualStep 模型 ----------
def test_visual_step_defaults():
    s = VisualStep(observation="o", plan="p")
    assert s.done is False
    assert s.transports == []
    assert s.tracks == []
    assert s.region_ops == []
    assert s.mix == []
    assert s.actions == []


def test_visual_step_full():
    s = VisualStep(
        observation="看到混音器", plan="打开混音器", done=False,
        actions=[UIAction(op="open_mixer")],
        rationale="需要看到音量推子",
    )
    assert s.observation == "看到混音器"
    assert len(s.actions) == 1
    assert s.rationale == "需要看到音量推子"


# ---------- _accumulate_history ----------
def test_accumulate_history():
    loop = VisualLoop(
        ai=MagicMock(), daw=_make_daw_mock(),
        screenshot=MagicMock(spec=ScreenshotCapture),
    )
    step = VisualStep(
        observation="o", plan="p1", done=False,
        transports=[TransportAction(op="goto", bar=1)],
        region_ops=[RegionOp(op="quantize", track="鼓组")],
    )
    h = loop._accumulate_history("", step, 1)
    assert "步骤1" in h
    assert "p1" in h
    assert "传输" in h
    assert "片段" in h

    step2 = VisualStep(observation="o2", plan="p2", done=True)
    h2 = loop._accumulate_history(h, step2, 2)
    assert "步骤1" in h2
    assert "步骤2" in h2
