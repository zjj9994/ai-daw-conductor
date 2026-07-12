"""单元测试：任务追踪器。运行 `pytest tests/`"""
import json
from pathlib import Path

from backend.task_tracker import TaskTracker, RenderRecord


def test_start_sets_running_state():
    t = TaskTracker()
    t.start(mode="pipeline", prompt="测试", total=4)
    assert t.status.state == "running"
    assert t.status.mode == "pipeline"
    assert t.status.total_stages == 4
    assert t.status.prompt == "测试"
    assert t.status.started_at > 0


def test_progress_zero_when_nothing_done():
    t = TaskTracker()
    t.start(mode="pipeline", total=4)
    assert t.status.progress == 0.0


def test_progress_half_when_stage_running():
    t = TaskTracker()
    t.start(mode="pipeline", total=4)
    t.set_stage("compose")
    # 当前阶段进行中算一半 → 0.5/4 = 0.125
    assert t.status.progress == 0.125


def test_progress_increases_as_stages_complete():
    t = TaskTracker()
    t.start(mode="pipeline", total=4)
    t.set_stage("compose")
    t.complete_stage("compose")
    assert t.status.progress == 0.25
    t.set_stage("arrange")
    t.complete_stage("arrange")
    assert t.status.progress == 0.5


def test_progress_full_when_all_done():
    t = TaskTracker()
    t.start(mode="pipeline", total=4)
    for s in ("compose", "arrange", "mix", "master"):
        t.set_stage(s)
        t.complete_stage(s)
    assert t.status.progress == 1.0


def test_finish_and_fail_set_state():
    t = TaskTracker()
    t.start(mode="stage", total=1)
    t.finish()
    assert t.status.state == "done"
    assert t.status.finished_at > 0

    t.start(mode="stage", total=1)
    t.fail("出错了")
    assert t.status.state == "error"
    assert t.status.error == "出错了"


def test_cancel_sets_state():
    t = TaskTracker()
    t.start(mode="pipeline", total=4)
    t.cancel()
    assert t.status.state == "cancelled"


def test_complete_stage_idempotent():
    t = TaskTracker()
    t.start(mode="pipeline", total=4)
    t.complete_stage("compose")
    t.complete_stage("compose")
    assert t.status.completed_stages.count("compose") == 1


def test_add_render_appends_record():
    t = TaskTracker()
    t.add_render(path="/tmp/a.wav", filename="a", stage="master", size=1024)
    t.add_render(path="/tmp/b.wav", filename="b", stage="master", size=2048)
    renders = t.renders_dict()
    assert len(renders) == 2
    assert renders[0]["filename"] == "a"
    assert renders[0]["size"] == 1024
    assert renders[1]["path"] == "/tmp/b.wav"


def test_to_dict_contains_expected_fields():
    t = TaskTracker()
    t.start(mode="pipeline", prompt="hi", total=4)
    d = t.status.to_dict()
    for key in ("state", "mode", "current_stage", "completed_stages",
                "total_stages", "progress", "error", "elapsed", "prompt"):
        assert key in d
    assert d["state"] == "running"
    assert d["prompt"] == "hi"


def test_add_render_captures_task_context(tmp_path):
    t = TaskTracker(history_file=None)
    t.start(mode="pipeline", prompt="测试指令", total=4)
    rec = t.add_render(path="/tmp/x.wav", filename="x", stage="master", size=10)
    assert rec.task_mode == "pipeline"
    assert rec.prompt == "测试指令"


def test_persistence_roundtrip(tmp_path):
    f = tmp_path / "history.json"
    t1 = TaskTracker(history_file=f)
    t1.add_render(path="/tmp/a.wav", filename="a", stage="master", size=100)
    t1.add_render(path="/tmp/b.wav", filename="b", stage="master", size=200)
    assert f.exists()
    # 新实例应从文件加载历史
    t2 = TaskTracker(history_file=f)
    assert len(t2.renders) == 2
    assert t2.renders[0].filename == "a"
    assert t2.renders[1].size == 200


def test_clear_history_empties_renders(tmp_path):
    f = tmp_path / "history.json"
    t = TaskTracker(history_file=f)
    t.add_render(path="/tmp/a.wav", filename="a", stage="master")
    assert len(t.renders) == 1
    n = t.clear_history()
    assert n == 1
    assert len(t.renders) == 0
    # 清空也应落盘
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["renders"] == []


def test_no_history_file_means_memory_only():
    t = TaskTracker(history_file=None)
    t.add_render(path="/tmp/a.wav", filename="a", stage="master")
    # 没有文件也不会报错
    assert len(t.renders) == 1


def test_render_record_dataclass_fields():
    r = RenderRecord(path="/x", filename="y", stage="master", timestamp=1.0)
    assert r.size == 0
    assert r.task_mode == ""
    assert r.prompt == ""
