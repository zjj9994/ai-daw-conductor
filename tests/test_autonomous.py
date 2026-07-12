"""单元测试：自主流水线编排逻辑。运行 `pytest tests/`

使用 mock 的 AI 引擎与 commander，验证：
- 四阶段全部执行
- 上下文累积正确
- 自评估不达标触发重做
- 取消信号传播
- 健康检查与重连
"""
import asyncio
from unittest.mock import MagicMock, AsyncMock

from backend.autonomous import AutonomousPipeline, STAGE_ORDER
from backend.models import Stage, StageResult, ProjectPlan, TempoSpec, TrackSpec


def _make_result(stage: Stage, title: str = "测试曲", bpm: int = 100) -> StageResult:
    return StageResult(
        stage=stage,
        summary=f"{stage.value} 产出",
        project=ProjectPlan(
            title=title, genre="Pop",
            tempo=TempoSpec(bpm=bpm, time_signature="4/4", key="A minor"),
            structure=["intro", "verse", "chorus"], key="A minor",
            description="测试",
        ) if stage == Stage.COMPOSE else None,
        tracks=[TrackSpec(name="主旋律", type="software", instrument="Piano")],
        rationale=f"{stage.value} 创作思路",
    )


def _make_ai_mock(evaluations=None, online=True):
    """构造 mock AI 引擎。evaluations: dict[stage.value -> list[bool]] 控制每次评估结果。"""
    ai = MagicMock()
    ai.online = online
    ai.health_check = AsyncMock(return_value=True)
    ai.reconnect = AsyncMock(return_value=True)
    ai._generate_demo = MagicMock(return_value=_make_result(Stage.COMPOSE))

    # evaluate_stage 按 evaluations 配置返回
    eval_state = {k: list(v) for k, v in (evaluations or {}).items()}

    async def _eval(stage, result, context=None, log_cb=None):
        seq = eval_state.get(stage.value, [])
        if not seq:
            return True, ""
        acceptable = seq.pop(0)
        return acceptable, "" if acceptable else "需改进"

    ai.evaluate_stage = _eval
    return ai


def _make_commander_mock():
    """构造 mock commander，run_stage 返回对应阶段的 result。"""
    commander = MagicMock()
    commander._cancel = False
    commander.cancel = MagicMock()
    commander.daw = MagicMock()
    commander.daw.log = AsyncMock()
    commander.daw.emit = AsyncMock()

    async def _run_stage(stage, user_prompt, context, bpm):
        return _make_result(stage)

    commander.run_stage = _run_stage

    async def _execute_stage(result, bpm):
        pass

    commander.execute_stage = _execute_stage
    return commander


def test_runs_all_four_stages():
    ai = _make_ai_mock()
    commander = _make_commander_mock()
    pipe = AutonomousPipeline(ai, commander, tracker=None, enable_self_eval=False)

    results = asyncio.run(pipe.run("测试"))

    assert len(results) == 4
    assert [r.stage for r in results] == STAGE_ORDER


def test_context_accumulates_between_stages():
    ai = _make_ai_mock()
    commander = _make_commander_mock()
    captured_context = []

    async def _run_stage_capturing(stage, user_prompt, context, bpm):
        captured_context.append(context)
        return _make_result(stage)

    commander.run_stage = _run_stage_capturing
    pipe = AutonomousPipeline(ai, commander, tracker=None, enable_self_eval=False)

    asyncio.run(pipe.run("测试"))

    # 第一阶段无上下文，后续阶段应有累积
    assert captured_context[0] == ""
    assert "compose" in captured_context[1]
    assert "主旋律" in captured_context[1]  # 轨道名应出现在上下文


def test_self_eval_rejects_then_accepts():
    """compose 第一次评估不通过，第二次通过 —— 应重做一次。"""
    ai = _make_ai_mock(evaluations={"compose": [False, True]})
    commander = _make_commander_mock()
    call_count = {"compose": 0}

    async def _run_stage(stage, user_prompt, context, bpm):
        call_count[stage.value] = call_count.get(stage.value, 0) + 1
        return _make_result(stage)

    commander.run_stage = _run_stage
    pipe = AutonomousPipeline(ai, commander, tracker=None,
                              enable_self_eval=True, max_stage_retries=2)

    asyncio.run(pipe.run("测试"))

    # compose 应被调用 2 次（初次 + 1 次重做）
    assert call_count["compose"] == 2
    # 其他阶段各 1 次
    assert call_count.get("arrange") == 1
    assert call_count.get("master") == 1


def test_cancel_stops_pipeline():
    ai = _make_ai_mock()
    commander = _make_commander_mock()
    pipe = AutonomousPipeline(ai, commander, tracker=None, enable_self_eval=False)

    # 在第一个阶段执行后取消
    async def _run_stage(stage, user_prompt, context, bpm):
        pipe.cancel()  # 触发取消
        return _make_result(stage)

    commander.run_stage = _run_stage
    results = asyncio.run(pipe.run("测试"))

    # 只应完成 1 个阶段
    assert len(results) == 1


def test_stage_failure_falls_back_to_demo():
    ai = _make_ai_mock()
    commander = _make_commander_mock()

    call_count = {"fail": 0}

    async def _run_stage(stage, user_prompt, context, bpm):
        if stage == Stage.ARRANGE:
            call_count["fail"] += 1
            if call_count["fail"] <= 3:  # 前几次都失败
                raise RuntimeError("模拟网页 AI 故障")
        return _make_result(stage)

    commander.run_stage = _run_stage
    pipe = AutonomousPipeline(ai, commander, tracker=None,
                              enable_self_eval=False, max_stage_retries=2)

    results = asyncio.run(pipe.run("测试"))

    # 流水线不应中断，应完成 4 个阶段
    assert len(results) == 4


def test_tracker_receives_lifecycle_events():
    from backend.task_tracker import TaskTracker
    tracker = TaskTracker(history_file=None)
    ai = _make_ai_mock()
    commander = _make_commander_mock()
    pipe = AutonomousPipeline(ai, commander, tracker=tracker, enable_self_eval=False)

    asyncio.run(pipe.run("测试"))

    assert tracker.status.state == "done"
    assert tracker.status.mode == "autonomous"
    assert len(tracker.status.completed_stages) == 4


def test_health_check_called_when_online():
    ai = _make_ai_mock(online=True)
    commander = _make_commander_mock()
    pipe = AutonomousPipeline(ai, commander, tracker=None, enable_self_eval=False)

    asyncio.run(pipe.run("测试"))

    # 每个阶段前都应做健康检查
    assert ai.health_check.call_count >= 4


def test_health_check_skipped_when_offline():
    ai = _make_ai_mock(online=False)
    commander = _make_commander_mock()
    pipe = AutonomousPipeline(ai, commander, tracker=None, enable_self_eval=False)

    asyncio.run(pipe.run("测试"))

    # 离线模式不应触发健康检查
    assert ai.health_check.call_count == 0


def test_cancel_method_propagates_to_commander():
    ai = _make_ai_mock()
    commander = _make_commander_mock()
    pipe = AutonomousPipeline(ai, commander, tracker=None)

    pipe.cancel()

    assert commander.cancel.called
    assert pipe._cancel is True


def test_stage_order_is_compose_arrange_mix_master():
    assert STAGE_ORDER == [Stage.COMPOSE, Stage.ARRANGE, Stage.MIX, Stage.MASTER]


def test_accumulate_context_includes_key_info():
    ai = _make_ai_mock()
    commander = _make_commander_mock()
    pipe = AutonomousPipeline(ai, commander, tracker=None, enable_self_eval=False)
    result = _make_result(Stage.COMPOSE, title="我的歌", bpm=120)

    ctx = pipe._accumulate_context("", result, Stage.COMPOSE)

    assert "我的歌" in ctx
    assert "120" in ctx
    assert "A minor" in ctx
    assert "主旋律" in ctx
