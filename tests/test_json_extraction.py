"""单元测试：JSON 提取（ai_engine.AIEngine._extract_json）。"""
from backend.ai_engine import AIEngine


def test_plain_json():
    raw = '{"stage": "compose", "summary": "测试"}'
    assert AIEngine._extract_json(raw) == {"stage": "compose", "summary": "测试"}


def test_json_in_code_fence():
    raw = '```json\n{"stage": "mix", "summary": "混音"}\n```'
    assert AIEngine._extract_json(raw) == {"stage": "mix", "summary": "混音"}


def test_json_with_surrounding_text():
    raw = '好的，这是结果：\n{"stage": "master", "summary": "母带"}\n以上为决策。'
    assert AIEngine._extract_json(raw) == {"stage": "master", "summary": "母带"}


def test_json_with_thinking_tag():
    raw = '<think>让我想想...</think>\n{"stage": "compose", "summary": "作曲"}'
    assert AIEngine._extract_json(raw) == {"stage": "compose", "summary": "作曲"}


def test_json_with_trailing_comma():
    raw = '{"stage": "arrange", "summary": "编曲",}'
    assert AIEngine._extract_json(raw) == {"stage": "arrange", "summary": "编曲"}


def test_empty_returns_none():
    assert AIEngine._extract_json("") is None
    assert AIEngine._extract_json("无 JSON 内容") is None


def test_nested_braces():
    raw = '{"project": {"title": "曲名", "tempo": {"bpm": 100}}}'
    out = AIEngine._extract_json(raw)
    assert out["project"]["tempo"]["bpm"] == 100
