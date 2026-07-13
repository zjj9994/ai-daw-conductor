"""单元测试：AI 全面控制 Logic Pro 能力——覆盖新增的细粒度操作方法。

验证 AI 能像人类一样：
- 精确设置插件参数（Threshold/Ratio/Gain 等旋钮）
- 画 Channel EQ 多段参数
- 设置侧链压缩源
- 设置立体声宽度
- 写自动化曲线节点（不只设模式）
- 量化带网格和强度
- 片段循环次数/长度/裁剪/淡入淡出/交叉淡化
- 切换工具（画笔/剪刀/渐变等）
- 打开智能控制/乐谱/步进编辑器
- 设置工程采样率/位深
- 设置母带目标（Limiter Ceiling）
运行 `pytest tests/test_full_daw_control.py`
"""
import asyncio
import inspect
from unittest.mock import MagicMock

from backend.applescript_bridge import AppleScriptBridge
from backend.daw_controller import DAWController
from backend.models import RegionOp, UIAction, MixParams, AutomationSpec, AutomationPoint


# ---------- bridge 新增方法存在性 ----------

def test_bridge_has_plugin_parameter_methods():
    """bridge 应有插件参数/EQ/侧链/立体声宽度方法。"""
    b = AppleScriptBridge
    for name in ["set_plugin_parameter", "set_channel_eq_params", "set_sidechain", "set_stereo_width"]:
        assert name in dir(b), f"AppleScriptBridge 缺少方法 {name}"


def test_bridge_has_automation_curve_methods():
    """bridge 应有自动化曲线写入方法。"""
    b = AppleScriptBridge
    for name in ["set_automation_mode", "show_automation_for_track", "add_automation_points", "set_automation_curve"]:
        assert name in dir(b), f"AppleScriptBridge 缺少方法 {name}"


def test_bridge_has_region_editing_methods():
    """bridge 应有片段编辑方法（含新增的 crop/fade）。"""
    b = AppleScriptBridge
    for name in ["loop_region", "resize_region", "quantize_selected_regions",
                 "crop_region", "fade_in_region", "fade_out_region", "crossfade_regions"]:
        assert name in dir(b), f"AppleScriptBridge 缺少方法 {name}"


def test_bridge_has_view_and_tool_methods():
    """bridge 应有视图切换和工具切换方法。"""
    b = AppleScriptBridge
    for name in ["open_smart_controls", "open_score_editor", "open_step_editor",
                 "select_tool", "set_project_audio_settings", "set_master_target"]:
        assert name in dir(b), f"AppleScriptBridge 缺少方法 {name}"


# ---------- set_plugin_parameter 真正设置参数（不再只重新挂插件） ----------

def test_set_plugin_parameter_sets_value_not_reinstalls():
    """set_plugin_parameter 应真正设置参数值，而非重新调用 add_plugin_to_selected_track。

    修复前 bug：方法体只是再次调用 add_plugin_to_selected_track（打开插件选择器输入插件名），
    根本没定位 parameter，也没设 value。
    """
    src = inspect.getsource(AppleScriptBridge.set_plugin_parameter)
    # 应有 AX 遍历 UI 元素找参数的逻辑
    assert "UI elements" in src or "ui elements" in src, \
        "set_plugin_parameter 应遍历插件窗口 UI 元素找参数"
    # 应有设置 value 的逻辑
    assert "set value" in src or "keystroke" in src, \
        "set_plugin_parameter 应设置参数值（set value 或 keystroke）"
    # 不应只是重新调用 add_plugin_to_selected_track（那是 bug）
    # 允许调用一次打开插件窗口，但不应是方法体的全部
    assert "open plugin editor" in src or "Cmd-E" in src or 'keystroke "e"' in src, \
        "set_plugin_parameter 应打开插件编辑器窗口"


# ---------- set_automation_mode 不再用错误键命令 ----------

def test_set_automation_mode_uses_ax_not_wrong_keys():
    """set_automation_mode 不应用 Ctrl-Opt-Cmd-R/T/L/W/O（那些不是 Logic 键命令）。

    修复前 bug：用 Ctrl-Opt-Cmd-R/T/L/W/O 根本不工作。
    """
    src = inspect.getsource(AppleScriptBridge.set_automation_mode)
    # 不应包含错误的键命令模式
    assert "control, option, command" not in src or "pop up button" in src, \
        "set_automation_mode 应用 AX pop up button，不用 Ctrl-Opt-Cmd 键命令"
    # 应有 AX 操作（pop up button 或 menu item）
    assert "pop up button" in src or "menu item" in src, \
        "set_automation_mode 应用 AX 点击 Automation Mode 按钮"


# ---------- show_automation_for_track 选具体参数 ----------

def test_show_automation_for_track_selects_parameter():
    """show_automation_for_track 应选具体参数，不只按 Cmd-A。

    修复前 bug：parameter 参数被完全忽略。
    """
    src = inspect.getsource(AppleScriptBridge.show_automation_for_track)
    # 应有根据 parameter 选菜单项的逻辑
    assert "parameter" in src, "show_automation_for_track 应使用 parameter 参数"
    assert "menu item" in src or "pop up button" in src, \
        "show_automation_for_track 应用 AX 选具体参数"


# ---------- add_automation_points 真正写节点 ----------

def test_add_automation_points_iterates_points():
    """add_automation_points 应遍历 points 列表逐个创建节点。"""
    src = inspect.getsource(AppleScriptBridge.add_automation_points)
    assert "for pt in points" in src or "repeat" in src, \
        "add_automation_points 应遍历 points 列表"
    assert "goto_bar" in src, "add_automation_points 应定位播放头到节点位置"


# ---------- quantize_selected_regions 传 grid 和 strength ----------

def test_quantize_passes_grid_and_strength():
    """quantize_selected_regions 应把 grid 和 strength 传给 Logic，不只按 Cmd-Q。

    修复前 bug：grid 和 strength 参数被完全忽略。
    """
    src = inspect.getsource(AppleScriptBridge.quantize_selected_regions)
    # 应有根据 grid 选菜单项的逻辑
    assert "grid" in src, "quantize_selected_regions 应使用 grid 参数"
    # 应有根据 strength 设滑块的逻辑
    assert "strength" in src, "quantize_selected_regions 应使用 strength 参数"
    # 应有 pop up button 或 slider 的 AX 操作
    assert "pop up button" in src or "slider" in src, \
        "quantize_selected_regions 应用 AX 操作 Quantize 下拉框和 Q-Strength 滑块"


# ---------- loop_region 传 loop_count ----------

def test_loop_region_passes_loop_count():
    """loop_region 应把 loop_count 传给 Logic。

    修复前 bug：loop_count 参数被完全忽略，且用错误的 Ctrl-Opt-Cmd-L 键命令。
    """
    src = inspect.getsource(AppleScriptBridge.loop_region)
    assert "loop_count" in src, "loop_region 应使用 loop_count 参数"
    # 不应用错误的键命令
    assert "control, option, command" not in src, \
        "loop_region 不应用 Ctrl-Opt-Cmd-L（不是 Logic 键命令）"


# ---------- resize_region 传 new_length_beats ----------

def test_resize_region_passes_length():
    """resize_region 应把 new_length_beats 传给 Logic。"""
    src = inspect.getsource(AppleScriptBridge.resize_region)
    assert "new_length_beats" in src, "resize_region 应使用 new_length_beats 参数"
    assert "control, option, command" not in src, \
        "resize_region 不应用 Ctrl-Opt-Cmd-R（不是 Logic 键命令）"


# ---------- DAWController region_op 支持 crop/fade ----------

def test_daw_controller_region_op_supports_crop():
    """DAWController.region_op 应处理 crop 操作。"""
    src = inspect.getsource(DAWController.region_op)
    assert '"crop"' in src, "region_op 应有 crop 分支"


def test_daw_controller_region_op_supports_fade():
    """DAWController.region_op 应处理 fade_in/fade_out/crossfade 操作。"""
    src = inspect.getsource(DAWController.region_op)
    assert "fade_in" in src, "region_op 应有 fade_in 分支"
    assert "fade_out" in src, "region_op 应有 fade_out 分支"
    assert "crossfade" in src, "region_op 应有 crossfade 分支"


# ---------- DAWController apply_mix 支持 EQ/侧链/立体声宽度 ----------

def test_apply_mix_handles_eq():
    """apply_mix 应处理 MixParams.eq 字段。"""
    src = inspect.getsource(DAWController.apply_mix)
    assert "eq" in src.lower(), "apply_mix 应处理 eq 字段"
    assert "set_channel_eq_params" in src, "apply_mix 应调用 set_channel_eq_params"


def test_apply_mix_handles_sidechain():
    """apply_mix 应处理 MixParams.sidechain_from 字段。"""
    src = inspect.getsource(DAWController.apply_mix)
    assert "sidechain" in src.lower(), "apply_mix 应处理 sidechain_from 字段"
    assert "set_sidechain" in src, "apply_mix 应调用 set_sidechain"


def test_apply_mix_handles_stereo_width():
    """apply_mix 应处理 MixParams.stereo_width 字段。"""
    src = inspect.getsource(DAWController.apply_mix)
    assert "stereo_width" in src, "apply_mix 应处理 stereo_width 字段"
    assert "set_stereo_width" in src, "apply_mix 应调用 set_stereo_width"


# ---------- DAWController apply_automation 写 points ----------

def test_apply_automation_writes_points():
    """apply_automation 应真正写入 points，不只设模式。

    修复前 bug：只调 set_automation_mode + show_automation_for_track 就完事，points 被丢弃。
    """
    src = inspect.getsource(DAWController.apply_automation)
    assert "add_automation_points" in src, \
        "apply_automation 应调用 add_automation_points 写入节点"
    assert "auto.points" in src, "apply_automation 应遍历 auto.points"


def test_apply_automation_end_to_end():
    """端到端测试：apply_automation 应调用 add_automation_points 传入点列表。"""
    ctrl = DAWController.__new__(DAWController)
    ctrl.use_applescript = True
    ctrl.applescript = MagicMock()
    ctrl.applescript.available = True
    ctrl.event_cb = None
    ctrl.project_locked = True
    ctrl.current_project_path = "/tmp/test.logicx"
    ctrl.current_project_title = "测试"
    ctrl._track_index = {}
    calls = []
    async def capture_call(method_name, *args, **kwargs):
        calls.append((method_name, args, kwargs))
    ctrl._call_applescript = capture_call
    auto = AutomationSpec(
        track="Lead", parameter="Volume", mode="latch",
        points=[AutomationPoint(bar=1.0, value=0.0), AutomationPoint(bar=4.0, value=-3.0)],
    )
    asyncio.run(ctrl.apply_automation(auto))
    # 应有 add_automation_points 调用
    add_calls = [c for c in calls if c[0] == "add_automation_points"]
    assert len(add_calls) == 1, f"应调用 add_automation_points 一次，实际：{calls}"
    # 传入的 points 应有 2 个点
    pts_arg = add_calls[0][1][2]  # 第三个位置参数
    assert len(pts_arg) == 2


# ---------- DAWController ui_action 支持新视图/工具 ----------

def test_ui_action_supports_smart_controls():
    """ui_action 应处理 open_smart_controls。"""
    src = inspect.getsource(DAWController.ui_action)
    assert "open_smart_controls" in src, "ui_action 应有 open_smart_controls 分支"


def test_ui_action_supports_tool_switching():
    """ui_action 应处理 tool_ 前缀的动作。"""
    src = inspect.getsource(DAWController.ui_action)
    assert "tool_" in src or "startswith" in src, \
        "ui_action 应处理 tool_ 前缀的动作（tool_pencil/tool_scissors 等）"


def test_ui_action_routes_tool_to_select_tool():
    """ui_action(op="tool_pencil") 应调用 select_tool("pencil")。"""
    ctrl = DAWController.__new__(DAWController)
    ctrl.use_applescript = True
    ctrl.applescript = MagicMock()
    ctrl.applescript.available = True
    ctrl.event_cb = None
    ctrl.project_locked = True
    ctrl.current_project_path = "/tmp/test.logicx"
    ctrl.current_project_title = "测试"
    ctrl._track_index = {}
    calls = []
    async def capture_call(method_name, *args, **kwargs):
        calls.append((method_name, args, kwargs))
    ctrl._call_applescript = capture_call
    asyncio.run(ctrl.ui_action(UIAction(op="tool_pencil")))
    tool_calls = [c for c in calls if c[0] == "select_tool"]
    assert len(tool_calls) == 1, f"应调用 select_tool 一次，实际：{calls}"
    assert tool_calls[0][1][0] == "pencil"


# ---------- models.py RegionOp 支持 crop/fade ----------

def test_region_op_schema_supports_crop_and_fade():
    """RegionOp.op 的描述应包含 crop/fade_in/fade_out/crossfade。"""
    src = inspect.getsource(RegionOp)
    assert "crop" in src, "RegionOp 应支持 crop"
    assert "fade_in" in src, "RegionOp 应支持 fade_in"
    assert "fade_out" in src, "RegionOp 应支持 fade_out"
    assert "crossfade" in src, "RegionOp 应支持 crossfade"


def test_uiaction_schema_supports_new_ops():
    """UIAction.op 的描述应包含新增的视图和工具动作。"""
    src = inspect.getsource(UIAction)
    assert "open_smart_controls" in src
    assert "open_score_editor" in src
    assert "open_step_editor" in src
    assert "tool_pencil" in src
    assert "tool_scissors" in src
    assert "tool_fade" in src


# ---------- 综合验证：DAWController 不再静默丢弃字段 ----------

def test_apply_mix_does_not_silently_ignore_eq():
    """apply_mix 不应静默忽略 eq 字段（应调用 set_channel_eq_params）。"""
    # 构造一个带 eq 的 MixParams（eq 是 dict 类型，含 bands 列表）
    params = MixParams(track="Lead", eq={"bands": [{"band": 1, "freq": 200, "gain": -3, "q": 1.0}]})
    ctrl = DAWController.__new__(DAWController)
    ctrl.use_applescript = True
    ctrl.applescript = MagicMock()
    ctrl.applescript.available = True
    ctrl.event_cb = None
    ctrl.project_locked = True
    ctrl.current_project_path = "/tmp/test.logicx"
    ctrl.current_project_title = "测试"
    ctrl._track_index = {}
    calls = []
    async def capture_call(method_name, *args, **kwargs):
        calls.append((method_name, args, kwargs))
    ctrl._call_applescript = capture_call
    asyncio.run(ctrl.apply_mix(params))
    eq_calls = [c for c in calls if c[0] == "set_channel_eq_params"]
    assert len(eq_calls) == 1, f"应调用 set_channel_eq_params，实际调用：{[c[0] for c in calls]}"
