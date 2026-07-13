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
    ctrl.project_locked = True  # 模拟已锁定状态（ui_action 守卫需要）
    ctrl._track_index = {}
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
    """create_project 后 current_project_path 应被记录并加锁，供后续阶段引用。"""
    ctrl = _make_real_controller_in_sim_mode()
    with tempfile.TemporaryDirectory() as td:
        ctrl.project_dir = Path(td)
        ctrl.current_project_path = None
        ctrl.current_project_title = ""
        ctrl.project_locked = False  # 从未锁定开始，允许 create_project
        ok = asyncio.run(ctrl.create_project(
            TempoSpec(bpm=120, time_signature="4/4", key="C minor"),
            title="我的歌",
        ))
        assert ok is True
        assert ctrl.current_project_path is not None
        assert "我的歌.logicx" in ctrl.current_project_path
        assert ctrl.current_project_title == "我的歌"
        assert ctrl.project_locked is True  # 创建后应自动加锁


def test_create_project_sanitizes_unsafe_title():
    """含非法字符的标题应被安全化，不破坏文件路径。"""
    ctrl = _make_real_controller_in_sim_mode()
    with tempfile.TemporaryDirectory() as td:
        ctrl.project_dir = Path(td)
        ctrl.current_project_path = None
        ctrl.current_project_title = ""
        ctrl.project_locked = False
        asyncio.run(ctrl.create_project(
            TempoSpec(bpm=120), title='bad/name:"*?',
        ))
        path = ctrl.current_project_path
        assert path.endswith(".logicx")
        # 文件名部分（去掉目录与扩展名）不应含非法字符
        filename = Path(path).stem
        for ch in '/\\:*?"<>|':
            assert ch not in filename, f"文件名 {filename} 含非法字符 {ch}"


# ---------- 工程锁：防止意外创建其他工程 ----------

def test_create_project_refused_when_already_locked():
    """已有锁定工程时，再次 create_project 应被拒绝，不创建新工程。"""
    ctrl = _make_real_controller_in_sim_mode()
    # 已锁定状态（_make_real_controller_in_sim_mode 默认 project_locked=True）
    original_path = ctrl.current_project_path
    original_title = ctrl.current_project_title
    ctrl.project_dir = Path(tempfile.mkdtemp())  # _lock_project 在 create_project 内先调用，project_dir 需存在
    ok = asyncio.run(ctrl.create_project(
        TempoSpec(bpm=140), title="另一首歌",
    ))
    assert ok is False  # 应被拒绝
    # 工程锚点不应改变
    assert ctrl.current_project_path == original_path
    assert ctrl.current_project_title == original_title


def test_mutating_ops_refused_without_project():
    """无锁定工程时，修改操作（如 ensure_track/add_region/apply_mix/bounce）应被跳过。"""
    ctrl = _make_real_controller_in_sim_mode()
    ctrl.project_locked = False
    ctrl.current_project_path = None
    from backend.models import TrackSpec, MidiRegionSpec, NoteSpec, MixParams, BounceSpec
    # ensure_track 应跳过
    asyncio.run(ctrl.ensure_track(TrackSpec(name="Test", type="software")))
    assert "Test" not in ctrl._track_index  # 未创建
    # bounce 应返回 None
    out = asyncio.run(ctrl.bounce(BounceSpec(filename="test", format="wav")))
    assert out is None


def test_open_existing_project_locks_anchor():
    """open_existing_project 应锁定工程锚点。"""
    ctrl = _make_real_controller_in_sim_mode()
    ctrl.project_locked = False
    ctrl.current_project_path = None
    with tempfile.TemporaryDirectory() as td:
        # 创建一个假的 .logicx 文件
        fake = Path(td) / "我的工程.logicx"
        fake.write_bytes(b"fake logicx")
        ok = asyncio.run(ctrl.open_existing_project(str(fake)))
        assert ok is True
        assert ctrl.project_locked is True
        assert ctrl.current_project_title == "我的工程"


def test_open_existing_project_refused_when_locked():
    """已有锁定工程时，open_existing_project 应被拒绝。"""
    ctrl = _make_real_controller_in_sim_mode()
    # 已锁定状态
    original_path = ctrl.current_project_path
    with tempfile.TemporaryDirectory() as td:
        fake = Path(td) / "other.logicx"
        fake.write_bytes(b"fake")
        ok = asyncio.run(ctrl.open_existing_project(str(fake)))
        assert ok is False
        assert ctrl.current_project_path == original_path  # 未切换


def test_open_existing_project_rejects_nonexistent():
    """不存在的文件应被拒绝。"""
    ctrl = _make_real_controller_in_sim_mode()
    ctrl.project_locked = False
    ok = asyncio.run(ctrl.open_existing_project("/nonexistent/path.logicx"))
    assert ok is False
    assert ctrl.project_locked is False


def test_unlock_project_clears_state():
    """unlock_project 应清空锁定状态，允许开始新一首作品。"""
    ctrl = _make_real_controller_in_sim_mode()
    assert ctrl.project_locked is True
    ctrl.unlock_project()
    assert ctrl.project_locked is False
    assert ctrl.current_project_path is None
    assert ctrl.current_project_title == ""


# ---------- MIDI 导入不会新建工程 ----------

def test_add_region_does_not_create_new_project():
    """写入 MIDI 片段（add_region）不得触发 new_project。

    这是用户反馈的核心问题：原来 import_midi_file 用 open POSIX file，
    Logic Pro 会把 MIDI 文件当作新工程打开，导致每段 MIDI 都新建工程。
    修复后 import_midi_file 用 File > Import 菜单，导入到当前工程。
    """
    ctrl = _make_real_controller_in_sim_mode()
    ctrl.project_locked = True
    ctrl.current_project_path = "/tmp/locked.logicx"
    ctrl.current_project_title = "已锁定工程"
    # 把 applescript 设为 MagicMock，记录所有调用
    ctrl.applescript = MagicMock()
    ctrl.applescript.available = True  # _real=True
    ctrl.applescript.import_midi_file = AsyncMock()
    ctrl.applescript.new_project = MagicMock()
    ctrl.applescript.select_track_by_name = MagicMock()
    ctrl.applescript.activate = MagicMock()
    # add_region 会调 self.midi.build_midi_file 生成 MIDI 文件
    ctrl.midi = MagicMock()
    ctrl.midi.build_midi_file = MagicMock(return_value=Path("/tmp/test.mid"))
    from backend.models import MidiRegionSpec, NoteSpec, TempoSpec
    region = MidiRegionSpec(
        track="Piano", start=0.0, length=4.0,
        notes=[NoteSpec(pitch=60, start=0.0, duration=1.0, velocity=80)],
    )
    asyncio.run(ctrl.add_region(region, bpm=120))
    # import_midi_file 应被调用（导入到当前工程）
    ctrl.applescript.import_midi_file.assert_awaited_once()
    # new_project 绝不应被调用——工程已锁定，不得新建
    ctrl.applescript.new_project.assert_not_called()


def test_import_midi_file_does_not_use_open_posix():
    """import_midi_file 的实现不得用 `open POSIX file`（那会新建工程）。

    检查实际 AppleScript 命令行（含 open POSIX file " 的，即带路径的命令）
    不存在。文档字符串里的说明用反引号包裹，不会匹配。
    """
    import backend.applescript_bridge as ab
    import inspect
    src = inspect.getsource(ab.AppleScriptBridge.import_midi_file)
    # 实际命令形式：open POSIX file "{path}" —— 含引号和花括号
    # 文档说明里是 `open POSIX file`（反引号），不会匹配这个模式
    assert 'open POSIX file "' not in src, \
        "import_midi_file 不应用 'open POSIX file \"...'（会新建工程），应改用 File > Import 菜单"
