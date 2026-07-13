"""单元测试：Logic Pro 弹窗处理——确保 AI 控制 DAW 时不会被弹窗卡住。

覆盖：
- AppleScriptBridge 提供 dismiss_dialogs / has_dialog 方法
- _send_key / _menu_click 前置自动 dismiss 残留弹窗
- activate 启动时清场（关 Missing Audio Units 等警告）
- save_as / bounce_master / bounce_stems 主动点 Replace 确认覆盖
- DAWController 提供 dismiss_dialogs 高层 API
- ui_action(op="dismiss_dialog") 路由到 applescript.dismiss_dialogs
- UIAction 接受 "dismiss_dialog" 字符串形式
运行 `pytest tests/test_dialog_handling.py`
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from backend.applescript_bridge import AppleScriptBridge
from backend.daw_controller import DAWController
from backend.models import UIAction


# ---------- AppleScriptBridge 弹窗方法存在性 ----------

def test_bridge_has_dismiss_and_has_dialog_methods():
    """AppleScriptBridge 必须提供 dismiss_dialogs / has_dialog 方法。"""
    bridge = AppleScriptBridge.__new__(AppleScriptBridge)
    bridge.app_name = "Logic Pro"
    bridge.render_dir = MagicMock()
    bridge._available = False  # 模拟模式
    assert callable(getattr(bridge, "dismiss_dialogs", None)), \
        "AppleScriptBridge 必须有 dismiss_dialogs 方法（关闭弹窗）"
    assert callable(getattr(bridge, "has_dialog", None)), \
        "AppleScriptBridge 必须有 has_dialog 方法（检测弹窗）"


def test_dismiss_dialogs_returns_int():
    """dismiss_dialogs 应返回整数（关闭的弹窗数），模拟模式返回 0。"""
    bridge = AppleScriptBridge.__new__(AppleScriptBridge)
    bridge.app_name = "Logic Pro"
    bridge.render_dir = MagicMock()
    bridge._available = False
    closed = bridge.dismiss_dialogs(action="cancel", max_count=5)
    assert isinstance(closed, int)
    assert closed == 0  # 模拟模式不执行 AppleScript，关闭 0 个


def test_has_dialog_returns_bool():
    """has_dialog 应返回 bool，模拟模式返回 False。"""
    bridge = AppleScriptBridge.__new__(AppleScriptBridge)
    bridge.app_name = "Logic Pro"
    bridge.render_dir = MagicMock()
    bridge._available = False
    assert bridge.has_dialog() is False


def test_dismiss_dialogs_accepts_all_action_modes():
    """dismiss_dialogs 应接受 cancel/confirm/ok 三种动作模式。"""
    bridge = AppleScriptBridge.__new__(AppleScriptBridge)
    bridge.app_name = "Logic Pro"
    bridge.render_dir = MagicMock()
    bridge._available = False
    # 三种模式都不应抛异常
    for action in ("cancel", "confirm", "ok"):
        bridge.dismiss_dialogs(action=action, max_count=1)


# ---------- _send_key / _menu_click 前置自动 dismiss ----------

def test_send_key_auto_dismiss_before_sending():
    """_send_key(auto_dismiss=True) 应在发键前调 dismiss_dialogs。"""
    bridge = AppleScriptBridge.__new__(AppleScriptBridge)
    bridge.app_name = "Logic Pro"
    bridge.render_dir = MagicMock()
    bridge._available = False
    bridge._run = MagicMock(return_value="")
    bridge.dismiss_dialogs = MagicMock(return_value=0)
    bridge._send_key("key code 49", delay=0.1, auto_dismiss=True)
    bridge.dismiss_dialogs.assert_called_once_with(action="cancel", max_count=3)


def test_send_key_auto_dismiss_can_be_disabled():
    """auto_dismiss=False 时不应调 dismiss_dialogs（用于 dismiss 自身避免递归）。"""
    bridge = AppleScriptBridge.__new__(AppleScriptBridge)
    bridge.app_name = "Logic Pro"
    bridge.render_dir = MagicMock()
    bridge._available = False
    bridge._run = MagicMock(return_value="")
    bridge.dismiss_dialogs = MagicMock(return_value=0)
    bridge._send_key("key code 49", delay=0.1, auto_dismiss=False)
    bridge.dismiss_dialogs.assert_not_called()


def test_menu_click_auto_dismiss_before_clicking():
    """_menu_click 应在点菜单前调 dismiss_dialogs。"""
    bridge = AppleScriptBridge.__new__(AppleScriptBridge)
    bridge.app_name = "Logic Pro"
    bridge.render_dir = MagicMock()
    bridge._available = False
    bridge._run = MagicMock(return_value="")
    bridge.dismiss_dialogs = MagicMock(return_value=0)
    bridge._menu_click(["File", "Save"], delay=0.1)
    bridge.dismiss_dialogs.assert_called_once_with(action="cancel", max_count=3)


# ---------- activate 启动时清场 ----------

def test_activate_clears_startup_dialogs():
    """activate 应在激活后调 dismiss_dialogs(action='ok')，清掉启动警告弹窗。"""
    bridge = AppleScriptBridge.__new__(AppleScriptBridge)
    bridge.app_name = "Logic Pro"
    bridge.render_dir = MagicMock()
    bridge._available = False
    bridge._run = MagicMock(return_value="")
    bridge.dismiss_dialogs = MagicMock(return_value=0)
    bridge.activate()
    # 应该用 ok 模式点确定（启动警告点 Cancel 反而卡住）
    bridge.dismiss_dialogs.assert_called_once_with(action="ok", max_count=5)


# ---------- save_as / bounce 主动点 Replace 确认覆盖 ----------

def test_save_as_dismisses_replace_dialog():
    """save_as 完成后应调 dismiss_dialogs(confirm)，点 Replace 确认覆盖。"""
    bridge = AppleScriptBridge.__new__(AppleScriptBridge)
    bridge.app_name = "Logic Pro"
    bridge.render_dir = MagicMock()
    bridge._available = False
    bridge._run = MagicMock(return_value="")
    bridge._run_async = AsyncMock(return_value="")
    bridge._send_key = MagicMock()
    bridge.dismiss_dialogs = MagicMock(return_value=0)
    bridge.save_as("/tmp/test.logicx")
    # 应该有 confirm 模式的 dismiss_dialogs 调用
    confirm_calls = [c for c in bridge.dismiss_dialogs.call_args_list
                     if c.kwargs.get("action") == "confirm"]
    assert len(confirm_calls) == 1, "save_as 应在末尾调 dismiss_dialogs(action='confirm')"


def test_bounce_master_dismisses_replace_dialog():
    """bounce_master 应在导出后调 dismiss_dialogs(confirm)，点 Replace 确认覆盖。"""
    bridge = AppleScriptBridge.__new__(AppleScriptBridge)
    bridge.app_name = "Logic Pro"
    bridge.render_dir = MagicMock()
    bridge._available = False
    bridge._run = MagicMock(return_value="")
    bridge._run_async = AsyncMock(return_value="")
    bridge._send_key = MagicMock()
    bridge.set_cycle = MagicMock()
    bridge.dismiss_dialogs = MagicMock(return_value=0)
    asyncio.run(bridge.bounce_master("test", fmt="wav"))
    # 应有 confirm 调用（处理「文件已存在/是否替换」）
    confirm_calls = [c for c in bridge.dismiss_dialogs.call_args_list
                     if c.kwargs.get("action") == "confirm"]
    assert len(confirm_calls) >= 1, "bounce_master 应调 dismiss_dialogs(action='confirm')"


def test_bounce_stems_dismisses_replace_dialog():
    """bounce_stems 应在导出后调 dismiss_dialogs(confirm)。"""
    bridge = AppleScriptBridge.__new__(AppleScriptBridge)
    bridge.app_name = "Logic Pro"
    bridge.render_dir = MagicMock()
    bridge._available = False
    bridge._run = MagicMock(return_value="")
    bridge._run_async = AsyncMock(return_value="")
    bridge._menu_click = MagicMock()
    bridge.dismiss_dialogs = MagicMock(return_value=0)
    asyncio.run(bridge.bounce_stems("stem", fmt="wav"))
    confirm_calls = [c for c in bridge.dismiss_dialogs.call_args_list
                     if c.kwargs.get("action") == "confirm"]
    assert len(confirm_calls) >= 1


# ---------- DAWController 高层 API ----------

def test_daw_controller_has_dismiss_dialogs_method():
    """DAWController 应提供 dismiss_dialogs 高层 API。"""
    ctrl = DAWController.__new__(DAWController)
    assert callable(getattr(ctrl, "dismiss_dialogs", None)), \
        "DAWController 必须有 dismiss_dialogs 方法（让 AI/commander 能主动清弹窗）"


def test_daw_controller_dismiss_dialogs_routes_to_applescript():
    """DAWController.dismiss_dialogs 应委托给 applescript.dismiss_dialogs。"""
    ctrl = DAWController.__new__(DAWController)
    ctrl.use_applescript = True
    ctrl.applescript = MagicMock()
    ctrl.applescript.available = True  # _real=True
    ctrl.applescript.dismiss_dialogs = MagicMock(return_value=2)
    closed = ctrl.dismiss_dialogs(action="cancel", max_count=5)
    assert closed == 2
    ctrl.applescript.dismiss_dialogs.assert_called_once_with(
        action="cancel", max_count=5)


def test_daw_controller_dismiss_dialogs_noop_in_sim_mode():
    """模拟模式（_real=False）下 dismiss_dialogs 应返回 0，不调 applescript。"""
    ctrl = DAWController.__new__(DAWController)
    ctrl.use_applescript = True
    ctrl.applescript = MagicMock()
    ctrl.applescript.available = False  # _real=False
    ctrl.applescript.dismiss_dialogs = MagicMock(return_value=0)
    closed = ctrl.dismiss_dialogs(action="cancel")
    assert closed == 0


# ---------- ui_action(op="dismiss_dialog") 路由 ----------

def test_ui_action_dismiss_dialog_routes_to_applescript():
    """ui_action(op="dismiss_dialog") 应调 applescript.dismiss_dialogs。"""
    ctrl = DAWController.__new__(DAWController)
    ctrl.event_cb = None
    ctrl.use_applescript = True
    ctrl.applescript = MagicMock()
    ctrl.applescript.available = True  # _real=True
    ctrl.applescript.dismiss_dialogs = MagicMock(return_value=1)
    ctrl.current_project_path = "/tmp/test.logicx"
    ctrl.current_project_title = "测试"
    ctrl.project_locked = True
    ctrl._track_index = {}
    asyncio.run(ctrl.ui_action(UIAction(op="dismiss_dialog")))
    ctrl.applescript.dismiss_dialogs.assert_called_once()


def test_uiaction_accepts_dismiss_dialog_string():
    """UIAction 应接受 "dismiss_dialog" 字符串形式（AI 常输出字符串数组）。"""
    a = UIAction.model_validate("dismiss_dialog")
    assert a.op == "dismiss_dialog"


# ---------- dismiss_dialogs 源码不含递归调用 ----------

def test_dismiss_dialogs_does_not_call_send_key():
    """dismiss_dialogs 不应调 _send_key（否则会递归：_send_key 又调 dismiss_dialogs）。

    实现上 dismiss_dialogs 应直接用 _run 而非 _send_key。
    """
    import inspect
    src = inspect.getsource(AppleScriptBridge.dismiss_dialogs)
    # dismiss_dialogs 内部不应调 self._send_key（会触发 auto_dismiss 递归）
    assert "self._send_key" not in src, \
        "dismiss_dialogs 不应调 _send_key（会递归触发 auto_dismiss）"


def test_dismiss_dialogs_uses_run_directly():
    """dismiss_dialogs 应直接用 self._run 执行 AppleScript。"""
    import inspect
    src = inspect.getsource(AppleScriptBridge.dismiss_dialogs)
    assert "self._run(" in src, "dismiss_dialogs 应直接用 self._run 执行 AppleScript"
