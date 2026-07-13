"""单元测试:即兴编曲架构——打破流水线,让 AI 像人类一样随心所欲编曲。

验证:
- FreeAction 模型无 stage 字段(打破阶段约束)
- SessionConductor 能执行任意动作组合
- 试听-反馈闭环(listen 动作)
- 回退能力(undo_to)
- 满意完成判定(satisfied)
- /api/improvise 端点
- 会话上下文连续(不每阶段开新对话)
运行 `pytest tests/test_improvise_arch.py`
"""
import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

from backend.models import FreeAction, ListenAction, AudioFeedback, StageResult


# ---------- FreeAction 模型打破阶段约束 ----------

def test_free_action_has_no_stage_field():
    """FreeAction 不应有 stage 字段(打破阶段流水线约束)。"""
    src = inspect.getsource(FreeAction)
    # 不应有 stage: Stage 字段
    assert "stage: Stage" not in src, "FreeAction 不应有 stage 字段(打破阶段约束)"
    assert "stage: " not in src.split('\n')[0:20].__str__(), "FreeAction 不应有 stage 字段"


def test_free_action_accepts_arbitrary_action_combos():
    """FreeAction 应能接受任意动作组合(写旋律+调混音+试听同时)。"""
    action = FreeAction(
        intent="写副歌旋律+同时调音色+试听",
        tracks=[{"name": "Lead", "type": "software"}],
        regions=[{"track": "Lead", "start": 0, "notes": [{"pitch": 60, "start": 0, "duration": 1}]}],
        mix=[{"track": "Lead", "volume_db": -3}],
        listen={"start_bar": 1, "end_bar": 4, "focus": "副歌爆发力"},
    )
    assert action.intent == "写副歌旋律+同时调音色+试听"
    assert len(action.tracks) == 1
    assert len(action.regions) == 1
    assert len(action.mix) == 1
    assert action.listen is not None
    assert action.listen.focus == "副歌爆发力"


def test_free_action_supports_undo_to():
    """FreeAction 应支持 undo_to(回退到之前某步)。"""
    action = FreeAction(intent="不喜欢刚才的,回退", undo_to=3)
    assert action.undo_to == 3


def test_free_action_supports_satisfied():
    """FreeAction 应支持 satisfied(完成判定)。"""
    action = FreeAction(intent="作品完成", satisfied=True)
    assert action.satisfied is True


# ---------- 试听-反馈闭环 ----------

def test_listen_action_model():
    """ListenAction 应能表示试听请求。"""
    listen = ListenAction(start_bar=1, end_bar=4, focus="主旋律亮度")
    assert listen.start_bar == 1
    assert listen.end_bar == 4
    assert listen.focus == "主旋律亮度"


def test_audio_feedback_model():
    """AudioFeedback 应能表示听觉反馈。"""
    feedback = AudioFeedback(
        heard="主旋律偏闷,缺高频",
        issues=["主旋律太闷", "低频糊"],
        suggestions=["加 8kHz 高频", "贝斯切高通"],
        rating=5,
    )
    assert feedback.rating == 5
    assert len(feedback.issues) == 2
    assert len(feedback.suggestions) == 2


def test_visual_step_supports_listen():
    """VisualStep 应支持 listen 字段(视觉循环里也能试听)。"""
    from backend.models import VisualStep
    src = inspect.getsource(VisualStep)
    assert "listen" in src, "VisualStep 应有 listen 字段"


def test_stage_result_supports_listen():
    """StageResult 应支持 listen 字段(流水线里也能试听)。"""
    src = inspect.getsource(StageResult)
    assert "listen" in src, "StageResult 应有 listen 字段"


# ---------- SessionConductor ----------

def test_session_conductor_exists():
    """SessionConductor 类应存在。"""
    from backend.session_conductor import SessionConductor
    assert SessionConductor is not None


def test_session_state_tracks_history():
    """SessionState 应记录操作历史和听觉反馈。"""
    from backend.session_conductor import SessionState
    state = SessionState()
    state.user_goal = "做一首钢琴曲"
    state.add_step("加钢琴轨道", {"tracks": 1})
    state.add_step("写旋律", {"regions": 1})
    state.add_audio_feedback("1-4", {"heard": "不错", "rating": 7, "issues": [], "suggestions": []})

    context = state.get_context_for_ai()
    assert "做一首钢琴曲" in context
    assert "加钢琴轨道" in context
    assert "写旋律" in context
    assert "不错" in context
    assert state.step_count == 2


def test_session_conductor_executes_free_action():
    """SessionConductor 应能执行 FreeAction(任意动作组合)。"""
    from backend.session_conductor import SessionConductor
    from backend.models import TrackSpec

    # mock daw
    daw = MagicMock()
    daw.has_project = True
    daw.ensure_track = AsyncMock()
    daw.apply_mix = AsyncMock()
    daw.save_project = AsyncMock()
    daw.log = AsyncMock()
    daw.emit = AsyncMock()
    daw.undo = AsyncMock()

    conductor = SessionConductor(daw=daw)
    action = FreeAction(
        intent="加钢琴+调音量",
        tracks=[TrackSpec(name="Piano", type="software")],
        mix=[{"track": "Piano", "volume_db": -3}],
    )
    summary = asyncio.run(conductor.execute_free_action(action))
    assert daw.ensure_track.await_count == 1
    assert daw.apply_mix.await_count == 1
    assert daw.save_project.await_count == 1
    assert conductor.state.step_count == 1


def test_session_conductor_handles_undo_to():
    """SessionConductor 应处理 undo_to(回退到之前某步)。"""
    from backend.session_conductor import SessionConductor

    daw = MagicMock()
    daw.has_project = True
    daw.save_project = AsyncMock()
    daw.log = AsyncMock()
    daw.emit = AsyncMock()
    daw.undo = AsyncMock()

    conductor = SessionConductor(daw=daw)
    # 模拟已做了 5 步
    for i in range(5):
        conductor.state.add_step(f"步骤{i+1}", {})
    assert conductor.state.step_count == 5

    # 回退到第 2 步
    action = FreeAction(intent="回退重做", undo_to=2)
    asyncio.run(conductor.execute_free_action(action))
    # 应调用 undo 3 次(5-2=3)
    assert daw.undo.await_count == 3
    # history 应被截断到第 2 步,然后回退动作本身被记录为第 3 步
    assert len(conductor.state.history) == 3  # 步骤1+步骤2+回退重做
    assert conductor.state.step_count == 3
    # 但前两步应是原来的步骤1和步骤2(后面的步骤3/4/5被丢弃)
    assert conductor.state.history[0]["intent"] == "步骤1"
    assert conductor.state.history[1]["intent"] == "步骤2"
    assert conductor.state.history[2]["intent"] == "回退重做"


def test_session_conductor_handles_satisfied():
    """SessionConductor 应处理 satisfied=True(完成判定)。"""
    from backend.session_conductor import SessionConductor

    daw = MagicMock()
    daw.has_project = True
    daw.save_project = AsyncMock()
    daw.log = AsyncMock()
    daw.emit = AsyncMock()

    conductor = SessionConductor(daw=daw)
    action = FreeAction(intent="作品完成", satisfied=True)
    asyncio.run(conductor.execute_free_action(action))
    assert conductor.state.completed is True


# ---------- 试听-反馈闭环集成 ----------

def test_session_conductor_listen_triggers_audio_capture():
    """SessionConductor 执行 listen 时应触发音频录制 + AI 听取。"""
    from backend.session_conductor import SessionConductor

    daw = MagicMock()
    daw.has_project = True
    daw.save_project = AsyncMock()
    daw.log = AsyncMock()
    daw.emit = AsyncMock()
    daw.listen_and_capture = AsyncMock(return_value="/tmp/test.wav")

    ai = MagicMock()
    ai.listen_to_audio = AsyncMock(return_value={
        "heard": "主旋律偏闷",
        "issues": ["缺高频"],
        "suggestions": ["加 8kHz"],
        "rating": 5,
    })

    conductor = SessionConductor(daw=daw, ai=ai)
    action = FreeAction(
        intent="试听副歌",
        listen=ListenAction(start_bar=9, end_bar=16, focus="副歌爆发力"),
    )
    asyncio.run(conductor.execute_free_action(action))

    # 应调用 listen_and_capture
    daw.listen_and_capture.assert_awaited_once_with(9, 16)
    # 应调用 ai.listen_to_audio
    ai.listen_to_audio.assert_awaited_once()
    # 听觉反馈应被记录到 state
    assert len(conductor.state.audio_feedbacks) == 1
    fb = conductor.state.audio_feedbacks[0]
    assert "9-16" in fb["bars"]
    assert fb["feedback"]["heard"] == "主旋律偏闷"


# ---------- DAWController.listen_and_capture ----------

def test_daw_controller_has_listen_and_capture():
    """DAWController 应有 listen_and_capture 方法。"""
    from backend.daw_controller import DAWController
    assert hasattr(DAWController, "listen_and_capture"), \
        "DAWController 应有 listen_and_capture 方法(试听-反馈闭环)"


def test_listen_and_capture_noop_in_sim_mode():
    """模拟模式下 listen_and_capture 应返回 None 并 emit listen_simulated。"""
    from backend.daw_controller import DAWController

    ctrl = DAWController.__new__(DAWController)
    ctrl.use_applescript = True
    ctrl.applescript = MagicMock()
    ctrl.applescript.available = False  # 模拟模式
    ctrl.event_cb = None

    emit_calls = []
    async def capture_emit(**payload):
        emit_calls.append(payload)
    ctrl.emit = capture_emit

    result = asyncio.run(ctrl.listen_and_capture(1, 4))
    assert result is None
    assert any(c.get("kind") == "listen_simulated" for c in emit_calls)


# ---------- AI 引擎支持即兴 ----------

def test_ai_engine_has_generate_free_action():
    """AIEngine 应有 generate_free_action 方法。"""
    from backend.ai_engine import AIEngine
    assert hasattr(AIEngine, "generate_free_action"), \
        "AIEngine 应有 generate_free_action 方法(AI 主动决策即兴动作)"


def test_ai_engine_has_listen_to_audio():
    """AIEngine 应有 listen_to_audio 方法。"""
    from backend.ai_engine import AIEngine
    assert hasattr(AIEngine, "listen_to_audio"), \
        "AIEngine 应有 listen_to_audio 方法(听取音频反馈)"


def test_improvise_prompt_exists():
    """应有即兴编曲专用提示词 IMPROVISE_PROMPT。"""
    from backend.ai_engine import IMPROVISE_PROMPT
    assert "即兴" in IMPROVISE_PROMPT or "随心所欲" in IMPROVISE_PROMPT
    assert "listen" in IMPROVISE_PROMPT.lower() or "试听" in IMPROVISE_PROMPT
    assert "satisfied" in IMPROVISE_PROMPT.lower() or "满意" in IMPROVISE_PROMPT


# ---------- 会话连续性 ----------

def test_new_chat_per_stage_defaults_false():
    """new_chat_per_stage 默认应为 False(保持上下文连续,不每阶段开新对话)。"""
    from backend.ai_engine import WebAIDriver
    src = inspect.getsource(WebAIDriver.__init__)
    # 默认值应是 False(从 True 改为 False,保持上下文连续)
    assert 'new_chat_per_stage", False' in src or 'new_chat_per_stage",  False' in src, \
        "new_chat_per_stage 默认应为 False(保持上下文连续)"


# ---------- /api/improvise 端点 ----------

def test_server_has_improvise_endpoints():
    """server 应有 /api/improvise 和 /api/improvise/action 端点。"""
    import backend.server as srv
    # 检查路由注册
    routes = [r.path for r in srv.app.routes]
    assert "/api/improvise" in routes, "应有 /api/improvise 端点"
    assert "/api/improvise/action" in routes, "应有 /api/improvise/action 端点"


def test_server_has_listen_endpoint():
    """server 应有 /api/listen 端点。"""
    import backend.server as srv
    routes = [r.path for r in srv.app.routes]
    assert "/api/listen" in routes, "应有 /api/listen 端点"


# ---------- 综合验证:即兴模式 vs 流水线模式 ----------

def test_free_action_vs_stage_result_comparison():
    """FreeAction 与 StageResult 的核心区别:FreeAction 无 stage 字段。"""
    free_src = inspect.getsource(FreeAction)
    stage_src = inspect.getsource(StageResult)

    # StageResult 应有 stage 字段
    assert "stage" in stage_src.lower()
    # FreeAction 不应有 stage 字段(打破阶段约束)
    # 检查类定义部分(前 30 行)
    free_class_def = '\n'.join(free_src.split('\n')[:30])
    assert "stage:" not in free_class_def, \
        "FreeAction 类定义不应有 stage 字段(这是与 StageResult 的核心区别)"


def test_free_action_has_listen_but_stage_result_also_has():
    """FreeAction 和 StageResult 都应支持 listen(两种模式都能试听)。"""
    from backend.models import FreeAction, StageResult
    free_src = inspect.getsource(FreeAction)
    stage_src = inspect.getsource(StageResult)
    assert "listen" in free_src
    assert "listen" in stage_src


def test_free_action_has_undo_to_but_stage_result_does_not():
    """FreeAction 应有 undo_to(回退能力),StageResult 不应有(流水线不能回退)。"""
    from backend.models import FreeAction, StageResult
    free_src = inspect.getsource(FreeAction)
    stage_src = inspect.getsource(StageResult)
    assert "undo_to" in free_src, "FreeAction 应有 undo_to(回退能力)"
    # StageResult 不应有 undo_to(流水线模式不支持回退)
    assert "undo_to" not in stage_src, "StageResult 不应有 undo_to(流水线不支持回退)"


def test_free_action_has_satisfied_but_stage_result_does_not():
    """FreeAction 应有 satisfied(完成判定),StageResult 不应有(流水线必须跑完)。"""
    from backend.models import FreeAction, StageResult
    free_src = inspect.getsource(FreeAction)
    stage_src = inspect.getsource(StageResult)
    assert "satisfied" in free_src, "FreeAction 应有 satisfied(完成判定)"
    assert "satisfied" not in stage_src, "StageResult 不应有 satisfied(流水线必须跑完)"
