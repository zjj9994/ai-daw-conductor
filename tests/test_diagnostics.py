"""单元测试：诊断模块。运行 `pytest tests/`"""
import asyncio
from unittest.mock import MagicMock

from backend import diagnostics


def test_check_playwright_returns_dict_with_ok():
    r = diagnostics._check_playwright()
    assert "ok" in r and "detail" in r


def test_check_mido_returns_dict_with_ok():
    r = diagnostics._check_mido()
    assert "ok" in r and "detail" in r


def test_check_cdp_invalid_url_returns_not_ok():
    r = diagnostics._check_cdp("")
    assert r["ok"] is False


def test_check_cdp_unreachable_returns_not_ok():
    # 用一个几乎肯定连不上的端口
    r = diagnostics._check_cdp("http://127.0.0.1:1")
    assert r["ok"] is False
    assert "无法连接" in r["detail"]


def test_run_diagnostics_returns_expected_fields():
    cfg = {"ai": {"provider": "kimi"}, "browser": {"cdp_url": "http://127.0.0.1:1"}}
    result = asyncio.run(diagnostics.run_diagnostics(cfg, ai_engine=None))
    for key in ("platform", "python", "playwright", "mido", "rtmidi",
                "applescript", "cdp", "ai_provider", "ai_online",
                "browser_connected", "suggestions", "ready"):
        assert key in result
    assert result["ai_provider"] == "kimi"
    assert result["ai_online"] is False
    assert isinstance(result["suggestions"], list)


def test_run_diagnostics_ready_requires_playwright_and_mido():
    cfg = {"ai": {"provider": "doubao"}, "browser": {"cdp_url": ""}}
    result = asyncio.run(diagnostics.run_diagnostics(cfg, ai_engine=None))
    pw_ok = result["playwright"]["ok"]
    mido_ok = result["mido"]["ok"]
    assert result["ready"] == (pw_ok and mido_ok)


def test_run_diagnostics_suggestions_not_empty_when_cdp_fails():
    cfg = {"ai": {"provider": "doubao"}, "browser": {"cdp_url": "http://127.0.0.1:1"}}
    result = asyncio.run(diagnostics.run_diagnostics(cfg, ai_engine=None))
    # CDP 不可达时应给出建议（除非 playwright 没装，那时是另一条建议）
    assert len(result["suggestions"]) >= 1


def test_run_diagnostics_with_mock_engine():
    engine = MagicMock()
    engine.online = True
    engine.driver.connected = True
    cfg = {"ai": {"provider": "qwen"}, "browser": {"cdp_url": "http://127.0.0.1:1"}}
    result = asyncio.run(diagnostics.run_diagnostics(cfg, ai_engine=engine))
    assert result["ai_online"] is True
    assert result["browser_connected"] is True
