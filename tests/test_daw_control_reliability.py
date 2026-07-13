"""单元测试：DAW 控制链路可靠性——修复 AI 无法正确控制 Logic Pro 的根因。

覆盖 P0/P1 级修复：
- _send_key/_menu_click 默认 auto_dismiss=False（不误关功能对话框）
- DAWController._call_applescript 用 asyncio.to_thread（不阻塞事件循环）
- _call_applescript 捕获异常并 emit daw_error（错误透传给 AI/前端）
- update_settings/api_provider_switch 检查 _current_task（运行中禁止重建引擎）
- _menu_click 支持任意层级递归 + 省略号兼容
- KEY_COMMANDS 修正后的键码（open_mixer=X 无修饰，toggle_loop=C）
- _run_async 超时时 kill 子进程
运行 `pytest tests/test_daw_control_reliability.py`
"""
import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

from backend.applescript_bridge import AppleScriptBridge, KEY_COMMANDS
from backend.daw_controller import DAWController


# ---------- _send_key 默认不 auto_dismiss（P0-1 修复） ----------

def test_send_key_default_no_auto_dismiss():
    """_send_key 默认 auto_dismiss=False，不误关刚打开的功能对话框。

    这是 P0 级修复：原来 auto_dismiss 默认 True，导致 new_track→return 序列里
    第二步 return 之前把刚打开的 New Tracks 对话框关掉了，轨道永远建不出来。
    """
    bridge = AppleScriptBridge.__new__(AppleScriptBridge)
    bridge.app_name = "Logic Pro"
    bridge.render_dir = MagicMock()
    bridge._available = False
    bridge._run = MagicMock(return_value="")
    bridge.dismiss_dialogs = MagicMock(return_value=0)
    bridge._send_key("key code 49", delay=0.1)  # 不传 auto_dismiss
    bridge.dismiss_dialogs.assert_not_called()


# ---------- KEY_COMMANDS 修正（P1-4 修复） ----------

def test_open_mixer_has_no_command_modifier():
    """open_mixer 应是 X 无修饰（Cmd-X 是剪切，会删片段！）。"""
    cmd = KEY_COMMANDS["open_mixer"]
    assert "command" not in cmd, f"open_mixer 不应含 command 修饰（Cmd-X 是剪切）：{cmd}"
    assert 'keystroke "x"' in cmd


def test_open_inspector_has_no_command_modifier():
    """open_inspector 应是 I 无修饰（Cmd-I 是 Import）。"""
    cmd = KEY_COMMANDS["open_inspector"]
    assert "command" not in cmd, f"open_inspector 不应含 command 修饰：{cmd}"


def test_toggle_loop_uses_c_not_cmd_l():
    """toggle_loop 应是 C（Cycle），不是 Cmd-L（Cmd-L 打开 Loop Browser 窗口！）。"""
    cmd = KEY_COMMANDS["toggle_loop"]
    assert "command" not in cmd.lower() or "key code 8" in cmd, \
        f"toggle_loop 不应用 Cmd-L（会打开 Loop Browser）：{cmd}"


def test_toggle_metronome_not_cmd_k():
    """toggle_metronome 不应是 Cmd-K（Cmd-K 打开 Key Commands 编辑器！）。"""
    cmd = KEY_COMMANDS["toggle_metronome"]
    # 不应是 'keystroke "k" using {command down}'
    assert not ('keystroke "k"' in cmd and "command down" in cmd), \
        f"toggle_metronome 不应用 Cmd-K（会打开 Key Commands 编辑器）：{cmd}"


def test_open_piano_roll_uses_key_6():
    """open_piano_roll 应是数字键 6（Logic 默认），不是 Cmd-P。"""
    cmd = KEY_COMMANDS["open_piano_roll"]
    assert "key code 22" in cmd or "6" in cmd, \
        f"open_piano_roll 应用数字键 6（Logic 默认），不是 Cmd-P：{cmd}"


def test_join_regions_is_cmd_j_not_cmd_opt_shift_j():
    """join_regions 应是 Cmd-J，不是 Cmd-Opt-Shift-J。"""
    cmd = KEY_COMMANDS["join_regions"]
    assert "command down" in cmd
    assert "option" not in cmd or "shift" not in cmd, \
        f"join_regions 不应是 Cmd-Opt-Shift-J：{cmd}"


# ---------- DAWController._call_applescript 用 to_thread（P0-2 修复） ----------

def test_call_applescript_uses_to_thread():
    """_call_applescript 应通过 asyncio.to_thread 执行，不阻塞事件循环。

    这是 P0 级修复：原来直接在 async def 里调同步 subprocess.run，
    会让整个事件循环停转，WebSocket 断连、截图饥饿、cancel 传不进来。
    """
    src = inspect.getsource(DAWController._call_applescript)
    assert "asyncio.to_thread" in src, \
        "_call_applescript 必须用 asyncio.to_thread 执行同步调用"


def test_call_applescript_noop_in_sim_mode():
    """模拟模式（_real=False）下应直接返回 None，不调 applescript。"""
    ctrl = DAWController.__new__(DAWController)
    ctrl.use_applescript = True
    ctrl.applescript = MagicMock()
    ctrl.applescript.available = False  # _real=False
    ctrl.event_cb = None
    result = asyncio.run(ctrl._call_applescript("play"))
    assert result is None


def test_call_applescript_routes_to_thread():
    """_real=True 时应通过 to_thread 调用 applescript 方法。"""
    ctrl = DAWController.__new__(DAWController)
    ctrl.use_applescript = True
    ctrl.applescript = MagicMock()
    ctrl.applescript.available = True
    ctrl.applescript.play = MagicMock(return_value="ok")
    ctrl.event_cb = None
    result = asyncio.run(ctrl._call_applescript("play"))
    assert result == "ok"
    ctrl.applescript.play.assert_called_once()


def test_call_applescript_emits_error_on_exception():
    """applescript 方法抛异常时，应 emit daw_error 事件，让 AI 能看到失败。"""
    ctrl = DAWController.__new__(DAWController)
    ctrl.use_applescript = True
    ctrl.applescript = MagicMock()
    ctrl.applescript.available = True
    ctrl.applescript.play = MagicMock(side_effect=RuntimeError("Logic Pro 未运行"))
    ctrl.event_cb = None
    # 捕获 emit 调用
    emit_calls = []
    async def capture_emit(**payload):
        emit_calls.append(payload)
    ctrl.emit = capture_emit
    # 不应抛异常（应被捕获）
    result = asyncio.run(ctrl._call_applescript("play"))
    assert result is None
    # 应有 error 日志和 daw_error 事件
    assert any(c.get("kind") == "daw_error" for c in emit_calls), \
        f"应 emit daw_error 事件，实际：{emit_calls}"


def test_call_applescript_reraises_cancelled_error():
    """asyncio.CancelledError 应重新抛出，不被吞掉（让取消信号能传播）。"""
    ctrl = DAWController.__new__(DAWController)
    ctrl.use_applescript = True
    ctrl.applescript = MagicMock()
    ctrl.applescript.available = True
    ctrl.applescript.play = MagicMock(side_effect=asyncio.CancelledError())
    ctrl.event_cb = None
    import pytest
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ctrl._call_applescript("play"))


# ---------- update_settings / api_provider_switch 守卫（P1-8 修复） ----------

def test_update_settings_rejected_while_task_running():
    """任务运行中，update_settings 应返回 409，不重建引擎。"""
    import backend.server as srv

    async def run_test():
        # 在事件循环内构造一个"正在运行"的 task
        async def noop():
            await asyncio.sleep(100)
        srv._current_task = asyncio.ensure_future(noop())
        try:
            resp = await srv.update_settings(srv.SettingsIn(timeout=200))
            if hasattr(resp, "status_code"):
                assert resp.status_code == 409
            else:
                assert resp.get("ok") is False
                assert "运行" in resp.get("error", "")
        finally:
            srv._current_task.cancel()
            try:
                await asyncio.sleep(0.01)
            except Exception:
                pass
            srv._current_task = None

    asyncio.run(run_test())


def test_provider_switch_rejected_while_task_running():
    """任务运行中，api_provider_switch 应返回 409。"""
    import backend.server as srv

    async def run_test():
        async def noop():
            await asyncio.sleep(100)
        srv._current_task = asyncio.ensure_future(noop())
        try:
            resp = await srv.api_provider_switch(srv.ProviderSwitchIn(provider="kimi"))
            if hasattr(resp, "status_code"):
                assert resp.status_code == 409
            else:
                assert resp.get("ok") is False
        finally:
            srv._current_task.cancel()
            try:
                await asyncio.sleep(0.01)
            except Exception:
                pass
            srv._current_task = None

    asyncio.run(run_test())


# ---------- _menu_click 递归 + 省略号兼容（P1-6 修复） ----------

def test_menu_click_supports_three_level_path():
    """_menu_click 应支持三级菜单路径（File > Export > All Tracks as Audio Files…）。"""
    bridge = AppleScriptBridge.__new__(AppleScriptBridge)
    bridge.app_name = "Logic Pro"
    bridge.render_dir = MagicMock()
    bridge._available = False
    captured_script = []
    bridge._run = lambda script, **kw: captured_script.append(script) or ""
    bridge._menu_click(["File", "Export", "All Tracks as Audio Files…"], delay=0.1)
    assert len(captured_script) == 1
    script = captured_script[0]
    # 应构造出递归菜单路径：menu item "All Tracks..." of menu 1 of menu item "Export" of menu 1 of menu bar item "File"
    assert 'menu bar item "File"' in script
    assert 'menu item "Export"' in script
    assert 'menu item "All Tracks as Audio Files…"' in script


def test_menu_click_fallback_fuzzy_match():
    """_menu_click 精确匹配失败时应遍历菜单项做模糊匹配（兼容 …/...）。"""
    bridge = AppleScriptBridge.__new__(AppleScriptBridge)
    bridge.app_name = "Logic Pro"
    bridge.render_dir = MagicMock()
    bridge._available = False
    captured_script = []
    bridge._run = lambda script, **kw: captured_script.append(script) or ""
    # 传 ASCII ... 但 Logic Pro 用 Unicode …
    bridge._menu_click(["File", "Export", "All Tracks as Audio Files..."], delay=0.1)
    script = captured_script[0]
    # 应有 on error 兜底 + 遍历菜单项的逻辑
    assert "on error" in script
    assert "repeat with mi in menu items" in script
    # 模糊匹配关键词应是去掉省略号后的
    assert 'contains "All Tracks as Audio Files"' in script


# ---------- _run_async 超时 kill 子进程（P3-17 修复） ----------

def test_run_async_kills_process_on_timeout():
    """_run_async 超时应 kill 子进程，避免僵尸 osascript 继续跑。"""
    src = inspect.getsource(AppleScriptBridge._run_async)
    assert "proc.kill" in src, "_run_async 超时时必须 kill 子进程"
    assert "asyncio.CancelledError" in src, "_run_async 必须处理 CancelledError"


# ---------- 综合验证：DAWController 方法的源码不再直接同步调用 applescript ----------

def test_daw_controller_methods_use_call_applescript_not_direct():
    """DAWController 的 async 方法不应直接调 self.applescript.xxx()（应走 _call_applescript）。

    这是 P0-2 修复的核心验证：所有同步 applescript 调用必须走 asyncio.to_thread。
    例外：import_midi_file/bounce_stems/bounce_master 本身是 async 方法，可直接 await。
    """
    src = inspect.getsource(DAWController)
    # 找所有 "self.applescript.xxx(" 但不是 "await self.applescript." 且不是属性访问
    # 且不是在 _call_applescript 方法内部
    lines = src.split("\n")
    violations = []
    in_call_applescript_def = False
    for i, line in enumerate(lines, 1):
        if "async def _call_applescript" in line:
            in_call_applescript_def = True
            continue
        if in_call_applescript_def and line and not line[0].isspace():
            in_call_applescript_def = False
        if in_call_applescript_def:
            continue  # 跳过 _call_applescript 方法内部
        # 检查直接同步调用（非 await）
        if "self.applescript." in line and "await" not in line:
            # 排除属性访问（available, render_dir）和定义行
            if any(attr in line for attr in [".available", ".render_dir", "self.applescript ="]):
                continue
            # 排除注释
            stripped = line.lstrip()
            if stripped.startswith("#") or stripped.startswith('"""'):
                continue
            violations.append((i, line.rstrip()))
    # 允许 dismiss_dialogs 同步方法里的调用（它本身就是同步方法）
    real_violations = [v for v in violations if "dismiss_dialogs" not in v[1] or "def dismiss_dialogs" not in v[1]]
    assert not real_violations, \
        f"DAWController 不应在 async 方法里直接同步调 self.applescript.xxx()，应走 _call_applescript：\n" + \
        "\n".join(f"  L{ln}: {code}" for ln, code in real_violations[:5])
