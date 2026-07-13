"""单元测试：工程生命周期守卫——确保一首音乐的所有操作指向同一个 Logic Pro 工程。

覆盖：
- 只有 compose 阶段允许新建工程；非 compose 阶段输出 project 字段会被忽略并警告
- ui_action 禁止 open/close（防止切换/关闭工程）
- create_project 后 current_project_path 被记录，后续阶段引用同一工程
- save_as 的 path 被忽略，强制使用系统管理的工程路径
运行 `pytest tests/test_project_lifecycle.py`
"""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.commander import Commander
from backend.daw_controller import DAWController
from backend.models import (
    ProjectPlan, Stage, StageResult, TempoSpec, UIAction,
)


def _make_daw_mock():
    daw = MagicMock()
    daw.cfg = {"default_bpm": 100}
    daw.current_project_path = None
    daw.current_project_title = ""
    for name in [
        "emit", "log", "create_project", "add_tempo_change", "add_marker",
        "ensure_track", "create_track_stack", "add_region", "region_op",
        "transport", "create_bus", "apply_mix", "set_plugin_param",
        "apply_automation", "setup_record", "apply_master", "bounce", "ui_action",
        "save_project", "save_as",
    ]:
        setattr(daw, name, AsyncMock())
    return daw


def _project_plan() -> ProjectPlan:
    return ProjectPlan(
        title="测试曲", genre="Pop",
        tempo=TempoSpec(bpm=120, time_signature="4/4", key="C minor"),
        structure=["intro", "chorus"], key="C minor",
    )


def _make_real_controller_in_sim_mode() -> DAWController:
    """构造一个真实 DAWController（模拟模式，_real=False），用 MagicMock 替换 applescript。"""
    ctrl = DAWController.__new__(DAWController)
    ctrl.event_cb = None
    ctrl.use_applescript = True
    ctrl.applescript = MagicMock()
    ctrl.applescript.available = False  # 模拟模式：_real=False
    ctrl.current_project_path = "/tmp/test.logicx"
    ctrl.current_project_title = "测试"
    return ctrl


# ---------- stage 守卫：只有 compose 允许建工程 ----------

def test_compose_stage_creates_project():
    """compose 阶段输出 project 字段应正常调用 create_project。"""
    daw = _make_daw_mock()
    cmd = Commander(ai=MagicMock(), daw=daw)
    result = StageResult(stage=Stage.COMPOSE, summary="作曲", project=_project_plan())
    asyncio.run(cmd.execute_stage(result, bpm=120))
    daw.create_project.assert_awaited_once()


def test_arrange_stage_ignores_project_field():
    """arrange 阶段输出 project 字段应被忽略，不调用 create_project，并发出警告。"""
    daw = _make_daw_mock()
    cmd = Commander(ai=MagicMock(), daw=daw)
    result = StageResult(stage=Stage.ARRANGE, summary="编曲", project=_project_plan())
    asyncio.run(cmd.execute_stage(result, bpm=120))
    daw.create_project.assert_not_awaited()
    log_calls = [c.args for c in daw.log.call_args_list]
    warned = any("忽略" in str(a) and "project" in str(a) for a in log_calls)
    assert warned, f"应警告忽略了 project 字段，实际日志：{log_calls}"


def test_mix_stage_ignores_project_field():
    """mix 阶段输出 project 字段应被忽略。"""
    daw = _make_daw_mock()
    cmd = Commander(ai=MagicMock(), daw=daw)
    result = StageResult(stage=Stage.MIX, summary="混音", project=_project_plan())
    asyncio.run(cmd.execute_stage(result, bpm=120))
    daw.create_project.assert_not_awaited()


def test_master_stage_ignores_project_field():
    """master 阶段输出 project 字段应被忽略。"""
    daw = _make_daw_mock()
    cmd = Commander(ai=MagicMock(), daw=daw)
    result = StageResult(stage=Stage.MASTER, summary="母带", project=_project_plan())
    asyncio.run(cmd.execute_stage(result, bpm=120))
    daw.create_project.assert_not_awaited()


# ---------- ui_action 守卫：禁止 open/close ----------

def test_ui_action_open_is_blocked():
    """open 动作应被忽略，不调用 applescript.open_project。"""
    ctrl = _make_real_controller_in_sim_mode()
    asyncio.run(ctrl.ui_action(UIAction(op="open", path="/other.logicx")))
    ctrl.applescript.open_project.assert_not_called()


def test_ui_action_close_is_blocked():
    """close 动作应被忽略，不调用 applescript.close_project。"""
    ctrl = _make_real_controller_in_sim_mode()
    asyncio.run(ctrl.ui_action(UIAction(op="close")))
    ctrl.applescript.close_project.assert_not_called()


def test_ui_action_save_is_allowed():
    """save 动作应被允许（保存到系统管理的工程路径）。"""
    ctrl = _make_real_controller_in_sim_mode()
    ctrl.applescript.available = True  # 让 _real=True
    asyncio.run(ctrl.ui_action(UIAction(op="save")))
    ctrl.applescript.save_project.assert_called_once()


def test_ui_action_save_as_uses_system_path():
    """save_as 应使用系统管理的路径，忽略 AI 给的 path，只调 save_project 不调 save_as。"""
    ctrl = _make_real_controller_in_sim_mode()
    ctrl.applescript.available = True
    asyncio.run(ctrl.ui_action(UIAction(op="save_as", path="/ai-given-path.logicx")))
    ctrl.applescript.save_project.assert_called_once()
    ctrl.applescript.save_as.assert_not_called()


# ---------- 工程路径记录 ----------

def test_create_project_records_path():
    """create_project 后 current_project_path 应被记录，供后续阶段引用。"""
    ctrl = _make_real_controller_in_sim_mode()
    with tempfile.TemporaryDirectory() as td:
        ctrl.project_dir = Path(td)
        ctrl.current_project_path = None
        ctrl.current_project_title = ""
        asyncio.run(ctrl.create_project(
            TempoSpec(bpm=120, time_signature="4/4", key="C minor"),
            title="我的歌",
        ))
        assert ctrl.current_project_path is not None
        assert "我的歌.logicx" in ctrl.current_project_path
        assert ctrl.current_project_title == "我的歌"


def test_create_project_sanitizes_unsafe_title():
    """含非法字符的标题应被安全化，不破坏文件路径。"""
    ctrl = _make_real_controller_in_sim_mode()
    with tempfile.TemporaryDirectory() as td:
        ctrl.project_dir = Path(td)
        ctrl.current_project_path = None
        ctrl.current_project_title = ""
        asyncio.run(ctrl.create_project(
            TempoSpec(bpm=120), title='bad/name:"*?',
        ))
        path = ctrl.current_project_path
        assert path.endswith(".logicx")
        # 文件名部分（去掉目录与扩展名）不应含非法字符
        filename = Path(path).stem
        for ch in '/\\:*?"<>|':
            assert ch not in filename, f"文件名 {filename} 含非法字符 {ch}"
